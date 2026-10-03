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
