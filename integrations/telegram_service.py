"""
Telegram-интеграция: исходящие уведомления + входящие команды (/start код, /tasks).

Бот на aiogram НЕ обязателен — используется raw Bot API через httpx,
чтобы не тянуть тяжёлый фреймворк. Команды обрабатывает long-polling воркер
(integrations/tg_worker.py) или webhook (POST /api/tg/webhook).
"""
from __future__ import annotations

import logging

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.core import User

log = logging.getLogger("caesar.tg")


async def send_message(chat_id: int, text: str) -> bool:
    s = get_settings()
    if not s.telegram_bot_token:
        return False
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            f"https://api.telegram.org/bot{s.telegram_bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
    ok = r.status_code == 200
    if not ok:
        log.warning("TG send failed %s: %s", r.status_code, r.text[:200])
    return ok


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


async def bind_by_code(session: AsyncSession, chat_id: int, code: str) -> str:
    """Привязка по коду из профиля пользователя (код генерируется в /api/profile/telegram/bind)."""
    u = await session.scalar(select(User).where(User.telegram_bind_code == code))
    if u is None:
        return "Код не найден. Откройте «Мои интеграции» и сгенерируйте новый."
    u.telegram_chat_id = str(chat_id)
    u.telegram_bind_code = None
    u.telegram_optout = None
    await session.commit()
    return f"Готово, {u.display_name}! Уведомления будут приходить сюда. Команды: /tasks, /mute, /help."


async def handle_update(session: AsyncSession, upd: dict) -> str | None:
    """Обработка входящего апдейта Bot API. Возвращает текст ответа."""
    msg = upd.get("message") or upd.get("edited_message")
    if not msg or "text" not in msg:
        return None
    chat_id = msg["chat"]["id"]
    text = msg["text"].strip()
    if text.startswith("/start"):
        parts = text.split()
        if len(parts) == 2:
            return await bind_by_code(session, chat_id, parts[1])
        return "Пришлите код привязки: /start <код>"
    if text == "/tasks":
        from integrations.tg_queries import my_tasks_text
        return await my_tasks_text(session, chat_id)
    if text.startswith("/help"):
        return ("Команды: /start <код> — привязать аккаунт; "
                "/tasks — мои активные задачи; /mute — отключить уведомления.")
    if text.startswith("/mute"):
        u = await session.scalar(select(User).where(User.telegram_chat_id == chat_id))
        if u:
            u.telegram_optout = "all"
            await session.commit()
            return "Уведомления отключены. /unmute — включить."
        return "Аккаунт не привязан."
    if text.startswith("/unmute"):
        u = await session.scalar(select(User).where(User.telegram_chat_id == chat_id))
        if u:
            u.telegram_optout = None
            await session.commit()
            return "Уведомления включены."
        return "Аккаунт не привязан."
    return None
