"""
Сервис справочников (статусы, приоритеты, типы, маршруты, группы задач).

Единая точка истины для модулей и админ-панели. Реализует требования ТЗ:

1. ИЕРАРХИЯ ШАБЛОНОВ (преемственность и подчинённость):
   - department_id = NULL  -> глобальный шаблон администратора; применяется ко ВСЕМ
     подразделениям, к которым подключён соответствующий модуль;
   - department_id = <id>  -> запись подразделения: либо собственная (source_global_id
     = NULL), либо активированная руководителем КОПИЯ глобального шаблона
     (source_global_id = id глобальной записи).

2. АКТИВАЦИЯ / ДЕАКТИВАЦИЯ руководителем:
   - activate(dept, global_row)  — создаёт копию (или восстанавливает ранее
     деактивированную copy по source_global_id — преемственность сохраняется);
   - deactivate(copy)            — снимает is_active (копия НЕ удаляется, чтобы
     исторические status_key/priority_key сущностей не «откреплялись»);
   - повторная активация возвращает ту же запись.

3. СВЯЗАННОСТЬ С МОДУЛЯМИ: набор справочников, доступных подразделению, ограничен
   модулями, включёнными в его рабочее пространство (workspace_modules.is_enabled):
     statuses:  task->tasks, project->projects, doc->docs, soft->softs,
                grant->grants, scholarship->schols, priority->priority, meeting->meetings
     types:     doc_type->docs, soft_type->softs, schol_type->schols
     priorities / wf-маршруты / группы задач — задачи/совещания (wf — ещё docs/projects/softs)

4. МЫСЛИМОСТЬ ДАННЫХ (informative CRUD): удаление статуса/типа/приоритета/шаблона
   запрещено, если он используется живыми сущностями (как stUsage()/delStatus()
   в прототипе sipm_lite_v3.html).
"""
from __future__ import annotations

import re
import uuid
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func as sa_func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import log_event
from app.models.core import (
    Department, DepartmentMember, Entity, EntityKind, PriorityLevel, RefType, Role,
    StatusSet, TaskGroup, User, WfTemplate, Workspace, WorkspaceModule,
)
from app.models.module_payloads import DocPayload, ScholarPayload, SoftPayload

# Цвета badge из CSS прототипа (COLORS в sipm_lite_v3.html)
COLORS = ["b-blue", "b-green", "b-amber", "b-red", "b-gray", "b-purple", "b-orange"]

# EntityKind <-> значение StatusSet.kind (в Enum имя SCHOL, значение 'scholarship')
KIND_BY_SET = {
    "task": EntityKind.TASK, "project": EntityKind.PROJECT, "doc": EntityKind.DOC,
    "soft": EntityKind.SOFT, "grant": EntityKind.GRANT, "schol": EntityKind.SCHOL,
    "priority": EntityKind.PRIORITY, "meet": EntityKind.MEETING,
}
SET_NAME_BY_KIND = {v: k for k, v in KIND_BY_SET.items()}

# вид справочника -> модуль, который должен быть включён в workspace подразделения
REFDATA_MODULE = {
    "status:task": "tasks", "status:project": "projects", "status:doc": "docs",
    "status:soft": "softs", "status:grant": "grants", "status:schol": "schols",
    "status:priority": "priority", "status:meet": "meetings",
    "type:doc_type": "docs", "type:soft_type": "softs", "type:schol_type": "schols",
    "prio": "tasks",
    "groups": "meetings",
    # маршруты применимы к документам/проектам/ПО (wfTemplates в демо) + задачи/совещания
    "wf": ("docs", "projects", "softs"),
}


class RefDataError(HTTPException):
    def __init__(self, status_code: int, detail: str):
        super().__init__(status_code=status_code, detail=detail)


# ---------------------------------------------------------------------------
# Полномочия и области видимости
# ---------------------------------------------------------------------------

async def _head_dept(session: AsyncSession, user: User, dept_id: Optional[int]) -> Optional[int]:
    """Если пользователь — руководитель dept_id (или руководитель какого-либо
    подразделения при dept_id=None) — вернуть id того подразделения, иначе None."""
    conds = [DepartmentMember.user_id == user.id,
             DepartmentMember.role_in_dept == Role.HEAD]
    if dept_id is not None:
        conds.append(DepartmentMember.department_id == dept_id)
    return await session.scalar(
        select(DepartmentMember.department_id).where(*conds)
        .order_by(DepartmentMember.id))


async def resolve_dept(session: AsyncSession, user: User, dept_id: Optional[int]) -> Optional[int]:
    """Подразделение, для которого пользователь правит справочники.

    Админ/координатор: dept_id как передан; None = режим «Вся система»
    (глобальные шаблоны, department_id=NULL). HEAD — только своё подразделение
    (пустой выбор = возглавляемое).
    """
    if user.role in (Role.ADMIN, Role.COORDINATOR):
        return dept_id
    head = await _head_dept(session, user, dept_id)
    if head is None:
        raise RefDataError(403, "Справочники подразделения управляет только его руководитель")
    return head


async def enabled_modules(session: AsyncSession, dept_id: Optional[int]) -> set[str]:
    """Модули, реально включённые в workspace подразделения.

    dept_id=None (глобальный справочник администратора) — ограничение по модулям
    не применяется: возвращаем все известные модулям-справочникам ключи."""
    if dept_id is None:
        allv: set[str] = set()
        for vals in REFDATA_MODULE.values():
            allv.update(vals if isinstance(vals, tuple) else (vals,))
        return allv
    ws_id = await session.scalar(select(Workspace.id).where(Workspace.department_id == dept_id))
    if ws_id is None:
        return set()
    rows = (await session.scalars(
        select(WorkspaceModule.module_key).where(
            WorkspaceModule.workspace_id == ws_id,
            WorkspaceModule.is_enabled == True))).all()  # noqa: E712
    return set(rows)


