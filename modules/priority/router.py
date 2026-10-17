"""Модуль-плагин «Приоритетные показатели»: REST + HTML (prioSet/pc из прототипа).

Специфика (direction/metric/target_value/current_value) — в PriorityPayload;
общие поля и права — через ядро. Календарь: событие на due_date (контрольная дата).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import access, entity_service, refdata
from app.core.security import get_current_user
from app.deps import session_dep
from app.models.core import (
    Department, DepartmentMember, Entity, EntityKind, EntityShare, Permission,
    StatusSet, User,
)
from app.models.module_payloads import PriorityPayload
from app.ui import render_page

router = APIRouter()

MODULE_KEY = "priority"


async def _has_shares(session: AsyncSession, ent: Entity) -> bool:
    """Признак межпространственности — ЯВНЫЙ SELECT (см. tasks/_has_shares)."""
    return await session.scalar(
        select(EntityShare.id).where(EntityShare.entity_id == ent.id)) is not None


class PriorityIn(BaseModel):
    title: str = Field(min_length=2, max_length=500)
    status_key: str | None = None            # plan/track/risk/achieved/missed
    due_date: str | None = None              # контрольная дата -> календарь
    direction: str | None = ""               # направление приоритетного проекта
    metric: str | None = ""                  # наименование показателя
    target_value: str | None = ""            # целевое значение
    current_value: str | None = ""           # текущее значение
    description: str | None = ""
    department_id: int | None = None
    responsible_user_id: int | None = None
    executor_user_ids: list[int] = []        # исполнители (может быть несколько, как в прототипе)
    project_entity_uid: str | None = None    # привязка к проекту (связанность категорий)


async def _load_payload(session: AsyncSession, ent_id: int) -> PriorityPayload | None:
    return await session.scalar(
        select(PriorityPayload).where(PriorityPayload.entity_id == ent_id))


def _executors(ent: Entity) -> list[dict]:
    """Список исполнителей [{id,name}] из денормализованного extra_json."""
    return (ent.extra_json or {}).get("executors") or []


def _prio_dict(ent: Entity, p: PriorityPayload | None, perm: Permission | None,
               resp_names: list[str], cross_dept: bool = False,
               project_title: str | None = None) -> dict:
             execs = _executors(ent)
    return {
        "uid": ent.uid, "title": ent.title, "description": ent.description or "",
        "status_key": ent.status_key, "due_date": ent.due_date,
        "department_id": ent.owner_department_id,
        "cross_dept": cross_dept,
        "direction": p.direction if p else "", "metric": p.metric if p else "",
        "target_value": p.target_value if p else "",
        "current_value": p.current_value if p else "",
        "project_entity_uid": p.project_entity_uid if p else None,
        "project_title": project_title or "",
        "responsibles": resp_names,
        "executors": execs,
        "executor_names": [e["name"] for e in execs] or resp_names,
        "perm": perm.value if perm else None,
        "can_edit": access.can_write(perm),
        "can_share": access.can_manage_sharing(perm),
        "sync_state": ent.sync_state.value,
    }


async def _pick_department(session: AsyncSession, user: User, body: PriorityIn) -> int:
    """Детерминированный выбор пространства-владельца (изоляция workspace)."""
    from app.models.core import DepartmentMember, Role
    mine = (await session.scalars(
        select(DepartmentMember.department_id).where(DepartmentMember.user_id == user.id)
        .order_by(DepartmentMember.id))).all()
    if body.department_id:
        if user.role not in (Role.ADMIN, Role.COORDINATOR):
            raise HTTPException(403, "Выбор чужого пространства доступен только админу/координатору")
        if not await session.get(Department, body.department_id):
            raise HTTPException(404, "Подразделение не найдено")
        return body.department_id
    if not mine:
        raise HTTPException(400, "Вы не состоите ни в одном подразделении")
    return mine[0]


async def _visible_projects(session: AsyncSession, ctx: access.AccessContext) -> list[Entity]:
    """Доступные пользователю проекты (для привязки сущностей модуля)."""
    ents = (await session.scalars(
        select(Entity).where(Entity.kind == EntityKind.PROJECT,
                             access.entity_filter(ctx))
        .options(selectinload(Entity.shares)))).all()
    out = []
    for ent in ents:
        if await access.resolve_permission(session, ctx, ent) is None:
            continue
        cross = await _has_shares(session, ent)
        if not ctx.is_global and not cross \
                and ent.owner_department_id not in ctx.department_ids:
            continue
        out.append(ent)
    return out


async def _project_titles(session: AsyncSession, payloads: list[PriorityPayload | None]) -> dict[str, str]:
    """uid проекта -> заголовок (для карточек/форм; uid хранится в payload)."""
    puids = {p.project_entity_uid for p in payloads if p and p.project_entity_uid}
    if not puids:
        return {}
    rows = (await session.scalars(
        select(Entity).where(Entity.kind == EntityKind.PROJECT,
                             Entity.uid.in_(puids)))).all()
    return {e.uid: e.title for e in rows}


async def _check_project(session: AsyncSession, ctx: access.AccessContext,
                         project_uid: str | None) -> None:
    """Привязка к проекту: сущность проекта должна существовать и быть доступна."""
    if not project_uid:
        return
    parent = await session.scalar(
        select(Entity).where(Entity.uid == project_uid,
                             Entity.kind == EntityKind.PROJECT))
    if not parent:
        raise HTTPException(404, "Проект не найден")
    if not await access.resolve_permission(session, ctx, parent):
        raise HTTPException(403, "Нет доступа к выбранному проекту")


# ---------------- HTML ----------------

@router.get("")
@router.get("/")
async def priority_page(request: Request, user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(session_dep)):
    ctx = await access.build_context(session, user)
    ents = (await session.scalars(
        select(Entity).where(Entity.kind == EntityKind.PRIORITY, access.entity_filter(ctx))
        .options(selectinload(Entity.shares), selectinload(Entity.responsibles))
        .order_by(Entity.due_date.desc()))).all()
    items = []
    for ent in ents:
        perm = await access.resolve_permission(session, ctx, ent)
        if perm is None:
            continue
        cross = await _has_shares(session, ent)
        if not ctx.is_global and not cross \
                and ent.owner_department_id not in ctx.department_ids:
            continue
        resp_names = []
        for r in ent.responsibles:
            if r.user_id:
                u = await session.get(User, r.user_id)
                if u:
                    resp_names.append(u.display_name)
        items.append(_prio_dict(ent, await _load_payload(session, ent.id), perm,
                                resp_names, cross_dept=cross))
    # Справочник — иерархический (ядро app/core/refdata): глобальные шаблоны
    # администратора + собственные/активированные записи подразделения; набор
    # ограничен подключённым модулем данного workspace.
    dept_ctx = await refdata.context_dept(session, user, None)
    mods = await refdata.enabled_modules(session, dept_ctx)
    statuses = await refdata.effective_statuses(
        session, EntityKind.PRIORITY, dept_ctx, mods)
    depts: list[dict] = []
    if ctx.is_global:
        depts = [{"id": d.id, "name": d.name} for d in
                 (await session.scalars(
                     select(Department).where(Department.is_active == True))).all()]  # noqa: E712
    return await render_page(
        request, "priority.html", module_key=MODULE_KEY, user=user, session=session,
        page_title="Приоритетные показатели",
        page_sub="Целевые и текущие значения показателей; контрольные даты — в Яндекс.Календаре",
        counts={MODULE_KEY: len(items)},
        indicators=items,
        statuses=[{"key": s.key, "label": s.label, "color": s.color} for s in statuses],
        departments=depts,
    )


# ---------------- REST ----------------

@router.get("/api-list")
async def list_priority(user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(session_dep)):
    ctx = await access.build_context(session, user)
    ents = (await session.scalars(
        select(Entity).where(Entity.kind == EntityKind.PRIORITY, access.entity_filter(ctx))
        .options(selectinload(Entity.shares), selectinload(Entity.responsibles)))).all()
    out = []
    for ent in ents:
        perm = await access.resolve_permission(session, ctx, ent)
        if perm is None:
            continue
        cross = await _has_shares(session, ent)
        if not ctx.is_global and not cross \
                and ent.owner_department_id not in ctx.department_ids:
            continue
        resp_names = []
        for r in ent.responsibles:
            if r.user_id:
                u = await session.get(User, r.user_id)
                if u:
                    resp_names.append(u.display_name)
        out.append(_prio_dict(ent, await _load_payload(session, ent.id), perm,
                              resp_names, cross_dept=cross))
    return out


@router.post("")
async def create_priority(body: PriorityIn, user: User = Depends(get_current_user),
                          session: AsyncSession = Depends(session_dep)):
    dept_id = await _pick_department(session, user, body)
    ent = await entity_service.create_entity(
        session, user, kind=EntityKind.PRIORITY, module_key=MODULE_KEY,
        owner_department_id=dept_id, title=body.title.strip(),
        description=body.description or None, status_key=body.status_key,
        due_date=body.due_date or None,
        responsible_user_id=body.responsible_user_id or user.id,
    )
    session.add(PriorityPayload(
        entity_id=ent.id, direction=body.direction or None, metric=body.metric or None,
        target_value=body.target_value or None, current_value=body.current_value or None))
    await session.flush()
    await session.commit()
    return {"uid": ent.uid, "ok": True}


_PRIO_FIELDS = {"direction", "metric", "target_value", "current_value"}


@router.patch("/{uid}")
async def patch_priority(uid: str, body: dict, user: User = Depends(get_current_user),
                         session: AsyncSession = Depends(session_dep)):
    ent = await session.scalar(
        select(Entity).where(Entity.uid == uid, Entity.kind == EntityKind.PRIORITY))
    if not ent:
        raise HTTPException(404, "Показатель не найден")
    ctx = await access.build_context(session, user)
    perm = await access.resolve_permission(session, ctx, ent)
    if not access.can_write(perm):
        raise HTTPException(403, "Нет прав на редактирование (доступ только на чтение)")
    core_changes = {k: v for k, v in body.items()
                    if k in {"title", "description", "status_key", "due_date"}}
    await entity_service.update_entity(session, user, ent, core_changes)
    payload = await _load_payload(session, ent.id)
    if payload:
        for f in _PRIO_FIELDS:
            if f in body:
                setattr(payload, f, body[f])
    await session.commit()
    return {"ok": True}


@router.delete("/{uid}")
async def delete_priority(uid: str, user: User = Depends(get_current_user),
                          session: AsyncSession = Depends(session_dep)):
    ent = await session.scalar(
        select(Entity).where(Entity.uid == uid, Entity.kind == EntityKind.PRIORITY))
    if not ent:
        raise HTTPException(404, "Показатель не найден")
    ctx = await access.build_context(session, user)
    perm = await access.resolve_permission(session, ctx, ent)
    if perm is None:
        raise HTTPException(403)
    from app.models.core import Role
    if user.role == Role.MEMBER:
        mine = any(r.user_id == user.id for r in ent.responsibles)
        if not (mine and ent.owner_department_id in ctx.department_ids):
            raise HTTPException(403, "Участник удаляет только свои показатели")
    await entity_service.delete_entity(session, user, ent)
    await session.commit()
    return {"ok": True}
