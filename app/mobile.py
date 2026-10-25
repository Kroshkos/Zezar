"""Мобильный API (Zezar-app): REST-эндпоинты для приложения на React Native.

Все эндпоинты — под Bearer-аутентификацией API-токенов (app/routers/token_auth.py),
права — через штатный RBAC-механизм ядра (access.build_context / resolve_permission).

  GET  /api/mobile/bootstrap          профиль + подразделения + модули + справочники
  GET  /api/mobile/entities           карточки (filter: module_key, kind, dept_id, q)
  GET  /api/mobile/entities/{uid}     одна карточка по uid
  POST /api/mobile/tasks              создать задачу
  PATCH /api/mobile/entities/{uid}    изменить карточку (статус/приоритет/сроки/описание)
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import access, entity_service, refdata
from app.core.config import get_settings
from app.deps import session_dep
from app.models.core import (
    Department, DepartmentMember, Entity, EntityKind, EntityShare,
    Role, User, Workspace, WorkspaceModule,
)
from app.routers.token_auth import get_current_user_from_token

router = APIRouter(
    prefix="/api/mobile",
    tags=["mobile"],
    dependencies=[Depends(get_current_user_from_token)],
)


# ======================= Вспомогательные =======================

def _entity_brief(ent: Entity, perm, resp_names: list[str]) -> dict:
    return {
        "uid": ent.uid,
        "kind": str(getattr(ent.kind, "value", ent.kind)),
        "module_key": ent.module_key,
        "title": ent.title,
        "description": ent.description or "",
        "status_key": ent.status_key,
        "priority_key": ent.priority_key,
        "due_date": ent.due_date,
        "tags": ent.tags or [],
        "department_id": ent.owner_department_id,
        "responsibles": resp_names,
        "executors": (ent.extra_json or {}).get("executors") or [],
        "perm": perm.value if perm else None,
        "can_edit": access.can_write(perm),
        "created_at": ent.created_at.isoformat() if ent.created_at else None,
        "updated_at": ent.updated_at.isoformat() if ent.updated_at else None,
    }


async def _resp_names(session: AsyncSession, ent: Entity) -> list[str]:
    out: list[str] = []
    for r in ent.responsibles or []:
        if getattr(r, "user_id", None):
            u = await session.get(User, r.user_id)
            if u:
                out.append(u.display_name)
    return out


# ======================= Bootstrap =======================

@router.get("/bootstrap")
async def bootstrap(user: User = Depends(get_current_user_from_token),
                    session: AsyncSession = Depends(session_dep)):
    """Один запрос при старте приложения: кто я, где работаю, какие модули
    включены, какие статусы/приоритеты доступны, какой сервер."""
    settings = get_settings()
    ctx = await access.build_context(session, user)

    # Подразделения пользователя (глобальные роли — все активные)
    dsel = select(Department).where(Department.is_active == True)  # noqa: E712
    if not ctx.is_global:
        dsel = dsel.where(Department.id.in_(ctx.department_ids or [-1]))
    depts = (await session.scalars(dsel.order_by(Department.name))).all()
    departments = [{"id": d.id, "name": d.name, "code": d.code, "color": d.color}
                   for d in depts]

    # Включённые модули по всем пространствам пользователя
    ws_rows = (await session.scalars(
        select(Workspace).where(Workspace.department_id.in_(
            [d["id"] for d in departments] or [-1])))).all()
    enabled_keys: set[str] = set()
    if ws_rows:
        mods = (await session.scalars(
            select(WorkspaceModule).where(
                WorkspaceModule.workspace_id.in_([w.id for w in ws_rows]),
                WorkspaceModule.is_enabled == True))).all()  # noqa: E712
        enabled_keys = {m.module_key for m in mods}
    from app.core.module_registry import registry
    modules = [{"key": s.key, "title": s.title, "icon": s.icon}
               for s in sorted(registry.all(), key=lambda x: x.order)
               if s.is_core or s.key in enabled_keys or ctx.is_global]

    # Справочники: первый контекстный dept (или None для глобальных ролей)
    dept_for_ref = await refdata.context_dept(session, user, None)
    enabled_mods = await refdata.enabled_modules(session, dept_for_ref)
    statuses = {}
    for kind in EntityKind:
        rows = await refdata.effective_statuses(session, kind, dept_for_ref, enabled_mods)
        if rows:
            statuses[kind.value] = [{"key": s.key, "label": s.label, "color": s.color}
                                    for s in rows]
    prios = await refdata.effective_priorities(session, dept_for_ref, enabled_mods)

    return {
        "server": {"name": "ЦЕЗАРЬ (Zezar)", "version": getattr(settings, "version", "1.0")},
        "me": {
            "id": user.id, "name": user.display_name, "login": user.login,
            "role": user.role.value, "initials": user.initials, "color": user.color,
            "must_change_password": bool(user.must_change_password),
        },
        "departments": departments,
        "modules": modules,
        "statuses": statuses,
        "priorities": [{"key": p.key, "label": p.label, "color": p.color} for p in prios],
    }


# ======================= Список карточек =======================

@router.get("/entities")
async def list_entities(
    module_key: str | None = None,
    kind: str | None = None,
    department_id: int | None = None,
    q: str | None = None,
    limit: int = 200,
    user: User = Depends(get_current_user_from_token),
    session: AsyncSession = Depends(session_dep),
):
    """Карточки всех модулей, видимые пользователю (RBAC + шеринг как в web)."""
    ctx = await access.build_context(session, user)
    conds = [access.entity_filter(ctx)]
    if module_key:
        conds.append(Entity.module_key == module_key)
    if kind:
        valid_kinds = {k.value for k in EntityKind}
        if kind not in valid_kinds:
            raise HTTPException(422, f"Неизвестный kind: {kind}")
        conds.append(Entity.kind == EntityKind(kind))
    if department_id:
        conds.append(Entity.owner_department_id == department_id)
    if q and q.strip():
        like = f"%{q.strip().lower()}%"
        conds.append(or_(func.lower(Entity.title).like(like),
                         func.lower(Entity.description).like(like)))

    ents = (await session.scalars(
        select(Entity).where(*conds)
        .order_by(Entity.updated_at.desc()).limit(min(max(limit, 1), 500)))).all()

    out = []
    for ent in ents:
        perm = await access.resolve_permission(session, ctx, ent)
        if perm is None:
            continue
        cross = await session.scalar(
            select(EntityShare.id).where(EntityShare.entity_id == ent.id))
        if not ctx.is_global and not cross \
                and ent.owner_department_id not in ctx.department_ids:
            continue  # инвариант изоляции пространств (как в modules/*/router.py)
        out.append(_entity_brief(ent, perm, await _resp_names(session, ent)))
    return out


@router.get("/entities/{uid}")
async def get_entity(uid: str,
                     user: User = Depends(get_current_user_from_token),
                     session: AsyncSession = Depends(session_dep)):
    """Одна карточка по uid (для экрана деталей — без скачивания всего списка)."""
    ent = await session.scalar(select(Entity).where(Entity.uid == uid))
    if not ent:
        raise HTTPException(404, "Сущность не найдена")
    ctx = await access.build_context(session, user)
    perm = await access.resolve_permission(session, ctx, ent)
    if perm is None:
        raise HTTPException(404, "Сущность не найдена")  # без утечки факта существования
    return _entity_brief(ent, perm, await _resp_names(session, ent))


# ======================= Создание задачи =======================

class MobileTaskIn(BaseModel):
    title: str = Field(min_length=3, max_length=500)
    description: str | None = ""
    priority_key: str | None = "medium"
    due_date: str | None = None
    responsible_user_id: int | None = None
    executor_user_ids: list[int] = []
    department_id: int | None = None


@router.post("/tasks")
async def create_task(body: MobileTaskIn,
                      user: User = Depends(get_current_user_from_token),
                      session: AsyncSession = Depends(session_dep)):
    """Создать задачу (тот же путь, что и веб-модуль: entity_service + аудит + outbox)."""
    dept_ids = (await session.scalars(
        select(DepartmentMember.department_id)
        .where(DepartmentMember.user_id == user.id)
        .order_by(DepartmentMember.id))).all()
    dept_id = body.department_id
    if dept_id is not None:
        if user.role not in (Role.ADMIN, Role.COORDINATOR) and dept_id not in dept_ids:
            raise HTTPException(403, "Выбор чужого пространства доступен только админу/координатору")
        if not await session.get(Department, dept_id):
            raise HTTPException(422, f"Подразделение не найдено: id={dept_id}")
    else:
        if not dept_ids:
            raise HTTPException(400, "Вы не состоите ни в одном подразделении")
        dept_id = dept_ids[0]

    try:
        ent = await entity_service.create_entity(
            session, user, kind=EntityKind.TASK, module_key="tasks",
            owner_department_id=dept_id, title=body.title.strip(),
            description=body.description or None, status_key=None,
            priority_key=body.priority_key, due_date=body.due_date or None,
            tags=[], responsible_user_id=body.responsible_user_id or user.id,
            executor_user_ids=body.executor_user_ids,
        )
    except ValueError as e:
        raise HTTPException(422, str(e))
    from app.models.module_payloads import TaskPayload
    session.add(TaskPayload(entity_id=ent.id, assignee_names=[]))
    await session.commit()
    return {"uid": ent.uid, "ok": True}


# ======================= Изменение карточки =======================

class MobileEntityPatch(BaseModel):
    title: str | None = None
    description: str | None = None
    status_key: str | None = None
    priority_key: str | None = None
    due_date: str | None = None
    tags: list[str] | None = None
    responsible_user_id: int | None = None


@router.patch("/entities/{uid}")
async def patch_entity(uid: str, body: MobileEntityPatch,
                       user: User = Depends(get_current_user_from_token),
                       session: AsyncSession = Depends(session_dep)):
    """Частичное обновление: статус, приоритет, срок, описание, ответственный."""
    ent = await session.scalar(select(Entity).where(Entity.uid == uid))
    if not ent:
        raise HTTPException(404, "Сущность не найдена")
    ctx = await access.build_context(session, user)
    perm = await access.resolve_permission(session, ctx, ent)
    if not access.can_write(perm):
        raise HTTPException(403, "Нет прав на редактирование (доступ только на чтение)")

    changes = {k: v for k, v in body.model_dump(exclude_none=True).items()
               if k in {"title", "description", "status_key", "priority_key", "due_date", "tags"}}
    if changes:
        await entity_service.update_entity(session, user, ent, changes)
    if body.responsible_user_id is not None:
        try:
            await entity_service.assign_responsible(
                session, user, ent, ent.owner_department_id, body.responsible_user_id)
        except PermissionError as e:
            raise HTTPException(400, str(e))
    await session.commit()
    await session.refresh(ent)
    return _entity_brief(ent, perm, await _resp_names(session, ent))
