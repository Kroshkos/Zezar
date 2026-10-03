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
    AuditLog, Entity, EntityKind, EntityShare, EntityResponsible, OutboxEvent,
    Permission, SyncState, User,
)


def _uid() -> str:
    return uuid.uuid4().hex[:12]


async def create_entity(
    session: AsyncSession, user: User, *, kind: EntityKind, module_key: str,
    owner_department_id: int, title: str, description: str | None = None,
    status_key: str | None = None, priority_key: str | None = None,
    due_date: str | None = None, tags: list | None = None,
    extra_json: dict | None = None, responsible_user_id: int | None = None,
) -> Entity:
    ent = Entity(
        uid=_uid(), kind=kind, module_key=module_key,
        owner_department_id=owner_department_id, title=title,
        description=description, status_key=status_key, priority_key=priority_key,
        due_date=due_date, tags=tags or [], extra_json=extra_json or {},
        sync_state=SyncState.PENDING, cal_uid=f"caesar-{uuid.uuid4().hex[:10]}@app",
    )
    session.add(ent)
    await session.flush()
    # ответственный от подразделения-владельца
    session.add(EntityResponsible(
        entity_id=ent.id, department_id=owner_department_id,
        user_id=responsible_user_id, is_primary=True))
    await access.ensure_responsibles(session, ent)
    await _log_and_emit(session, user, "create", ent,
                        {"title": title, "responsible_user_id": responsible_user_id})
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
    await session.delete(ent)


async def assign_responsible(
    session: AsyncSession, user: User, ent: Entity,
    department_id: int, user_id: Optional[int],
) -> None:
    """Назначение ответственного от конкретного подразделения (слот создаётся шерингом)."""
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
