"""Модуль-плагин «Документооборот»: REST + HTML (карточки как renderDocs() из прототипа).

Специфика (doc_type/number/issued_at/file_ref) — в DocPayload;
общие поля (title/status/due_date) и права — через ядро (entity_service/access).
Календарь: событие на дату документа (due_date), см. integrations/yandex_calendar.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import access, entity_service, filters, refdata, wf_service
from app.core.security import get_current_user
from app.deps import session_dep
from app.models.core import (
    Department, DepartmentMember, Entity, EntityKind, EntityShare, Permission,
    StatusSet, User,
)
from app.models.module_payloads import DocPayload
from app.ui import render_page

router = APIRouter()

MODULE_KEY = "docs"


async def _has_shares(session: AsyncSession, ent: Entity) -> bool:
    """Признак межпространственности — ЯВНЫЙ SELECT (см. tasks/_has_shares)."""
    return await session.scalar(
        select(EntityShare.id).where(EntityShare.entity_id == ent.id)) is not None


class DocIn(BaseModel):
    title: str = Field(min_length=2, max_length=500)
    status_key: str | None = None            # draft/signed/in_work/archived/canceled
    due_date: str | None = None              # дедлайн документа (поле ядра -> календарь)
    doc_type: str | None = ""                # вид документа (справочник doc_type)
    file_ref: str | None = ""                # ссылка на файл
    description: str | None = ""
    department_id: int | None = None         # для админа/координатора: чьё пространство
    responsible_user_id: int | None = None
    executor_user_ids: list[int] = []        # исполнители (может быть несколько, как в прототипе)
    project_entity_uid: str | None = None    # привязка к проекту (связанность категорий)
    share_with: list[int] = []               # межпространственный контекст: dept_id[]
    stages_json: str | None = None           # JSON [{name, deadline}] — маршрут (wf) документа


async def _load_payload(session: AsyncSession, ent_id: int) -> DocPayload | None:
    return await session.scalar(select(DocPayload).where(DocPayload.entity_id == ent_id))


def _executors(ent: Entity) -> list[dict]:
    """Список исполнителей [{id,name}] из денормализованного extra_json."""
    return (ent.extra_json or {}).get("executors") or []


def _doc_dict(ent: Entity, p: DocPayload | None, perm: Permission | None,
              resp_names: list[str], cross_dept: bool = False,
              project_title: str | None = None,
              responsible_uid: int | None = None) -> dict:
    execs = _executors(ent)
    stages = (p.stages if p else []) or []
    done, total = wf_service.wf_stats(stages)
    return {
        "uid": ent.uid, "title": ent.title, "description": ent.description or "",
        "status_key": ent.status_key, "due_date": ent.due_date,
        "responsible_user_id": responsible_uid,
        "department_id": ent.owner_department_id,
        "cross_dept": cross_dept,
        "doc_type": p.doc_type if p else "", "file_ref": p.file_ref if p else "",
        "stages": stages, "stage_done": done, "stage_total": total,
        "progress": round(done / total * 100) if total else 0,
        "project_entity_uid": p.project_entity_uid if p else None,
        "project_title": project_title or "",
        "responsibles": resp_names,
        "executors": execs,
        "executor_names": [e["name"] for e in execs] or resp_names,
        "perm": perm.value if perm else None,
        "can_edit": access.can_write(perm),
        "can_share": access.can_manage_sharing(perm),
        "sync_state": ent.sync_state.value,
    }


async def _pick_department(session: AsyncSession, user: User, body: DocIn) -> int:
    """Детерминированный выбор пространства-владельца (изоляция workspace)."""
    from app.models.core import DepartmentMember, Role
    mine = (await session.scalars(
        select(DepartmentMember.department_id).where(DepartmentMember.user_id == user.id)
        .order_by(DepartmentMember.id))).all()
    if body.department_id is not None:
        if body.department_id <= 0:
            raise HTTPException(422, f"Некорректный department_id: {body.department_id}")
        # Руководитель/участник вправе явно указать СВОЁ пространство
        # (фронтенд всегда шлёт department_id из селекта со своими подразделениями);
        # чужое пространство доступно только админу/координатору.
        if user.role not in (Role.ADMIN, Role.COORDINATOR) \
                and body.department_id not in mine:
            raise HTTPException(403, "Выбор чужого пространства доступен только админу/координатору")
        if not await session.get(Department, body.department_id):
            raise HTTPException(404, "Подразделение не найдено")
        return body.department_id
    if not mine:
        raise HTTPException(400, "Вы не состоите ни в одном подразделении")
    return mine[0]


async def _check_ref_type(session: AsyncSession, user: User, dept_id: int,
                          label: str | None) -> None:
    """Вид документа обязан быть из справочника администрирования
    (RefType kind=doc_type): глобальные шаблоны администратора + записи/
    активированные копии подразделения. Пустое значение разрешено."""
    if not label:
        return
    from sqlalchemy import or_
    from app.models.core import RefType
    ok = await session.scalar(
        select(RefType.id).where(
            RefType.kind == RefType.KIND_DOC, RefType.is_active == True,  # noqa: E712
            RefType.label == label,
            or_(RefType.department_id.is_(None), RefType.department_id == dept_id)))
    if ok is None:
        raise HTTPException(422, "Вид документа должен быть выбран из справочника "
                                 "(администрирование → Виды документов)")


async def _assign_doc_people(session: AsyncSession, user: User, ent: Entity,
                             perm: Permission | None,
                             responsible_user_id: int | None,
                             executor_ids: list[int]) -> None:
    """Назначение ответственного и исполнителей с учётом изоляции пространств.

    Администратор/координатор может выбрать сотрудника из ЛЮБОГО подразделения:
    если выбранный не состоит в пространстве-владельце, для его подразделения
    открывается служебный слот (+шэр). Для обычных пользователей действует
    стандартная проверка членства (entity_service.assign_responsible/set_executors).
    Логика идентична модулю «ПО» (softs/_assign_soft_people).
    """
    from app.models.core import Role, EntityResponsible
    is_global = user.role in (Role.ADMIN, Role.COORDINATOR)
    owner_dept = ent.owner_department_id

    async def _primary_uid() -> int | None:
        return await session.scalar(
            select(EntityResponsible.user_id).where(
                EntityResponsible.entity_id == ent.id,
                EntityResponsible.is_primary == True))  # noqa: E712

    async def _member_dept(uid: int) -> int | None:
        return await session.scalar(
            select(DepartmentMember.department_id).where(
                DepartmentMember.user_id == uid).order_by(DepartmentMember.id))

    async def _ensure_slot(uid: int, *, primary: bool = False) -> None:
        """Служебный слот (и, при необходимости, шэр READ/EDIT) для внешнего
        сотрудника. UNIQUE(entity_id, department_id) — перед вставкой
        перечитываем состояние через flush+SELECT."""
        md = await _member_dept(uid)
        if md is None or md == owner_dept:
            return
        has = await session.scalar(
            select(EntityResponsible).where(
                EntityResponsible.entity_id == ent.id,
                EntityResponsible.user_id == uid))
        if has:
            if primary:
                has.is_primary = True
            return
        sh = await session.scalar(
            select(EntityShare).where(
                EntityShare.entity_id == ent.id,
                EntityShare.target_department_id == md))
        if sh is None:
            session.add(EntityShare(entity_id=ent.id, target_department_id=md,
                                    permission=Permission.READ,
                                    granted_by_user_id=user.id))
        elif access._perm_lt(sh.permission, Permission.EDIT):
            sh.permission = Permission.EDIT
        await session.flush()
        slot = await session.scalar(
            select(EntityResponsible).where(
                EntityResponsible.entity_id == ent.id,
                EntityResponsible.department_id == md,
                EntityResponsible.user_id.is_(None)))
        if slot is None:
            occupied = await session.scalar(
                select(EntityResponsible).where(
                    EntityResponsible.entity_id == ent.id,
                    EntityResponsible.department_id == md))
            if occupied is not None:
                if not primary:
                    return
                occupied.user_id = uid
                occupied.is_primary = True
                return
            slot = EntityResponsible(entity_id=ent.id, department_id=md)
            session.add(slot)
        slot.user_id = uid
        if primary:
            slot.is_primary = True

    try:
        slots_before = {r.user_id: r.id for r in (await session.scalars(
            select(EntityResponsible).where(
                EntityResponsible.entity_id == ent.id,
                EntityResponsible.user_id.is_not(None))))
            if r.user_id}
        if responsible_user_id:
            md = await _member_dept(responsible_user_id)
            if is_global and md is not None and md != owner_dept:
                old_primary = await session.scalar(
                    select(EntityResponsible).where(
                        EntityResponsible.entity_id == ent.id,
                        EntityResponsible.is_primary == True))  # noqa: E712
                if old_primary is not None and old_primary.user_id == responsible_user_id:
                    pass  # уже основной
                else:
                    if old_primary is not None:
                        old_primary.is_primary = False
                    await _ensure_slot(responsible_user_id, primary=True)
                    await session.flush()
                    new_slot = await session.scalar(
                        select(EntityResponsible).where(
                            EntityResponsible.entity_id == ent.id,
                            EntityResponsible.user_id == responsible_user_id))
                    if new_slot is not None:
                        new_slot.is_primary = True
                    elif old_primary is not None:
                        old_primary.is_primary = True
                        raise HTTPException(
                            400, "Выбранный ответственный уже занимает слот своего "
                                 "подразделения в этой карточке другим сотрудником")
                await session.flush()
                from app.core.entity_service import _log_only, _emit_assigned
                await _log_only(session, user, "reassign", ent,
                                {"department_id": md, "user_id": responsible_user_id})
                if responsible_user_id != user.id:
                    await _emit_assigned(session, user, ent, [responsible_user_id])
                    from app.core.mobile_notify import emit_mobile_event
                    await emit_mobile_event(
                        session, user_id=responsible_user_id, type_="assigned",
                        title=f"Вас назначили ответственным: {ent.title}",
                        body=ent.description or None, entity_uid=ent.uid,
                        module_key=ent.module_key,
                        payload={"kind": ent.kind.value, "due_date": ent.due_date})
            else:
                await entity_service.assign_responsible(
                    session, user, ent, owner_dept, responsible_user_id)
        if executor_ids:
            prev_primary = await _primary_uid()
            if is_global:
                for e in dict.fromkeys(executor_ids):
                    if e:
                        await _ensure_slot(e)
            await entity_service.set_executors(session, user, ent, executor_ids)
            for uid_in_req in dict.fromkeys(executor_ids):
                if not uid_in_req:
                    continue
                still = await session.scalar(
                    select(EntityResponsible.id).where(
                        EntityResponsible.entity_id == ent.id,
                        EntityResponsible.user_id == uid_in_req))
                if still or uid_in_req not in slots_before:
                    continue
                freed = await session.get(EntityResponsible, slots_before[uid_in_req])
                if freed is not None and freed.user_id is None \
                        and freed.user_id != prev_primary:
                    freed.user_id = uid_in_req
            now_primary = await _primary_uid()
            if now_primary is None and prev_primary:
                slot = await session.scalar(
                    select(EntityResponsible).where(
                        EntityResponsible.entity_id == ent.id,
                        EntityResponsible.user_id == prev_primary))
                if slot is not None:
                    slot.is_primary = True
                else:
                    owner_slot = await session.scalar(
                        select(EntityResponsible).where(
                            EntityResponsible.entity_id == ent.id,
                            EntityResponsible.department_id == owner_dept))
                    if owner_slot is not None:
                        owner_slot.user_id = prev_primary
                        owner_slot.is_primary = True
    except PermissionError as e:
        raise HTTPException(400, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))


async def _external_executors(session: AsyncSession, user: User, ent: Entity,
                              dept_id: int, exec_ids: list[int]) -> None:
    """Внешние исполнители при СОЗДАНИИ (админ/координатор): открываем слот
    в их пространстве и поднимаем шэр до EDIT, чтобы set_executors внутри
    create_entity нашёл свободный слот. UNIQUE(entity_id, department_id) —
    перечитываем состояние через flush+SELECT."""
    from app.models.core import EntityResponsible
    exec_ids = [e for e in dict.fromkeys(exec_ids) if e]
    if not exec_ids:
        return
    await session.flush()
    for e in exec_ids:
        md = await session.scalar(
            select(DepartmentMember.department_id).where(
                DepartmentMember.user_id == e).order_by(DepartmentMember.id))
        if md is None or md == dept_id:
            continue  # внутренний исполнитель: слот владельца уже обработан ядром
        occupied = await session.scalar(
            select(EntityResponsible.id).where(
                EntityResponsible.entity_id == ent.id,
                EntityResponsible.user_id == e))
        if occupied:
            continue
        sh = await session.scalar(
            select(EntityShare).where(
                EntityShare.entity_id == ent.id,
                EntityShare.target_department_id == md))
        if sh is None:
            session.add(EntityShare(entity_id=ent.id, target_department_id=md,
                                    permission=Permission.EDIT,
                                    granted_by_user_id=user.id))
        elif access._perm_lt(sh.permission, Permission.EDIT):
            sh.permission = Permission.EDIT
        await session.flush()
        slot = await session.scalar(
            select(EntityResponsible).where(
                EntityResponsible.entity_id == ent.id,
                EntityResponsible.department_id == md))
        if slot is None:
            session.add(EntityResponsible(entity_id=ent.id,
                                          department_id=md, user_id=e))
        elif slot.user_id is None:
            slot.user_id = e
    await session.flush()


async def _visible_projects(session: AsyncSession, ctx: access.AccessContext) -> list[Entity]:
    """Доступные пользователю проекты (для привязки сущностей модуля)."""
    ents = (await session.scalars(
        select(Entity).where(Entity.kind == EntityKind.PROJECT,
                             access.entity_filter(ctx))
        .options(selectinload(Entity.shares)))).all()
    out = []
    for ent in ents:
        if await access.resolve_permission(session, ctx, ent) is None:
            continue
        cross = await _has_shares(session, ent)
        if not ctx.is_global and not cross \
                and ent.owner_department_id not in ctx.department_ids:
            continue
        out.append(ent)
    return out


async def _project_titles(session: AsyncSession, payloads: list[DocPayload | None]) -> dict[str, str]:
    """uid проекта -> заголовок (для карточек/форм; uid хранится в payload)."""
    puids = {p.project_entity_uid for p in payloads if p and p.project_entity_uid}
    if not puids:
        return {}
    rows = (await session.scalars(
        select(Entity).where(Entity.kind == EntityKind.PROJECT,
                             Entity.uid.in_(puids)))).all()
    return {e.uid: e.title for e in rows}


# ---------------- HTML ----------------

@router.get("")
@router.get("/")
async def docs_page(request: Request, user: User = Depends(get_current_user),
                    session: AsyncSession = Depends(session_dep)):
    ctx = await access.build_context(session, user)
    # fctx — общие фильтры страницы (?dept=&df_today=&df_week=&df_month=&df_overdue=),
    # приходят в т.ч. по кликам с карточек дашборда.
    fctx = await filters.page_filters(request, session, user.id, "doc",
                                      ctx.department_ids, ctx.is_global)
    ents = (await session.scalars(
        select(Entity).where(Entity.kind == EntityKind.DOC, access.entity_filter(ctx))
        .options(selectinload(Entity.shares), selectinload(Entity.responsibles))
        .order_by(Entity.due_date.desc()))).all()
    items = []
    payloads = []
    for ent in ents:
        perm = await access.resolve_permission(session, ctx, ent)
        if perm is None:
            continue
        cross = await _has_shares(session, ent)
        if not ctx.is_global and not cross \
                and ent.owner_department_id not in ctx.department_ids:
            continue
        resp_names = []
        primary_uid = None
        for r in ent.responsibles:
            if r.is_primary:
                primary_uid = r.user_id
            if r.user_id:
                u = await session.get(User, r.user_id)
                if u:
                    resp_names.append(u.display_name)
        pl = await _load_payload(session, ent.id)
        payloads.append(pl)
        items.append((ent, pl, perm, resp_names, cross, primary_uid))
    ptitles = await _project_titles(session, payloads)
    cards = [_doc_dict(e, pl, p, rn, cross_dept=c, responsible_uid=ru,
                       project_title=ptitles.get((pl.project_entity_uid if pl else "") or ""))
             for e, pl, p, rn, c, ru in items]
    # Справочник — иерархический (ядро app/core/refdata): глобальные шаблоны
    # администратора + собственные/активированные записи подразделения; набор
    # ограничен подключённым модулем данного workspace.
    dept_ctx = await refdata.context_dept(session, user, None)
    mods = await refdata.enabled_modules(session, dept_ctx)
    statuses = await refdata.effective_statuses(
        session, EntityKind.DOC, dept_ctx, mods)
    doc_types = await refdata.effective_types(
        session, "doc_type", dept_ctx, mods)
    depts: list[dict] = []
    if ctx.is_global:
        depts = [{"id": d.id, "name": d.name} for d in
                 (await session.scalars(
                     select(Department).where(Department.is_active == True))).all()]  # noqa: E712
    else:
        rows = (await session.scalars(
            select(Department).where(Department.id.in_(ctx.department_ids or [-1])))).all()
        depts = [{"id": d.id, "name": d.name} for d in rows]
    # сотрудники для назначения ответственного/исполнителей — как people в
    # прототипе. Для админа/координатора — все активные пользователи (они вправе
    # создавать записи в любом пространстве и назначать любого сотрудника);
    # иначе — только участники доступных подразделений (изоляция workspace).
    people: list[dict] = []
    if ctx.is_global:
        users = (await session.scalars(
            select(User).where(User.is_active == True)  # noqa: E712
            .order_by(User.display_name))).all()
        people = [{"id": u.id, "name": u.display_name} for u in users]
    else:
        members = (await session.scalars(
            select(DepartmentMember).where(
                DepartmentMember.department_id.in_(ctx.department_ids or [-1])))).all()
        for m in members:
            u = await session.get(User, m.user_id)
            if u and u.is_active and not any(x["id"] == u.id for x in people):
                people.append({"id": u.id, "name": u.display_name})
    # проекты для привязки документа (связанность категорий в рамках модулей)
    proj_pairs = await _visible_projects(session, ctx)
    projects = [{"uid": e.uid, "title": e.title} for e in proj_pairs]
    return await render_page(
        request, "docs.html", module_key=MODULE_KEY, user=user, session=session,
        page_title="Документы",
        page_sub="Реестр документов со статусами; события синхронизируются с Яндекс.Календарём",
        counts={MODULE_KEY: len(cards)},
        docs=filters.apply_page_filters(cards, fctx, me=user.id),
        fctx=fctx,
        statuses=[{"key": s.key, "label": s.label, "color": s.color} for s in statuses],
        doc_types=[{"label": t.label} for t in doc_types],
        departments=depts, people=people, projects=projects, me=user.id,
    )


# ---------------- REST ----------------

@router.get("/api-list")
async def list_docs(user: User = Depends(get_current_user),
                    session: AsyncSession = Depends(session_dep)):
    ctx = await access.build_context(session, user)
    ents = (await session.scalars(
        select(Entity).where(Entity.kind == EntityKind.DOC, access.entity_filter(ctx))
        .options(selectinload(Entity.shares), selectinload(Entity.responsibles)))).all()
    rows = []
    for ent in ents:
        perm = await access.resolve_permission(session, ctx, ent)
        if perm is None:
            continue
        cross = await _has_shares(session, ent)
        if not ctx.is_global and not cross \
                and ent.owner_department_id not in ctx.department_ids:
            continue
        resp_names = []
        primary_uid = None
        for r in ent.responsibles:
            if r.is_primary:
                primary_uid = r.user_id
            if r.user_id:
                u = await session.get(User, r.user_id)
                if u:
                    resp_names.append(u.display_name)
        rows.append((ent, await _load_payload(session, ent.id), perm, resp_names, cross, primary_uid))
    ptitles = await _project_titles(session, [pl for _, pl, _, _, _, _ in rows])
    return [_doc_dict(e, pl, p, rn, cross_dept=c, responsible_uid=ru,
                      project_title=ptitles.get((pl.project_entity_uid if pl else "") or ""))
            for e, pl, p, rn, c, ru in rows]


async def _check_project(session: AsyncSession, ctx: access.AccessContext,
                         project_uid: str | None) -> None:
    """Привязка к проекту: сущность проекта должна существовать и быть доступна."""
    if not project_uid:
        return
    parent = await session.scalar(
        select(Entity).where(Entity.uid == project_uid,
                             Entity.kind == EntityKind.PROJECT))
    if not parent:
        raise HTTPException(404, "Проект не найден")
    if not await access.resolve_permission(session, ctx, parent):
        raise HTTPException(403, "Нет доступа к выбранному проекту")


@router.post("")
async def create_doc(body: DocIn, user: User = Depends(get_current_user),
                     session: AsyncSession = Depends(session_dep)):
    dept_id = await _pick_department(session, user, body)
    ctx = await access.build_context(session, user)
    await _check_project(session, ctx, body.project_entity_uid)
    await _check_ref_type(session, user, dept_id, body.doc_type)
    resp_id = body.responsible_user_id or user.id
    try:
        ent = await entity_service.create_entity(
            session, user, kind=EntityKind.DOC, module_key=MODULE_KEY,
            owner_department_id=dept_id, title=body.title.strip(),
            description=body.description or None, status_key=body.status_key,
            due_date=body.due_date or None,
            responsible_user_id=resp_id,
            executor_user_ids=body.executor_user_ids,
            share_with=[(d, Permission.EDIT) for d in dict.fromkeys(body.share_with)]
            if body.share_with else None,
        )
    except ValueError as e:
        raise HTTPException(422, str(e))
    # Внешний ответственный/исполнители: админ/координатор может выбрать
    # сотрудников из ЛЮБОГО подразделения — доназначаем внешние слоты + шэры.
    if ctx.is_global:
        await _external_executors(session, user, ent, dept_id,
                                  list(body.executor_user_ids or []))
    else:
        # Обычный участник: чужого (несостоящего в пространстве-владельце)
        # ответственного принимать нельзя — create_entity при внешнем выборе
        # открывает служебный слот+шэр в чужом подразделении (лазейка доступа).
        # Ядро в таких случаях ставит owner-слоту user_id=None — проверяем, что
        # ответственный реально занял слот своего пространства.
        from app.models.core import EntityResponsible
        owner_slot = await session.scalar(
            select(EntityResponsible).where(
                EntityResponsible.entity_id == ent.id,
                EntityResponsible.department_id == dept_id))
        if resp_id != user.id and (owner_slot is None or owner_slot.user_id != resp_id):
            await session.rollback()
            raise HTTPException(400, "Ответственный должен состоять в выбранном подразделении")
    # маршрут (wf) документа: [{name, deadline}] -> нормализованные этапы + автозадачи
    import json as _json
    stages_raw = []
    if body.stages_json:
        try:
            stages_raw = _json.loads(body.stages_json)
        except (TypeError, ValueError):
            raise HTTPException(422, "Некорректный JSON в поле stages")
    stages = wf_service.wf_build_stages(stages_raw if isinstance(stages_raw, list) else [])
    session.add(DocPayload(
        entity_id=ent.id, doc_type=body.doc_type or None,
        file_ref=body.file_ref or None,
        project_entity_uid=body.project_entity_uid or None, stages=stages))
    await session.flush()
    if stages:
        await wf_service.wf_create_stage_tasks(
            session, user, ent, stages, dept_id, title_prefix="Документ «")
        pl = await _load_payload(session, ent.id)
        if pl:
            pl.stages = stages
    await session.commit()
    return {"uid": ent.uid, "ok": True}


_DOC_FIELDS = {"doc_type", "file_ref", "project_entity_uid"}


@router.patch("/{uid}")
async def patch_doc(uid: str, body: dict, user: User = Depends(get_current_user),
                    session: AsyncSession = Depends(session_dep)):
    ent = await session.scalar(
        select(Entity).where(Entity.uid == uid, Entity.kind == EntityKind.DOC))
    if not ent:
        raise HTTPException(404, "Документ не найден")
    ctx = await access.build_context(session, user)
    perm = await access.resolve_permission(session, ctx, ent)
    if not access.can_write(perm):
        raise HTTPException(403, "Нет прав на редактирование (доступ только на чтение)")
    core_changes = {k: v for k, v in body.items()
                    if k in {"title", "description", "status_key", "due_date"}}
    await entity_service.update_entity(session, user, ent, core_changes)
    if "doc_type" in body:
        await _check_ref_type(session, user, ent.owner_department_id, body["doc_type"])
    # Ответственный/исполнители: единая логика назначения (в т.ч. внешние
    # сотрудники для админа/координатора — служебные слоты + шэры).
    # set_executors вызывается внутри _assign_doc_people, поэтому отдельный
    # блок executor_user_ids ниже убран (иначе двойное обновление/уведомления).
    if "responsible_user_id" in body or "executor_user_ids" in body:
        exec_ids = [int(x) for x in (body.get("executor_user_ids") or [])]
        resp_id = body.get("responsible_user_id")
        await _assign_doc_people(
            session, user, ent, perm,
            int(resp_id) if resp_id else None, exec_ids)
    payload = await _load_payload(session, ent.id)
    if payload:
        if "project_entity_uid" in body:
            await _check_project(session, ctx, body["project_entity_uid"])
        for f in _DOC_FIELDS:
            if f in body:
                setattr(payload, f, body[f] or None)
        # маршрут (wf): PATCH {stages:[...]} — слияние по id/имени + автозадачи новых
        if "stages" in body:
            merged = wf_service.wf_merge_stages(payload.stages or [], body["stages"])
            payload.stages = merged
            await session.flush()
            await wf_service.wf_create_stage_tasks(
                session, user, ent, merged, ent.owner_department_id,
                title_prefix="Документ «")
            payload.stages = merged
    await session.commit()
    return {"ok": True}


@router.delete("/{uid}")
async def delete_doc(uid: str, user: User = Depends(get_current_user),
                     session: AsyncSession = Depends(session_dep)):
    ent = await session.scalar(
        select(Entity).where(Entity.uid == uid, Entity.kind == EntityKind.DOC))
    if not ent:
        raise HTTPException(404, "Документ не найден")
    ctx = await access.build_context(session, user)
    perm = await access.resolve_permission(session, ctx, ent)
    if perm is None:
        raise HTTPException(403)
    from app.models.core import Role
    if user.role == Role.MEMBER:
        mine = any(r.user_id == user.id for r in ent.responsibles)
        if not (mine and ent.owner_department_id in ctx.department_ids):
            raise HTTPException(403, "Участник удаляет только свои документы")
    await entity_service.delete_entity(session, user, ent)
    await session.commit()
    return {"ok": True}
