"""
Jinja2-окружение ядра: рендер страниц модулей с сохранением вёрстки прототипа.
nav_modules строится из реестра + включений workspace пользователя (как NAV[] в демо).
"""
from __future__ import annotations

import pathlib

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, FileSystemLoader
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import build_context, entity_filter
from app.core.module_registry import registry
from app.models.core import Entity, EntityKind, Role, User, Workspace, WorkspaceModule

TEMPLATES_DIR = pathlib.Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def init_templates() -> None:
    """Подключаем папки modules/<key>/templates к окружению ядра."""
    dirs = [str(TEMPLATES_DIR)]
    for spec in registry.all():
        p = pathlib.Path("modules") / spec.key / "templates"
        if p.is_dir():
            dirs.append(str(p))
    templates.env.loader = ChoiceLoader([FileSystemLoader(d) for d in dirs])


async def render_page(request: Request, template: str, *, module_key: str,
                      user: User, session: AsyncSession,
                      counts: dict[str, int] | None = None, **ctx):
    # Принудительная смена пароля: ни одна страница модулей не открывается до
    # смены начального пароля (раньше это делалось только на '/', остальные
    # пути отдавали страницу с JS-редиректом из base.html — теперь сервером).
    if getattr(user, "must_change_password", False):
        return RedirectResponse("/change-password?forced=1", status_code=302)

    nav_ctx = await build_context(session, user)

    enabled_keys: set[str] = set()
    dept_ids = nav_ctx.department_ids or [-1]
    ws_rows = (await session.scalars(
        select(Workspace).where(Workspace.department_id.in_(dept_ids)))).all()
    for ws in ws_rows:
        mods = (await session.scalars(
            select(WorkspaceModule).where(WorkspaceModule.workspace_id == ws.id,
                                          WorkspaceModule.is_enabled == True))).all()  # noqa: E712
        enabled_keys |= {m.module_key for m in mods}

    items = []
    for spec in registry.all():
        if not spec.is_core and spec.key not in enabled_keys and not nav_ctx.is_global:
            continue
        cnt = None
        if counts and spec.key in counts:
            cnt = counts[spec.key]
        elif spec.entity_kinds:
            kinds = [EntityKind[k] for k in spec.entity_kinds if k in EntityKind.__members__]
            if kinds:
                q = select(func.count(Entity.id)).where(
                    Entity.kind.in_(kinds), entity_filter(nav_ctx))
                cnt = await session.scalar(q) or 0
        items.append({"key": spec.key, "title": spec.title, "icon": spec.icon,
                      "url": f"/{spec.key}/", "count": cnt})

    base = {"user": user, "nav_modules": items, "active_module": module_key,
            "is_admin": user.role in (Role.ADMIN, Role.COORDINATOR)}
    base.update(ctx)
    return templates.TemplateResponse(request, template, base)
