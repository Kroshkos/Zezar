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
    AuditLog, DepartmentMember, Entity, EntityShare, EntityResponsible, Permission, Role, User,
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
    # я НАЗНАЧЕННЫЙ ответственный (user_id != NULL; пустые слоты-заготовки не дают доступа).
    # ВАЖНО: это условие НЕ ограничивается списком shared — иначе сущности, расшаренные
    # через служебный шэр (напр. при назначении внешнего ответственного в entity_service),
    # просачивались бы в чужие пространства на уровне SQL-фильтра. Точная проверка прав
    # всегда выполняется в resolve_permission (минимум прав).
    resp = (
        select(EntityResponsible.entity_id)
        .where(EntityResponsible.user_id == ctx.user.id)
    )
    conds.append(Entity.id.in_(resp))
    return or_(*conds)


async def resolve_permission(session: AsyncSession, ctx: AccessContext, entity: Entity) -> Permission | None:
    """Максимальный уровень прав пользователя на конкретную сущность (None = нет доступа).

    Единый источник истины для ВСЕХ точек входа (HTML, REST, TG, воркеры).
    Права определяются СТРОГО фактом шеринга (EntityShare), а не членством:
      - пользователь состоит в подразделении-владельце          -> EDIT;
      - сущность расшарена одному из его подразделений          -> permission шэра;
      - он назначенный ответственный принимающей стороны         -> не ниже EDIT;
      - иначе                                                    -> None.
    Особый случай: сущность «бесхозная» (подразделение-владелец удалено / данных
    о членстве нет) и при этом НЕ имеет ни одного активного шэра — она видна
    глобальным ролям (админ/координатор) и больше никому (иначе нарушается
    изоляция пространств). Это же правило действует и для is_global: см. ниже.
    """
    shares = (await session.scalars(
        select(EntityShare).where(EntityShare.entity_id == entity.id))).all()
    if ctx.is_global:
        # Админ/координатор: полный доступ ко всем сущностям системы (по ТЗ),
        # но бесхозная сущность без активных шэров остаётся доступна только им.
        return Permission.FULL
    my_depts = set(ctx.department_ids)
    share = next((s for s in shares if s.target_department_id in my_depts), None)
    is_owner_dept = entity.owner_department_id in my_depts
    my_resp = await session.scalar(
        select(EntityResponsible).where(
            EntityResponsible.entity_id == entity.id,
            EntityResponsible.user_id == ctx.user.id))
    # Инвариант изоляции рабочих пространств: доступ есть только подразделению-
    # владельцу и подразделениям из списка шэров. Членство админа в каком-либо
    # пространстве само по себе доступ к чужим сущностям не даёт.
    if not is_owner_dept and share is None:
        return None
    # роль в пространстве: HEAD — полный доступ к сущностям своего подразделения,
    # MEMBER — редактирование (своих и поручений пространства)
    role_in_dept = await session.scalar(
        select(DepartmentMember.role_in_dept).where(
            DepartmentMember.user_id == ctx.user.id,
            DepartmentMember.department_id == entity.owner_department_id)) \
        if is_owner_dept else None
    if role_in_dept == Role.HEAD:
        return Permission.FULL
    if is_owner_dept:
        return Permission.EDIT
    perm = share.permission
    # ответственный из принимающего подразделения >= EDIT
    if my_resp is not None and _perm_lt(perm, Permission.EDIT):
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

