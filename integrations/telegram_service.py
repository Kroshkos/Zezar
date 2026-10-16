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
        return None
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
    """Ждём пароль от chat_id: хеш пароля НЕ храним, храним только сам пароль
    в памяти процесса на короткое окно (<= 3 минут) — сразу после проверки
    запись стирается, история чата очищается."""
    __slots__ = ("login", "pass_hash_check", "expires")

    def __init__(self, login: str):
        self.login = login
        self.expires = time.time() + 180


_pending: dict[int, _PendingAuth] = {}          # chat_id -> ожидающий логин
_pending_pw: dict[int, tuple[str, float]] = {}  # chat_id -> (пароль, deadline)
_throttle: dict[int, float] = {}                # chat_id -> когда можно повторить


def _pending_put(chat_id: int, login: str) -> None:
    _pending[chat_id] = _PendingAuth(login)


def _gc_pending() -> None:
    now = time.time()
    for cid in [c for c, p in _pending.items() if p.expires < now]:
        _pending.pop(cid, None)
    for cid in [c for c, (_, dl) in _pending_pw.items() if dl < now]:
        _pending_pw.pop(cid, None)


WELCOME = ("<b>👋 Добро пожаловать, {name}!</b>\n\n"
           "Аккаунт привязан к системе ЦЕЗАРЬ. Здесь я буду присылать уведомления:\n"
           "• задачи — назначение, изменения статуса, приближение срока;\n"
           "• совещания и проекты — решения по вашему участию.\n\n"
           "Отвечать в этом чате не нужно — бот работает только на отправку. "
           "Команды: /mute — отключить уведомления, /unmute — включить.")

ASK_PASSWORD = ("Отлично, логин <b>{login}</b> найден.\n"
                "Теперь одним сообщением отправьте <b>пароль</b> от вашей учётной "
                "записи ЦЕЗАРЬ.\n\n🔒 После проверки я удалю оба сообщения из этого "
                "диалога — пароль нигде не останется.")

BAD_CREDENTIALS = ("⛔ Логин или пароль не подходят. Диалог очищен.\n"
                   "Если забыли пароль — обратитесь к администратору системы.")

ALREADY_BOUND = ("✅ Этот Telegram уже привязан к учётной записи <b>{name}</b>. "
                 "Сообщения удаляются — бот работает только на отправку уведомлений.")

NOT_BOUND_YET = ("Аккаунт ещё не привязан. Откройте систему ЦЕЗАРЬ → «Мои интеграции» "
                 "и напишите боту свой логин.")


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


async def bind_by_code(session: AsyncSession, chat_id: int, code: str) -> str | None:
    """Легас-привязка по одноразовому коду (/start <код>). Возвращает приветствие."""
    u = await session.scalar(select(User).where(User.telegram_bind_code == code))
    if u is None:
        return None  # код не найден — обработчик решит, что это ввод логина
    u.telegram_chat_id = str(chat_id)
    u.telegram_bind_code = None
    u.telegram_optout = None
    await session.commit()
    return WELCOME.format(name=_esc(u.display_name))


def _esc(s: str) -> str:
    """Экранирование для HTML-режима сообщений Bot API."""
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


async def _try_login_step(session: AsyncSession, chat_id: int, text: str) -> str | None:
    """Шаг «логин»: пользователь прислал логин (или /start <логин>).

    Если такой пользователь существует и ещё не привязан — запрашиваем пароль.
    """
    login = text.split(maxsplit=1)[1].strip().lower() if text.startswith("/start") else text.strip().lower()
    if not login or len(login) > 64:
        return "NOT_BOUND_YET"
    u = await session.scalar(select(User).where(User.login == login))
    if u is None:
        # неизвестный логин: подсказку не шлём (защита от перебора), окно не открываем
        return None
    if u.telegram_chat_id and str(u.telegram_chat_id) != str(chat_id):
        # чат уже занят другой УЗ — отвечаем (текст поверх маркера, чтобы поллинер
        # не удалил его как «запрос пароля») и окно не открываем
        return "ALREADY_BOUND:" + ALREADY_BOUND.format(name=_esc(u.display_name))
    if u.telegram_chat_id and str(u.telegram_chat_id) == str(chat_id):
        return None  # этот же чат уже привязан — ветка «уже привязан» выше
    _pending_put(chat_id, login)
    return ASK_PASSWORD.format(login=_esc(login))