def _module_allowed(kind_ref: str, mods: set[str]) -> bool:
    need = REFDATA_MODULE.get(kind_ref)
    if need is None:
        return False
    needed = need if isinstance(need, tuple) else (need,)
    return any(m in mods for m in needed)


# ---------------------------------------------------------------------------
# Эффективные наборы для подразделения (глобальные + активированные копии + свои)
# ---------------------------------------------------------------------------

async def effective_statuses(session: AsyncSession, kind: EntityKind,
                             dept_id: Optional[int], mods: set[str] | None = None) -> list[StatusSet]:
    """Статусы вида kind, применяемые к подразделению: глобальные шаблоны админа
    (для всех подключённых пространств) + собственные записи подразделения.
    Копии активированных шаблонов переопределяют глобальную запись с тем же key."""
    conds = [StatusSet.department_id.is_(None)]
    if dept_id is not None:
        conds.append(StatusSet.department_id == dept_id)
    q = (select(StatusSet).where(StatusSet.kind == kind, or_(*conds))
         .order_by(StatusSet.order, StatusSet.id))
    rows = list((await session.scalars(q)).all())
    if dept_id is not None and mods is not None:
        setname = SET_NAME_BY_KIND.get(kind)
        if setname and not _module_allowed(f"status:{setname}", mods):
            return []
    if dept_id is not None:
        own = [r for r in rows if r.department_id == dept_id]
        eff = {r.key: r for r in rows if r.department_id is None and r.is_active}
        for r in own:
            if r.source_global_id:
                # копия шаблона администратора: деактивированная копия «выключает»
                # глобальный шаблон для этого подразделения (преемственность ТЗ)
                if r.is_active:
                    eff[r.key] = r
                else:
                    eff.pop(r.key, None)
            elif r.is_active:
                eff[r.key] = r      # собственная запись подразделения приоритетнее глобальной
        rows = list(eff.values())
    else:
        rows = [r for r in rows if r.department_id is None and r.is_active]
    return sorted(rows, key=lambda r: (r.order, r.id))


async def effective_priorities(session: AsyncSession, dept_id: Optional[int],
                               mods: set[str] | None = None) -> list[PriorityLevel]:
    conds = [PriorityLevel.department_id.is_(None)]
    if dept_id is not None:
        conds.append(PriorityLevel.department_id == dept_id)
    rows = list((await session.scalars(
        select(PriorityLevel).where(or_(*conds)).order_by(PriorityLevel.weight.desc(),
                                                          PriorityLevel.id))).all())
    if dept_id is not None and mods is not None and not _module_allowed("prio", mods):
        return []
    if dept_id is not None:
        eff = {r.key: r for r in rows if r.department_id is None and r.is_active}
        for r in rows:
            if r.department_id != dept_id:
                continue
            if r.source_global_id:
                if r.is_active:
                    eff[r.key] = r
                else:
                    eff.pop(r.key, None)   # деактивированная копия выключает глобальный шаблон
            elif r.is_active:
                eff[r.key] = r
        rows = list(eff.values())
    else:
        rows = [r for r in rows if r.department_id is None and r.is_active]
    return sorted(rows, key=lambda r: (-r.weight, r.id))


async def effective_types(session: AsyncSession, ref_kind: str, dept_id: Optional[int],
                          mods: set[str] | None = None) -> list[RefType]:
    conds = [RefType.kind == ref_kind, RefType.department_id.is_(None), RefType.is_active == True]  # noqa: E712
    if dept_id is not None:
        conds = [RefType.kind == ref_kind, RefType.is_active == True,  # noqa: E712
                 or_(RefType.department_id.is_(None), RefType.department_id == dept_id)]
    rows = list((await session.scalars(
        select(RefType).where(*conds).order_by(RefType.label))).all())
    if dept_id is not None and mods is not None and not _module_allowed(f"type:{ref_kind}", mods):
        return []
    if dept_id is not None:
        # своя запись подразделения с таким же label перекрывает глобальную;
        # деактивированная копия шаблона — «выключает» глобальный тип для пространства
        eff: dict[str, RefType] = {}
        for r in rows:
            if r.department_id is None:
                eff[r.label] = r
            elif r.is_active and r.department_id == dept_id:
                eff[r.label] = r
        for g in rows:  # деактивированные копии убирают своего глобального «двойника»
            if g.department_id == dept_id and g.source_global_id and not g.is_active:
                eff.pop(g.label, None)
        rows = list(eff.values())
    else:
        rows = [r for r in rows if r.department_id is None]
    return sorted(rows, key=lambda r: r.label)


async def effective_templates(session: AsyncSession, dept_id: Optional[int],
                              mods: set[str] | None = None) -> list[WfTemplate]:
    conds = [WfTemplate.department_id.is_(None), WfTemplate.is_active == True]  # noqa: E712
    if dept_id is not None:
        conds = [WfTemplate.is_active == True,  # noqa: E712
                 or_(WfTemplate.department_id.is_(None), WfTemplate.department_id == dept_id)]
    rows = list((await session.scalars(
        select(WfTemplate).where(*conds).order_by(WfTemplate.id))).all())
    if dept_id is not None and mods is not None and not _module_allowed("wf", mods):
        return []
    if dept_id is None:
        rows = [r for r in rows if r.department_id is None]
    return rows


