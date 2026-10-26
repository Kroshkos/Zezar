"""
Telegram-интеграция: исходящие уведомления + привязка аккаунта.

Сценарий регистрации в боте (по ТЗ):
  1. Пользователь открывает чат с ботом и отправляет свой логин (или /start <логин>).
  2. Бот запрашивает пароль одним сообщением.
  3. Пользователь отправляет пароль — бот проверяет его, НЕМЕДЛЕННО очищает диалог
     (удаляет оба сообщения), привязывает chat_id и шлёт приветствие.
  4. После регистрации бот только отправляет уведомления; любые входящие сообщения
     пользователя просто удаляются из чата.

Бот на aiogram НЕ обязателен — используется raw Bot API через httpx,
чтобы не тянуть тяжёлый фреймворк. Сообщения обрабатывает long-polling воркер
(integrations/workers.py :: tg_poller).
"""
from __future__ import annotations

import logging
import time

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import verify_password
from app.models.core import User

log = logging.getLogger("caesar.tg")

BOT_API = "https://api.telegram.org/bot{token}/{method}"


def _bot_session() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=15)


async def call_bot(method: str, **kwargs) -> dict | None:
    """Вызов произвольного метода Bot API (sendMessage/deleteChatMessage и т.п.)."""
    s = get_settings()
    if not s.telegram_bot_token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN не задан — уведомления в Telegram не доставляются. "
            "Задайте токен бота в переменных окружения приложения (Amvera: "
            "«Переменные и секреты») и перезапустите сервис.")
    async with _bot_session() as client:
        r = await client.post(BOT_API.format(token=s.telegram_bot_token, method=method),
                              json=kwargs)
    if r.status_code != 200:
        log.warning("TG %s failed %s: %s", method, r.status_code, r.text[:200])
        return None
    return r.json().get("result")


async def delete_message(chat_id: int, message_id: int) -> None:
    """Удалить сообщение из диалога (используется для очистки пароля из истории)."""
    try:
        await call_bot("deleteMessage", chat_id=chat_id, message_id=message_id)
    except Exception as e:  # noqa: BLE001
        log.warning("tg delete_message: %s", e)


async def clear_history(chat_id: int) -> None:
    """Очистить весь диалог пользователя (deleteWebhook+clear, best effort)."""
    try:
        await call_bot("clearUserMessages", chat_id=chat_id)
    except Exception:  # noqa: BLE001
        pass  # метод доступен только для бизнес-аккаунтов — не страшно


# ---------------- state machine привязки «логин -> пароль» ----------------

class _PendingAuth:
    """Окно «логин → пароль»: помним только введённый логин, пароль не храним
    вообще. Окно живёт <= 5 минут и закрывается ровно одной попыткой ввода
    пароля (состояние стирается до проверки)."""
    __slots__ = ("login", "expires")

    def __init__(self, login: str):
        self.login = login
        self.expires = time.time() + 300


_pending: dict[int, _PendingAuth] = {}   # chat_id -> ожидающий логин
_throttle: dict[int, float] = {}         # chat_id -> когда можно повторить ответ


def _pending_put(chat_id: int, login: str) -> None:
    _pending[chat_id] = _PendingAuth(login)


def _gc_pending() -> None:
    now = time.time()
    for cid in [c for c, p in _pending.items() if p.expires < now]:
        _pending.pop(cid, None)


WELCOME = ("<b>👋 Добро пожаловать, {name}!</b>\n\n"
           "Аккаунт привязан к системе ЦЕЗАРЬ. Здесь я буду присылать уведомления:\n"
           "• задачи — назначение, изменения статуса, приближение срока;\n"
           "• совещания и проекты — решения по вашему участию.\n\n"
           "Отвечать в этом чате не нужно — бот работает только на отправку. "
           "Команды: /mute — отключить уведомления, /unmute — включить, "
           "/unbind — отвязать Telegram от учётной записи.")

ASK_PASSWORD = ("Отлично, логин <b>{login}</b> найден.\n"
                "Теперь одним сообщением отправьте <b>пароль</b> от вашей учётной "
                "записи ЦЕЗАРЬ.\n\n🔒 После проверки я удалю оба сообщения из этого "
                "диалога — пароль нигде не останется.")

BAD_CREDENTIALS = ("⛔ Логин или пароль не подходят. Диалог очищен.\n"
                   "Если забыли пароль — обратитесь к администратору системы.")

ALREADY_BOUND = ("✅ Этот Telegram уже привязан к учётной записи <b>{name}</b>. "
                 "Сообщения удаляются — бот работает только на отправку уведомлений.")

