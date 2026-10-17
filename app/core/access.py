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

from sqlalchemy import func, select, or_, and_
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


# ---------------------------------------------------------------------------
# Единая точка назначения/изменения роли в пространстве (RBAC-сервис)
# ---------------------------------------------------------------------------

async def count_heads(session: AsyncSession, department_id: int) -> int:
    """Число руководителей (HEAD) в подразделении."""
    return await session.scalar(
        select(func.count(DepartmentMember.id))
        .where(DepartmentMember.department_id == department_id,
               DepartmentMember.role_in_dept == Role.HEAD)) or 0


async def set_department_role(
    session: AsyncSession, *, department_id: int, user_id: int,
    new_role: Role, actor: User, allow_head_promotion: bool = True,
) -> str:
    """УНИФИЦИРОВАННОЕ назначение роли в рабочем пространстве.

    Единственная точка входа для ЛЮБЫХ изменений role_in_dept (роутеры
    подразделений, админ-панель, фронтенд-селекты). Гарантирует:
      - инвариант «в подразделении всегда есть хотя бы один HEAD»:
        понижение последнего руководителя запрещено (HTTP 409);
      - синхронизацию глобальной роли: повышение до HEAD ставит User.role=HEAD
        (если пользователь не admin/coordinator — их глобальная роль выше),
        понижение снимает HEAD, если это его последняя руководимая область;
      - инвалидацию кэша прав: bump_epoch + expire_all — открытые страницы и
        токены получают актуальные права без перезагрузки/перелогина.

    Права вызывающего проверяет РОУТЕР (он знает контекст: admin vs head);
    здесь — только целостность данных. Возвращает человекочитаемое описание
    изменения для журнала событий. Commit НЕ выполняет — транзакцию фиксирует
    вызывающий код (epoch меняется атомарно с правами).
    """
    from fastapi import HTTPException

    from app.core.permcache import bump_epoch, invalidate_session

    target = await session.get(User, user_id)
    if not target:
        raise HTTPException(404, "Пользователь не найден")
    # Скалярные копиИ полей актора/цели: в конце функции вызывается
    # invalidate_session (expire_all), после чего ленивое чтение атрибутов
    # expired-объекта вне зелёного потока бросает MissingGreenlet.
    actor_name = actor.login or actor.display_name
    target_display = target.display_name

    member = await session.scalar(
        select(DepartmentMember).where(
            DepartmentMember.user_id == user_id,
            DepartmentMember.department_id == department_id))

    if member is None:
        # новое членство: повышение до HEAD разрешено только тем, кому разрешено
        # (админ/координатор); head-вызывающий добавляет лишь MEMBER — это
        # проверяется в роутере до вызова сервиса.
        if new_role == Role.HEAD and not allow_head_promotion:
            raise HTTPException(403, "Руководитель может добавлять только участников")
        session.add(DepartmentMember(user_id=user_id, department_id=department_id,
                                     role_in_dept=new_role))
        action = "включён"
    else:
        if member.role_in_dept == new_role:
            return "без изменений"
        if (new_role != Role.HEAD and member.role_in_dept == Role.HEAD
                and await count_heads(session, department_id) <= 1):
            raise HTTPException(
                409, "В подразделении должен остаться хотя бы один руководитель — "
                     "сначала назначьте нового")
        member.role_in_dept = new_role
        action = "роль изменена"

    # Синхронизация ГЛОБЛЬНОЙ роли (устраняет расхождение users.role vs role_in_dept):
    target = await session.get(User, user_id)
    if new_role == Role.HEAD and target.role not in (Role.ADMIN, Role.COORDINATOR):
        target.role = Role.HEAD
    elif new_role != Role.HEAD and target.role == Role.HEAD:
        # снимаем HEAD глобально, только если больше нигде не руководитель
        other_heads = await session.scalar(
            select(func.count(DepartmentMember.id))
            .where(DepartmentMember.user_id == user_id,
                   DepartmentMember.role_in_dept == Role.HEAD,
                   DepartmentMember.department_id != department_id))
        if not other_heads:
            target.role = Role.MEMBER

    await bump_epoch(session)          # инвалидация кэша прав (токены/клиенты)
    invalidate_session(session)        # сброс identity map текущей сессии
    return (f"{action}: {new_role.value} (актор: {actor_name}, "
            f"цель: {target_display})")


def can_write(perm: Permission | None) -> bool:
    return perm is not None and perm != Permission.READ


def can_manage_sharing(perm: Permission | None) -> bool:
    return perm == Permission.FULL


async def ensure_responsibles(session: AsyncSession, entity: Entity) -> None:
    """Инвариант «по одному ответственному от каждого участвующего подразделения»:
    при шеринге создаём запись-слот для принимающего подразделения (назначает head),
    при снятии шеринга — удаляем слот этого подразделения.

    ВАЖНО: списки читаем ЯВНЫМИ SELECT'ами и materialize'руем в списки до любых
    add/delete. Иначе autoflush перед новым SELECT удаляет pending-объекты
    EntityShare, созданные вызывающим кодом (share_with при создании сущности),
    что приводило к потере шеринга и нарушению инварианта ответственных.
    """
    dept_ids = {entity.owner_department_id}
    shares = list(await session.scalars(
        select(EntityShare).where(EntityShare.entity_id == entity.id)
    ))
    for s in shares:
        dept_ids.add(s.target_department_id)
    existing = list(await session.scalars(
        select(EntityResponsible).where(EntityResponsible.entity_id == entity.id)
    ))
    have = {r.department_id for r in existing}
    for d in dept_ids - have:
        session.add(EntityResponsible(entity_id=entity.id, department_id=d, user_id=None))
    # user_id=NULL до назначения; не удаляем уже заполненные слоты при снятии шеринга,
    # если на них висят задачи? — по ТЗ удаляем пустые слоты чужих подразделений:
    for r in existing:
        if r.department_id not in dept_ids and r.user_id is None:
            await session.delete(r)
    await session.flush()