async def effective_groups(session: AsyncSession, dept_id: Optional[int],
                           mods: set[str] | None = None) -> list[TaskGroup]:
    conds = [TaskGroup.department_id.is_(None), TaskGroup.is_active == True]  # noqa: E712
    if dept_id is not None:
        conds = [TaskGroup.is_active == True,  # noqa: E712
                 or_(TaskGroup.department_id.is_(None), TaskGroup.department_id == dept_id)]
    rows = list((await session.scalars(
        select(TaskGroup).where(*conds).order_by(TaskGroup.name))).all())
    if dept_id is not None and mods is not None and not _module_allowed("groups", mods):
        return []
    if dept_id is None:
        rows = [r for r in rows if r.department_id is None]
    return rows


async def context_dept(session: AsyncSession, user: User,
                       department_id: Optional[int]) -> Optional[int]:
    """Подразделение контекста для форм модулей.

    Глобальные роли (ADMIN/COORDINATOR) — переданный department_id или None
    (агрегированный просмотр: глобальные шаблоны). Остальные — своё первое
    подразделение (детерминированно, по id), если department_id не передан;
    переданный чужой department_id игнорируется (изоляция пространств)."""
    if user.role in (Role.ADMIN, Role.COORDINATOR):
        return department_id
    mine = (await session.scalars(
        select(DepartmentMember.department_id).where(
            DepartmentMember.user_id == user.id).order_by(DepartmentMember.id))).all()
    if department_id and department_id in mine:
        return department_id
    return mine[0] if mine else None


# ---------------------------------------------------------------------------
# Проверка использования (информативность: нельзя удалить занятый элемент)
# ---------------------------------------------------------------------------

async def entity_usage(session: AsyncSession, *, kind: Optional[EntityKind],
                       status_key: Optional[str] = None,
                       priority_key: Optional[str] = None,
                       dept_ids: Optional[list[int]] = None) -> int:
    """Число сущностей, использующих статус/приоритет (в подразделении или везде)."""
    conds = []
    if kind is not None:
        conds.append(Entity.kind == kind)
    if status_key is not None:
        conds.append(Entity.status_key == status_key)
    if priority_key is not None:
        conds.append(Entity.priority_key == priority_key)
    if dept_ids is not None:
        conds.append(Entity.owner_department_id.in_(dept_ids or [-1]))
    q = select(sa_func.count(Entity.id)).where(*conds)
    return await session.scalar(q) or 0


async def type_usage(session: AsyncSession, ref_kind: str, label: str,
                     dept_ids: Optional[list[int]]) -> int:
    """Сколько документов/ПО/стипендий используют тип (по payload-таблице модуля)."""
    mapping = {RefType.KIND_DOC: (DocPayload, DocPayload.doc_type),
               RefType.KIND_SOFT: (SoftPayload, SoftPayload.soft_type),
               RefType.KIND_SCHOL: (ScholarPayload, ScholarPayload.schol_type)}
    model, col = mapping[ref_kind]
    q = (select(sa_func.count(Entity.id))
         .select_from(Entity)
         .join(model, model.entity_id == Entity.id)
         .where(col == label))
    if dept_ids is not None:
        q = q.where(Entity.owner_department_id.in_(dept_ids or [-1]))
    return await session.scalar(q) or 0


async def template_usage(session: AsyncSession, name: str,
                         dept_ids: Optional[list[int]]) -> int:
    """Маршрут используется сущностями, у которых extra_json.wf.template_name == name
    (docs/projects/softs/tasks/meetings — как в демо x.templateName===tp.name)."""
    conds = []
    if dept_ids is not None:
        conds.append(Entity.owner_department_id.in_(dept_ids or [-1]))
    rows = (await session.scalars(select(Entity.extra_json).where(*conds))).all()
    n = 0
    for ej in rows:
        wf = (ej or {}).get("wf") or {}
        if wf.get("template_name") == name:
            n += 1
    return n


# ---------------------------------------------------------------------------
# CRUD-операции (администратор: глобальные; руководитель: свои/копии)
# ---------------------------------------------------------------------------

def _norm_key(label: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "", label.lower().replace(" ", "_"))[:40] or uuid.uuid4().hex[:8]


async def _unique_status_key(session: AsyncSession, kind, dept_id, base: str) -> str:
    """Уникальный key статуса внутри (kind, department_id): суффикс при коллизии
    с уже существующим ключом (например, два разных кириллических названия,
    дающих одинаковый пустой/совпадающий нормализованный key)."""
    key = base
    n = 2
    while True:
        exists = await session.scalar(select(StatusSet.id).where(
            StatusSet.kind == kind, StatusSet.key == key,
            StatusSet.department_id.is_(None) if dept_id is None
            else StatusSet.department_id == dept_id))
        if not exists:
            return key
        key = f"{base[:36]}_{n}"
        n += 1


async def get_or_404(session: AsyncSession, model, row_id: int):
    row = await session.get(model, row_id)
    if row is None:
        raise RefDataError(404, "Элемент справочника не найден")
    return row


async def check_write_scope(user: User, row, dept_id: Optional[int]):
    """Разграничение: руководитель правит только записи своего подразделения."""
    if user.role in (Role.ADMIN, Role.COORDINATOR):
        return
    # страховка: запись глобального шаблона редактирует/удаляет только админ
    if row.department_id is None:
        raise RefDataError(403, "Глобальный шаблон администратора: можно только "
                                "активировать/деактивировать его копию в своём подразделении")
    if row.department_id != dept_id:
        raise RefDataError(403, "Нет доступа к справочнику другого подразделения")


# ---- Статусы --------------------------------------------------------------

