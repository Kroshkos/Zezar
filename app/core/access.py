"""
Сервис доступа (ядро RBAC + шеринг). Единая точка для роутеров модулей и шаблонов.

Правила разрешения сущности пользователю:
  ADMIN/COORDINATOR -> всё во всех пространствах;
  HEAD              -> всё своего подразделения + расшаренное ему (мин. READ);
  MEMBER            -> свои сущности + сущности подразделения, где он состоит,
                       + расшаренные его подразделению (по permission);
  Ответственный из принимающего подразделения получает EDIT независимо от permission
  (иначе не сможет вести свою часть поручения).
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.core import (
    DepartmentMember, Entity, EntityShare, EntityResponsible, Permission, Role, User,
)

_PERM_ORDER = {Permission.READ: 0, Permission.EDIT: 1, Permission.FULL: 2}


@dataclass
class AccessContext:
    user: User
    department_ids: list[int]          # пространства, где состоит
    is_global: bool                    # admin/coordinator — видят все пространства

    @property
    def can_admin_system(self) -> bool:
        return self.user.role in (Role.ADMIN, Role.COORDINATOR)


async def build_context(session: AsyncSession, user: User) -> AccessContext:
    rows = await session.execute(
        select(DepartmentMember.department_id).where(DepartmentMember.user_id == user.id)
    )
    dept_ids = [r for r in rows.scalars().all()]
    return AccessContext(
        user=user,
        department_ids=dept_ids,
        is_global=user.role in (Role.ADMIN, Role.COORDINATOR),
    )


def entity_filter(ctx: AccessContext):
    """SQL-условие видимости списка сущностей для данного пользователя."""
    if ctx.is_global:
        return Entity.id.is_not(None)  # всё

    conds = [Entity.owner_department_id.in_(ctx.department_ids)]
    # расшаренное моим подразделениям
    shared = (
        select(EntityShare.entity_id)
        .where(EntityShare.target_department_id.in_(ctx.department_ids or [-1]))
    )
    conds.append(Entity.id.in_(shared))
    # я ответственный (даже если подразделение сменилось)
    resp = (
        select(EntityResponsible.entity_id)
        .where(EntityResponsible.user_id == ctx.user.id)
    )
    conds.append(Entity.id.in_(resp))
    return or_(*conds)


async def resolve_permission(session: AsyncSession, ctx: AccessContext, entity: Entity) -> Permission | None:
    """Максимальный уровень прав пользователя на конкретную сущность (None = нет доступа)."""
    if ctx.is_global:
        return Permission.FULL
    if entity.owner_department_id in ctx.department_ids:
        # внутри своего пространства: head/участник с ролью head_in_dept — edit+, member — edit своих
        return Permission.EDIT
    share = await session.scalar(
        select(EntityShare).where(
            EntityShare.entity_id == entity.id,
            EntityShare.target_department_id.in_(ctx.department_ids or [-1]),
        )
    )
    perm = share.permission if share else None
    # ответственный из принимающего подразделения >= EDIT
    if perm is not None and _perm_lt(perm, Permission.EDIT):
        is_resp = await session.scalar(
            select(EntityResponsible).where(
                EntityResponsible.entity_id == entity.id,
                EntityResponsible.user_id == ctx.user.id,
            )
        )
        if is_resp:
            perm = Permission.EDIT
    return perm


def _perm_lt(a: Permission, b: Permission) -> bool:
    return _PERM_ORDER[a] < _PERM_ORDER[b]


def can_write(perm: Permission | None) -> bool:
    return perm is not None and perm != Permission.READ


def can_manage_sharing(perm: Permission | None) -> bool:
    return perm == Permission.FULL


async def ensure_responsibles(session: AsyncSession, entity: Entity) -> None:
    """Инвариант «по одному ответственному от каждого участвующего подразделения»:
    при шеринге создаём запись-слот для принимающего подразделения (назначает head),
    при снятии шеринга — удаляем слот этого подразделения."""
    dept_ids = {entity.owner_department_id}
    shares = await session.scalars(
        select(EntityShare).where(EntityShare.entity_id == entity.id)
    )
    for s in shares:
        dept_ids.add(s.target_department_id)
    existing = await session.scalars(
        select(EntityResponsible).where(EntityResponsible.entity_id == entity.id)
    )
    have = {r.department_id for r in existing}
    for d in dept_ids - have:
        session.add(EntityResponsible(entity_id=entity.id, department_id=d, user_id=None))
    # user_id=NULL до назначения; не удаляем уже заполненные слоты при снятии шеринга,
    # если на них висят задачи? — по ТЗ удаляем пустые слоты чужих подразделений:
    for r in existing:
        if r.department_id not in dept_ids and r.user_id is None:
            await session.delete(r)
    await session.flush()

