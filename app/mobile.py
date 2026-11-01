"""Мобильный API (Zezar-app): REST-эндпоинты для приложения на React Native.

Все эндпоинты — под Bearer-аутентификацией API-токенов (app/routers/token_auth.py),
права — через штатный RBAC-механизм ядра (access.build_context / resolve_permission).

  GET   /api/mobile/bootstrap          профиль + подразделения + модули + справочники
  GET   /api/mobile/entities           карточки (filter: module_key, kind, dept_id, q)
  GET   /api/mobile/entities/{uid}     одна карточка по uid
  POST  /api/mobile/tasks              создать задачу
  PATCH /api/mobile/entities/{uid}     изменить карточку (статус/приоритет/сроки/описание)
  POST  /api/mobile/push/register      зарегистрировать Expo Push Token устройства
  POST  /api/mobile/push/unregister    удалить подписку устройства
  GET   /api/mobile/events             лента событий (?since_id=N&wait=сек — long-poll)
  POST  /api/mobile/events/read        отметить события прочитанными
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import access, entity_service, refdata
from app.core.config import get_settings
from app.db.base import utcnow
from app.deps import session_dep
from app.models.core import (
    Department, DepartmentMember, Entity, EntityKind, EntityShare,
    Role, User, Workspace, WorkspaceModule,
)
from app.models.mobile import MobileEvent, PushSubscription
from app.routers.token_auth import get_current_user_from_token

router = APIRouter(
    prefix="/api/mobile",
    tags=["mobile"],
    dependencies=[Depends(get_current_user_from_token)],
)


# ======================= Вспомогательные =======================

def _entity_brief(ent: Entity, perm, resp_names: list[str], *,
                  parent_task_uid: str | None = None,
                  subtasks: list[dict] | None = None) -> dict:
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
        # Иерархия задач (печать): подзадача не имеет собственной карточки —
        # клиент обязан рисовать её строкой «наименование · дедлайн · статус»
        # в составе родителя (изоморфно /api/tasks/cards/api-list).
        "parent_task_uid": parent_task_uid,
        "is_subtask": bool(parent_task_uid),
        "subtasks": subtasks or [],
        "created_at": ent.created_at.isoformat() if ent.created_at else None,
        "updated_at": ent.updated_at.isoformat() if ent.updated_at else None,
    }


async def _task_hierarchy(session: AsyncSession, ents: list[Entity]) -> tuple[
        dict[int, str], dict[str, list[dict]]]:
    """(entity_id -> parent_uid, parent_uid -> [строки подзадач]) для списка задач.

    Строки подзадач — тот же формат, что _subtask_row в modules/tasks/router.py:
    наименование · дедлайн · статус выполнения.
    """
    from app.models.core import StatusSet
    from app.models.module_payloads import TaskPayload

    ids = [e.id for e in ents] or [-1]
    payloads = (await session.scalars(
        select(TaskPayload).where(TaskPayload.entity_id.in_(ids)))).all()
    parent_by_eid = {p.entity_id: p.parent_task_uid for p in payloads
                     if getattr(p, "parent_task_uid", None)}

    all_ents = {e.id: e for e in (await session.scalars(
        select(Entity).where(Entity.kind == EntityKind.TASK))).all()}
    labels: dict[tuple, str] = {}
    for s in (await session.scalars(
            select(StatusSet).where(StatusSet.is_active == True))).all():  # noqa: E712
        k = s.kind.value if hasattr(s.kind, "value") else str(s.kind)
        if k == "task":
            labels[(s.department_id, s.key)] = s.label

    subs_by_parent: dict[str, list[dict]] = {}
    for p in (await session.scalars(select(TaskPayload))).all():
        pu = getattr(p, "parent_task_uid", None)
        cent = all_ents.get(p.entity_id)
        if not pu or cent is None:
            continue
        skey = cent.status_key or ""
        subs_by_parent.setdefault(pu, []).append({
            "uid": cent.uid, "title": cent.title,
            "due_date": cent.due_date or "",
            "status_key": skey,
            "status_label": labels.get((cent.owner_department_id, skey))
                            or labels.get((None, skey)) or skey or "—",
        })
    for v in subs_by_parent.values():
        v.sort(key=lambda x: (x["due_date"] or "9999", x["title"]))
    return parent_by_eid, subs_by_parent


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
    modules = [{"key": s.key, "title": s.title, "icon": s.icon,
                # v1.6: клиент показывает «+» только там, где есть создаваемый вид
                "entity_kind": (s.entity_kinds[0].lower() if s.entity_kinds else None)}
               for s in sorted(registry.all(), key=lambda x: x.order)
               if s.is_core or s.key in enabled_keys or ctx.is_global]
    module_titles = {s.key: s.title for s in registry.all()}

    # Справочники: первый контекстный dept (или None для глобальных ролей)
    module_statuses = {}
    dept_for_ref = await refdata.context_dept(session, user, None)
    enabled_mods = await refdata.enabled_modules(session, dept_for_ref)
    statuses = {}
    for kind in EntityKind:
        rows = await refdata.effective_statuses(session, kind, dept_for_ref, enabled_mods)
        if rows:
            entry = [{"key": s.key, "label": s.label, "color": s.color} for s in rows]
            statuses[kind.value] = entry
            # v1.6: тот же справочник под ключом модуля — клиент берёт статусы
            # для карточек конкретного модуля (module_statuses[module_key])
            mkeys = [s.key for s in registry.all() if kind.name in s.entity_kinds]
            for mkey in mkeys:
                module_statuses[mkey] = entry
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
        "module_statuses": module_statuses,
        "module_titles": module_titles,
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
    parents, subs_by_parent = await _task_hierarchy(session, list(ents)) \
        if any(e.kind == EntityKind.TASK for e in ents) else ({}, {})
    for ent in ents:
        perm = await access.resolve_permission(session, ctx, ent)
        if perm is None:
            continue
        cross = await session.scalar(
            select(EntityShare.id).where(EntityShare.entity_id == ent.id))
        if not ctx.is_global and not cross \
                and ent.owner_department_id not in ctx.department_ids:
            continue  # инвариант изоляции пространств (как в modules/*/router.py)
        out.append(_entity_brief(ent, perm, await _resp_names(session, ent),
                                 parent_task_uid=parents.get(ent.id),
                                 subtasks=subs_by_parent.get(ent.uid, [])))
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
    parents: dict[int, str] = {}
    subs_by_parent: dict[str, list[dict]] = {}
    if ent.kind == EntityKind.TASK:
        parents, subs_by_parent = await _task_hierarchy(session, [ent])
    return _entity_brief(ent, perm, await _resp_names(session, ent),
                         parent_task_uid=parents.get(ent.id),
                         subtasks=subs_by_parent.get(ent.uid, []))


# ======================= Создание задачи =======================

class MobileTaskIn(BaseModel):
    title: str = Field(min_length=3, max_length=500)
    description: str | None = ""
    priority_key: str | None = "medium"
    due_date: str | None = None
    responsible_user_id: int | None = None
    executor_user_ids: list[int] = []
    department_id: int | None = None
    # Иерархия задач: родительская задача (uid). Подзадача НЕ имеет собственной
    # печатной карточки — печатается в составе родителя (наименование · дедлайн ·
    # статус), см. _printable_task_cards в modules/tasks/router.py.
    parent_task_uid: str | None = None


class MobileEntityIn(BaseModel):
    """Универсальное создание карточки для мобильного клиента (v1.6).
    module_key определяет вид сущности по реестру модулей; для 'tasks'
    сохраняется прежняя логика (payload TaskPayload)."""
    module_key: str = "tasks"
    title: str = Field(min_length=3, max_length=500)
    description: str | None = ""
    priority_key: str | None = "medium"
    due_date: str | None = None
    responsible_user_id: int | None = None
    executor_user_ids: list[int] = []
    department_id: int | None = None
    parent_task_uid: str | None = None


def _kind_for_module(module_key: str) -> EntityKind:
    from app.core.module_registry import registry
    spec = registry.get(module_key)
    if spec is None or not spec.entity_kinds:
        raise HTTPException(422, f"Модуль не поддерживает создание карточек: {module_key}")
    return EntityKind[spec.entity_kinds[0]]


# payload-таблицы модулей: без записи в них веб-страница модуля считает карточку
# «несуществующей» — при создании из приложения пишем пустой payload каждого вида.
_PAYLOAD_BY_KIND = {
    "task":        ("TaskPayload",   {"assignee_names": []}),
    "doc":         ("DocPayload",    {}),
    "soft":        ("SoftPayload",   {}),
    "project":     ("ProjectPayload", {}),
    "grant":       ("GrantPayload",  {}),
    "scholarship": ("ScholarPayload", {}),
    "priority":    ("PriorityPayload", {}),
    "meeting":     ("MeetingPayload", {}),
}


async def _ensure_payload(session, kind: EntityKind, ent_id: int,
                          parent_task_uid: str | None = None) -> None:
    entry = _PAYLOAD_BY_KIND.get(kind.value)
    if not entry:
        return
    from app.models import module_payloads as mp
    cls = getattr(mp, entry[0])
    fields = dict(entry[1])
    # иерархия задач: родитель пишется в TaskPayload — без него веб-печать
    # не сочтёт задачу подзадачей и напечатает ей отдельную карточку
    if kind == EntityKind.TASK and parent_task_uid:
        fields["parent_task_uid"] = parent_task_uid
    session.add(cls(entity_id=ent_id, **fields))


async def _check_parent_task(session, user, parent_uid: str, self_uid: str | None) -> None:
    """Родительская задача существует, видна и иерархия ациклична.

    Та же логика, что _check_parent в modules/tasks/router.py (веб-контур):
    мобильное создание/правка с parent_task_uid обязаны проходить те же
    проверки, иначе «битая» ссылка ломает печать карточек.
    """
    from modules.tasks.router import _check_parent
    ctx = await access.build_context(session, user)
    await _check_parent(session, ctx, parent_uid, self_uid=self_uid)


@router.post("/entities")
async def create_entity_ep(body: MobileEntityIn,
                           user: User = Depends(get_current_user_from_token),
                           session: AsyncSession = Depends(session_dep)):
    """Создать карточку любого модуля (единый путь: entity_service + аудит + outbox)."""
    kind = _kind_for_module(body.module_key)
    ent = await _create_common(session, user, kind=kind, module_key=body.module_key, body=body)
    parent_uid = (getattr(body, "parent_task_uid", None) or "").strip() or None
    if parent_uid:
        if parent_uid == ent.uid:
            raise HTTPException(422, "Задача не может быть подзадачей самой себя")
        await _check_parent_task(session, user, parent_uid, self_uid=None)
    await _ensure_payload(session, kind, ent.id, parent_task_uid=parent_uid)
    await session.commit()
    return {"uid": ent.uid, "ok": True}


async def _default_status_key(session, user, kind: EntityKind, dept_id: int) -> str | None:
    """Первый статус справочника вида для пространства (как дефолт в веб-формах)."""
    try:
        rows = await refdata.effective_statuses(session, kind, dept_id, None)
        return rows[0].key if rows else None
    except Exception:
        return None


@router.post("/tasks")
async def create_task(body: MobileTaskIn,
                      user: User = Depends(get_current_user_from_token),
                      session: AsyncSession = Depends(session_dep)):
    """Создать задачу (обратная совместимость: тоже /entities c module_key=tasks)."""
    ent = await _create_common(session, user, kind=EntityKind.TASK, module_key="tasks", body=body)
    parent_uid = (body.parent_task_uid or "").strip() or None
    if parent_uid:
        if parent_uid == ent.uid:
            raise HTTPException(422, "Задача не может быть подзадачей самой себя")
        await _check_parent_task(session, user, parent_uid, self_uid=None)
    await _ensure_payload(session, EntityKind.TASK, ent.id, parent_task_uid=parent_uid)
    await session.commit()
    return {"uid": ent.uid, "ok": True}


async def _create_common(session, user, *, kind, module_key, body):
    """Общая часть создания: пространство-владелец + entity_service."""
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
            session, user, kind=kind, module_key=module_key,
            owner_department_id=dept_id, title=body.title.strip(),
            description=body.description or None,
            status_key=await _default_status_key(session, user, kind, dept_id),
            priority_key=body.priority_key, due_date=body.due_date or None,
            tags=[], responsible_user_id=body.responsible_user_id or user.id,
            executor_user_ids=body.executor_user_ids,
        )
    except ValueError as e:
        raise HTTPException(422, str(e))
    return ent


# ======================= Изменение карточки =======================

class MobileEntityPatch(BaseModel):
    title: str | None = None
    description: str | None = None
    status_key: str | None = None
    priority_key: str | None = None
    due_date: str | None = None
    tags: list[str] | None = None
    responsible_user_id: int | None = None
    # Иерархия задач (только для задач): uid новой родительской задачи или ""/null
    # — сброс родителя (задача снова печатается отдельной карточкой).
    parent_task_uid: str | None = None


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
    # Иерархия задач: ключ присутствует в запросе — меняем (в т.ч. сбрасываем
    # на null). Без этой ветки мобильное приложение создавало/перепривязывало
    # подзадачи «в никуда»: payload не трогался, и задача продолжала печататься
    # отдельной карточкой вместо состава родителя.
    if body.parent_task_uid is not None and ent.kind == EntityKind.TASK:
        from app.models.module_payloads import TaskPayload
        parent_uid = (body.parent_task_uid or "").strip() or None
        if parent_uid == uid:
            raise HTTPException(422, "Задача не может быть подзадачей самой себя")
        if parent_uid:
            await _check_parent_task(session, user, parent_uid, self_uid=uid)
        payload = await session.scalar(
            select(TaskPayload).where(TaskPayload.entity_id == ent.id))
        if payload is None:  # legacy-карточка без payload — создаём
            payload = TaskPayload(entity_id=ent.id, assignee_names=[])
            session.add(payload)
        payload.parent_task_uid = parent_uid
    await session.commit()
    await session.refresh(ent)
    parents: dict[int, str] = {}
    subs_by_parent: dict[str, list[dict]] = {}
    if ent.kind == EntityKind.TASK:
        parents, subs_by_parent = await _task_hierarchy(session, [ent])
    return _entity_brief(ent, perm, await _resp_names(session, ent),
                         parent_task_uid=parents.get(ent.id),
                         subtasks=subs_by_parent.get(ent.uid, []))


# ======================= Push-подписки =======================

class PushRegisterIn(BaseModel):
    expo_token: str = Field(min_length=10, max_length=120)
    platform: str = Field(default="ios", pattern="^(ios|android|web)$")
    device_label: str | None = None


@router.post("/push/register")
async def push_register(body: PushRegisterIn,
                        user: User = Depends(get_current_user_from_token),
                        session: AsyncSession = Depends(session_dep)):
    """Зарегистрировать Expo Push Token устройства пользователя (идемпотентно)."""
    sub = await session.scalar(
        select(PushSubscription).where(PushSubscription.expo_token == body.expo_token))
    if sub is None:
        session.add(PushSubscription(
            user_id=user.id, expo_token=body.expo_token,
            platform=body.platform, device_label=body.device_label,
            is_active=True, last_seen_at=utcnow()))
    else:
        # токен мог «переехать» на другого пользователя (сброс приложения) —
        # перепривязываем и реактивируем
        sub.user_id, sub.platform = user.id, body.platform
        sub.is_active, sub.fail_count = True, 0
        sub.last_seen_at = utcnow()
    await session.commit()
    return {"ok": True}


class PushUnregisterIn(BaseModel):
    expo_token: str = Field(min_length=10, max_length=120)


@router.post("/push/unregister")
async def push_unregister(body: PushUnregisterIn,
                          user: User = Depends(get_current_user_from_token),
                          session: AsyncSession = Depends(session_dep)):
    """Деактивировать подписку устройства (при выходе из приложения)."""
    sub = await session.scalar(
        select(PushSubscription).where(
            PushSubscription.expo_token == body.expo_token,
            PushSubscription.user_id == user.id))
    if sub:
        sub.is_active = False
        await session.commit()
    return {"ok": True}


# ======================= Лента событий (long-poll) =======================

def _iso(dt) -> Optional[str]:
    """Безопасная сериализация datetime для клиента.

    На legacy-БД (Amvera) колонки создавались ALTER TABLE с NULL-дефолтами, а
    SQLite хранит даты строками без tzinfo — прямой .isoformat() давал клиенту
    время без часового пояса (лента «События» считала «N дн назад» неверно).
    Naive-значения трактуем как MSK: app.db.base.utcnow() возвращает московское
    время (см. app/core/timezone.py), поэтому метке принудительно проставляется
    смещение +03:00, а не UTC — иначе клиент на хосте в другой стране видел бы
    события «сдвинутыми» на разницу часовых поясов."""
    if dt is None:
        return None
    try:
        if isinstance(dt, str):
            return dt  # уже сериализовано — отдаём как есть
        from app.core.timezone import MSK_TZ
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=MSK_TZ)
        return dt.isoformat()
    except Exception:  # noqa: BLE001 — лучше null, чем 500 на весь список
        return None


def _event_brief(ev: MobileEvent) -> dict:
    return {
        "id": ev.id, "type": ev.type, "title": ev.title, "body": ev.body or "",
        "entity_uid": ev.entity_uid, "module_key": ev.module_key,
        "payload": ev.payload_json or {},
        "created_at": _iso(ev.created_at),
        "read_at": _iso(ev.read_at),
    }


@router.get("/events")
async def list_events(
    since_id: int = 0,
    wait: int = 0,           # сек. long-poll (0..25) — ждём новые события
    limit: int = 50,
    user: User = Depends(get_current_user_from_token),
    session: AsyncSession = Depends(session_dep),
):
    """События пользователя c id > since_id; опционально — долгое голосование."""
    wait = max(0, min(wait, 25))
    deadline = asyncio.get_event_loop().time() + wait

    async def _query():
        return (await session.scalars(
            select(MobileEvent)
            .where(MobileEvent.user_id == user.id, MobileEvent.id > since_id)
            .order_by(MobileEvent.id.desc()).limit(min(max(limit, 1), 200)))).all()

    events = await _query()
    while not events and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(1.5)
        # long-poll: сессия держит snapshot — начинаем новую транзакцию
        await session.rollback()
        events = await _query()

    unread = await session.scalar(
        select(func.count(MobileEvent.id)).where(
            MobileEvent.user_id == user.id, MobileEvent.read_at.is_(None)))
    return {"events": [_event_brief(e) for e in events],
            "unread": int(unread or 0),
            "last_id": max([e.id for e in events], default=since_id)}


class EventsReadIn(BaseModel):
    ids: list[int] = []
    all: bool = False


@router.post("/events/read")
async def mark_events_read(body: EventsReadIn,
                           user: User = Depends(get_current_user_from_token),
                           session: AsyncSession = Depends(session_dep)):
    """Отметить события прочитанными (по списку id или все сразу)."""
    from sqlalchemy import update as _upd
    cond = [MobileEvent.user_id == user.id, MobileEvent.read_at.is_(None)]
    if not body.all:
        if not body.ids:
            return {"ok": True, "updated": 0}
        cond.append(MobileEvent.id.in_(body.ids))
    res = await session.execute(_upd(MobileEvent).where(*cond)
                                 .values(read_at=utcnow()))
    await session.commit()
    return {"ok": True, "updated": res.rowcount or 0}