async def resolve_write_dept(session: AsyncSession, user: User, ref: str,
                             dept_id: Optional[int],
                             set_name: Optional[str] = None) -> Optional[int]:
    """Единая точка определения области записи справочника (унификация логики).

    - Администратор/координатор: dept_id как передан (None = общесистемный шаблон,
      конкретный id = запись в выбранном пространстве);
    - Руководитель: только своё подразделение (пустой выбор = возглавляемое).
    Возвращает итоговый department_id для create-функций.
    """
    if user.role in (Role.ADMIN, Role.COORDINATOR):
        return dept_id
    own = await _head_dept(session, user, None)
    if dept_id is None:
        dept_id = own
    elif own is None or dept_id != own:
        raise RefDataError(403, "Можно создавать записи только в своём подразделении")
    if dept_id is None:
        raise RefDataError(403, "Выберите подразделение: руководитель создаёт записи "
                                "только в возглавляемом пространстве")
    return dept_id


async def create_status(session: AsyncSession, user: User, *, set_name: str, label: str,
                        color: str, order: int, dept_id: Optional[int]) -> StatusSet:
    kind = KIND_BY_SET.get(set_name)
    if kind is None:
        raise RefDataError(400, f"Неизвестный набор статусов: {set_name}")
    if user.role not in (Role.ADMIN, Role.COORDINATOR):
        # руководитель без явного department_id — создаёт в возглавляемом им
        # подразделении; чужое подразделение запрещено (проверка ниже)
        own = await _head_dept(session, user, None)
        if dept_id is None:
            dept_id = own
        elif dept_id != own:
            raise RefDataError(403, "Руководитель создаёт записи только в своём подразделении")
        if dept_id is None:
            raise RefDataError(403, "Руководитель создаёт статусы только в своём подразделении")
    if color not in COLORS:
        color = "b-gray"
    key = _norm_key(label)
    exists = await session.scalar(select(StatusSet).where(
        StatusSet.kind == kind, StatusSet.key == key,
        StatusSet.department_id.is_(None) if dept_id is None
        else StatusSet.department_id == dept_id))
    if exists:
        raise RefDataError(409, "Статус с таким названием уже есть")
    # кириллические названия дают пустой/коллизионный key — гарантируем уникальность
    key = await _unique_status_key(session, kind, dept_id, key)
    row = StatusSet(kind=kind, key=key, label=label.strip(), color=color,
                    order=order, department_id=dept_id, is_active=True)
    session.add(row)
    await log_event(session, "refdata_status_create", category="admin", user=user,
                    object=f"{set_name}:{label}",
                    message=f"Создан статус «{label}» ({'глобально' if dept_id is None else f'подразделение #{dept_id}'})")
    await session.commit()
    return row


async def patch_status(session: AsyncSession, user: User, row_id: int, changes: dict,
                       dept_id: Optional[int]) -> StatusSet:
    row = await get_or_404(session, StatusSet, row_id)
    await check_write_scope(user, row, dept_id)
    for f in ("label", "color", "order"):
        if f in changes and changes[f] is not None:
            setattr(row, f, changes[f])
    await log_event(session, "refdata_status_update", category="admin", user=user,
                    object=f"{SET_NAME_BY_KIND.get(row.kind, '')}:{row.label}",
                    message=f"Обновлён статус «{row.label}»")
    await session.commit()
    return row


async def delete_status(session: AsyncSession, user: User, row_id: int,
                        dept_id: Optional[int]) -> None:
    row = await get_or_404(session, StatusSet, row_id)
    await check_write_scope(user, row, dept_id)
    used = await entity_usage(session, kind=row.kind, status_key=row.key,
                              dept_ids=[row.department_id] if row.department_id else None)
    if used:
        raise RefDataError(400, f"Нельзя удалить: статус используется в объектах ({used})")
    # каскад: при удалении глобального шаблона снимаем активированные им копии
    if row.department_id is None:
        await session.execute(
            update(StatusSet).where(StatusSet.source_global_id == row.id)
            .values(is_active=False))
    await session.delete(row)
    await log_event(session, "refdata_status_delete", category="admin", user=user,
                    object=f"{SET_NAME_BY_KIND.get(row.kind, '')}:{row.label}",
                    message=f"Удалён статус «{row.label}»")
    await session.commit()


# ---- Приоритеты -----------------------------------------------------------

async def _head_write_dept(session: AsyncSession, user: User,
                           dept_id: Optional[int]) -> int:
    """Подразделение для создания записи руководителем: явно переданный свой
    (иначе 403) либо возглавляемое по умолчанию."""
    own = await _head_dept(session, user, None)
    if dept_id is None:
        dept_id = own
    elif own is None or dept_id != own:
        raise RefDataError(403, "Можно создавать записи только в своём подразделении")
    if dept_id is None:
        raise RefDataError(403, "Вы не являетесь руководителем подразделения")
    return dept_id


async def create_priority(session: AsyncSession, user: User, *, label: str, color: str,
                          weight: int, dept_id: Optional[int]) -> PriorityLevel:
    if user.role not in (Role.ADMIN, Role.COORDINATOR):
        dept_id = await _head_write_dept(session, user, dept_id)
    if color not in COLORS:
        color = "b-blue"
    key = _norm_key(label)
    # уникальность key — глобальная (Entity.priority_key хранится без привязки к
    # подразделению); при коллизии добавляем суффикс вместо ошибки 409
    base_key, n = key, 1
    while await session.scalar(select(PriorityLevel.id).where(PriorityLevel.key == key)):
        key = f"{base_key}_{n}"
        n += 1
    row = PriorityLevel(key=key, label=label.strip(), color=color, weight=weight,
                        department_id=dept_id, is_active=True)
    session.add(row)
    await log_event(session, "refdata_prio_create", category="admin", user=user,
                    object=label, message=f"Создан приоритет «{label}»")
    await session.commit()
    return row


