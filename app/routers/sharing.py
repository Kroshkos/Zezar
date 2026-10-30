"""
Кросс-подразделенческий шеринг сущностей (ядро).

  GET  /api/core/entities/{uid}/shares
  POST /api/core/entities/{uid}/shares        {target_department_id, permission}
  DELETE /api/core/entities/{uid}/shares/{dept_id}
  GET  /api/core/entities/{uid}/responsibles
  PUT  /api/core/entities/{uid}/responsibles/{dept_id}   {user_id|null}
  GET  /api/core/departments/{dept_id}/users-for-resp    — кого можно назначить
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import access, entity_service
from app.core.access import build_context, resolve_permission, can_write, can_manage_sharing
from app.core.security import get_current_user
from app.deps import session_dep
from app.models.core import (
    Department, DepartmentMember, Entity, EntityResponsible, EntityShare,
    Permission, Role, User,
)

router = APIRouter()


async def _get_entity(session, uid: str) -> Entity:
    ent = await session.scalar(select(Entity).where(Entity.uid == uid))
    if not ent:
        raise HTTPException(404, "Сущность не найдена")
    return ent


class ShareIn(BaseModel):
    target_department_id: int
    permission: Permission = Permission.READ


@router.get("/departments/for-share")
async def departments_for_share(user: User = Depends(get_current_user),
                                session: AsyncSession = Depends(session_dep)):
    """Все активные подразделения как возможные цели шеринга.

    Модульные страницы раньше отдавали в выпадающий список только пространства
    пользователя (ctx.department_ids) — руководитель не мог расшарить сущность
    другим подразделениям, хотя бэкенд (POST /shares) это разрешает.
    """
    rows = (await session.scalars(
        select(Department).where(Department.is_active == True)  # noqa: E712
        .order_by(Department.name))).all()
    return [{"id": d.id, "name": d.name} for d in rows]


@router.get("/entities/{uid}/shares")
async def list_shares(uid: str, user: User = Depends(get_current_user),
                      session: AsyncSession = Depends(session_dep)):
    ent = await _get_entity(session, uid)
    ctx = await build_context(session, user)
    if await resolve_permission(session, ctx, ent) is None:
        raise HTTPException(403)
    rows = (await session.scalars(select(EntityShare).where(EntityShare.entity_id == ent.id))).all()
    out = []
    for s in rows:
        d = await session.get(__import__("app.models.core", fromlist=["Department"]).Department,
                              s.target_department_id)
        out.append({"department_id": s.target_department_id, "department_name": d.name,
                    "permission": s.permission.value})
    return out


@router.post("/entities/{uid}/shares")
async def share(uid: str, body: ShareIn, user: User = Depends(get_current_user),
               session: AsyncSession = Depends(session_dep)):
    ent = await _get_entity(session, uid)
    ctx = await build_context(session, user)
    perm = await resolve_permission(session, ctx, ent)
    # шерингом управляет владелец (head/edit+ в своём пространстве) или глобальная роль
    owner_here = ent.owner_department_id in ctx.department_ids
    if not (ctx.is_global or (owner_here and can_write(perm))):
        raise HTTPException(403, "Шеринг доступен только подразделению-владельцу")
    if body.target_department_id == ent.owner_department_id:
        raise HTTPException(400, "Нельзя расшарить самому себе")
    await entity_service.share_entity(session, user, ent,
                                      body.target_department_id, body.permission)
    await session.commit()
    return {"ok": True, "note": "Слот ответственного принимающего подразделения создан"}


@router.delete("/entities/{uid}/shares/{dept_id}")
async def unshare(uid: str, dept_id: int, user: User = Depends(get_current_user),
                  session: AsyncSession = Depends(session_dep)):
    ent = await _get_entity(session, uid)
    ctx = await build_context(session, user)
    perm = await resolve_permission(session, ctx, ent)
    if not (ctx.is_global or ent.owner_department_id in ctx.department_ids
            or can_manage_sharing(perm)):
        raise HTTPException(403)
    await entity_service.unshare_entity(session, user, ent, dept_id)
    await session.commit()
    return {"ok": True}


class SharePermIn(BaseModel):
    permission: Permission


@router.patch("/entities/{uid}/shares/{dept_id}")
async def patch_share(uid: str, dept_id: int, body: SharePermIn,
                      user: User = Depends(get_current_user),
                      session: AsyncSession = Depends(session_dep)):
    """Изменение уровня прав исходящего шеринга (владелец сущности / глобальная роль)."""
    ent = await _get_entity(session, uid)
    ctx = await build_context(session, user)
    perm = await resolve_permission(session, ctx, ent)
    owner_here = ent.owner_department_id in ctx.department_ids
    if not (ctx.is_global or (owner_here and can_write(perm))):
        raise HTTPException(403, "Права шеринга меняет только подразделение-владелец")
    row = await session.scalar(select(EntityShare).where(
        EntityShare.entity_id == ent.id,
        EntityShare.target_department_id == dept_id))
    if row is None:
        raise HTTPException(404, "Шеринг не найден")
    await entity_service.share_entity(session, user, ent, dept_id, body.permission)
    await session.commit()
    return {"ok": True, "permission": body.permission.value}


@router.post("/entities/{uid}/shares/reject")
async def reject_share(uid: str, body: dict, user: User = Depends(get_current_user),
                       session: AsyncSession = Depends(session_dep)):
    """Отказ принимающей стороны от полученного шеринга.

    Подразделение-получатель удаляет себе доступ к чужой сущности; владелец
    получает событие в журнал («отказ принят»). Требуется HEAD получателя
    (или глобальная роль).
    """
    ent = await _get_entity(session, uid)
    ctx = await build_context(session, user)
    target_dept_id = body.get("department_id") or next(
        iter(sorted(ctx.department_ids)), None)
    if not ctx.is_global and target_dept_id not in ctx.department_ids:
        raise HTTPException(403, "Нельзя отклонить шеринг чужого подразделения")
    # право отказа — только у руководителя принимающего пространства (или админа)
    if not ctx.is_global:
        head = await session.scalar(select(DepartmentMember).where(
            DepartmentMember.department_id == target_dept_id,
            DepartmentMember.user_id == user.id,
            DepartmentMember.role_in_dept == Role.HEAD))
        if head is None:
            raise HTTPException(403, "Отклонять шеринг может руководитель подразделения")
    row = await session.scalar(select(EntityShare).where(
        EntityShare.entity_id == ent.id,
        EntityShare.target_department_id == target_dept_id))
    if row is None:
        raise HTTPException(404, "Входящий шеринг не найден")
    await session.delete(row)
    await session.flush()
    await access.ensure_responsibles(session, ent)
    from app.core.events import log_event
    await log_event(session, "share_rejected", category="entity", user=user,
                    object=f"{ent.kind.value}:{ent.title}"[:200], level="info",
                    message=f"Подразделение #{target_dept_id} отказалось от шеринга "
                            f"«{ent.title}»",
                    details={"entity_uid": ent.uid, "kind": ent.kind.value,
                             "target_department_id": target_dept_id, "rejected": True})
    await session.commit()
    return {"ok": True}


class RespIn(BaseModel):
    user_id: int | None = None


@router.get("/entities/{uid}/responsibles")
async def responsibles(uid: str, user: User = Depends(get_current_user),
                       session: AsyncSession = Depends(session_dep)):
    ent = await _get_entity(session, uid)
    ctx = await build_context(session, user)
    if await resolve_permission(session, ctx, ent) is None:
        raise HTTPException(403)
    rows = (await session.scalars(
        select(EntityResponsible).where(EntityResponsible.entity_id == ent.id))).all()
    out = []
    for r in rows:
        u = await session.get(User, r.user_id) if r.user_id else None
        d = await session.get(Department, r.department_id)
        out.append({"department_id": r.department_id, "department_name": d.name,
                    "department_code": d.code,
                    "user_id": r.user_id, "user_name": u.display_name if u else None,
                    "is_primary": r.is_primary})
    return out


@router.put("/entities/{uid}/responsibles/{dept_id}")
async def set_responsible(uid: str, dept_id: int, body: RespIn,
                          user: User = Depends(get_current_user),
                          session: AsyncSession = Depends(session_dep)):
    """Назначает head своего подразделения (или админ/координатор — любого)."""
    ent = await _get_entity(session, uid)
    ctx = await build_context(session, user)
    if not ctx.is_global and dept_id not in ctx.department_ids:
        raise HTTPException(403, "Можно назначать ответственных только от своих пространств")
    if body.user_id is not None:
        member = await session.scalar(
            select(DepartmentMember).where(DepartmentMember.department_id == dept_id,
                                           DepartmentMember.user_id == body.user_id))
        if not member:
            raise HTTPException(400, "Пользователь не состоит в этом подразделении")
    try:
        await entity_service.assign_responsible(session, user, ent, dept_id, body.user_id)
    except PermissionError as e:
        raise HTTPException(400, str(e))
    await session.commit()
    return {"ok": True}


@router.get("/departments/{dept_id}/users-for-resp")
async def users_for_resp(dept_id: int, user: User = Depends(get_current_user),
                         session: AsyncSession = Depends(session_dep)):
    ctx = await build_context(session, user)
    if not ctx.is_global and dept_id not in ctx.department_ids:
        raise HTTPException(403)
    rows = (await session.scalars(
        select(DepartmentMember).where(DepartmentMember.department_id == dept_id))).all()
    out = []
    for m in rows:
        u = await session.get(User, m.user_id)
        out.append({"id": u.id, "name": u.display_name})
    return out