NOT_BOUND_YET = ("Чтобы привязать уведомления, отправьте мне ОДНИМ сообщением ваш "
                 "<b>логин</b> от системы ЦЕЗАРЬ (как при входе на сайт), затем — "
                 "пароль. Диалог будет очищен автоматически.")

RESET_HINT = ("🔄 Окно ввода пароля закрыто. Чтобы начать заново — отправьте свой "
              "<b>логин</b> от системы ЦЕЗАРЬ одним сообщением.")


async def upload_media_to_chat(chat_id: int, data: bytes, filename: str, mime: str) -> dict | None:
    """Загрузить фото/видео (multipart) в чат бота и вернуть сырой ответ Bot API.

    Используется веб-админкой: файл попадает в чат администратора, а Telegram
    возвращает message с photo/video[].file_id — постоянным для этого бота.
    Дальше этот file_id переиспользуется в sendPhoto/sendVideo для всех
    получателей рассылки без повторной загрузки файла.
    """
    s = get_settings()
    if not s.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан")
    method = "sendPhoto" if mime.startswith("image/") else "sendVideo"
    field = "photo" if method == "sendPhoto" else "video"
    async with _bot_session() as client:
        r = await client.post(
            BOT_API.format(token=s.telegram_bot_token, method=method),
            data={"chat_id": chat_id},
            files={field: (filename, data, mime)})
    if r.status_code != 200:
        log.warning("TG %s(upload) failed %s: %s", method, r.status_code, r.text[:200])
        return None
    return r.json()


async def send_media(chat_id: int, m: dict, caption: str = "") -> bool:
    """Отправить вложение (photo/video) по file_id бота.

    file_id привязан к боту и перевыпускается сколько угодно раз — повторная
    загрузка файла не нужна. При протухшем file_id Telegram вернёт ошибку —
    считаем доставку медиа неудачной (текст всё равно уходит).

    ВАЖНО: Bot API принимает параметр именно как ``photo``/``video`` (имя метода),
    а не универсальный ``media`` (это только у sendMediaGroup).
    """
    method = "sendPhoto" if m.get("type") == "photo" else "sendVideo"
    cap = (m.get("caption") or caption or "")[:1024] or None
    kwargs = {"chat_id": chat_id, method.lower(): m["file_id"]}
    if cap:
        kwargs["caption"] = cap
        kwargs["parse_mode"] = "HTML"
    res = await call_bot(method, **kwargs)
    return res is not None


async def send_message(chat_id: int, text: str, keyboard: list | dict | None = None) -> bool:
    """Отправить текст с клавиатурой.

    keyboard — список строк: reply-клавиатура (закреплённые кнопки над полем
    ввода), если кнопки без callback_data, иначе inline-кнопки под сообщением.
    dict — служебная клавиатура ({remove_keyboard: True} / {force_reply: ...}).
    """
    kwargs = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if keyboard:
        kwargs["reply_markup"] = _markup(keyboard)
    result = await call_bot("sendMessage", **kwargs)
    return result is not None


def _is_inline(kb: list) -> bool:
    return any(isinstance(b, dict) and b.get("callback_data") for row in kb for b in row)


def _markup(keyboard: list | dict) -> dict:
    if isinstance(keyboard, dict):
        return keyboard
    if _is_inline(keyboard):
        return {"inline_keyboard": keyboard}
    return {"keyboard": keyboard, "resize_keyboard": True, "is_persistent": True}


# message_id последнего сервисного сообщения с клавиатурой (чтобы удалить его
# ПОСЛЕ отправки нового, а не до — иначе окно без кнопок видно пользователю)
_kb_service_msg: dict[int, int] = {}


async def set_chat_reply_keyboard(chat_id: int, keyboard: list | dict | None) -> None:
    """Обновить закреплённую reply-клавиатуру чата.

    Telegram хранит одну persistent-клавиатуру на чат; editMessageText её не
    меняет (она относится к сообщениям). Ставим «сервисное» сообщение с новой
    клавиатурой и удаляем ПРЕДЫДУЩЕЕ сервисное сообщение только после этого —
    так кнопки обновляются бесшовно: между операциями в чате всегда есть
    актуальная persistent-клавиатура, и пользователь не видит мигания
    «появились и пропали».
    """
    if keyboard is None:
        return
    prev_mid = _kb_service_msg.pop(chat_id, None)
    res = await call_bot("sendMessage", chat_id=chat_id, text="…",
                         reply_markup=_markup(keyboard))
    new_mid = res.get("message_id") if isinstance(res, dict) else None
    if new_mid:
        _kb_service_msg[chat_id] = new_mid
    if prev_mid and prev_mid != new_mid:
        try:
            await call_bot("deleteMessage", chat_id=chat_id, message_id=prev_mid)
        except Exception as e:  # noqa: BLE001
            log.debug("tg delete old kb msg: %s", e)


