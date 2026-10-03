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
    # DISTINCT: пользователь может иметь несколько членств в одном подразделении —
    # дубликаты ломали SQL-условия IN (...) и сравнения множеств в RBAC.
    rows = await session.execute(
        select(DepartmentMember.department_id)
        .where(DepartmentMember.user_id == user.id)
        .distinct()
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
    # Я — НАЗНАЧЕННЫЙ ответственный (user_id != NULL; пустые слоты-заготовки не дают доступа).
    # ВАЖНО (изоляция пространств): ответственность даёт доступ ТОЛЬКО если моё
    # подразделение участвует в сущности легально:
    #   1) я состою в подразделении-владельце И это мой primary-слот, ИЛИ
    #   2) моё подразделение реально получило шэр (EntityShare) и слот открыт в нём.
    # Служебные слоты внешних исполнителей (is_primary=False в чужом пространстве)
    # доступ НЕ открывают — иначе «личная» сущность админа просачивалась бы в его
    # родное пространство через первичный слот чужого подразделения.
    resp = (
        select(EntityResponsible.entity_id)
        .where(EntityResponsible.user_id == ctx.user.id)
        .where(or_(
            and_(
                EntityResponsible.is_primary.is_(True),
                EntityResponsible.department_id.in_(ctx.department_ids or [-1]),
            ),
            and_(
                EntityResponsible.department_id.in_(ctx.department_ids or [-1]),
                select(EntityShare.id).where(
                    EntityShare.entity_id == EntityResponsible.entity_id,
                    EntityShare.target_department_id == EntityResponsible.department_id,
                ).exists(),
            ),
        ))
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
    # Инвариант «по одному ответственному от подразделения»: доступ как
    # «подразделение-владелец» даёт ТОЛЬКО заполненный PRIMARY-слот (is_primary=True,
    # user_id=наш). Служебные слоты внешних исполнителей (is_primary=False),
    # открываемые при межпространственном назначении, не должны трактоваться
    # как членство в подразделении-владельце — иначе исполнитель получал бы
    # EDIT вместо прав шэра, а его родное пространство видело бы чужие сущности.
    is_owner_dept = entity.owner_department_id in my_depts or await session.scalar(
        select(EntityResponsible.id).where(
            EntityResponsible.entity_id == entity.id,
            EntityResponsible.user_id == ctx.user.id,
            EntityResponsible.is_primary.is_(True)))
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
    # MEMBER — редактирование (своих и поручений пространства).
    # ВАЖНО: права владельца пространства вычисляются строго по ФАКТУ членства в
    # подразделении-владельце, а не по «is_owner_dept» (которое может быть открыто
    # primary-слотом внешнего исполнителя) — иначе исполнитель из другого
    # пространства получал бы FULL/EDIT как «владелец».
    member_role = await session.scalar(
        select(DepartmentMember.role_in_dept).where(
            DepartmentMember.user_id == ctx.user.id,
            DepartmentMember.department_id == entity.owner_department_id))
    if entity.owner_department_id in my_depts:
        if member_role == Role.HEAD:
            return Permission.FULL
        return Permission.EDIT
    if share is None:
        # доступ только через заполненный primary-слот (см. is_owner_dept выше)
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

