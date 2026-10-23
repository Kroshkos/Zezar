"""Модуль-плагин «Совещания»: REST + HTML (карточки как renderMeetings() из прототипа).

Специфика (num/chair/secretary/attendees/time/agenda/decisions/cards) — в MeetingPayload;
общие поля (title/status/priority/due_date) и права — через ядро (entity_service/access).
Календарь: событие на дату+время совещания (см. integrations/yandex_calendar._window).

Раздел «Карточки» (cards в payload) — по образу и подобию формы совещания из
прототипа ИС_Цезарь.html: карточки задач, заведённых на совещании
(new_tasks -> mPushNewTasksToKanban), с кнопкой переноса в модуль «Задачи».
"""
from __future__ import annotations

import re

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
from app.models.module_payloads import MeetingPayload
from app.ui import render_page

router = APIRouter()

MODULE_KEY = "meetings"


async def _has_shares(session: AsyncSession, ent: Entity) -> bool:
    """Признак межпространственности — ЯВНЫЙ SELECT (см. tasks/_has_shares)."""
    return await session.scalar(
        select(EntityShare.id).where(EntityShare.entity_id == ent.id)) is not None


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
    executor_user_ids: list[int] = []        # исполнители/ответственные (несколько, как в прототипе)
    project_entity_uid: str | None = None    # привязка к проекту (связанность категорий)

    model_config = {"populate_by_name": True}


async def _load_payload(session: AsyncSession, ent_id: int) -> MeetingPayload | None:
    return await session.scalar(select(MeetingPayload).where(MeetingPayload.entity_id == ent_id))


def _executors(ent: Entity) -> list[dict]:
    """Список исполнителей [{id,name}] из денормализованного extra_json."""
    return (ent.extra_json or {}).get("executors") or []