async def edit_message_text(chat_id: int, message_id: int, text: str,
                            keyboard: list | dict | None = None) -> bool:
    """Редактировать текст/клавиатуру сообщения бота (ответ на нажатие inline-кнопки)."""
    kwargs = {"chat_id": chat_id, "message_id": message_id,
              "text": text, "parse_mode": "HTML"}
    if keyboard:
        kwargs["reply_markup"] = _markup(keyboard)
    result = await call_bot("editMessageText", **kwargs)
    return result is not None


async def answer_callback(callback_query_id: str, text: str = "") -> None:
    """Закрыть «часики» на нажатой кнопке."""
    try:
        await call_bot("answerCallbackQuery", callback_query_id=callback_query_id, text=text)
    except Exception as e:  # noqa: BLE001
        log.warning("tg answer_callback_query: %s", e)


async def notify_user(session: AsyncSession, user_id: int | None, text: str) -> None:
    """Никаких await-сетевых вызовов из HTTP-запроса: вызывается только из воркера outbox."""
    if not user_id:
        return
    u = await session.get(User, user_id)
    if u and u.telegram_chat_id and u.telegram_optout != "all":
        await send_message(u.telegram_chat_id, text)


def format_entity_event(p: dict) -> str:
    """Карточка события для Telegram (как в демо-уведомлениях)."""
    actions = {"create": "создана", "update": "обновлена", "status": "сменила статус",
               "reassign": "переназначен ответственный", "share": "расшарена",
               "delete": "удалена", "done": "выполнена"}
    a = actions.get(p.get("action", ""), p.get("action", ""))
    d = p.get("details", {})
    lines = [f"<b>{p.get('module','')}</b>: «{p.get('title','')}» — {a}"]
    if "changed" in d and "status_key" in d["changed"]:
        lines.append(f"Статус: {d.get('old_status')} → {d['changed']['status_key']}")
    if d.get("due_date"):
        lines.append(f"Срок: {d['due_date']}")
    lines.append(f"Автор изменения: {p.get('actor','')}")
    return "\n".join(lines)


async def unbind(session: AsyncSession, chat_id: int | str) -> bool:
    """Отвязать Telegram от учётной записи (кнопка «Отвязать» в «Мои интеграции»)."""
    u = await session.scalar(select(User).where(User.telegram_chat_id == str(chat_id)))
    if u is None:
        return False
    u.telegram_chat_id = None
    u.telegram_bind_code = None
    u.telegram_optout = None
    await session.commit()
    return True


def _esc(s: str) -> str:
    """Экранирование для HTML-режима сообщений Bot API."""
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


async def _try_login_step(session: AsyncSession, chat_id: int, text: str) -> str | None:
    """Шаг «логин»: пользователь прислал логин (или /start <логин>).

    Если такой пользователь существует и ещё не привязан — запрашиваем пароль.
    Возвращает готовый текст ответа или None (молчаливое удаление ввода).
    """
    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        login = parts[1].strip().lower() if len(parts) == 2 else ""
    else:
        login = " ".join(text.strip().lower().split())   # «Ivanov  I» -> «ivanov i»
    if not login:
        return NOT_BOUND_YET  # просто /start — объясняем сценарий
    if len(login) > 64:
        return None
    u = await session.scalar(select(User).where(User.login == login))
    if u is None:
        # неизвестный логин: подсказку не шлём (защита от перебора), окно не открываем
        return None
    if u.telegram_chat_id and str(u.telegram_chat_id) != str(chat_id):
        # логин уже привязан к другому чату — молча удаляем (не раскрываем чужие данные)
        return None
    if u.telegram_chat_id and str(u.telegram_chat_id) == str(chat_id):
        return None  # этот же чат уже привязан — ветка «уже привязан» выше
    _pending_put(chat_id, login)
    return ASK_PASSWORD.format(login=_esc(login))


class _LastAsk:
    map: dict[int, int] = {}


_last_ask = _LastAsk()


