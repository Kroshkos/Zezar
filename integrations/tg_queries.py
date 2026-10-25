"""Запросы к данным для Telegram-команд."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import build_context, entity_filter
from app.models.core import Entity, EntityKind, EntityResponsible, User


async def my_tasks_text(session: AsyncSession, chat_id: int) -> str:
    u = await session.scalar(select(User).where(User.telegram_chat_id == chat_id))
    if not u:
        return "Сначала привяжите аккаунт: /start <код>"
    ctx = await build_context(session, u)
    rows = (await session.scalars(
        select(Entity).where(Entity.kind == EntityKind.TASK, entity_filter(ctx),
                             Entity.status_key.notin_(["done", "closed"]))
        .order_by(Entity.due_date))).all()
    mine = [e for e in rows if e.responsible_user_id == u.id]
    if not mine:
        return "Активных задач на вас нет ✅"
    out = ["<b>Мои задачи:</b>"]
    for t in mine[:15]:
        out.append(f"• {t.title} — до {t.due_date or '—'} ({t.status_key})")
    return "\n".join(out)


# ---------------- команды администратора (делегирование в tg_admin) ----------------
# Реестр и обработчики вынесены в integrations/tg_admin.py, чтобы модуль
# запросов не тял Bot API и машину состояний рассылки.

from integrations.tg_admin import (  # noqa: E402,F401
    AUDIENCE_BUTTONS, HELP_ADMIN, attach_pending_media, audience_keyboard, back_keyboard,
    bcast_kb, cancel_keyboard, cmd_broadcast, cmd_cancel, cmd_find, cmd_help,
    cmd_menu, cmd_overdue, cmd_pending, cmd_report, cmd_task, confirm_keyboard,
    dept_keyboard, download_and_rehost_file, find_keyboard, handle_broadcast_input,
    handle_callback, handle_media, main_keyboard, overdue_entities, pending_keyboard,
)
