"""Модуль-плагин «Дашборд»: живая сводка по всем модулям реестра.

Контекст строит modules/dashboard/queries.build_dashboard_data (агрегаты с
учётом изоляции пространств пользователя); шаблон — dashboard.html.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.deps import session_dep
from app.models.core import User
from app.ui import render_page
from modules.dashboard.queries import build_dashboard_data

router = APIRouter()


@router.get("")
@router.get("/")
async def dashboard(request: Request, dept: int | None = None,
                    user: User = Depends(get_current_user),
                    session: AsyncSession = Depends(session_dep)):
    # dept — выбор в выпадающем списке шапки дашборда: None/0 — «Вся система»,
    # иначе — агрегаты только по выбранному подразделению (см. queries).
    data = await build_dashboard_data(session, user, department_id=dept)
    # dash_departments/dash_selected_dept — выпадающий список «подразделение /
    # вся система» в правом верхнем углу шапки (block topbar в base.html).
    return await render_page(
        request, "dashboard.html", module_key="dashboard", user=user, session=session,
        page_title="Дашборд",
        page_sub="Сводка по всем рабочим пространствам · данные обновляются при открытии",
        dash_departments=data.pop("departments", []),
        dash_selected_dept=data.pop("selected_dept", None),
        **data)
