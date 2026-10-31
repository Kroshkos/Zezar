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

from app.core import access, entity_service, refdata
from app.core.security import get_current_user
from app.deps import session_dep
from app.models.core import (
    Department, DepartmentMember, Entity, EntityKind, EntityShare, Permission,
    Role, StatusSet, User,
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
    executor_user_ids: list[int] = []         # исполнители (может быть несколько)
    department_id: int | None = None          # для админа/координатора: чьё пространство
    project_entity_uid: str | None = None
    group_key: str | None = None
    parent_task_uid: str | None = None        # родительская задача (иерархия задач)
    share_with: list[int] = []               # межпространственный контекст (проект): dept_id[]


async def _parent_titles(session: AsyncSession, payloads: list[TaskPayload]) -> dict[str, str]:
    """uid -> заголовок для родительских задач (для отображения в списках)."""
    puids = {p.parent_task_uid for p in payloads if getattr(p, "parent_task_uid", None)}
    if not puids:
        return {}
    rows = (await session.execute(
        select(Entity.uid, Entity.title).where(Entity.uid.in_(puids)))).all()
    return {u: t for u, t in rows}


async def _check_parent(session, ctx, uid: str, self_uid: str | None) -> None:
    """Родительская задача существует и доступна; иерархия ациклична.

    self_uid — uid редактируемой задачи (при создании — None): защита от
    «задача — родитель сама себе» и от циклов произвольной длины
    (по образцу _cycle_guard в модуле «Проекты»).
    """
    parent = await session.scalar(
        select(Entity).where(Entity.uid == uid, Entity.kind == EntityKind.TASK))
    if not parent:
        raise HTTPException(404, "Родительская задача не найдена")
    if not await access.resolve_permission(session, ctx, parent):
        raise HTTPException(403, "Нет доступа к родительской задаче")
    cur: str | None = uid
    seen = {self_uid} if self_uid else set()
    while cur:
        if cur in seen:
            raise HTTPException(
                422, "Задача не может быть подзадачей самой себя (или своей подзадачи)")
        seen.add(cur)
        p = await session.scalar(select(TaskPayload).join(
            Entity, Entity.id == TaskPayload.entity_id).where(Entity.uid == cur))
        cur = p.parent_task_uid if p else None


def _executors(ent: Entity) -> list[dict]:
    """Список исполнителей [{id,name}] из денормализованного extra_json."""
    return (ent.extra_json or {}).get("executors") or []


def _task_dict(ent: Entity, payload: TaskPayload | None, perm: Permission | None,
               resp_names: list[str], cross_dept: bool = False,
               parent_title: str | None = None,
               subtasks: list[dict] | None = None) -> dict:
    execs = _executors(ent)
    return {
        "uid": ent.uid, "title": ent.title, "description": ent.description or "",
        "status_key": ent.status_key, "priority_key": ent.priority_key,
        "due_date": ent.due_date, "tags": ent.tags or [],
        "department_id": ent.owner_department_id,
        # признак «межпространственная»: задача видна не только подразделению-владельцу
        "cross_dept": cross_dept,
        "project_entity_uid": payload.project_entity_uid if payload else None,
        "group_key": payload.group_key if payload else None,
        # иерархия задач: родитель (uid/заголовок) и подзадачи — для формы,
        # таблицы и печати карточек (см. static/js/tasks.js printTaskCards)
        "parent_task_uid": payload.parent_task_uid if payload else None,
        "parent_task_title": parent_title or "",
        "subtasks": subtasks or [],
        "checklist": (payload.checklist if payload else []) or [],
        "responsibles": resp_names,
        "executors": execs,
        "executor_names": [e["name"] for e in execs] or resp_names,
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


async def _decorate(session: AsyncSession, items: list[dict]) -> list[dict]:
    """Обогатить список задач данными иерархии (см. TaskIn.parent_task_uid):

    - parent_task_title — заголовок родительской задачи;
    - subtasks — [{uid,title,due_date,status_key,status_label}] прямых подзадач
      (в порядке срока). В подзадачи попадают ВСЕ потомки из БД (в т.ч. те, что
      не видны текущему пользователю) — сами они отдельными строками списка не
      идут, но должны печататься в содержании родительской карточки.
    """
    payloads = (await session.scalars(select(TaskPayload))).all()
    tp = {p.entity_id: p for p in payloads}
    cents = {e.id: e for e in (await session.scalars(
        select(Entity).where(Entity.kind == EntityKind.TASK))).all()}
    # статусы для подписей подзадач (печать карточек: наименование/дедлайн/статус)
    labels: dict[str, str] = {}
    for s in (await session.scalars(select(StatusSet))).all():
        k = s.kind.value if hasattr(s.kind, "value") else str(s.kind)
        if k == "task":
            labels[f"{s.department_id}:{s.key}"] = s.label
            labels.setdefault(f"*:{s.key}", s.label)
    ptitles = await _parent_titles(session, list(tp.values()))
    subs_by_parent: dict[str, list[dict]] = {}
    for p in payloads:
        pu = getattr(p, "parent_task_uid", None)
        cent = cents.get(p.entity_id)
        if not pu or cent is None:
            continue
        skey = cent.status_key or ""
        subs_by_parent.setdefault(pu, []).append({
            "uid": cent.uid, "title": cent.title,
            "due_date": cent.due_date or "",
            "status_key": skey,
            "status_label": (labels.get(f"{cent.owner_department_id}:{skey}")
                             or labels.get(f"*:{skey}") or skey),
        })
    for v in subs_by_parent.values():
        v.sort(key=lambda x: (x["due_date"] or "9999", x["title"]))
    for it in items:
        it["parent_task_title"] = ptitles.get(it.get("parent_task_uid") or "", "")
        it["subtasks"] = subs_by_parent.get(it["uid"], [])
    return items


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
    statuses = await refdata.effective_statuses(
        session, EntityKind.TASK,
        await refdata.context_dept(session, user, None),
        await refdata.enabled_modules(session, await refdata.context_dept(session, user, None)))
    _pdept = await refdata.context_dept(session, user, None)
    prios = await refdata.effective_priorities(
        session, _pdept, await refdata.enabled_modules(session, _pdept))
    depts: list[dict] = []
    if ctx.is_global:
        depts = [{"id": d.id, "name": d.name} for d in
                 (await session.scalars(
                     select(Department).where(Department.is_active == True))).all()]  # noqa: E712
    else:
        rows = (await session.scalars(
            select(Department).where(Department.id.in_(ctx.department_ids or [-1])))).all()
        depts = [{"id": d.id, "name": d.name} for d in rows]
    members = (await session.scalars(
        select(DepartmentMember).where(
            DepartmentMember.department_id.in_(
                ctx.department_ids or [d["id"] for d in depts] or [-1])))).all()
    people: list[dict] = []
    for m in members:
        u = await session.get(User, m.user_id)
        if u and u.is_active and not any(x["id"] == u.id for x in people):
            people.append({"id": u.id, "name": u.display_name})
    return await render_page(
        request, "tasks.html", module_key=MODULE_KEY, user=user, session=session,
        page_title="Задачи", page_sub="Задачи, поручения и автозадачи этапов workflow",
        counts={MODULE_KEY: len(items)},
        tasks=items,
        statuses=[{"key": s.key, "label": s.label, "color": s.color} for s in statuses],
        priorities=[{"key": p.key, "label": p.label, "color": p.color} for p in prios],
        departments=depts, people=people, me=user.id,
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
    if body.department_id is not None:
        if body.department_id <= 0:
            raise HTTPException(422, f"Некорректный department_id: {body.department_id}")
        # Руководитель/участник вправе явно указать СВОЁ пространство
        # (фронтенд всегда шлёт department_id из селекта со своими подразделениями);
        # чужое пространство доступно только админу/координатору.
        if user.role not in (Role.ADMIN, Role.COORDINATOR)                 and body.department_id not in dept_ids:
            raise HTTPException(403, "Выбор чужого пространства доступен только админу/координатору")
        # проверка существования ДО записи: несуществующий dept_id иначе давал
        # sqlite3.IntegrityError: FOREIGN KEY constraint failed на flush()
        from app.models.core import Department
        if not await session.get(Department, body.department_id):
            raise HTTPException(422, f"Подразделение не найдено: id={body.department_id}")
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
    try:
        ent = await entity_service.create_entity(
            session, user, kind=EntityKind.TASK, module_key=MODULE_KEY,
            owner_department_id=dept_id, title=body.title.strip(),
            description=body.description or None, status_key=body.status_key,
            priority_key=body.priority_key, due_date=body.due_date or None,
            tags=[], responsible_user_id=body.responsible_user_id or user.id,
            executor_user_ids=body.executor_user_ids,
            # межпространственный контекст: в сервис передаётся ПОЛНЫЙ список
            # участников; сервис сам исключит «шеринг самому себе» (owner) и
            # откроет слоты ответственных по одному от каждого подразделения
            share_with=[(d, Permission.EDIT) for d in dict.fromkeys(body.share_with)]
            if body.share_with else None,
        )
    except ValueError as e:
        raise HTTPException(422, str(e))
    # ЯВНЫЙ SELECT: не читать ORM-коллекцию ent.shares — после add() pending-объектов
    # любое последующее flush-событие (autoflush) может откатить непрочитанную
    # коллекцию; только запрос к БД даёт достоверный факт шеринга.
    await session.flush()  # гарантируем, что EntityShare уже физически в транзакции
    cross = bool(await session.scalar(
        select(EntityShare.id).where(EntityShare.entity_id == ent.id)))
    session.add(TaskPayload(entity_id=ent.id, group_key=body.group_key,
                            project_entity_uid=body.project_entity_uid,
                            assignee_names=[], cross_dept=cross))
    await session.flush()  # payload с cross_dept записан до commit — перечитаем для верификации
    saved = await session.get(TaskPayload, ent.id)
    if saved is not None and saved.cross_dept != cross:
        saved.cross_dept = cross
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
    if "executor_user_ids" in body:
        try:
            await entity_service.set_executors(
                session, user, ent, [int(x) for x in (body["executor_user_ids"] or [])])
        except ValueError as e:
            raise HTTPException(422, str(e))
    if "responsible_user_id" in body:
        try:
            await entity_service.assign_responsible(
                session, user, ent, ent.owner_department_id, body["responsible_user_id"])
        except PermissionError as e:
            raise HTTPException(400, str(e))
    # Привязка к проекту: проверяем существование и доступность проекта (как в docs/grants/meetings)
    if body.get("project_entity_uid"):
        parent = await session.scalar(
            select(Entity).where(Entity.uid == body["project_entity_uid"],
                                 Entity.kind == EntityKind.PROJECT))
        if not parent:
            raise HTTPException(404, "Проект не найден")
        if not await access.resolve_permission(session, ctx, parent):
            raise HTTPException(403, "Нет доступа к выбранному проекту")
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