async def patch_priority(session: AsyncSession, user: User, row_id: int, changes: dict,
                         dept_id: Optional[int]) -> PriorityLevel:
    row = await get_or_404(session, PriorityLevel, row_id)
    await check_write_scope(user, row, dept_id)
    for f in ("label", "color", "weight"):
        if f in changes and changes[f] is not None:
            setattr(row, f, changes[f])
    await log_event(session, "refdata_prio_update", category="admin", user=user,
                    object=row.label, message=f"Обновлён приоритет «{row.label}»")
    await session.commit()
    return row


async def delete_priority(session: AsyncSession, user: User, row_id: int,
                          dept_id: Optional[int]) -> None:
    row = await get_or_404(session, PriorityLevel, row_id)
    await check_write_scope(user, row, dept_id)
    used = await entity_usage(session, kind=None, priority_key=row.key,
                              dept_ids=[row.department_id] if row.department_id else None)
    if used:
        raise RefDataError(400, f"Нельзя удалить: приоритет используется в задачах ({used})")
    if row.department_id is None:
        await session.execute(
            update(PriorityLevel).where(PriorityLevel.source_global_id == row.id)
            .values(is_active=False))
    await session.delete(row)
    await log_event(session, "refdata_prio_delete", category="admin", user=user,
                    object=row.label, message=f"Удалён приоритет «{row.label}»")
    await session.commit()


# ---- Типы (документов / ПО / стипендий) -----------------------------------

async def create_type(session: AsyncSession, user: User, *, ref_kind: str, label: str,
                      dept_id: Optional[int]) -> RefType:
    if ref_kind not in (RefType.KIND_DOC, RefType.KIND_SOFT, RefType.KIND_SCHOL):
        raise RefDataError(400, "Неизвестный вид типа")
    if user.role not in (Role.ADMIN, Role.COORDINATOR):
        dept_id = await _head_write_dept(session, user, dept_id)
    label = label.strip()
    if not label:
        raise RefDataError(400, "Укажите название типа")
    exists = await session.scalar(select(RefType).where(
        RefType.kind == ref_kind, RefType.label == label,
        RefType.department_id.is_(None) if dept_id is None
        else RefType.department_id == dept_id))
    if exists:
        raise RefDataError(409, "Такой тип уже есть")
    row = RefType(kind=ref_kind, label=label, department_id=dept_id, is_active=True)
    session.add(row)
    await log_event(session, "refdata_type_create", category="admin", user=user,
                    object=f"{ref_kind}:{label}", message=f"Создан тип «{label}» ({ref_kind})")
    await session.commit()
    return row


async def patch_type(session: AsyncSession, user: User, row_id: int, label: str,
                     dept_id: Optional[int]) -> RefType:
    row = await get_or_404(session, RefType, row_id)
    await check_write_scope(user, row, dept_id)
    old = row.label
    new = (label or "").strip()
    if not new:
        raise RefDataError(400, "Укажите название типа")
    if new != old:
        # переименование: обновляем использование в payload-записях того же scope
        used = await type_usage(session, row.kind, old,
                                [row.department_id] if row.department_id else None)
        if used:
            raise RefDataError(400,
                f"Нельзя переименовать: тип используется ({used}). "
                "Создайте новый тип и переведите объекты.")
        row.label = new
    await log_event(session, "refdata_type_update", category="admin", user=user,
                    object=f"{row.kind}:{row.label}", message=f"Обновлён тип «{row.label}»")
    await session.commit()
    return row


async def delete_type(session: AsyncSession, user: User, row_id: int,
                      dept_id: Optional[int]) -> None:
    row = await get_or_404(session, RefType, row_id)
    await check_write_scope(user, row, dept_id)
    used = await type_usage(session, row.kind, row.label,
                            [row.department_id] if row.department_id else None)
    if used:
        raise RefDataError(400, f"Нельзя удалить: тип используется в объектах ({used})")
    if row.department_id is None:
        await session.execute(
            update(RefType).where(RefType.source_global_id == row.id)
            .values(is_active=False))
    await session.delete(row)
    await log_event(session, "refdata_type_delete", category="admin", user=user,
                    object=f"{row.kind}:{row.label}", message=f"Удалён тип «{row.label}»")
    await session.commit()


# ---- Маршруты (wfTemplates) ------------------------------------------------

async def create_template(session: AsyncSession, user: User, *, name: str, steps: list,
                          kind_scope: Optional[str], dept_id: Optional[int]) -> WfTemplate:
    if user.role not in (Role.ADMIN, Role.COORDINATOR):
        dept_id = await _head_write_dept(session, user, dept_id)
    name = (name or "").strip()
    steps = [str(s).strip() for s in (steps or []) if str(s).strip()]
    if not name or len(steps) < 2:
        raise RefDataError(400, "Укажите название маршрута и минимум два шага")
    row = WfTemplate(name=name, steps=steps, kind_scope=kind_scope or None,
                     department_id=dept_id, is_active=True)
    session.add(row)
    await log_event(session, "refdata_wf_create", category="admin", user=user,
                    object=name, message=f"Создан маршрут «{name}» ({len(steps)} шагов)")
    await session.commit()
    return row


async def patch_template(session: AsyncSession, user: User, row_id: int, changes: dict,
                         dept_id: Optional[int]) -> WfTemplate:
    row = await get_or_404(session, WfTemplate, row_id)
    await check_write_scope(user, row, dept_id)
    if "name" in changes and changes["name"]:
        row.name = str(changes["name"]).strip()
    if "steps" in changes:
        steps = [str(s).strip() for s in (changes["steps"] or []) if str(s).strip()]
        if len(steps) >= 2:
            row.steps = steps
    if "kind_scope" in changes:
        row.kind_scope = changes["kind_scope"] or None
    await log_event(session, "refdata_wf_update", category="admin", user=user,
                    object=row.name, message=f"Обновлён маршрут «{row.name}»")
    await session.commit()
    return row