def _meet_dict(ent: Entity, p: MeetingPayload | None, perm: Permission | None,
               resp_names: list[str], cross_dept: bool = False,
               project_title: str | None = None) -> dict:
    execs = _executors(ent)
    return {
        "uid": ent.uid, "title": ent.title, "description": ent.description or "",
        "status_key": ent.status_key, "due_date": ent.due_date,
        "department_id": ent.owner_department_id,
        "cross_dept": cross_dept,
        "num": p.num if p else "", "chair": p.chair if p else "",
        "secretary": p.secretary if p else "", "attendees": p.attendees if p else "",
        "time": (p.time_ if p else "") or "", "location": p.location if p else "",
        "agenda": (p.agenda if p else []) or [],
        "decisions": (p.decisions if p else []) or [],
        # Раздел «Карточки»: карточки задач, заведённых на совещании
        # (new_tasks из прототипа ИС_Цезарь.html).
        "cards": (p.cards if p else []) or [],
        "linked_task_uids": (p.linked_task_uids if p else []) or [],
        "project_entity_uid": p.project_entity_uid if p else None,
        "project_title": project_title or "",
        "executors": execs,
        "executor_names": [e["name"] for e in execs] or resp_names,
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
    if body.department_id is not None:
        if body.department_id <= 0:
            raise HTTPException(422, f"Некорректный department_id: {body.department_id}")
        # Руководитель/участник вправе явно указать СВОЁ пространство
        # (фронтенд всегда шлёт department_id из селекта со своими подразделениями);
        # чужое пространство доступно только админу/координатору.
        if user.role not in (Role.ADMIN, Role.COORDINATOR) \
                and body.department_id not in mine:
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


async def _project_titles(session: AsyncSession, payloads: list[MeetingPayload | None]) -> dict[str, str]:
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
async def meetings_page(request: Request, user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(session_dep)):
    ctx = await access.build_context(session, user)
    ents = (await session.scalars(
        select(Entity).where(Entity.kind == EntityKind.MEETING, access.entity_filter(ctx))
        .options(selectinload(Entity.shares), selectinload(Entity.responsibles))
        .order_by(Entity.due_date.desc()))).all()
    payloads: list[MeetingPayload | None] = []
    rows: list[tuple] = []
    for ent in ents:
        perm = await access.resolve_permission(session, ctx, ent)
        if perm is None:
            continue
        # Инвариант изоляции пространств (см. list_meetings)
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
        pl = await _load_payload(session, ent.id)
        payloads.append(pl)
        rows.append((ent, pl, perm, resp_names, cross))
    ptitles = await _project_titles(session, payloads)
    items = [_meet_dict(e, pl, p, rn, cross_dept=c,
                        project_title=ptitles.get((pl.project_entity_uid if pl else "") or ""))
             for e, pl, p, rn, c in rows]
    # Справочник — иерархический (ядро app/core/refdata): глобальные шаблоны
    # администратора + собственные/активированные записи подразделения; набор
    # ограничен подключённым модулем данного workspace.
    dept_ctx = await refdata.context_dept(session, user, None)
    mods = await refdata.enabled_modules(session, dept_ctx)
    statuses = await refdata.effective_statuses(
        session, EntityKind.MEETING, dept_ctx, mods)
    depts: list[dict] = []
    if ctx.is_global:
        depts = [{"id": d.id, "name": d.name} for d in
                 (await session.scalars(
                     select(Department).where(Department.is_active == True))).all()]  # noqa: E712
    else:
        drows = (await session.scalars(
            select(Department).where(Department.id.in_(ctx.department_ids or [-1])))).all()
        depts = [{"id": d.id, "name": d.name} for d in drows]
    # сотрудники для назначения ответственных/исполнителей (несколько) — как people в прототипе
    members = (await session.scalars(
        select(DepartmentMember).where(
            DepartmentMember.department_id.in_(ctx.department_ids or [-1])))).all()
    people: list[dict] = []
    for m in members:
        u = await session.get(User, m.user_id)
        if u and u.is_active and not any(x["id"] == u.id for x in people):
            people.append({"id": u.id, "name": u.display_name})
    # проекты для привязки совещания (связанность категорий в рамках модулей)
    projects = [{"uid": e.uid, "title": e.title}
                for e in await _visible_projects(session, ctx)]
    return await render_page(
        request, "meetings.html", module_key=MODULE_KEY, user=user, session=session,
        page_title="Совещания",
        page_sub="Повестка, решения и протоколы; события синхронизируются с Яндекс.Календарём",
        counts={MODULE_KEY: len(items)},
        meetings=items,
        statuses=[{"key": s.key, "label": s.label, "color": s.color} for s in statuses],
        departments=depts, people=people, projects=projects,
    )


# ---------------- REST ----------------

@router.get("/cards/api-list")
async def cards_api_list(user: User = Depends(get_current_user),
                         session: AsyncSession = Depends(session_dep)):
    """Раздел «Карточки»: печатные карточки для всех сущностей.

    Тип entity (чек-лист этапов) — для проектов/ПО/документов и прочих сущностей
    с workflow; тип task (место под рукописные заметки) — для автономных задач.
    Подразделения — только свои сущности, администраторы/координаторы — все.
    """
    from modules.meetings.cards_api import cards_api as _cards_api
    return await _cards_api(user, session)


@router.get("/api-list")
async def list_meetings(user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(session_dep)):
    ctx = await access.build_context(session, user)
    ents = (await session.scalars(
        select(Entity).where(Entity.kind == EntityKind.MEETING, access.entity_filter(ctx))
        .options(selectinload(Entity.shares), selectinload(Entity.responsibles)))).all()
    rows: list[tuple] = []
    payloads: list[MeetingPayload | None] = []
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
        pl = await _load_payload(session, ent.id)
        payloads.append(pl)
        rows.append((ent, pl, perm, resp_names, cross))
    ptitles = await _project_titles(session, payloads)
    return [_meet_dict(e, pl, p, rn, cross_dept=c,
                       project_title=ptitles.get((pl.project_entity_uid if pl else "") or ""))
            for e, pl, p, rn, c in rows]


@router.post("")
async def create_meeting(body: MeetingIn, user: User = Depends(get_current_user),
                         session: AsyncSession = Depends(session_dep)):
    dept_id = await _pick_department(session, user, body)
    ctx = await access.build_context(session, user)
    await _check_project(session, ctx, body.project_entity_uid)
    ent = await entity_service.create_entity(
        session, user, kind=EntityKind.MEETING, module_key=MODULE_KEY,
        owner_department_id=dept_id, title=body.title.strip(),
        description=body.description or None, status_key=body.status_key,
        due_date=body.due_date or None,
        responsible_user_id=body.responsible_user_id or user.id,
        executor_user_ids=body.executor_user_ids or None,
    )
    # нормализуем 'HH:MM' -> 'HH:MM:00', иначе fmt_iso в ICS-хуке не распознает значение
    t = (body.time_ or "").strip()
    if re.fullmatch(r"\d{2}:\d{2}", t):
        t += ":00"
    start_at = f"{body.due_date}T{t}" if body.due_date and t else None
    session.add(MeetingPayload(
        entity_id=ent.id, num=body.num or None, chair=body.chair or None,
        secretary=body.secretary or None, attendees=body.attendees or None,
        time_=body.time_ or None, location=body.location or None,
        start_at=start_at, project_entity_uid=body.project_entity_uid or None,
        agenda=[], decisions=[], linked_task_uids=[], cards=[]))
    await session.commit()
    return {"uid": ent.uid, "ok": True}


_MEET_FIELDS = {"num", "chair", "secretary", "attendees", "location", "agenda",
                "decisions", "linked_task_uids", "project_entity_uid", "cards"}


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
    if "project_entity_uid" in body and body["project_entity_uid"]:
        await _check_project(session, ctx, body["project_entity_uid"])
    await entity_service.update_entity(session, user, ent, core_changes)
    if "executor_user_ids" in body:
        try:
            await entity_service.set_executors(
                session, user, ent, body["executor_user_ids"] or [])
        except ValueError as e:
            raise HTTPException(400, str(e))
    payload = await _load_payload(session, ent.id)
    if payload:
        for f in _MEET_FIELDS:
            if f in body:
                setattr(payload, f, body[f] or None if isinstance(body[f], str) else body[f])
        if "time_" in body or "due_date" in body:
            d = body.get("due_date", ent.due_date)
            t = (body.get("time_", payload.time_) or "").strip()
            if re.fullmatch(r"\d{2}:\d{2}", t):
                t += ":00"
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
