"""
REST-API справочников (ядро): статусы, приоритеты, типы, маршруты, группы задач.

Prefix: /api/core/refdata  (подключается в app/main.py рядом с остальным core-API).

Разграничение прав (ролевая модель ТЗ):
  - Администратор/Координатор — управляет ГЛОБАЛЬНЫМИ шаблонами (department_id=NULL),
    которые применяются ко всем подразделениям с подключённым соответствующим
    модулем; может работать и в контексте конкретного пространства (?department_id=);
  - Руководитель (HEAD) — управляет справочниками СВОЕГО подразделения: создаёт
    собственные элементы, редактирует/удаляет свои копии и активированные
    шаблоны администратора; глобальные шаблоны видит в разделе
    «Доступно для активации» и включает/выключает их для своего пространства;
  - Участник — только чтение эффективных наборов (GET list), мутации запрещены.

Эффективные наборы (то, что реально применяют модули при отображении форм)
отданы через refdata.effective_* — единый источник истины для UI и проверок.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.core import access, refdata
from app.core.refdata import RefDataError, resolve_dept
from app.core.security import get_current_user
from app.deps import session_dep
from app.models.core import Role, User
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()


class StatusIn(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    color: str = "b-gray"
    order: int = 0
    set: str = "task"                       # task|project|doc|soft|grant|schol|priority|meet
    department_id: Optional[int] = None     # null -> глобальный шаблон (админ)


class PrioIn(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    color: str = "b-blue"
    weight: int = 10
    department_id: Optional[int] = None


class TypeIn(BaseModel):
    label: str = Field(min_length=1, max_length=200)
    kind: str                               # doc_type | soft_type | schol_type
    department_id: Optional[int] = None


class WfIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    steps: list[str] = []
    kind_scope: Optional[str] = None
    department_id: Optional[int] = None


class GroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    criteria: dict = {}
    department_id: Optional[int] = None


class ActivateIn(BaseModel):
    global_id: int
    department_id: int


def _scope(user: User, dept_id: Optional[int]) -> Optional[int]:
    """Определяет контекст подразделения для запроса.

    Админ/координатор: переданный department_id (или None = глобальный реестр).
    Руководитель: всегда своё подразделение (проверка в resolve_dept асинхронная —
    здесь лишь грубая фильтрация роли; точная — в эндпоинтах).
    """
    return dept_id


@router.get("/view")
async def view(ref: str = Query(..., description="status|prio|type|wf|groups"),
               set: Optional[str] = None, department_id: Optional[int] = None,
               user: User = Depends(get_current_user),
               session: AsyncSession = Depends(session_dep)):
    """Дерево справочника + доступные для активации шаблоны (для вкладок админки)."""
    ctx = await access.build_context(session, user)
    if not ctx.is_global and department_id is None:
        department_id = next(iter(sorted(ctx.department_ids)), None) if ctx.department_ids else None
        if department_id is None:
            raise RefDataError(403, "Вы не состоите ни в одном подразделении")
    if not ctx.is_global:
        # руководитель/участник видят только своё пространство (несколько — первое
        # из выбранных; переключение пространства идёт через selector админ-панели)
        if department_id not in ctx.department_ids:
            raise RefDataError(403, "Нет доступа к этому подразделению")
    return await refdata.admin_view(session, user, ref, department_id, set)


@router.get("/sections")
async def sections(department_id: Optional[int] = None,
                   user: User = Depends(get_current_user),
                   session: AsyncSession = Depends(session_dep)):
    """Все разделы справочников одной выгрузкой (вкладка «Справочники»):
    статусы по наборам, приоритеты, типы, маршруты, группы задач. Разделы
    автоматически ограничены модулями, подключёнными к подразделению."""
    ctx = await access.build_context(session, user)
    if not ctx.is_global and department_id is None:
        department_id = next(iter(sorted(ctx.department_ids)), None) if ctx.department_ids else None
        if department_id is None:
            raise RefDataError(403, "Вы не состоите ни в одном подразделении")
    if not ctx.is_global and department_id not in ctx.department_ids:
        raise RefDataError(403, "Нет доступа к этому подразделению")
    return await refdata.sections_view(session, user, department_id)


@router.get("/effective")
async def effective(kind: str = "task", department_id: Optional[int] = None,
                    user: User = Depends(get_current_user),
                    session: AsyncSession = Depends(session_dep)):
    """Эффективные справочники для формы создания сущности (используются модулями)."""
    ctx = await access.build_context(session, user)
    if department_id is None:
        department_id = next(iter(sorted(ctx.department_ids)), None) if ctx.department_ids else None
    elif not ctx.is_global and department_id not in ctx.department_ids:
        raise RefDataError(403, "Нет доступа к этому подразделению")
    mods = await refdata.enabled_modules(session, department_id)
    out: dict = {"statuses": [], "priorities": [], "types": [], "templates": [],
                 "groups": [], "department_id": department_id}
    ek = refdata.KIND_BY_SET.get(kind)
    if ek is not None:
        rows = await refdata.effective_statuses(session, ek, department_id, mods)
        out["statuses"] = [{"key": r.key, "label": r.label, "color": r.color} for r in rows]
    prows = await refdata.effective_priorities(session, department_id, mods)
    out["priorities"] = [{"key": r.key, "label": r.label, "color": r.color,
                          "weight": r.weight} for r in prows]
    for rk in (refdata.RefType.KIND_DOC, refdata.RefType.KIND_SOFT, refdata.RefType.KIND_SCHOL):
        trows = await refdata.effective_types(session, rk, department_id, mods)
        out["types"].extend({"kind": rk, "label": t.label} for t in trows)
    wrows = await refdata.effective_templates(session, department_id, mods)
    out["templates"] = [{"id": w.id, "name": w.name, "steps": w.steps or [],
                         "kind_scope": w.kind_scope} for w in wrows]
    grows = await refdata.effective_groups(session, department_id, mods)
    out["groups"] = [{"id": g.id, "name": g.name, "criteria": g.criteria or {}} for g in grows]
    return out


# ---------------- Статусы ---------------------------------------------------

@router.post("/status")
async def create_status(body: StatusIn, user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(session_dep)):
    if user.role == Role.MEMBER:
        raise RefDataError(403, "Справочники ведут администратор и руководитель")
    dept = None if user.role in (Role.ADMIN, Role.COORDINATOR) \
        else await resolve_dept(session, user, body.department_id)
    row = await refdata.create_status(session, user, set_name=body.set, label=body.label,
                                      color=body.color, order=body.order, dept_id=dept)
    return {"id": row.id, "key": row.key, "ok": True}


@router.patch("/status/{row_id}")
async def patch_status(row_id: int, body: dict, user: User = Depends(get_current_user),
                       session: AsyncSession = Depends(session_dep)):
    if user.role == Role.MEMBER:
        raise RefDataError(403, "Только администратор/руководитель")
    dept = None if user.role in (Role.ADMIN, Role.COORDINATOR) \
        else await resolve_dept(session, user, body.get("department_id"))
    row = await refdata.patch_status(session, user, row_id, body, dept)
    return {"id": row.id, "ok": True}


@router.delete("/status/{row_id}")
async def delete_status(row_id: int, department_id: Optional[int] = None,
                        user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(session_dep)):
    if user.role == Role.MEMBER:
        raise RefDataError(403, "Только администратор/руководитель")
    dept = None if user.role in (Role.ADMIN, Role.COORDINATOR) \
        else await resolve_dept(session, user, department_id)
    await refdata.delete_status(session, user, row_id, dept)
    return {"ok": True}


# ---------------- Приоритеты -------------------------------------------------

@router.post("/prio")
async def create_prio(body: PrioIn, user: User = Depends(get_current_user),
                      session: AsyncSession = Depends(session_dep)):
    if user.role == Role.MEMBER:
        raise RefDataError(403, "Только администратор/руководитель")
    dept = None if user.role in (Role.ADMIN, Role.COORDINATOR) \
        else await resolve_dept(session, user, body.department_id)
    row = await refdata.create_priority(session, user, label=body.label, color=body.color,
                                       weight=body.weight, dept_id=dept)
    return {"id": row.id, "key": row.key, "ok": True}


@router.patch("/prio/{row_id}")
async def patch_prio(row_id: int, body: dict, user: User = Depends(get_current_user),
                     session: AsyncSession = Depends(session_dep)):
    if user.role == Role.MEMBER:
        raise RefDataError(403, "Только администратор/руководитель")
    dept = None if user.role in (Role.ADMIN, Role.COORDINATOR) \
        else await resolve_dept(session, user, body.get("department_id"))
    row = await refdata.patch_priority(session, user, row_id, body, dept)
    return {"id": row.id, "ok": True}


@router.delete("/prio/{row_id}")
async def delete_prio(row_id: int, department_id: Optional[int] = None,
                      user: User = Depends(get_current_user),
                      session: AsyncSession = Depends(session_dep)):
    if user.role == Role.MEMBER:
        raise RefDataError(403, "Только администратор/руководитель")
    dept = None if user.role in (Role.ADMIN, Role.COORDINATOR) \
        else await resolve_dept(session, user, department_id)
    await refdata.delete_priority(session, user, row_id, dept)
    return {"ok": True}


# ---------------- Типы --------------------------------------------------------

@router.post("/type")
async def create_type(body: TypeIn, user: User = Depends(get_current_user),
                      session: AsyncSession = Depends(session_dep)):
    if user.role == Role.MEMBER:
        raise RefDataError(403, "Только администратор/руководитель")
    dept = None if user.role in (Role.ADMIN, Role.COORDINATOR) \
        else await resolve_dept(session, user, body.department_id)
    row = await refdata.create_type(session, user, ref_kind=body.kind, label=body.label,
                                    dept_id=dept)
    return {"id": row.id, "ok": True}


@router.patch("/type/{row_id}")
async def patch_type(row_id: int, body: dict, user: User = Depends(get_current_user),
                     session: AsyncSession = Depends(session_dep)):
    if user.role == Role.MEMBER:
        raise RefDataError(403, "Только администратор/руководитель")
    dept = None if user.role in (Role.ADMIN, Role.COORDINATOR) \
        else await resolve_dept(session, user, body.get("department_id"))
    row = await refdata.patch_type(session, user, row_id, body.get("label", ""), dept)
    return {"id": row.id, "ok": True}


@router.delete("/type/{row_id}")
async def delete_type(row_id: int, department_id: Optional[int] = None,
                      user: User = Depends(get_current_user),
                      session: AsyncSession = Depends(session_dep)):
    if user.role == Role.MEMBER:
        raise RefDataError(403, "Только администратор/руководитель")
    dept = None if user.role in (Role.ADMIN, Role.COORDINATOR) \
        else await resolve_dept(session, user, department_id)
    await refdata.delete_type(session, user, row_id, dept)
    return {"ok": True}


# ---------------- Маршруты (wf) -----------------------------------------------

@router.post("/wf")
async def create_wf(body: WfIn, user: User = Depends(get_current_user),
                    session: AsyncSession = Depends(session_dep)):
    if user.role == Role.MEMBER:
        raise RefDataError(403, "Только администратор/руководитель")
    dept = None if user.role in (Role.ADMIN, Role.COORDINATOR) \
        else await resolve_dept(session, user, body.department_id)
    row = await refdata.create_template(session, user, name=body.name, steps=body.steps,
                                        kind_scope=body.kind_scope, dept_id=dept)
    return {"id": row.id, "ok": True}


@router.patch("/wf/{row_id}")
async def patch_wf(row_id: int, body: dict, user: User = Depends(get_current_user),
                   session: AsyncSession = Depends(session_dep)):
    if user.role == Role.MEMBER:
        raise RefDataError(403, "Только администратор/руководитель")
    dept = None if user.role in (Role.ADMIN, Role.COORDINATOR) \
        else await resolve_dept(session, user, body.get("department_id"))
    row = await refdata.patch_template(session, user, row_id, body, dept)
    return {"id": row.id, "ok": True}


@router.delete("/wf/{row_id}")
async def delete_wf(row_id: int, department_id: Optional[int] = None,
                    user: User = Depends(get_current_user),
                    session: AsyncSession = Depends(session_dep)):
    if user.role == Role.MEMBER:
        raise RefDataError(403, "Только администратор/руководитель")
    dept = None if user.role in (Role.ADMIN, Role.COORDINATOR) \
        else await resolve_dept(session, user, department_id)
    await refdata.delete_template(session, user, row_id, dept)
    return {"ok": True}


# ---------------- Группы задач -------------------------------------------------

@router.post("/groups")
async def create_group(body: GroupIn, user: User = Depends(get_current_user),
                       session: AsyncSession = Depends(session_dep)):
    if user.role == Role.MEMBER:
        raise RefDataError(403, "Только администратор/руководитель")
    dept = None if user.role in (Role.ADMIN, Role.COORDINATOR) \
        else await resolve_dept(session, user, body.department_id)
    row = await refdata.create_group(session, user, name=body.name, criteria=body.criteria,
                                     dept_id=dept)
    return {"id": row.id, "ok": True}


@router.patch("/groups/{row_id}")
async def patch_group(row_id: int, body: dict, user: User = Depends(get_current_user),
                      session: AsyncSession = Depends(session_dep)):
    if user.role == Role.MEMBER:
        raise RefDataError(403, "Только администратор/руководитель")
    dept = None if user.role in (Role.ADMIN, Role.COORDINATOR) \
        else await resolve_dept(session, user, body.get("department_id"))
    row = await refdata.patch_group(session, user, row_id, body, dept)
    return {"id": row.id, "ok": True}


@router.delete("/groups/{row_id}")
async def delete_group(row_id: int, department_id: Optional[int] = None,
                       user: User = Depends(get_current_user),
                       session: AsyncSession = Depends(session_dep)):
    if user.role == Role.MEMBER:
        raise RefDataError(403, "Только администратор/руководитель")
    dept = None if user.role in (Role.ADMIN, Role.COORDINATOR) \
        else await resolve_dept(session, user, department_id)
    await refdata.delete_group(session, user, row_id, dept)
    return {"ok": True}


# ---------------- Активация/деактивация шаблонов руководителем ------------------

@router.post("/activate")
async def activate(body: ActivateIn, user: User = Depends(get_current_user),
                   session: AsyncSession = Depends(session_dep)):
    """Включить глобальный шаблон администратора в своём подразделении (копия)."""
    dept = await resolve_dept(session, user, body.department_id)
    # вид справочника определяем по глобальной записи: пробуем каждую модель
    from app.models.core import PriorityLevel, RefType, StatusSet, TaskGroup, WfTemplate
    model_ref = None
    for ref_name, model in (("status", StatusSet), ("prio", PriorityLevel),
                            ("type", RefType), ("wf", WfTemplate), ("groups", TaskGroup)):
        if await session.get(model, body.global_id) is not None:
            model_ref = ref_name
            break
    if model_ref is None:
        raise RefDataError(404, "Шаблон не найден")
    row = await refdata.activate_template(session, user, model_ref, body.global_id, dept)
    return {"ok": True, "copy_id": row.id}


@router.post("/deactivate/{ref}/{copy_id}")
async def deactivate(ref: str, copy_id: int, department_id: int,
                     user: User = Depends(get_current_user),
                     session: AsyncSession = Depends(session_dep)):
    """Выключить применённый шаблон администратора (копия сохраняется —
    преемственность: можно включить обратно)."""
    dept = await resolve_dept(session, user, department_id)
    row = await refdata.deactivate_template(session, user, ref, copy_id, dept)
    return {"ok": True, "is_active": row.is_active}