async def delete_template(session: AsyncSession, user: User, row_id: int,
                          dept_id: Optional[int]) -> None:
    row = await get_or_404(session, WfTemplate, row_id)
    await check_write_scope(user, row, dept_id)
    used = await template_usage(session, row.name,
                                [row.department_id] if row.department_id else None)
    if used:
        raise RefDataError(400, f"Нельзя удалить: маршрут используется объектами ({used})")
    if row.department_id is None:
        await session.execute(
            update(WfTemplate).where(WfTemplate.source_global_id == row.id)
            .values(is_active=False))
    await session.delete(row)
    await log_event(session, "refdata_wf_delete", category="admin", user=user,
                    object=row.name, message=f"Удалён маршрут «{row.name}»")
    await session.commit()


# ---- Группы задач для совещаний (taskGroups) --------------------------------

async def create_group(session: AsyncSession, user: User, *, name: str, criteria: dict,
                       dept_id: Optional[int]) -> TaskGroup:
    if user.role not in (Role.ADMIN, Role.COORDINATOR):
        dept_id = await _head_write_dept(session, user, dept_id)
    name = (name or "").strip()
    if not name:
        raise RefDataError(400, "Укажите название группы")
    from app.core.meet_groups import norm_criteria
    row = TaskGroup(name=name, criteria=norm_criteria(criteria), department_id=dept_id,
                    is_active=True)
    session.add(row)
    await log_event(session, "refdata_group_create", category="admin", user=user,
                    object=name, message=f"Создана группа задач «{name}»")
    await session.commit()
    return row


async def patch_group(session: AsyncSession, user: User, row_id: int, changes: dict,
                      dept_id: Optional[int]) -> TaskGroup:
    row = await get_or_404(session, TaskGroup, row_id)
    await check_write_scope(user, row, dept_id)
    if changes.get("name"):
        row.name = str(changes["name"]).strip()
    if "criteria" in changes and isinstance(changes["criteria"], dict):
        from app.core.meet_groups import norm_criteria
        row.criteria = norm_criteria(changes["criteria"])
    await log_event(session, "refdata_group_update", category="admin", user=user,
                    object=row.name, message=f"Обновлена группа задач «{row.name}»")
    await session.commit()
    return row


async def delete_group(session: AsyncSession, user: User, row_id: int,
                       dept_id: Optional[int]) -> None:
    row = await get_or_404(session, TaskGroup, row_id)
    await check_write_scope(user, row, dept_id)
    if row.department_id is None:
        await session.execute(
            update(TaskGroup).where(TaskGroup.source_global_id == row.id)
            .values(is_active=False))
    await session.delete(row)
    await log_event(session, "refdata_group_delete", category="admin", user=user,
                    object=row.name, message=f"Удалена группа задач «{row.name}»")
    await session.commit()


# ---------------------------------------------------------------------------
# Активация / деактивация глобальных шаблонов руководителем подразделения
# ---------------------------------------------------------------------------

_MODEL_BY_REF = {
    "status": StatusSet, "prio": PriorityLevel, "type": RefType,
    "wf": WfTemplate, "groups": TaskGroup,
}


async def activate_template(session: AsyncSession, user: User, ref: str, global_id: int,
                            dept_id: int):
    """Руководитель активирует глобальный шаблон администратора в своём
    подразделении: создаётся копия (source_global_id=global_id). Если копия уже
    существовала и была деактивирована — восстанавливается та же запись."""
    model = _MODEL_BY_REF.get(ref)
    if model is None:
        raise RefDataError(400, "Неизвестный вид справочника")
    glob = await get_or_404(session, model, global_id)
    if glob.department_id is not None:
        raise RefDataError(400, "Активировать можно только глобальный шаблон администратора")
    # ключевое поле для матчинга копии
    if model is StatusSet:
        match_cond = (model.kind == glob.kind, model.key == glob.key)
    elif model is PriorityLevel:
        match_cond = (model.key == glob.key,)
    elif model is RefType:
        match_cond = (model.kind == glob.kind, model.label == glob.label)
    elif model is TaskGroup:
        match_cond = (model.name == glob.name,)
    else:
        match_cond = (model.name == glob.name,)
    existing = await session.scalar(
        select(model).where(model.department_id == dept_id, *match_cond))
    if existing is not None:
        existing.is_active = True
        existing.source_global_id = glob.id
        row = existing
        action = "refdata_template_reactivate"
    else:
        fields = {c.key: getattr(glob, c.key) for c in model.__table__.columns
                  if c.key not in ("id", "created_at", "updated_at")}
        fields.update(department_id=dept_id, source_global_id=glob.id, is_active=True)
        row = model(**fields)
        session.add(row)
        action = "refdata_template_activate"
    await log_event(session, action, category="admin", user=user,
                    object=f"{ref}:#{global_id}",
                    message=f"Шаблон «{_tpl_name(glob)}» активирован в подразделении #{dept_id}")
    await session.commit()
    return row


async def deactivate_template(session: AsyncSession, user: User, ref: str, copy_id: int,
                              dept_id: int):
    """Руководитель деактивирует применённый у него шаблон администратора:
    копия остаётся в БД с is_active=False (можно включить обратно в любой момент)."""
    model = _MODEL_BY_REF.get(ref)
    if model is None:
        raise RefDataError(400, "Неизвестный вид справочника")
    row = await get_or_404(session, model, copy_id)
    if row.department_id != dept_id:
        raise RefDataError(403, "Деактивировать можно только копию шаблона в своём подразделении")
    if user.role not in (Role.ADMIN, Role.COORDINATOR) and row.source_global_id is None:
        raise RefDataError(403, "Собственный элемент подразделения удаляется, а не деактивируется")
    row.is_active = False
    await log_event(session, "refdata_template_deactivate", category="admin", user=user,
                    object=f"{ref}:#{copy_id}",
                    message=f"Шаблон «{_tpl_name(row)}» деактивирован в подразделении #{dept_id}")
    await session.commit()
    return row


