"""
Сервис мутаций сущностей (ядро). Используется ВСЕМИ модулями-плагинами.

Гарантии на каждую запись:
  - инвариант ответственных (access.ensure_responsibles);
  - AuditLog + OutboxEvent в одной транзакции (Transactional Outbox) ->
    Telegram/Календарь обрабатываются фонами, HTTP не блокируется;
  - entity.sync_state=pending — маркер для diff-прохода календаря.
"""
from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import access
from app.models.core import (
    AuditLog, DepartmentMember, Entity, EntityKind, EntityShare, EntityResponsible,
    OutboxEvent, Permission, SyncState, User,
)


_PERM_ORDER = {Permission.READ: 0, Permission.EDIT: 1, Permission.FULL: 2}


def _perm_lt_local(a: Permission, b: Permission) -> bool:
    return _PERM_ORDER[a] < _PERM_ORDER[b]


def _uid() -> str:
    return uuid.uuid4().hex[:12]


async def create_entity(
    session: AsyncSession, user: User, *, kind: EntityKind, module_key: str,
    owner_department_id: int, title: str, description: str | None = None,
    status_key: str | None = None, priority_key: str | None = None,
    due_date: str | None = None, tags: list | None = None,
    extra_json: dict | None = None, responsible_user_id: int | None = None,
    share_with: list[tuple[int, Permission]] | None = None,
) -> Entity:
    """Создание сущности.

    share_with — межпространственный контекст (например, задача проекта,
    в котором участвуют несколько подразделений): [(dept_id, permission), ...].
    Для каждого участвующего подразделения создаётся свой ответственный/слот
    (инвариант ТЗ «несколько ответственных» реализуется через ensure_responsibles).
    """
    ent = Entity(
        uid=_uid(), kind=kind, module_key=module_key,
        owner_department_id=owner_department_id, title=title,
        description=description, status_key=status_key, priority_key=priority_key,
        due_date=due_date, tags=tags or [], extra_json=extra_json or {},
        sync_state=SyncState.PENDING, cal_uid=f"caesar-{uuid.uuid4().hex[:10]}@app",
    )
    session.add(ent)
    await session.flush()
    for dept_id, perm in (share_with or []):
        if dept_id != owner_department_id:
            session.add(EntityShare(
                entity_id=ent.id, target_department_id=dept_id,
                permission=perm, granted_by_user_id=user.id))
    # Ответственный от подразделения-владельца. ВАЖНО (изоляция пространств):
    # если явный responsible — сотрудник ДРУГОГО подразделения (например, админ/
    # координатор создаёт задачу в чужом workspace и назначает себя), слот
    # владельца остаётся незаполненным (назначит head), а пользователь получает
    # доступ к сущности через свой слот в принимающем подразделении (шэр).
    owner_slot_user_id = responsible_user_id
    external_resp_dept: int | None = None
    if responsible_user_id is not None:
        in_owner = await session.scalar(
            select(DepartmentMember.id).where(
                DepartmentMember.user_id == responsible_user_id,
                DepartmentMember.department_id == owner_department_id))
        if not in_owner:
            owner_slot_user_id = None
            # Подразделение внешнего исполнителя выбираем ДЕТЕРМИНИРОВАННО:
            # 1) если исполнитель входит в share_with — берём именно это
            #    подразделение (иначе у админа/координатора, состоящего в своём
            #    пространстве, служебный слот открывался бы в его родном
            #    департаменте и сущность просачивалась бы туда);
            # 2) иначе — первое по id подразделение исполнителя + служебный шэр.
            share_depts = {d for d, _ in (share_with or []) if d != owner_department_id}
            resp_depts = (await session.scalars(
                select(DepartmentMember.department_id).where(
                    DepartmentMember.user_id == responsible_user_id)
                .order_by(DepartmentMember.id))).all()
            external_resp_dept = next(
                (d for d in resp_depts if d in share_depts),
                resp_depts[0] if resp_depts else None)
    session.add(EntityResponsible(
        entity_id=ent.id, department_id=owner_department_id,
        user_id=owner_slot_user_id, is_primary=True))
    if external_resp_dept is not None:
        # Внешний ответственный: доступ к сущности даётся ТОЛЬКО через явный
        # механизм шеринга (иначе нарушается изоляция пространств). Если
        # принимающая сторона уже указана в share_with — назначаем его в слот
        # этого подразделения; иначе открываем служебный шэр (READ) + слот.
        resp_perm = None
        for d, p in (share_with or []):
            if d == external_resp_dept:
                resp_perm = p
                break
        if resp_perm is not None:
            row = await session.scalar(
                select(EntityResponsible).where(
                    EntityResponsible.entity_id == ent.id,
                    EntityResponsible.department_id == external_resp_dept))
            if row is None:
                row = EntityResponsible(entity_id=ent.id,
                                        department_id=external_resp_dept)
                session.add(row)
                await session.flush()
            row.user_id = responsible_user_id
            if _perm_lt_local(resp_perm, Permission.EDIT):
                # ответственный принимающей стороны >= EDIT (см. access.resolve_permission)
                sh = await session.scalar(
                    select(EntityShare).where(
                        EntityShare.entity_id == ent.id,
                        EntityShare.target_department_id == external_resp_dept))
                if sh:
                    sh.permission = Permission.EDIT
        else:
            session.add(EntityShare(
                entity_id=ent.id, target_department_id=external_resp_dept,
                permission=Permission.READ, granted_by_user_id=user.id))
            session.add(EntityResponsible(
                entity_id=ent.id, department_id=external_resp_dept,
                user_id=responsible_user_id))
    await access.ensure_responsibles(session, ent)
    await _log_and_emit(session, user, "create", ent,
                        {"title": title, "responsible_user_id": responsible_user_id,
                         "shared_with": [d for d, _ in (share_with or [])]})
    return ent


