"""Модуль-плагин «Дашборд»: KPI по доступным сущностям (агрегация всех модулей реестра)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import access
from app.core.security import get_current_user
from app.deps import session_dep
from app.models.core import Entity, SyncState, User
from app.ui import render_page

router = APIRouter()


@router.get("")
@router.get("/")
async def dashboard(request: Request, user: User = Depends(get_current_user),
                    session: AsyncSession = Depends(session_dep)):
    ctx = await access.build_context(session, user)
    base = select(Entity.module_key, func.count(Entity.id)).where(
        access.entity_filter(ctx)).group_by(Entity.module_key)
    by_module = dict((await session.execute(base)).all())
    overdue = await session.scalar(
        select(func.count(Entity.id)).where(access.entity_filter(ctx),
                                            Entity.due_date != None,  # noqa: E711
                                            Entity.status_key != "done",
                                            Entity.due_date < __import__("datetime").date.today().isoformat())) or 0
    pending_sync = await session.scalar(
        select(func.count(Entity.id)).where(access.entity_filter(ctx),
                                            Entity.sync_state.in_([SyncState.PENDING, SyncState.ERROR]))) or 0
    return await render_page(
        request, "dashboard.html", module_key="dashboard", user=user, session=session,
        page_title="Дашборд", page_sub="Сводка по всем рабочим пространствам",
        stats=[{"label": title, "value": by_module.get(key, 0)}
               for key, title in [("tasks", "Задачи"), ("projects", "Проектная работа"),
                                  ("meetings", "Совещания"), ("docs", "Документы")]
               if by_module.get(key, 0) or True],
        overdue=overdue, pending_sync=pending_sync,
    )
