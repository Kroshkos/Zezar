"""
Модуль-плагин «Задачи»: REST + HTML-страница.

Контракт с ядром:
  - все мутации идут через entity_service (AuditLog + Outbox -> Telegram/Календарь);
  - права — через access.resolve_permission/can_write (RBAC + шеринг);
  - специфика модуля хранится в TaskPayload (1:1 к Entity).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import access, entity_service
from app.core.security import get_current_user
from app.deps import session_dep
from app.models.core import (
    Department, DepartmentMember, Entity, EntityKind, EntityShare, Permission,
    PriorityLevel, Role, StatusSet, User,
)
from app.models.module_payloads import TaskPayload
from app.ui import render_page

router = APIRouter()

MODULE_KEY = "tasks"


async def _has_shares(session: AsyncSession, ent: Entity) -> bool:
    """Признак межпространственности — ЯВНЫЙ SELECT, а не ORM-коллекция:
    после commit коллекция shares содержит snapshot состояния на момент
    добавления pending-объектов и может быть пустой в новой сессии."""
    return await session.scalar(
        select(EntityShare.id).where(EntityShare.entity_id == ent.id)) is not None


class TaskIn(BaseModel):
    title: str = Field(min_length=3, max_length=500)
    description: str | None = ""
    status_key: str | None = None
    priority_key: str | None = "medium"
    due_date: str | None = None
    responsible_user_id: int | None = None
    department_id: int | None = None          # для админа/координатора: чьё пространство
    project_entity_uid: str | None = None
    group_key: str | None = None
    share_with: list[int] = []               # межпространственный контекст (проект): dept_id[]


def _task_dict(ent: Entity, payload: TaskPayload | None, perm: Permission | None,
               resp_names: list[str], cross_dept: bool = False) -> dict:
    return {
        "uid": ent.uid, "title": ent.title, "description": ent.description or "",
        "status_key": ent.status_key, "priority_key": ent.priority_key,
        "due_date": ent.due_date, "tags": ent.tags or [],
        "department_id": ent.owner_department_id,
        # признак «межпространственная»: задача видна не только подразделению-владельцу
        "cross_dept": cross_dept,
        "project_entity_uid": payload.project_entity_uid if payload else None,
        "group_key": payload.group_key if payload else None,
        "checklist": (payload.checklist if payload else []) or [],
        "responsibles": resp_names,
        "perm": perm.value if perm else None,
        "can_edit": access.can_write(perm),
        "can_share": access.can_manage_sharing(perm),
        "sync_state": ent.sync_state.value,
    }


async def _load_payload(session: AsyncSession, ent_id: int) -> TaskPayload | None:
    return await session.scalar(select(TaskPayload).where(TaskPayload.entity_id == ent_id))


async def _resp_names(session: AsyncSession, ent: Entity) -> list[str]:
    out = []
    for r in ent.responsibles:
        if r.user_id:
            u = await session.get(User, r.user_id)
            if u:
                out.append(u.display_name)
    return out


# ---------------- HTML ----------------

@router.get("")
@router.get("/")
async def tasks_page(request: Request, user: User = Depends(get_current_user),
                     session: AsyncSession = Depends(session_dep)):
    ctx = await access.build_context(session, user)
    ents = (await session.scalars(
        select(Entity).where(Entity.kind == EntityKind.TASK, access.entity_filter(ctx))
        .options(selectinload(Entity.shares), selectinload(Entity.responsibles))
        .order_by(Entity.created_at.desc()))).all()
    items = []
    for ent in ents:
        perm = await access.resolve_permission(session, ctx, ent)
        if perm is None:
            continue
        cross = bool(await session.scalar(
        select(EntityShare.id).where(EntityShare.entity_id == ent.id)))
        # Инвариант изоляции (см. list_tasks): сущность без шеринга видна только
        # подразделению-владельцу; админ/координатор — исключение по ТЗ.
        if not ctx.is_global and not cross \
                and ent.owner_department_id not in ctx.department_ids:
            continue
        items.append(_task_dict(ent, await _load_payload(session, ent.id), perm,
                                await _resp_names(session, ent), cross_dept=cross))
    statuses = (await session.scalars(
        select(StatusSet).where(StatusSet.kind == EntityKind.TASK).order_by(StatusSet.order))).all()
    prios = (await session.scalars(select(PriorityLevel).order_by(PriorityLevel.weight))).all()
    depts: list[dict] = []
    if ctx.is_global:
        depts = [{"id": d.id, "name": d.name} for d in
                 (await session.scalars(
                     select(Department).where(Department.is_active == True))).all()]  # noqa: E712
    members = (await session.scalars(
        select(DepartmentMember).where(
            DepartmentMember.department_id.in_(ctx.department_ids or [-1])))).all()
    people: list[dict] = []
    for m in members:
        u = await session.get(User, m.user_id)
        if u and u.is_active:
            people.append({"id": u.id, "name": u.display_name})
    return await render_page(
        request, "tasks.html", module_key=MODULE_KEY, user=user, session=session,
        page_title="Задачи", page_sub="Задачи, поручения и автозадачи этапов workflow",
        counts={MODULE_KEY: len(items)},
        tasks=items,
        statuses=[{"key": s.key, "label": s.label, "color": s.color} for s in statuses],
        priorities=[{"key": p.key, "label": p.label, "color": p.color} for p in prios],
        departments=depts, people=people,
    )


# ---------------- REST ----------------

@router.get("/api-list")
async def list_tasks(user: User = Depends(get_current_user),
                     session: AsyncSession = Depends(session_dep)):
    ctx = await access.build_context(session, user)
    ents = (await session.scalars(
        select(Entity).where(Entity.kind == EntityKind.TASK, access.entity_filter(ctx))
        .options(selectinload(Entity.shares), selectinload(Entity.responsibles)))).all()
    out = []
    for ent in ents:
        perm = await access.resolve_permission(session, ctx, ent)
        if perm is None:
            continue
        cross = bool(await session.scalar(
        select(EntityShare.id).where(EntityShare.entity_id == ent.id)))
        # Инвариант изоляции рабочих пространств: сущность без шеринга видна ТОЛЬКО
        # подразделению-владельцу. Админ/координатор — исключение (агрегированное
        # представление всех пространств по ТЗ).
        if not ctx.is_global and not cross \
                and ent.owner_department_id not in ctx.department_ids:
            continue
        out.append(_task_dict(ent, await _load_payload(session, ent.id), perm,
                              await _resp_names(session, ent), cross_dept=cross))
    return out


async def _pick_department(session: AsyncSession, user: User, body: TaskIn) -> tuple[int, list[int]]:
    """Возвращает (dept_id для создания, dept_ids пользователя)."""
    dept_ids = (await session.scalars(
        select(DepartmentMember.department_id).where(DepartmentMember.user_id == user.id)
        .order_by(DepartmentMember.id))).all()
    if body.department_id:
        if user.role not in (Role.ADMIN, Role.COORDINATOR):
            raise HTTPException(403, "Выбор чужого пространства доступен только админу/координатору")
        return body.department_id, list(dept_ids)
    # НЕ используем ленивую связь user.memberships (lazy-load в async недопустим) —
    # только явный SELECT; иначе MissingGreenlet / неопределённое первое подразделение
    if not dept_ids:
        raise HTTPException(400, "Вы не состоите ни в одном подразделении — попросите администратора")
    return dept_ids[0], list(dept_ids)


@router.post("")
async def create_task(body: TaskIn, user: User = Depends(get_current_user),
                      session: AsyncSession = Depends(session_dep)):
    dept_id, user_dept_ids = await _pick_department(session, user, body)
    # межпространственный контекст: задача проекта, в котором участвуют несколько
    # подразделений -> расшариваем всем участникам проекта (кроме владельца).
    # Права принимающей стороны: EDIT (могут вести свои подзадачи и назначать своего
    # ответственного — инвариант «несколько ответственных» из ТЗ).
    share_with: list[tuple[int, Permission]] = []
    if body.share_with:
        own = set(user_dept_ids) if user.role not in (Role.ADMIN, Role.COORDINATOR) else None
        for d in dict.fromkeys(body.share_with):
            if d == dept_id:
                continue
            if not await session.get(Department, d):
                raise HTTPException(404, f"Подразделение {d} не найдено")
            if own is not None and d not in own:
                raise HTTPException(403, "Можно расшаривать только своим подразделениям-участникам")
            share_with.append((d, Permission.EDIT))
    ent = await entity_service.create_entity(
        session, user, kind=EntityKind.TASK, module_key=MODULE_KEY,
        owner_department_id=dept_id, title=body.title.strip(),
        description=body.description or None, status_key=body.status_key,
        priority_key=body.priority_key, due_date=body.due_date or None,
        tags=[], responsible_user_id=body.responsible_user_id or user.id,
        share_with=share_with,
    )
    # ЯВНЫЙ SELECT: не читать ORM-коллекцию ent.shares — после add() pending-объектов
    # любое последующее flush-событие (autoflush) может откатить непрочитанную
    # коллекцию; только запрос к БД даёт достоверный факт шеринга.
    cross = bool(await session.scalar(
        select(EntityShare.id).where(EntityShare.entity_id == ent.id)))
    session.add(TaskPayload(entity_id=ent.id, group_key=body.group_key,
                            project_entity_uid=body.project_entity_uid,
                            assignee_names=[], cross_dept=cross))
    await session.commit()
    return {"uid": ent.uid, "ok": True}


@router.patch("/{uid}")
async def patch_task(uid: str, body: dict, user: User = Depends(get_current_user),
                     session: AsyncSession = Depends(session_dep)):
    ent = await session.scalar(select(Entity).where(Entity.uid == uid, Entity.kind == EntityKind.TASK))
    if not ent:
        raise HTTPException(404, "Задача не найдена")
    ctx = await access.build_context(session, user)
    perm = await access.resolve_permission(session, ctx, ent)
    if not access.can_write(perm):
        raise HTTPException(403, "Нет прав на редактирование (доступ только на чтение)")
    changes = {k: v for k, v in body.items()
               if k in {"title", "description", "status_key", "priority_key", "due_date", "tags"}}
    await entity_service.update_entity(session, user, ent, changes)
    if "responsible_user_id" in body:
        try:
            await entity_service.assign_responsible(
                session, user, ent, ent.owner_department_id, body["responsible_user_id"])
        except PermissionError as e:
            raise HTTPException(400, str(e))
    payload = await _load_payload(session, ent.id)
    if payload:
        for f in ("group_key", "project_entity_uid"):
            if f in body:
                setattr(payload, f, body[f])
        if "checklist" in body:
            payload.checklist = body["checklist"]
    await session.commit()
    return {"ok": True}


@router.delete("/{uid}")
async def delete_task(uid: str, user: User = Depends(get_current_user),
                      session: AsyncSession = Depends(session_dep)):
    ent = await session.scalar(select(Entity).where(Entity.uid == uid, Entity.kind == EntityKind.TASK))
    if not ent:
        raise HTTPException(404, "Задача не найдена")
    ctx = await access.build_context(session, user)
    perm = await access.resolve_permission(session, ctx, ent)
    if perm is None:
        raise HTTPException(403)
    if user.role == Role.MEMBER:
        # участник — только свои задачи своего пространства
        mine = any(r.user_id == user.id for r in ent.responsibles)
        if not (mine and ent.owner_department_id in ctx.department_ids):
            raise HTTPException(403, "Участник удаляет только свои задачи")
    await entity_service.delete_entity(session, user, ent)
    await session.commit()
    return {"ok": True}