async def handle_update(session: AsyncSession, upd: dict) -> tuple[str | None, list[int], list | dict | None]:
    """Обработка входящего апдейта Bot API.

    Возвращает (текст_ответа, [message_id для удаления], клавиатура или None):
    list — строки кнопок (reply-клавиатура закрепляется над полем ввода,
    inline — под сообщением), dict — служебная (remove/force_reply).
    Правила ТЗ: после успешной регистрации бот только отправляет уведомления;
    посторонние сообщения обычного пользователя удаляются из чата.
    Администратор/координатор: полноценный помощник — закреплённая клавиатура
    админ-меню (integrations/tg_admin.py, нажатия приходят текстом и
    обрабатываются tg_admin.handle_reply_menu) плюс inline-списки выбора
    (карточки/рассылки → tg_admin.handle_callback) и текстовые команды
    /report, /broadcast (с фото/видео), /find и т.д. Клавиатура с админ-кнопками
    прикладывается только если роль пользователя проходит tg_admin._allow,
    поэтому обычным пользователям админские функции недоступны и не видны.
    Сценарий привязки: «логин → пароль», код привязки не используется.
    """
    cbq = upd.get("callback_query")
    if cbq:
        from integrations import tg_admin
        res = await tg_admin.handle_callback(session, cbq)
        if isinstance(res, tuple):
            return res[0], [], res[1]
        return res, [], None

    msg = upd.get("message") or upd.get("edited_message")
    if not msg:
        return None, [], None
    _gc_pending()
    chat_id = msg["chat"]["id"]
    mid = msg["message_id"]
    text = (msg.get("text") or "").strip()
    user = await session.scalar(select(User).where(User.telegram_chat_id == str(chat_id)))

    # --- пользователь уже привязан: команды/кнопки обрабатываем, остальное удаляем молча ---
    if user is not None:
        from integrations import tg_admin

        is_adm = await tg_admin._allow(session, user)   # клавиатура — только админу/координатору

        def out(reply: str | None, delete: bool = True, kb: list | dict | None = None):
            # reply is None — «молчаливое удаление» входящего сообщения;
            # при reply != None удаляем ввод пользователя, чтобы логин/пароль
            # и служебные команды не оставались в истории диалога
            return reply, ([mid] if delete else []), kb

        # медиа от администратора: вложение к рассылке / библиотеку.
        # ВАЖНО: проверяем до text-ветки — у фото/видео бывает caption
        # (подпись к файлу), это всё равно медиа-сообщение.
        if ("photo" in msg or "video" in msg or "document" in msg):
            if is_adm:
                reply, kb = await tg_admin.handle_media(session, user, msg)
                return out(reply, kb=kb)
            return out(None)   # обычный пользователь прислал мусор — удаляем

        if text:
            cmd, _, args = text.partition(" ")
            cmd = cmd.lower().split("@", 1)[0]
            # команды привязки/общие — всегда через startswith (обработка ниже)
            if not cmd.startswith(("/unbind", "/mute", "/unmute")):
                if cmd == "/help":
                    return await tg_admin.cmd_help(session, user, args), [mid], \
                        tg_admin.main_keyboard(is_adm)
                if cmd == "/start":
                    # главное меню: закреплённая клавиатура админ-функций
                    # (только для админа/координатора)
                    return await tg_admin.cmd_menu(session, user), [mid], \
                        tg_admin.main_keyboard(is_adm)
                # нажатие закреплённой reply-кнопки приходит как обычный текст;
                # «📢 Рассылка» с готовым текстом в args — кнопка из подсказки
                # машины рассылки («отправить новый /broadcast …»).
                action = tg_admin.menu_action(text)
                if action is not None:
                    # во время активной рассылки подписи кнопок подтверждения
                    # обрабатывает машина состояний (свои ветки для этих текстов)
                    st_active = (tg_admin._bcast.get(int(user.telegram_chat_id))
                                 if user.telegram_chat_id else None)
                    if not (action in ("bcast_send", "bcast_edit", "bcast_media")
                            and st_active is not None
                            and st_active.get("step") == "confirm"):
                        reply, kb = await tg_admin.handle_reply_menu(session, user, action)
                        return out(reply, kb=kb)
                if cmd == "/broadcast":
                    if not is_adm:
                        return out(None)
                    # пустая команда = нажатие кнопки «📢 Рассылка»: тот же UX
                    # (закреплённые reply-кнопки вместо inline/текстовых подсказок)
                    if not args.strip():
                        st_reply, st_kb = await tg_admin.handle_reply_menu(
                            session, user, "bcast")
                        return out(st_reply, kb=st_kb)
                    reply, kb = await tg_admin.cmd_broadcast(session, user, args)
                    return out(reply, kb=kb)
                if cmd in ("/cancel", "/pending", "/overdue", "/find", "/task",
                           "/broadcast", "/report", "/media"):
                    # все эти команды — административные; внутри каждой стоит _allow,
                    # но для обычного пользователя корректный ответ — «удалить молча»,
                    # а не служебное сообщение про права (иначе тест member-поведения
                    # и UX ломаются: бот не должен светить существование команд).
                    if not is_adm:
                        return out(None)
                    if cmd == "/broadcast":
                        reply, kb = await tg_admin.cmd_broadcast(session, user, args)
                        return out(reply, kb=kb)
                    if cmd == "/report":
                        return await tg_admin.cmd_report(session, user, args), [mid], \
                            tg_admin.back_keyboard()
                    if cmd == "/media":
                        return ("Отправьте фото или видео следующим сообщением — "
                                "прикреплю его к следующей рассылке."), [mid], \
                            tg_admin.force_reply("Отправьте фото или видео")
                    fn = {"/cancel": tg_admin.cmd_cancel, "/pending": tg_admin.cmd_pending,
                          "/overdue": tg_admin.cmd_overdue, "/find": tg_admin.cmd_find,
                          "/task": tg_admin.cmd_task}[cmd]
                    reply = await fn(session, user, args)
                    kb = (tg_admin.cancel_keyboard() if cmd == "/pending" and is_adm
                          else tg_admin.back_keyboard())
                    return reply, [mid], kb
                # текст без слэша: может быть шагом машины /broadcast
                reply, kb = await tg_admin.handle_broadcast_input(session, user, text)
                if reply is not None:
                    return out(reply, kb=kb)
                if is_adm:
                    return ("Неизвестная команда. Кнопки меню — /start, список — /help",
                            [mid], tg_admin.main_keyboard(True))
                return out(None)   # «просто удаляет» сообщения пользователя

        if text.startswith("/unbind"):
            user.telegram_chat_id = None
            user.telegram_bind_code = None
            user.telegram_optout = None
            _pending.pop(chat_id, None)
            _last_ask.map.pop(chat_id, None)
            await session.commit()
            log.info("tg: chat %s отвязан от пользователя '%s'", chat_id, user.login)
            return ("Telegram отвязан от учётной записи <b>{name}</b>. Чтобы привязать "
                    "заново — отправьте свой логин.").format(name=_esc(user.display_name)), [mid], None
        if text.startswith("/mute"):
            user.telegram_optout = "all"
            await session.commit()
            return "Уведомления отключены. /unmute — включить.", [mid], None
        if text.startswith("/unmute"):
            user.telegram_optout = None
            await session.commit()
            return "Уведомления включены.", [mid], None
        if text.startswith("/help"):
            return ("Бот работает только на отправку уведомлений. "
                    "Команды: /mute — отключить, /unmute — включить, "
                    "/unbind — отвязать Telegram от аккаунта."), [mid], None
        return out(None)   # «просто удаляет» сообщения пользователя

    # --- шаг 2: ожидаем пароль — состояние закрывается ровно одной попыткой ---
    if chat_id in _pending:
        pending = _pending.pop(chat_id)
        ask_id = _last_ask.map.pop(chat_id, None)
        if pending.expires < time.time():
            return RESET_HINT, [m for m in (ask_id, mid) if m], None
        u = await session.scalar(select(User).where(User.login == pending.login))
        ok = bool(u and u.is_active and u.password_hash
                  and verify_password(text, u.password_hash))
        # диалог очищаем ВСЕГДА: и введённый пароль, и вопрос о пароле
        deletes = [m for m in (ask_id, mid) if m]
        if not ok:
            now = time.time()
            if now - _throttle.get(chat_id, 0) < 3:
                return None, deletes, None       # анти-брутфорс: молчаливый пропуск
            _throttle[chat_id] = now
            return BAD_CREDENTIALS, deletes, None
        u.telegram_chat_id = str(chat_id)
        u.telegram_bind_code = None
        u.telegram_optout = None
        await session.commit()
        log.info("tg: chat %s привязан к пользователю '%s'", chat_id, u.login)
        from integrations import tg_admin
        is_adm = await tg_admin._allow(session, u)
        return WELCOME.format(name=_esc(u.display_name)), deletes, tg_admin.main_keyboard(is_adm)

    # --- шаг 1: пользователь присылает логин (или /start [<логин>]) ---
    reply = await _try_login_step(session, chat_id, text)
    if chat_id in _pending:
        # окно «ждём пароль» открыто, reply = ASK_PASSWORD — шлём его сами,
        # id вопроса запоминаем, чтобы удалить вместе с паролем
        sent = await call_bot("sendMessage", chat_id=chat_id, text=reply, parse_mode="HTML")
        if sent:
            _last_ask.map[chat_id] = sent["message_id"]
        return None, [mid], None
    if reply:
        # подсказка без открытия окна (пустой /start)
        return reply, [mid], None
    # неизвестный логин / чужой логин / пустой ввод — молча удаляем
    return None, [mid], None