def _tpl_name(row) -> str:
    return getattr(row, "label", None) or getattr(row, "name", None) or \
        getattr(row, "key", "?")


# ---------------------------------------------------------------------------
# Агрегатор вкладок: все разделы справочников за один запрос (для UI)
# ---------------------------------------------------------------------------

async def sections_view(session: AsyncSession, user: User,
                        dept_id: Optional[int]) -> dict:
    """Одна выгрузка для вкладки «Справочники» админ-панели: статусы по всем
    наборам, приоритеты, типы (doc/soft/schol), маршруты и группы задач —
    каждый раздел уже ограничен включёнными модулями подразделения
    (преемственность ТЗ: шаблоны применяются согласно подключённым модулям)."""
    mods = await enabled_modules(session, dept_id)
    out: dict = {"department_id": dept_id, "modules_enabled": sorted(mods),
                 "colors": COLORS, "status_sets": {}, "types": {},
                 "prio": [], "wf": [], "groups": []}
    # В режиме «Вся система» (dept_id=None у админа/координатора) показываем ВСЕ
    # разделы независимо от модулей: в агрегате есть записи всех пространств,
    # где любой подключённый модуль имеет место быть.
    for set_name in KIND_BY_SET:
        if _module_allowed(f"status:{set_name}", mods) or dept_id is None:
            v = await admin_view(session, user, "status", dept_id, set_name)
            out["status_sets"][set_name] = v
    v = await admin_view(session, user, "prio", dept_id)
    out["prio"] = v
    for rk in (RefType.KIND_DOC, RefType.KIND_SOFT, RefType.KIND_SCHOL):
        if _module_allowed(f"type:{rk}", mods) or dept_id is None:
            out["types"][rk] = await admin_view(session, user, "type", dept_id, rk)
    if _module_allowed("wf", mods) or dept_id is None:
        out["wf"] = await admin_view(session, user, "wf", dept_id)
    if _module_allowed("groups", mods) or dept_id is None:
        out["groups"] = await admin_view(session, user, "groups", dept_id)
    return out


# ---------------------------------------------------------------------------
# Агрегатор для админ-панели: всё дерево справочника с флагами происхождения
# ---------------------------------------------------------------------------

