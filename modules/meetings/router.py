"""Модуль-плагин «Совещания»: REST + HTML (карточки как renderMeetings() из прототипа).

Специфика (num/chair/secretary/attendees/time/agenda/decisions) — в MeetingPayload;
общие поля (title/status/priority/due_date) и права — через ядро (entity_service/access).
Календарь: событие на дату+время совещания (см. integrations/yandex_calendar._window).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import access, entity_service
from app.core.security import get_current_user
from app.deps import session_dep
from app.models.core import (
    Department, Entity, EntityKind, Permission, StatusSet, User,
)
from app.models.module_payloads import MeetingPayload
from app.ui import render_page

router = APIRouter()

MODULE_KEY = "meetings"


class MeetingIn(BaseModel):
    title: str = Field(min_length=2, max_length=500)
    status_key: str | None = None            # planned/held/postponed/canceled (seed MEETING_STATUSES)
    due_date: str | None = None              # ISO-дата совещания (поле ядра -> календарь)
    num: str | None = ""                     # номер протокола
    chair: str | None = ""                   # председатель
    secretary: str | None = ""               # секретарь
    attendees: str | None = ""               # участники (текст, как в демо)
    time_: str | None = Field(default="", alias="time")   # 'HH:MM'
    location: str | None = ""                # место проведения
    description: str | None = ""
    department_id: int | None = None         # для админа/координатора: чьё пространство
    responsible_user_id: int | None = None

    model_config = {"populate_by_name": True}


async def _load_payload(session: AsyncSession, ent_id: int) -> MeetingPayload | None:
    return await session.scalar(select(MeetingPayload).where(MeetingPayload.entity_id == ent_id))


def _meet_dict(ent: Entity, p: MeetingPayload | None, perm: Permission | None,
               resp_names: list[str]) -> dict:
    return {
        "uid": ent.uid, "title": ent.title, "description": ent.description or "",
        "status_key": ent.status_key, "due_date": ent.due_date,
        "department_id": ent.owner_department_id,
        "cross_dept": bool(ent.shares),
        "num": p.num if p else "", "chair": p.chair if p else "",
        "secretary": p.secretary if p else "", "attendees": p.attendees if p else "",
        "time": (p.time_ if p else "") or "", "location": p.location if p else "",
        "agenda": (p.agenda if p else []) or [],
        "decisions": (p.decisions if p else []) or [],
        "linked_task_uids": (p.linked_task_uids if p else []) or [],
        "responsibles": resp_names,
        "perm": perm.value if perm else None,
        "can_edit": access.can_write(perm),
        "can_share": access.can_manage_sharing(perm),
        "sync_state": ent.sync_state.value,
    }


async def _pick_department(session: AsyncSession, user: User, body: MeetingIn) -> int:
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


# ---------------- HTML ----------------

@router.get("")
@router.get("/")
async def meetings_page(request: Request, user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(session_dep)):
    ctx = await access.build_context(session, user)
    ents = (await session.scalars(
        select(Entity).where(Entity.kind == EntityKind.MEETING, access.entity_filter(ctx))
        .order_by(Entity.due_date.desc()))).all()
    items = []
    for ent in ents:
        perm = await access.resolve_permission(session, ctx, ent)
        if perm is None:
            continue
        resp_names = []
        for r in ent.responsibles:
            if r.user_id:
                u = await session.get(User, r.user_id)
                if u:
                    resp_names.append(u.display_name)
        items.append(_meet_dict(ent, await _load_payload(session, ent.id), perm, resp_names))
    statuses = (await session.scalars(
        select(StatusSet).where(StatusSet.kind == EntityKind.MEETING)
        .order_by(StatusSet.order))).all()
    depts: list[dict] = []
    if ctx.is_global:
        depts = [{"id": d.id, "name": d.name} for d in
                 (await session.scalars(
                     select(Department).where(Department.is_active == True))).all()]  # noqa: E712
    return await render_page(
        request, "meetings.html", module_key=MODULE_KEY, user=user, session=session,
        page_title="Совещания",
        page_sub="Повестка, решения и протоколы; события синхронизируются с Яндекс.Календарём",
        counts={MODULE_KEY: len(items)},
        meetings=items,
        statuses=[{"key": s.key, "label": s.label, "color": s.color} for s in statuses],
        departments=depts,
    )


# ---------------- REST ----------------

@router.get("/api-list")
async def list_meetings(user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(session_dep)):
    ctx = await access.build_context(session, user)
    ents = (await session.scalars(
        select(Entity).where(Entity.kind == EntityKind.MEETING, access.entity_filter(ctx)))).all()
    out = []
    for ent in ents:
        perm = await access.resolve_permission(session, ctx, ent)
        if perm is None:
            continue
        if not ctx.is_global and not ent.shares \
                and ent.owner_department_id not in ctx.department_ids:
            continue
        resp_names = []
        for r in ent.responsibles:
            if r.user_id:
                u = await session.get(User, r.user_id)
                if u:
                    resp_names.append(u.display_name)
        out.append(_meet_dict(ent, await _load_payload(session, ent.id), perm, resp_names))
    return out


@router.post("")
async def create_meeting(body: MeetingIn, user: User = Depends(get_current_user),
                         session: AsyncSession = Depends(session_dep)):
    dept_id = await _pick_department(session, user, body)
    ent = await entity_service.create_entity(
        session, user, kind=EntityKind.MEETING, module_key=MODULE_KEY,
        owner_department_id=dept_id, title=body.title.strip(),
        description=body.description or None, status_key=body.status_key,
        due_date=body.due_date or None,
        responsible_user_id=body.responsible_user_id or user.id,
    )
    start_at = f"{body.due_date}T{body.time_}" if body.due_date and body.time_ else None
    session.add(MeetingPayload(
        entity_id=ent.id, num=body.num or None, chair=body.chair or None,
        secretary=body.secretary or None, attendees=body.attendees or None,
        time_=body.time_ or None, location=body.location or None,
        start_at=start_at, agenda=[], decisions=[], linked_task_uids=[]))
    await session.commit()
    return {"uid": ent.uid, "ok": True}


_MEET_FIELDS = {"num", "chair", "secretary", "attendees", "location", "agenda",
                "decisions", "linked_task_uids"}


@router.patch("/{uid}")
async def patch_meeting(uid: str, body: dict, user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(session_dep)):
    ent = await session.scalar(
        select(Entity).where(Entity.uid == uid, Entity.kind == EntityKind.MEETING))
    if not ent:
        raise HTTPException(404, "Совещание не найдено")
    ctx = await access.build_context(session, user)
    perm = await access.resolve_permission(session, ctx, ent)
    if not access.can_write(perm):
        raise HTTPException(403, "Нет прав на редактирование (доступ только на чтение)")
    core_changes = {k: v for k, v in body.items()
                    if k in {"title", "description", "status_key", "due_date"}}
    if "time" in body:
        body["time_"] = body.pop("time")
    await entity_service.update_entity(session, user, ent, core_changes)
    payload = await _load_payload(session, ent.id)
    if payload:
        for f in _MEET_FIELDS:
            if f in body:
                setattr(payload, f, body[f])
        if "time_" in body or "due_date" in body:
            d = body.get("due_date", ent.due_date)
            t = body.get("time_", payload.time_)
            payload.start_at = f"{d}T{t}" if d and t else None
    await session.commit()
    return {"ok": True}


@router.delete("/{uid}")
async def delete_meeting(uid: str, user: User = Depends(get_current_user),
                         session: AsyncSession = Depends(session_dep)):
    ent = await session.scalar(
        select(Entity).where(Entity.uid == uid, Entity.kind == EntityKind.MEETING))
    if not ent:
        raise HTTPException(404, "Совещание не найдено")
    ctx = await access.build_context(session, user)
    perm = await access.resolve_permission(session, ctx, ent)
    if perm is None:
        raise HTTPException(403)
    from app.models.core import Role
    if user.role == Role.MEMBER:
        mine = any(r.user_id == user.id for r in ent.responsibles)
        if not (mine and ent.owner_department_id in ctx.department_ids):
            raise HTTPException(403, "Участник удаляет только свои совещания")
    await entity_service.delete_entity(session, user, ent)
    await session.commit()
    return {"ok": True}
