"""Модуль-плагин «Проектная работа»: REST + HTML (карточки проектов как в прототипе).

Специфика (stages/customer/budget/links) — в ProjectPayload; общие поля и права — ядро.
Проект по ТЗ — межпространственная сущность: при создании можно указать участников
(share_with), тогда у каждого подразделения появляется свой слот ответственного
(инвариант «несколько ответственных»). Календарь: контрольные точки этапов (вехи).
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core import access, entity_service
from app.core.security import get_current_user
from app.deps import session_dep
from app.models.core import (
    Department, DepartmentMember, Entity, EntityKind, EntityShare, Permission,
    PriorityLevel, Role, StatusSet, User,
)
from app.models.module_payloads import ProjectPayload
from app.ui import render_page

router = APIRouter()

MODULE_KEY = "projects"


async def _has_shares(session: AsyncSession, ent: Entity) -> bool:
    """Признак межпространственности — ЯВНЫЙ SELECT (см. tasks/_has_shares)."""
    return await session.scalar(
        select(EntityShare.id).where(EntityShare.entity_id == ent.id)) is not None


class ProjectIn(BaseModel):
    title: str = Field(min_length=3, max_length=500)
    description: str | None = ""
    status_key: str | None = None            # init/planning/exec/monitor/done/canceled
    priority_key: str | None = "medium"
    due_date: str | None = None              # срок завершения проекта (календарная веха)
    customer: str | None = ""                # заказчик
    budget: str | None = ""                  # бюджет / источник финансирования
    stages_json: str | None = None           # JSON [{name, branches:[...]}] — как wfTemplates демо
    links_json: str | None = None            # JSON [{label, url}]
    department_id: int | None = None         # для админа/координатора: чьё пространство
    responsible_user_id: int | None = None
    share_with: list[int] = []               # подразделения-участники проекта (EDIT)


async def _load_payload(session: AsyncSession, ent_id: int) -> ProjectPayload | None:
    return await session.scalar(select(ProjectPayload).where(ProjectPayload.entity_id == ent_id))


def _proj_dict(ent: Entity, p: ProjectPayload | None, perm: Permission | None,
               resp_names: list[str], cross_dept: bool = False) -> dict:
    stages = (p.stages if p else []) or []
    done = sum(1 for s in stages if s.get("done"))
    return {
        "uid": ent.uid, "title": ent.title, "description": ent.description or "",
        "status_key": ent.status_key, "priority_key": ent.priority_key,
        "due_date": ent.due_date, "department_id": ent.owner_department_id,
        "cross_dept": cross_dept,
        "customer": p.customer if p else "", "budget": p.budget if p else "",
        "stages": stages, "stage_done": done, "stage_total": len(stages),
        "progress": round(done / len(stages) * 100) if stages else 0,
        "links": (p.links if p else []) or [],
        "responsibles": resp_names,
        "perm": perm.value if perm else None,
        "can_edit": access.can_write(perm),
        "can_share": access.can_manage_sharing(perm),
        "sync_state": ent.sync_state.value,
    }


async def _pick_department(session: AsyncSession, user: User, body: ProjectIn) -> int:
    """Детерминированный выбор пространства-владельца (изоляция workspace)."""
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


def _parse_json_field(raw: str | None, default: list) -> list:
    if not raw:
        return default
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else default
    except (ValueError, TypeError):
        raise HTTPException(422, "Некорректный JSON в поле stages/links")


async def _visible_entities(session: AsyncSession, ctx: access.AccessContext,
                            kind: EntityKind):
    ents = (await session.scalars(
        select(Entity).where(Entity.kind == kind, access.entity_filter(ctx))
        .options(selectinload(Entity.shares), selectinload(Entity.responsibles)))).all()
    out = []
    for ent in ents:
        perm = await access.resolve_permission(session, ctx, ent)
        if perm is None:
            continue
        # Инвариант изоляции пространств: без шеринга видна только владельцу.
        cross = bool(await session.scalar(
        select(EntityShare.id).where(EntityShare.entity_id == ent.id)))
        if not ctx.is_global and not cross \
                and ent.owner_department_id not in ctx.department_ids:
            continue
        out.append((ent, perm, cross))
    return out


async def _resp_names(session: AsyncSession, ent: Entity) -> list[str]:
    names = []
    for r in ent.responsibles:
        if r.user_id:
            u = await session.get(User, r.user_id)
            if u:
                names.append(u.display_name)
    return names


# ---------------- HTML ----------------

@router.get("")
@router.get("/")
async def projects_page(request: Request, user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(session_dep)):
    ctx = await access.build_context(session, user)
    pairs = await _visible_entities(session, ctx, EntityKind.PROJECT)
    items = [_proj_dict(e, await _load_payload(session, e.id), p,
                        await _resp_names(session, e), cross_dept=c)
             for e, p, c in sorted(pairs, key=lambda x: (x[0].due_date or "9999"))]
    statuses = (await session.scalars(
        select(StatusSet).where(StatusSet.kind == EntityKind.PROJECT)
        .order_by(StatusSet.order))).all()
    prios = (await session.scalars(select(PriorityLevel).order_by(PriorityLevel.weight))).all()
    depts: list[dict] = []
    if ctx.is_global:
        depts = [{"id": d.id, "name": d.name} for d in
                 (await session.scalars(
                     select(Department).where(Department.is_active == True))).all()]  # noqa: E712
    else:
        # непривилегированный пользователь выбирает только из своих подразделений
        rows = (await session.scalars(
            select(Department).where(Department.id.in_(ctx.department_ids or [-1])))).all()
        depts = [{"id": d.id, "name": d.name} for d in rows]
    return await render_page(
        request, "projects.html", module_key=MODULE_KEY, user=user, session=session,
        page_title="Проектная работа",
        page_sub="Межпространственные проекты: этапы, вехи, ответственные от каждого подразделения",
        counts={MODULE_KEY: len(items)},
        projects=items,
        statuses=[{"key": s.key, "label": s.label, "color": s.color} for s in statuses],
        priorities=[{"key": p.key, "label": p.label, "color": p.color} for p in prios],
        departments=depts,
    )


# ---------------- REST ----------------

@router.get("/api-list")
async def list_projects(user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(session_dep)):
    ctx = await access.build_context(session, user)
    out = []
    for ent, perm, cross in await _visible_entities(session, ctx, EntityKind.PROJECT):
        out.append(_proj_dict(ent, await _load_payload(session, ent.id), perm,
                              await _resp_names(session, ent), cross_dept=cross))
    return out


@router.post("")
async def create_project(body: ProjectIn, user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(session_dep)):
    dept_id = await _pick_department(session, user, body)
    # Межпространственный контекст проекта: участники получают EDIT и свой слот
    # ответственного (инвариант ТЗ «несколько ответственных»).
    mine = set((await session.scalars(
        select(DepartmentMember.department_id).where(
            DepartmentMember.user_id == user.id))).all())
    share_with: list[tuple[int, Permission]] = []
    for d in dict.fromkeys(body.share_with):
        if d == dept_id:
            continue
        if not await session.get(Department, d):
            raise HTTPException(404, f"Подразделение {d} не найдено")
        if user.role not in (Role.ADMIN, Role.COORDINATOR) and d not in mine:
            raise HTTPException(403, "Можно привлекать только свои подразделения-участники")
        share_with.append((d, Permission.EDIT))
    ent = await entity_service.create_entity(
        session, user, kind=EntityKind.PROJECT, module_key=MODULE_KEY,
        owner_department_id=dept_id, title=body.title.strip(),
        description=body.description or None, status_key=body.status_key,
        priority_key=body.priority_key, due_date=body.due_date or None,
        responsible_user_id=body.responsible_user_id or user.id,
        share_with=share_with,
    )
    session.add(ProjectPayload(
        entity_id=ent.id, stages=_parse_json_field(body.stages_json, []),
        customer=body.customer or None, budget=body.budget or None,
        links=_parse_json_field(body.links_json, [])))
    await session.commit()
    return {"uid": ent.uid, "ok": True}


_PROJ_CORE = {"title", "description", "status_key", "priority_key", "due_date"}
_PROJ_PAYLOAD = {"customer", "budget", "stages", "links"}


@router.patch("/{uid}")
async def patch_project(uid: str, body: dict, user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(session_dep)):
    ent = await session.scalar(
        select(Entity).where(Entity.uid == uid, Entity.kind == EntityKind.PROJECT))
    if not ent:
        raise HTTPException(404, "Проект не найден")
    ctx = await access.build_context(session, user)
    perm = await access.resolve_permission(session, ctx, ent)
    if not access.can_write(perm):
        raise HTTPException(403, "Нет прав на редактирование (доступ только на чтение)")
    unknown = set(body) - _PROJ_CORE - _PROJ_PAYLOAD
    if unknown:
        raise HTTPException(422, f"Неизвестные поля: {sorted(unknown)}")
    await entity_service.update_entity(
        session, user, ent, {k: v for k, v in body.items() if k in _PROJ_CORE})
    payload = await _load_payload(session, ent.id)
    if payload:
        for f in _PROJ_PAYLOAD & set(body):
            setattr(payload, f, body[f])
    await session.commit()
    return {"ok": True}


@router.delete("/{uid}")
async def delete_project(uid: str, user: User = Depends(get_current_user),
                         session: AsyncSession = Depends(session_dep)):
    ent = await session.scalar(
        select(Entity).where(Entity.uid == uid, Entity.kind == EntityKind.PROJECT))
    if not ent:
        raise HTTPException(404, "Проект не найден")
    ctx = await access.build_context(session, user)
    perm = await access.resolve_permission(session, ctx, ent)
    if perm is None:
        raise HTTPException(403)
    if user.role == Role.MEMBER:
        mine = any(r.user_id == user.id for r in ent.responsibles)
        if not (mine and ent.owner_department_id in ctx.department_ids):
            raise HTTPException(403, "Участник удаляет только свои проекты")
    await entity_service.delete_entity(session, user, ent)
    await session.commit()
    return {"ok": True}
