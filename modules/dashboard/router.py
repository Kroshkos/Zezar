"""Модуль-плагин «Дашборд»: KPI по доступным сущностям (агрегация всех модулей реестра)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import access
from app.core.security import get_current_user
from app.deps import session_dep
from app.models.core import Entity, EntityKind, SyncState, User
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
    # Просроченные: срок прошёл, статус не «завершающий». Сравнение только с
    # "done" считало просроченными снятые задачи и остановленные проекты.
    from app.core.notify import terminal_status_keys
    term_task = await terminal_status_keys(session, "task")
    term_project = await terminal_status_keys(session, "project")
    today_iso = __import__("datetime").date.today().isoformat()
    # session.execute, а не session.scalars: scalars при двух колонках возвращает
    # первые значения строк (status_key), и распаковка «sk, k» падает с
    # ValueError: too many values to unpack.
    overdue_rows = (await session.execute(
        select(Entity.status_key, Entity.kind).where(
            access.entity_filter(ctx),
            Entity.kind.in_([EntityKind.TASK, EntityKind.PROJECT]),
            Entity.due_date != None,  # noqa: E711
            Entity.due_date < today_iso))).all()
    overdue = sum(
        1 for row in overdue_rows
        if (row[0] or "") not in (term_task if str(getattr(row[1], "value", row[1])) == "task"
                                  else term_project))
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
