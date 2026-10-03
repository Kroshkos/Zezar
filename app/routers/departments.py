"""
Управление подразделениями и рабочими пространствами (ядро, доступно admin/coordinator).

  GET    /api/core/departments                 — список + мои пространства
  POST   /api/core/departments                 — создать подразделение (+workspace+базовые модули)
  PATCH  /api/core/departments/{id}            — имя/описание/активность
  GET    /api/core/departments/{id}/members    — состав
  POST   /api/core/departments/{id}/members    — добавить пользователя с ролью в пространстве
  DELETE /api/core/departments/{id}/members/{user_id}
  GET    /api/core/departments/{id}/modules    — включение/выключение модулей workspace
  PUT    /api/core/departments/{id}/modules/{key}
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import build_context
from app.core.module_registry import registry
from app.core.security import get_current_user
from app.deps import session_dep
from app.models.core import (
    Department, DepartmentMember, ModuleRegistry, Role, User, Workspace, WorkspaceModule,
)

router = APIRouter()


class DeptIn(BaseModel):
    code: str = Field(min_length=2, max_length=32, pattern=r"^[a-z0-9_-]+$")
    name: str = Field(min_length=2, max_length=200)
    description: str | None = None


async def _require_global(session, user):
    ctx = await build_context(session, user)
    if not ctx.can_admin_system:
        raise HTTPException(403, "Только администратор/координатор")
    return ctx


@router.get("/departments")
async def list_departments(user: User = Depends(get_current_user),
                           session: AsyncSession = Depends(session_dep)):
    ctx = await build_context(session, user)
    q = select(Department).where(Department.is_active == True)  # noqa: E712
    if not ctx.is_global:
        q = q.where(Department.id.in_(ctx.department_ids or [-1]))
    rows = (await session.scalars(q)).all()
    return [{"id": d.id, "code": d.code, "name": d.name, "description": d.description} for d in rows]


@router.post("/departments")
async def create_department(body: DeptIn, user: User = Depends(get_current_user),
                            session: AsyncSession = Depends(session_dep)):
    await _require_global(session, user)
    if await session.scalar(select(Department).where(Department.code == body.code)):
        raise HTTPException(409, "Код подразделения занят")
    dept = Department(code=body.code, name=body.name, description=body.description)
    session.add(dept)
    await session.flush()
    ws = Workspace(department_id=dept.id)
    session.add(ws)
    await session.flush()
    # базовые модули включаются автоматически списком реестра enabled_by_default
    for spec in registry.all():
        if spec.enabled_by_default:
            session.add(WorkspaceModule(workspace_id=ws.id, module_key=spec.key))
    # автор — head своего подразделения, если ещё не состоит
    if user.role in (Role.ADMIN, Role.COORDINATOR):
        pass  # глобальные роли видят всё без членства
    await session.commit()
    return {"id": dept.id, "code": dept.code, "name": dept.name}


@router.patch("/departments/{dept_id}")
async def patch_department(dept_id: int, body: dict, user: User = Depends(get_current_user),
                           session: AsyncSession = Depends(session_dep)):
    await _require_global(session, user)
    dept = await session.get(Department, dept_id)
    if not dept:
        raise HTTPException(404)
    for k in ("name", "description", "is_active"):
        if k in body:
            setattr(dept, k, body[k])
    await session.commit()
    return {"ok": True}


@router.get("/departments/{dept_id}/members")
async def members(dept_id: int, user: User = Depends(get_current_user),
                  session: AsyncSession = Depends(session_dep)):
    ctx = await build_context(session, user)
    if not ctx.is_global and dept_id not in ctx.department_ids:
        raise HTTPException(403)
    rows = (await session.scalars(
        select(DepartmentMember).where(DepartmentMember.department_id == dept_id)
    )).all()
    out = []
    for m in rows:
        u = await session.get(User, m.user_id)
        out.append({"user_id": u.id, "name": u.display_name, "role": u.role.value,
                    "role_in_dept": m.role_in_dept.value, "initials": u.initials})
    return out


class MemberIn(BaseModel):
    user_id: int
    role_in_dept: Role = Role.MEMBER


@router.post("/departments/{dept_id}/members")
async def add_member(dept_id: int, body: MemberIn, user: User = Depends(get_current_user),
                     session: AsyncSession = Depends(session_dep)):
    """Админ назначает; Head — только роль MEMBER внутри своего пространства."""
    ctx = await build_context(session, user)
    is_head_here = (not ctx.is_global and dept_id in ctx.department_ids and
                    any(m.role_in_dept == Role.HEAD for m in await session.scalars(
                        select(DepartmentMember).where(
                            DepartmentMember.department_id == dept_id,
                            DepartmentMember.user_id == user.id))))
    if not ctx.is_global and not is_head_here:
        raise HTTPException(403)
    if not ctx.is_global and body.role_in_dept != Role.MEMBER:
        raise HTTPException(403, "Head может добавлять только участников")
    target = await session.get(User, body.user_id)
    if not target:
        raise HTTPException(404, "Пользователь не найден")
    existing = await session.scalar(
        select(DepartmentMember).where(DepartmentMember.user_id == body.user_id,
                                       DepartmentMember.department_id == dept_id))
    if existing:
        existing.role_in_dept = body.role_in_dept
    else:
        session.add(DepartmentMember(user_id=body.user_id, department_id=dept_id,
                                     role_in_dept=body.role_in_dept))
    await session.commit()
    return {"ok": True}


@router.delete("/departments/{dept_id}/members/{member_user_id}")
async def remove_member(dept_id: int, member_user_id: int,
                        user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(session_dep)):
    await _require_global(session, user)
    row = await session.scalar(
        select(DepartmentMember).where(DepartmentMember.user_id == member_user_id,
                                       DepartmentMember.department_id == dept_id))
    if row:
        await session.delete(row)
        await session.commit()
    return {"ok": True}


@router.get("/departments/{dept_id}/modules")
async def dept_modules(dept_id: int, user: User = Depends(get_current_user),
                       session: AsyncSession = Depends(session_dep)):
    ctx = await build_context(session, user)
    if not ctx.is_global and dept_id not in ctx.department_ids:
        raise HTTPException(403)
    ws = await session.scalar(select(Workspace).where(Workspace.department_id == dept_id))
    rows = (await session.scalars(
        select(WorkspaceModule).where(WorkspaceModule.workspace_id == ws.id))).all()
    enabled = {r.module_key: r.is_enabled for r in rows}
    return [{"key": s.key, "title": s.title, "icon": s.icon, "order": s.order,
             "is_core": s.is_core, "enabled": enabled.get(s.key, False)}
            for s in registry.all()]


@router.put("/departments/{dept_id}/modules/{module_key}")
async def toggle_module(dept_id: int, module_key: int | str, enabled: bool,
                        user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(session_dep)):
    await _require_global(session, user)
    spec = registry.get(str(module_key))
    if not spec:
        raise HTTPException(404, "Модуль не зарегистрирован")
    if spec.is_core and not enabled:
        raise HTTPException(400, "Базовый модуль нельзя отключить")
    ws = await session.scalar(select(Workspace).where(Workspace.department_id == dept_id))
    row = await session.scalar(
        select(WorkspaceModule).where(WorkspaceModule.workspace_id == ws.id,
                                      WorkspaceModule.module_key == str(module_key)))
    if row is None:
        session.add(WorkspaceModule(workspace_id=ws.id, module_key=str(module_key),
                                    is_enabled=enabled))
    else:
        row.is_enabled = enabled
    await session.commit()
    return {"ok": True}
