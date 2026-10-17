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


async def send_message(chat_id: int, text: str) -> bool:
    result = await call_bot("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML")
    return result is not None


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


async def handle_update(session: AsyncSession, upd: dict) -> tuple[str | None, list[int]]:
    """Обработка входящего апдейта Bot API.

    Возвращает (текст_ответа, [message_id для удаления]).
    Правила ТЗ: после успешной регистрации бот только отправляет сообщения;
    любые входящие сообщения пользователя удаляются из чата.
    Сценарий привязки: «логин → пароль», код привязки не используется.
    """
    msg = upd.get("message") or upd.get("edited_message")
    if not msg or "text" not in msg:
        return None, []
    _gc_pending()
    chat_id = msg["chat"]["id"]
    text = msg["text"].strip()
    mid = msg["message_id"]
    user = await session.scalar(select(User).where(User.telegram_chat_id == str(chat_id)))

    # --- пользователь уже привязан: команды обрабатываем, остальное удаляем молча ---
    if user is not None:
        if text.startswith("/unbind"):
            user.telegram_chat_id = None
            user.telegram_bind_code = None
            user.telegram_optout = None
            _pending.pop(chat_id, None)
            _last_ask.map.pop(chat_id, None)
            await session.commit()
            log.info("tg: chat %s отвязан от пользователя '%s'", chat_id, user.login)
            return ("Telegram отвязан от учётной записи <b>{name}</b>. Чтобы привязать "
                    "заново — отправьте свой логин.").format(name=_esc(user.display_name)), [mid]
        if text.startswith("/mute"):
            user.telegram_optout = "all"
            await session.commit()
            return "Уведомления отключены. /unmute — включить.", [mid]
        if text.startswith("/unmute"):
            user.telegram_optout = None
            await session.commit()
            return "Уведомления включены.", [mid]
        if text.startswith("/help"):
            return ("Бот работает только на отправку уведомлений. "
                    "Команды: /mute — отключить, /unmute — включить, "
                    "/unbind — отвязать Telegram от аккаунта."), [mid]
        return None, [mid]   # «просто удаляет» сообщения пользователя

    # --- шаг 2: ожидаем пароль — состояние закрывается ровно одной попыткой ---
    if chat_id in _pending:
        pending = _pending.pop(chat_id)
        ask_id = _last_ask.map.pop(chat_id, None)
        if pending.expires < time.time():
            return RESET_HINT, [m for m in (ask_id, mid) if m]
        u = await session.scalar(select(User).where(User.login == pending.login))
        ok = bool(u and u.is_active and u.password_hash
                  and verify_password(text, u.password_hash))
        # диалог очищаем ВСЕГДА: и введённый пароль, и вопрос о пароле
        deletes = [m for m in (ask_id, mid) if m]
        if not ok:
            now = time.time()
            if now - _throttle.get(chat_id, 0) < 3:
                return None, deletes       # анти-брутфорс: молчаливый пропуск
            _throttle[chat_id] = now
            return BAD_CREDENTIALS, deletes
        u.telegram_chat_id = str(chat_id)
        u.telegram_bind_code = None
        u.telegram_optout = None
        await session.commit()
        log.info("tg: chat %s привязан к пользователю '%s'", chat_id, u.login)
        return WELCOME.format(name=_esc(u.display_name)), deletes

    # --- шаг 1: пользователь присылает логин (или /start [<логин>]) ---
    reply = await _try_login_step(session, chat_id, text)
    if chat_id in _pending:
        # окно «ждём пароль» открыто, reply = ASK_PASSWORD — шлём его сами,
        # id вопроса запоминаем, чтобы удалить вместе с паролем
        sent = await call_bot("sendMessage", chat_id=chat_id, text=reply, parse_mode="HTML")
        if sent:
            _last_ask.map[chat_id] = sent["message_id"]
        return None, [mid]
    if reply:
        # подсказка без открытия окна (пустой /start)
        return reply, [mid]
    # неизвестный логин / чужой логин / пустой ввод — молча удаляем
    return None, [mid]