async def admin_view(session: AsyncSession, user: User, ref: str,
                     dept_id: Optional[int], set_name: Optional[str] = None) -> dict:
    """Полная картина для вкладки администрирования:

    - items: записи в области действия (глобальные для админа; глобальные+свои
      для руководителя конкретного подразделения);
    - available_templates: глобальные шаблоны администратора, которые руководитель
      может активировать (с пометкой, активированы ли уже и не деактивированы ли).
    """
    mods = await enabled_modules(session, dept_id)
    out_items, avail = [], []
    # «Вся система» для администратора/координатора: в items попадают и
    # глобальные шаблоны, и записи всех подразделений (полное администрирование).
    view_all = dept_id is None and user.role in (Role.ADMIN, Role.COORDINATOR)

    if ref == "status":
        kind = KIND_BY_SET.get(set_name or "task")
        if kind is None:
            raise RefDataError(400, "Неизвестный набор статусов")
        rows = list((await session.scalars(
            select(StatusSet).where(StatusSet.kind == kind)
            .order_by(StatusSet.department_id.is_not(None), StatusSet.order))).all())
        visible = [r for r in rows
                   if r.department_id is None or (dept_id is not None and r.department_id == dept_id)
                   or view_all]
        for r in visible:
            used = await entity_usage(session, kind=r.kind, status_key=r.key,
                                      dept_ids=[r.department_id] if r.department_id else None)
            item = {"id": r.id, "key": r.key, "label": r.label, "color": r.color,
                    "order": r.order, "department_id": r.department_id,
                    "source_global_id": r.source_global_id, "is_active": r.is_active,
                    "usage": used}
            if view_all and r.department_id is not None:
                d = await session.get(Department, r.department_id)
                item["department_name"] = d.name if d else f"№{r.department_id}"
            out_items.append(item)
        if dept_id is not None:
            activated = {r.source_global_id for r in rows
                         if r.department_id == dept_id and r.source_global_id}
            deactivated = {r.source_global_id for r in rows
                           if r.department_id == dept_id and r.source_global_id
                           and not r.is_active}
            own_copy_id = {r.source_global_id: r.id for r in rows
                           if r.department_id == dept_id and r.source_global_id}
            for g in rows:
                if g.department_id is None:
                    avail.append({"id": g.id, "label": g.label, "color": g.color,
                                  "activated": g.id in activated,
                                  "deactivated": g.id in deactivated,
                                  "copy_id": own_copy_id.get(g.id)})
    elif ref == "prio":
        rows = list((await session.scalars(
            select(PriorityLevel).order_by(PriorityLevel.department_id.is_not(None),
                                           PriorityLevel.weight.desc()))).all())
        for r in rows:
            if r.department_id is not None and dept_id is not None and r.department_id != dept_id \
                    and not view_all:
                continue
            used = await entity_usage(session, kind=None, priority_key=r.key,
                                      dept_ids=[r.department_id] if r.department_id else None)
            item = {"id": r.id, "key": r.key, "label": r.label, "color": r.color,
                    "weight": r.weight, "department_id": r.department_id,
                    "source_global_id": r.source_global_id,
                    "is_active": r.is_active, "usage": used}
            if view_all and r.department_id is not None:
                d = await session.get(Department, r.department_id)
                item["department_name"] = d.name if d else f"№{r.department_id}"
            out_items.append(item)
        if dept_id is not None:
            copies = {r.source_global_id: r for r in rows
                      if r.department_id == dept_id and r.source_global_id}
            for g in rows:
                if g.department_id is None:
                    c = copies.get(g.id)
                    avail.append({"id": g.id, "label": g.label, "color": g.color,
                                  "activated": bool(c and c.is_active),
                                  "deactivated": bool(c and not c.is_active),
                                  "copy_id": c.id if c else None})
    elif ref == "type":
        rk = set_name if set_name in (RefType.KIND_DOC, RefType.KIND_SOFT,
                                      RefType.KIND_SCHOL) else RefType.KIND_DOC
        rows = list((await session.scalars(
            select(RefType).where(RefType.kind == rk)
            .order_by(RefType.department_id.is_not(None), RefType.label))).all())
        for r in rows:
            if r.department_id is not None and dept_id is not None and r.department_id != dept_id \
                    and not view_all:
                continue
            used = await type_usage(session, rk, r.label,
                                    [r.department_id] if r.department_id else None)
            item = {"id": r.id, "label": r.label, "kind": rk,
                    "department_id": r.department_id,
                    "source_global_id": r.source_global_id,
                    "is_active": r.is_active, "usage": used}
            if view_all and r.department_id is not None:
                d = await session.get(Department, r.department_id)
                item["department_name"] = d.name if d else f"№{r.department_id}"
            out_items.append(item)
        if dept_id is not None:
            copies = {r.source_global_id: r for r in rows
                      if r.department_id == dept_id and r.source_global_id}
            for g in rows:
                if g.department_id is None:
                    c = copies.get(g.id)
                    avail.append({"id": g.id, "label": g.label,
                                  "activated": bool(c and c.is_active),
                                  "deactivated": bool(c and not c.is_active),
                                  "copy_id": c.id if c else None})
    elif ref == "wf":
        rows = list((await session.scalars(
            select(WfTemplate).order_by(WfTemplate.department_id.is_not(None),
                                        WfTemplate.id))).all())
        for r in rows:
            if r.department_id is not None and dept_id is not None and r.department_id != dept_id \
                    and not view_all:
                continue
            used = await template_usage(session, r.name,
                                         [r.department_id] if r.department_id else None)
            item = {"id": r.id, "name": r.name, "steps": r.steps or [],
                    "kind_scope": r.kind_scope, "department_id": r.department_id,
                    "source_global_id": r.source_global_id,
                    "is_active": r.is_active, "usage": used}
            if view_all and r.department_id is not None:
                d = await session.get(Department, r.department_id)
                item["department_name"] = d.name if d else f"№{r.department_id}"
            out_items.append(item)
        if dept_id is not None:
            copies = {r.source_global_id: r for r in rows
                      if r.department_id == dept_id and r.source_global_id}
            for g in rows:
                if g.department_id is None:
                    c = copies.get(g.id)
                    avail.append({"id": g.id, "label": g.name,
                                  "activated": bool(c and c.is_active),
                                  "deactivated": bool(c and not c.is_active),
                                  "copy_id": c.id if c else None})
    elif ref == "groups":
        from app.core.meet_groups import (count_matches_for_groups, group_options,
                                          norm_criteria, _group_desc as _mg_desc)
        rows = list((await session.scalars(
            select(TaskGroup).order_by(TaskGroup.department_id.is_not(None),
                                       TaskGroup.name))).all())
        # соответствия критериям группы (для столбца «Параметры» и конструктора)
        match_counts: dict[int, int] = {}
        try:
            match_counts = await count_matches_for_groups(session, dept_id, rows)
        except Exception:   # pragma: no cover — не блокируем справочник при сбое подсчёта
            match_counts = {}
        opts: dict = {}
        try:
            opts = await group_options(session, dept_id)
        except Exception:   # pragma: no cover
            opts = {}
        proj_names = {p["id"]: p["label"] for p in opts.get("projects", [])}
        status_labels = {s["key"]: s["label"] for s in opts.get("statuses", [])}
        prio_labels = {p["key"]: p["label"] for p in opts.get("priorities", [])}
        for r in rows:
            if r.department_id is not None and dept_id is not None and r.department_id != dept_id \
                    and not view_all:
                continue
            crit = norm_criteria(r.criteria)
            item = {"id": r.id, "name": r.name, "criteria": crit,
                    "description": _mg_desc(crit, proj_names, status_labels, prio_labels),
                    "matches": match_counts.get(r.id, 0),
                    "department_id": r.department_id,
                    "source_global_id": r.source_global_id,
                    "is_active": r.is_active, "usage": 0}
            if view_all and r.department_id is not None:
                d = await session.get(Department, r.department_id)
                item["department_name"] = d.name if d else f"№{r.department_id}"
            out_items.append(item)
        if dept_id is not None:
            copies = {r.source_global_id: r for r in rows
                      if r.department_id == dept_id and r.source_global_id}
            for g in rows:
                if g.department_id is None:
                    c = copies.get(g.id)
                    avail.append({"id": g.id, "label": g.name,
                                  "activated": bool(c and c.is_active),
                                  "deactivated": bool(c and not c.is_active),
                                  "copy_id": c.id if c else None})
    else:
        raise RefDataError(400, "Неизвестный раздел справочника")
    return {"ref": ref, "set": set_name, "department_id": dept_id,
            "items": out_items, "available_templates": avail,
            "group_options": opts if ref == "groups" else None,
            "modules_enabled": sorted(mods),
            "allowed": _module_allowed(
                ref if ref != "status" else f"status:{set_name or 'task'}", mods)
            if dept_id is not None else True,
            "colors": COLORS}