async def update_entity(
    session: AsyncSession, user: User, ent: Entity, changes: dict[str, Any],
) -> Entity:
    """changes — только разрешённые поля ядра (payload обновляет сам модуль)."""
    allowed = {"title", "description", "status_key", "priority_key",
               "due_date", "tags", "extra_json"}
    old_status = ent.status_key
    changed: dict[str, Any] = {}
    for k, v in changes.items():
        if k in allowed and getattr(ent, k) != v:
            setattr(ent, k, v)
            changed[k] = v
    if changed:
        ent.sync_state = SyncState.PENDING
        action = "status" if "status_key" in changed else "update"
        await _log_and_emit(session, user, action, ent,
                            {"changed": changed, "old_status": old_status})
    return ent


async def delete_entity(session: AsyncSession, user: User, ent: Entity) -> None:
    await _log_and_emit(session, user, "delete", ent, {"title": ent.title})
    # Явное удаление зависимых строк ПЕРЕД удалением сущности. ORM-коллекция
    # shares — viewonly (защита от orphan-откатов pending-объектов), поэтому
    # каскад по relationship не срабатывает; на уровне БД включён PRAGMA
    # foreign_keys=ON и часть payload-таблиц ссылается без ondelete=CASCADE.
    from sqlalchemy import delete as _del
    from app.models.module_payloads import (
        DocPayload, GrantPayload, MeetingPayload, PriorityPayload,
        ProjectPayload, ScholarPayload, SoftPayload, TaskPayload)
    for payload_model in (TaskPayload, MeetingPayload, ProjectPayload, DocPayload,
                          SoftPayload, GrantPayload, ScholarPayload, PriorityPayload):
        await session.execute(_del(payload_model).where(payload_model.entity_id == ent.id))
    await session.execute(_del(EntityShare).where(EntityShare.entity_id == ent.id))
    await session.delete(ent)


async def assign_responsible(
    session: AsyncSession, user: User, ent: Entity,
    department_id: int, user_id: Optional[int],
) -> None:
    """Назначение ответственного от конкретного подразделения (слот создаётся шерингом).

    Инварианты изоляции рабочих пространств:
      - назначать можно только в подразделение-владельца либо в подразделение,
        которому сущность реально расшарена (EntityShare);
      - назначаемый пользователь обязан состоять в этом подразделении.
    Иначе назначение открывало бы «лазейку» доступа к чужому пространству.
    """
    if user_id is not None:
        member = await session.scalar(
            select(DepartmentMember.id).where(
                DepartmentMember.user_id == user_id,
                DepartmentMember.department_id == department_id))
        if not member:
            raise PermissionError(
                "Ответственный должен состоять в выбранном подразделении")
        participates = department_id == ent.owner_department_id or await session.scalar(
            select(EntityShare.id).where(
                EntityShare.entity_id == ent.id,
                EntityShare.target_department_id == department_id))
        if not participates:
            raise PermissionError(
                "Нельзя назначить ответственного из подразделения, "
                "которому сущность не принадлежит и не расшарена")
    row = await session.scalar(
        select(EntityResponsible).where(
            EntityResponsible.entity_id == ent.id,
            EntityResponsible.department_id == department_id))
    if row is None:
        row = EntityResponsible(
            entity_id=ent.id, department_id=department_id, user_id=user_id,
            is_primary=(department_id == ent.owner_department_id))
        session.add(row)
    else:
        row.user_id = user_id
    await _log_and_emit(session, user, "reassign", ent,
                        {"department_id": department_id, "user_id": user_id})


async def share_entity(
    session: AsyncSession, user: User, ent: Entity,
    target_department_id: int, permission: Permission,
) -> None:
    existing = await session.scalar(
        select(EntityShare).where(
            EntityShare.entity_id == ent.id,
            EntityShare.target_department_id == target_department_id))
    if existing:
        existing.permission = permission
    else:
        session.add(EntityShare(
            entity_id=ent.id, target_department_id=target_department_id,
            permission=permission, granted_by_user_id=user.id))
    await access.ensure_responsibles(session, ent)  # слот ответственного принимающей стороны
    await _log_and_emit(session, user, "share", ent,
                        {"target_department_id": target_department_id,
                         "permission": permission.value})


async def unshare_entity(session: AsyncSession, user: User, ent: Entity,
                         target_department_id: int) -> None:
    row = await session.scalar(
        select(EntityShare).where(
            EntityShare.entity_id == ent.id,
            EntityShare.target_department_id == target_department_id))
    if row:
        await session.delete(row)
        await session.flush()
        await access.ensure_responsibles(session, ent)
        await _log_and_emit(session, user, "unshare", ent,
                            {"target_department_id": target_department_id})


async def _log_and_emit(session: AsyncSession, user: User, action: str,
                        ent: Entity, details: dict) -> None:
    session.add(AuditLog(user_id=user.id, action=action, entity_id=ent.id,
                         details_json={"kind": ent.kind.value, "uid": ent.uid, **details}))
    # события интеграций (воркеры заберут после commit'а)
    session.add(OutboxEvent(topic="telegram.notify", payload_json={
        "action": action, "entity_id": ent.id, "entity_uid": ent.uid,
        "kind": ent.kind.value, "module": ent.module_key, "title": ent.title,
        "actor": user.display_name, "details": details,
    }))
    if action != "delete":
        session.add(OutboxEvent(topic="calendar.sync", payload_json={
            "entity_id": ent.id, "entity_uid": ent.uid, "reason": action,
        }))
    else:
        session.add(OutboxEvent(topic="calendar.remove", payload_json={
            "entity_id": ent.id, "cal_uid": ent.cal_uid,
        }))
    await session.flush()