async def _try_password_step(session: AsyncSession, upd: dict) -> str | None:
    """Шаг «пароль»: проверяем учётные данные, стираем всё из памяти и чата."""
    msg = upd["message"]
    chat_id = msg["chat"]["id"]
    text = msg["text"].strip()
    pending = _pending.get(chat_id)
    if pending is None or pending.expires < time.time():
        _pending.pop(chat_id, None)
        return None
    login = pending.login
    _pending.pop(chat_id, None)  # состояние живёт ровно один шаг
    u = await session.scalar(select(User).where(User.login == login))
    ok = bool(u and u.is_active and u.password_hash and verify_password(text, u.password_hash))
    # Очередь на очистку диалога: и пароль, и вопрос о пароле будут удалены
    # поллинером СРАЗУ после отправки ответа (см. handle_update).
    if not ok:
        now = time.time()
        prev = _throttle.get(chat_id, 0)
        if now - prev < 3:
            return None  # защита от брутфорса: молчаливый пропуск частых попыток
        _throttle[chat_id] = now
        return BAD_CREDENTIALS
    u.telegram_chat_id = str(chat_id)
    u.telegram_bind_code = None
    u.telegram_optout = None
    await session.commit()
    log.info("tg: chat %s привязан к пользователю '%s'", chat_id, u.login)
    return WELCOME.format(name=_esc(u.display_name))


class _LastAsk:
    map: dict[int, int] = {}


_last_ask = _LastAsk()


async def handle_update(session: AsyncSession, upd: dict) -> tuple[str | None, list[int]]:
    """Обработка входящего апдейта Bot API.

    Возвращает (текст_ответа, [message_id для удаления]).
    Правила ТЗ: после успешной регистрации бот только отправляет сообщения;
    любые входящие сообщения пользователя удаляются из чата.
    """
    msg = upd.get("message") or upd.get("edited_message")
    if not msg or "text" not in msg:
        return None, []
    _gc_pending()
    chat_id = msg["chat"]["id"]
    text = msg["text"].strip()
    user = await session.scalar(select(User).where(User.telegram_chat_id == str(chat_id)))

    # уже привязан: команды mute/unmute обрабатываем, остальное — удаляем молча
    if user is not None:
        if text.startswith("/mute"):
            user.telegram_optout = "all"
            await session.commit()
            return "Уведомления отключены. /unmute — включить.", [msg["message_id"]]
        if text.startswith("/unmute"):
            user.telegram_optout = None
            await session.commit()
            return "Уведомления включены.", [msg["message_id"]]
        if text.startswith("/help"):
            return ("Бот работает только на отправку уведомлений. "
                    "Команды: /mute — отключить, /unmute — включить."), [msg["message_id"]]
        if text.startswith("/start ") and len(text.split()) == 2 and len(text.split()[1]) == 8:
            pass  # возможная перепривязка по коду — уходим в легас-ветку ниже
        else:
            return None, [msg["message_id"]]   # «просто удаляет» сообщения пользователя

    # не привязан: ожидаем пароль?
    if chat_id in _pending:
        reply = await _try_password_step(session, upd)
        # очищаем диалог независимо от исхода проверки: пароль + запрос пароля
        deletes = [m for m in (_last_ask.map.pop(chat_id, None), msg["message_id"]) if m]
        return reply, deletes

    # легас: /start <код> (8 hex) — одноразовый код из «Мои интеграции»
    if text.startswith("/start ") and len(text.split()) == 2 and len(text.split()[1]) == 8:
        reply = await bind_by_code(session, chat_id, text.split()[1])
        if reply:
            return reply, []
        # код не найден — считаем это вводом логина и продолжаем основной сценарий

    # основной сценарий: первый шаг — пользователь присылает свой логин.
    reply = await _try_login_step(session, chat_id, text)
    if reply == "NOT_BOUND_YET":
        return NOT_BOUND_YET, [msg["message_id"]]
    if reply and reply.startswith("ALREADY_BOUND:"):
        return reply[len("ALREADY_BOUND:"):], [msg["message_id"]]
    if reply:  # ASK_PASSWORD — отправляем сами; id вопроса чистим после ввода пароля
        sent = await call_bot("sendMessage", chat_id=chat_id, text=reply, parse_mode="HTML")
        if sent:
            _last_ask.map[chat_id] = sent["message_id"]
    # неизвестный логин/ввод до привязки — молча удаляем (подсказку не спамим)
    return None, [msg["message_id"]]
