"""Модуль-плагин «Проектная работа»: REST + HTML (карточки проектов как в прототипе).

Специфика (stages/customer/budget/links) — в ProjectPayload; общие поля и права — ядро.
Проект по ТЗ — межпространственная сущность: при создании можно указать участников
(share_with), тогда у каждого подразделения появляется свой слот ответственного
(инвариант «несколько ответственных»). Календарь: контрольные точки этапов (вехи).
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core import access, entity_service, filters, wf_service
from app.core.security import get_current_user
from app.deps import session_dep
from app.models.core import (
    Department, DepartmentMember, Entity, EntityKind, EntityShare, Permission,
    PriorityLevel, Role, User, WfTemplate,
)
from app.core import refdata
from app.models.module_payloads import DocPayload, ProjectPayload, SoftPayload, TaskPayload
from app.ui import render_page

router = APIRouter()

MODULE_KEY = "projects"


def _executors(ent: Entity) -> list[dict]:
    """Список исполнителей [{id,name}] из денормализованного extra_json."""
    return (ent.extra_json or {}).get("executors") or []


async def _has_shares(session: AsyncSession, ent: Entity) -> bool:
    """Признак межпространственности — ЯВНЫЙ SELECT (см. tasks/_has_shares)."""
    return await session.scalar(
        select(EntityShare.id).where(EntityShare.entity_id == ent.id)) is not None


class ProjectIn(BaseModel):
    title: str = Field(min_length=3, max_length=500)
    description: str | None = ""
    status_key: str | None = None            # init/planning/exec/monitor/done/canceled
    priority_key: str | None = "medium"
    due_date: str | None = None              # срок завершения проекта (календарная веха)
    customer: str | None = ""                # заказчик
    code: str | None = ""                    # код проекта (П-01) — как в прототипе
    leader_user_id: int | None = None        # руководитель проекта из участников подразделения
    start_date: str | None = None            # дата начала
    end_date: str | None = None              # дата окончания
    grant_entity_uid: str | None = None      # связанный грант
    priority_indicator_uid: str | None = None  # показатель Приоритет-2030
    stages_json: str | None = None           # JSON [{name, state, deadline, branches:[...]}] — маршрут (wf)
    wf_template_id: int | None = None        # маршрут по шаблону (wfTemplates демо)
    links_json: str | None = None            # JSON [{label, url}]
    department_id: int | None = None         # для админа/координатора: чьё пространство
    responsible_user_id: int | None = None
    executor_user_ids: list[int] = []        # исполнители (может быть несколько, как в прототипе)
    parent_uid: str | None = None            # проект-родитель (иерархия проектов)
    share_with: list[int] = []               # подразделения-участники проекта (EDIT)


async def _load_payload(session: AsyncSession, ent_id: int) -> ProjectPayload | None:
    return await session.scalar(select(ProjectPayload).where(ProjectPayload.entity_id == ent_id))


def _proj_dict(ent: Entity, p: ProjectPayload | None, perm: Permission | None,
               resp_names: list[str], cross_dept: bool = False,
               parent_title: str | None = None, leader_name: str | None = None,
               grant_title: str | None = None, prio_ind_title: str | None = None,
               sub_entities: list | None = None, status_labels: dict | None = None) -> dict:
    stages = (p.stages if p else []) or []
    done = sum(1 for s in stages if s.get("done") or s.get("state") == "done")
    execs = _executors(ent)
    # подчинённые сущности (Документы/ПО) со статусами — для содержимого карточки
    subs = []
    for kind, title, skey, stgs in (sub_entities or []):
        mini = _sub_status_label(kind, skey, stgs, status_labels or {})
        d, t = wf_service.wf_stats(stgs)
        subs.append({"kind": kind, "title": title, "status": skey, "mini": mini,
                     "stage_done": d, "stage_total": t})
    return {
        "uid": ent.uid, "title": ent.title, "description": ent.description or "",
        "status_key": ent.status_key, "priority_key": ent.priority_key,
        "due_date": ent.due_date, "department_id": ent.owner_department_id,
        "cross_dept": cross_dept,
        "customer": p.customer if p else "",
        # бюджет/источник удалён из формы и карточек по ТЗ (связь с финансированием —
        # через связанный грант)
        "code": (p.code if p else "") or "",
        "leader_user_id": p.leader_user_id if p else None,
        "leader_name": leader_name or "",
        "start_date": p.start_date if p else None,
        "end_date": p.end_date if p else None,
        "grant_entity_uid": p.grant_entity_uid if p else None,
        "grant_title": grant_title or "",
        "priority_indicator_uid": p.priority_indicator_uid if p else None,
        "priority_indicator_title": prio_ind_title or "",
        "stages": stages, "stage_done": done, "stage_total": len(stages),
        "progress": round(done / len(stages) * 100) if stages else 0,
        "sub_entities": subs,
        "links": (p.links if p else []) or [],
        "parent_uid": (p.parent_uid if p else None),
        "parent_title": parent_title or "",
        "responsibles": resp_names,
        # исполнители: объединение ответственных по подразделениям и явного списка
        "executors": execs,
        "executor_names": [e["name"] for e in execs] or resp_names,
        "perm": perm.value if perm else None,
        "can_edit": access.can_write(perm),
        "can_share": access.can_manage_sharing(perm),
        "sync_state": ent.sync_state.value,
    }


async def _sub_entities_map(session: AsyncSession, project_uids: list[str]) -> dict[str, list[tuple]]:
    """Подчинённые сущности проектов (Документы и ПО), привязанные через
    payload.project_entity_uid. Возвращает {uid проекта: [(kind,title,status_key,stages)]}.
    Используется для отображения статусов подчиненных сущностей в карточках проектов."""
    if not project_uids:
        return {}
    out: dict[str, list[tuple]] = {}
    rows = (await session.execute(
        select(Entity.title, Entity.status_key, Entity.uid, DocPayload.stages)
        .join(DocPayload, DocPayload.entity_id == Entity.id)
        .where(Entity.kind == EntityKind.DOC,
               DocPayload.project_entity_uid.in_(project_uids)))).all()
    for title, skey, puid, stages in rows:
        out.setdefault(puid, []).append(("doc", title, skey, stages or []))
    rows = (await session.execute(
        select(Entity.title, Entity.status_key, Entity.uid, SoftPayload.stages)
        .join(SoftPayload, SoftPayload.entity_id == Entity.id)
        .where(Entity.kind == EntityKind.SOFT,
               SoftPayload.project_entity_uid.in_(project_uids)))).all()
    for title, skey, puid, stages in rows:
        out.setdefault(puid, []).append(("soft", title, skey, stages or []))
    return out


def _sub_status_label(kind: str, status_key: str, stages: list, labels: dict) -> str:
    """Мини-статус подчинённой сущности для чипа в карточке проекта:
    этап маршрута (wf) в работе → «🧭 <этап>», иначе — метка статуса из справочника."""
    st = [s for s in (stages or []) if isinstance(s, dict)]
    act = next((s for s in st if s.get("state") == "active"), None)
    if act and st and not all(s.get("done") or s.get("state") == "done" for s in st):
        return f"🧭 {str(act.get('name') or '').strip()}"[:60]
    return labels.get((kind, status_key)) or status_key or "—"


async def _status_labels(session: AsyncSession, dept_id) -> dict:
    """{(kind,value): label} по эффективным справочникам статусов docs/softs."""
    labels: dict = {}
    mods = await refdata.enabled_modules(session, dept_id)
    for kind in (EntityKind.DOC, EntityKind.SOFT):
        for s in await refdata.effective_statuses(session, kind, dept_id, mods):
            labels[(kind.value.lower(), s.key)] = s.label
    return labels


async def _linked_titles(session: AsyncSession, payloads: list[ProjectPayload]) -> tuple[dict, dict, dict[int, str]]:
    """Заголовки связанных сущностей для карточек/формы:
    returns ({grant_uid: title}, {prio_uid: title}, {user_id: name})."""
    guids = {p.grant_entity_uid for p in payloads if getattr(p, "grant_entity_uid", None)}
    puids = {p.priority_indicator_uid for p in payloads
             if getattr(p, "priority_indicator_uid", None)}
    gmap: dict[str, str] = {}
    pmap: dict[str, str] = {}
    if guids:
        for e in (await session.scalars(select(Entity).where(
                Entity.kind == EntityKind.GRANT, Entity.uid.in_(guids)))).all():
            gmap[e.uid] = e.title
    if puids:
        for e in (await session.scalars(select(Entity).where(
                Entity.kind == EntityKind.PRIORITY, Entity.uid.in_(puids)))).all():
            pmap[e.uid] = e.title
    lids = {p.leader_user_id for p in payloads if getattr(p, "leader_user_id", None)}
    lmap: dict[int, str] = {}
    if lids:
        for u in (await session.scalars(select(User).where(User.id.in_(lids)))).all():
            lmap[u.id] = u.display_name
    return gmap, pmap, lmap


async def _check_linked(session: AsyncSession, ctx: access.AccessContext, uid: str | None,
                        kind: EntityKind, label: str) -> None:
    """Связанная сущность (грант / показатель Приоритет-2030) должна существовать
    и быть доступна текущему пользователю — преемственность между модулями."""
    if not uid:
        return
    ent = await session.scalar(select(Entity).where(Entity.uid == uid, Entity.kind == kind))
    if not ent:
        raise HTTPException(404, f"{label} не найден")
    if not await access.resolve_permission(session, ctx, ent):
        raise HTTPException(403, f"Нет доступа к связанному объекту: {label}")


async def _parent_titles(session: AsyncSession, payloads: list[ProjectPayload]) -> dict[str, str]:
    """uid родителя -> заголовок родительского проекта (для карточек/формы)."""
    puids = {p.parent_uid for p in payloads if getattr(p, "parent_uid", None)}
    if not puids:
        return {}
    rows = (await session.scalars(
        select(Entity).where(Entity.kind == EntityKind.PROJECT,
                             Entity.uid.in_(puids)))).all()
    return {e.uid: e.title for e in rows}


async def _pick_department(session: AsyncSession, user: User, body: ProjectIn) -> int:
    """Детерминированный выбор пространства-владельца (изоляция workspace)."""
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


def _parse_json_field(raw: str | None, default: list) -> list:
    if not raw:
        return default
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else default
    except (ValueError, TypeError):
        raise HTTPException(422, "Некорректный JSON в поле stages/links")


async def _visible_entities(session: AsyncSession, ctx: access.AccessContext,
                            kind: EntityKind):
    ents = (await session.scalars(
        select(Entity).where(Entity.kind == kind, access.entity_filter(ctx))
        .options(selectinload(Entity.shares), selectinload(Entity.responsibles)))).all()
    out = []
    for ent in ents:
        perm = await access.resolve_permission(session, ctx, ent)
        if perm is None:
            continue
        # Инвариант изоляции пространств: без шеринга видна только владельцу.
        cross = bool(await session.scalar(
        select(EntityShare.id).where(EntityShare.entity_id == ent.id)))
        if not ctx.is_global and not cross \
                and ent.owner_department_id not in ctx.department_ids:
            continue
        out.append((ent, perm, cross))
    return out


async def _resp_names(session: AsyncSession, ent: Entity) -> list[str]:
    names = []
    for r in ent.responsibles:
        if r.user_id:
            u = await session.get(User, r.user_id)
            if u:
                names.append(u.display_name)
    return names


# ---------------- HTML ----------------

async def _cycle_guard(session: AsyncSession, uid: str | None,
                       self_uid: str | None = None) -> None:
    """Иерархия проектов ациклична: потомок не может стать родителем предка."""
    cur = uid
    seen = {self_uid} if self_uid else set()
    while cur:
        if cur in seen:
            raise HTTPException(422, "Проект не может быть частью самого себя (или своего потомка)")
        seen.add(cur)
        p = await session.scalar(select(ProjectPayload).join(
            Entity, Entity.id == ProjectPayload.entity_id).where(Entity.uid == cur))
        cur = p.parent_uid if p else None


@router.get("")
@router.get("/")
async def projects_page(request: Request, user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(session_dep)):
    ctx = await access.build_context(session, user)
    # fctx — общие фильтры страницы (?dept=&df_today=&df_week=&df_month=&df_overdue=),
    # приходят в т.ч. по кликам с карточек дашборда.
    fctx = await filters.page_filters(request, session, user.id, "project",
                                      ctx.department_ids, ctx.is_global)
    pairs = await _visible_entities(session, ctx, EntityKind.PROJECT)
    payloads = {e.id: await _load_payload(session, e.id) for e, _, _ in pairs}
    ptitles = await _parent_titles(session, list(payloads.values()))
    gmap, pimap, lmap = await _linked_titles(session, list(payloads.values()))
    submap = await _sub_entities_map(session, [e.uid for e, _, _ in pairs])
    slabels = await _status_labels(session, await refdata.context_dept(session, user, None))
    items = [_proj_dict(e, payloads[e.id], p, await _resp_names(session, e),
                        cross_dept=c,
                        parent_title=ptitles.get(payloads[e.id].parent_uid or ""),
                        leader_name=lmap.get(payloads[e.id].leader_user_id or 0, "") if payloads[e.id] else "",
                        grant_title=gmap.get((payloads[e.id].grant_entity_uid if payloads[e.id] else "") or ""),
                        prio_ind_title=pimap.get((payloads[e.id].priority_indicator_uid if payloads[e.id] else "") or ""),
                        sub_entities=submap.get(e.uid, []), status_labels=slabels)
             for e, p, c in sorted(pairs, key=lambda x: (x[0].due_date or "9999"))]
    dept = await refdata.context_dept(session, user, None)
    modules = await refdata.enabled_modules(session, dept)
    statuses = await refdata.effective_statuses(session, EntityKind.PROJECT, dept, modules)
    prios = await refdata.effective_priorities(session, dept, modules)
    wf_templates = await refdata.effective_templates(session, dept, modules)
    depts: list[dict] = []
    if ctx.is_global:
        depts = [{"id": d.id, "name": d.name} for d in
                 (await session.scalars(
                     select(Department).where(Department.is_active == True))).all()]  # noqa: E712
    else:
        # непривилегированный пользователь выбирает только из своих подразделений
        rows = (await session.scalars(
            select(Department).where(Department.id.in_(ctx.department_ids or [-1])))).all()
        depts = [{"id": d.id, "name": d.name} for d in rows]
    # сотрудники для назначения исполнителей (несколько) — как people в прототипе
    members = (await session.scalars(
        select(DepartmentMember).where(
            DepartmentMember.department_id.in_(
                ctx.department_ids or [d["id"] for d in depts] or [-1])))).all()
    people: list[dict] = []
    for m in members:
        u = await session.get(User, m.user_id)
        if u and u.is_active and not any(x["id"] == u.id for x in people):
            people.append({"id": u.id, "name": u.display_name})
    # справочники связанных модулей: гранты и показатели Приоритет-2030
    grants = [{"uid": e.uid, "title": e.title} for e, _, _ in
              await _visible_entities(session, ctx, EntityKind.GRANT)]
    indicators = [{"uid": e.uid, "title": e.title} for e, _, _ in
                  await _visible_entities(session, ctx, EntityKind.PRIORITY)]
    return await render_page(
        request, "projects.html", module_key=MODULE_KEY, user=user, session=session,
        page_title="Проектная работа",
        page_sub="Межпространственные проекты: этапы, вехи, ответственные от каждого подразделения",
        counts={MODULE_KEY: len(items)},
        # Единые фильтры страниц сущностей (дедлайны + подразделение) — см.
        # app/core/filters.py; JS-повтор на странице — core.js applyListFilters.
        projects=filters.apply_page_filters(items, fctx, me=user.id),
        fctx=fctx,
        statuses=[{"key": s.key, "label": s.label, "color": s.color} for s in statuses],
        priorities=[{"key": p.key, "label": p.label, "color": p.color} for p in prios],
        wf_templates=[{"id": t.id, "name": t.name, "steps": t.steps or []} for t in wf_templates],
        departments=depts, people=people, me=user.id, grants=grants, indicators=indicators,
    )


# ---------------- REST ----------------

@router.get("/api-list")
async def list_projects(user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(session_dep)):
    ctx = await access.build_context(session, user)
    pairs = await _visible_entities(session, ctx, EntityKind.PROJECT)
    payloads = {e.id: await _load_payload(session, e.id) for e, _, _ in pairs}
    ptitles = await _parent_titles(session, list(payloads.values()))
    gmap, pimap, lmap = await _linked_titles(session, list(payloads.values()))
    submap = await _sub_entities_map(session, [e.uid for e, _, _ in pairs])
    slabels = await _status_labels(session, await refdata.context_dept(session, user, None))
    out = []
    for ent, perm, cross in pairs:
        pl = payloads[ent.id]
        out.append(_proj_dict(ent, pl, perm, await _resp_names(session, ent),
                              cross_dept=cross,
                              parent_title=ptitles.get((pl.parent_uid if pl else "") or ""),
                              leader_name=lmap.get(pl.leader_user_id or 0, "") if pl else "",
                              grant_title=gmap.get((pl.grant_entity_uid if pl else "") or ""),
                              prio_ind_title=pimap.get((pl.priority_indicator_uid if pl else "") or ""),
                              sub_entities=submap.get(ent.uid, []), status_labels=slabels))
    return out


def _stage_uid() -> str:
    import uuid as _uuid
    return _uuid.uuid4().hex[:10]


def _build_stages(raw: list) -> list[dict]:
    """Нормализация этапов маршрута (wf): каждый этап получает id/state/taskId/branches.
    Для каждого этапа автозадача создаётся отдельно (см. _create_stage_tasks).

    Поля холста (Obsidian-like): x/y — координаты узла, links_out — явные связи
    «этот этап → следующий». Сохраняются прямо в payload.stages (JSON), поэтому
    раскладка переживает перезагрузку без отдельной миграции."""
    import uuid as _uuid
    out = []
    for i, s in enumerate(raw):
        if not isinstance(s, dict) or not str(s.get("name", "")).strip():
            continue
        st = {
            "id": str(s.get("id") or _uuid.uuid4().hex[:10]),
            "name": str(s["name"]).strip(),
            "state": ("active" if i == 0 else "pending"),
            "date": "", "deadline": str(s.get("deadline") or ""),
            "comment": "", "taskId": "", "branches": s.get("branches") or [],
        }
        # координаты и связи холста сохраняем как есть (если заданы числами)
        for pos in ("x", "y"):
            v = s.get(pos)
            if isinstance(v, (int, float)):
                st[pos] = int(v)
        lo = [t for t in (s.get("links_out") or []) if isinstance(t, str)]
        if lo:
            st["links_out"] = lo
        out.append(st)
    # ссылки links_out на несуществующие id (например, из старого JSON) — чистим
    valid = {st["id"] for st in out}
    for st in out:
        if "links_out" in st:
            st["links_out"] = [t for t in st["links_out"] if t in valid]
            if not st["links_out"]:
                del st["links_out"]
    return out


async def _create_stage_tasks(session: AsyncSession, user: User, ent: Entity,
                              stages: list[dict], dept_id: int,
                              leader_user_id: int | None) -> None:
    """Автозадачи этапов маршрута проекта (создание/редактирование — по образцу
    createStepTask из sipm_lite_v3.html): «<проект> — этап: <название>».
    Исполнитель — руководитель проекта (или автор), срок — дедлайн этапа."""
    resp = leader_user_id or user.id
    for i, s in enumerate(stages):
        if s.get("taskId"):
            continue
        t = await entity_service.create_entity(
            session, user, kind=EntityKind.TASK, module_key="tasks",
            owner_department_id=dept_id,
            title=f"{ent.title} — этап: {s['name']}",
            description=("Автозадача этапа «" + s["name"] + "». Завершение задачи "
                         "закрывает этап и переводит маршрут на следующий."),
            status_key="progress" if s["state"] == "active" else "new",
            priority_key="medium", due_date=s.get("deadline") or None,
            responsible_user_id=resp)
        session.add(TaskPayload(entity_id=t.id, project_entity_uid=ent.uid))
        s["taskId"] = t.uid
    # ответвления (условия) на холсте маршрута — тоже получают автозадачи
    for s in stages:
        for br in (s.get("branches") or []):
            if not isinstance(br, dict):
                continue
            for bs in (br.get("steps") or []):
                if isinstance(bs, dict) and not bs.get("taskId"):
                    t = await entity_service.create_entity(
                        session, user, kind=EntityKind.TASK, module_key="tasks",
                        owner_department_id=dept_id,
                        title=f"{ent.title} — ветка «{br.get('name', '')}»: {bs.get('name', '')}",
                        description=f"Автозадача этапа ветки «{br.get('name', '')}».",
                        status_key="new", priority_key="medium",
                        due_date=bs.get("deadline") or None,
                        responsible_user_id=resp)
                    session.add(TaskPayload(entity_id=t.id, project_entity_uid=ent.uid))
                    bs["taskId"] = t.uid


@router.post("")
async def create_project(body: ProjectIn, user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(session_dep)):
    dept_id = await _pick_department(session, user, body)
    ctx = await access.build_context(session, user)
    # родительский проект: проверяем существование и доступность
    if body.parent_uid:
        parent = await session.scalar(
            select(Entity).where(Entity.uid == body.parent_uid,
                                 Entity.kind == EntityKind.PROJECT))
        if not parent:
            raise HTTPException(404, "Родительский проект не найден")
        if not await access.resolve_permission(session, ctx, parent):
            raise HTTPException(403, "Нет доступа к родительскому проекту")
    # связанные сущности других модулей (грант, показатель Приоритет-2030)
    await _check_linked(session, ctx, body.grant_entity_uid, EntityKind.GRANT, "Грант")
    await _check_linked(session, ctx, body.priority_indicator_uid,
                        EntityKind.PRIORITY, "Показатель Приоритет-2030")
    # руководитель проекта — только из участников подразделения-владельца
    if body.leader_user_id:
        if not await session.scalar(select(DepartmentMember.id).where(
                DepartmentMember.user_id == body.leader_user_id,
                DepartmentMember.department_id == dept_id)):
            raise HTTPException(422, "Руководитель проекта должен быть участником подразделения")
    # Маршрут (workflow): по шаблону (wfTemplates демо) или индивидуальный;
    # при отсутствии того и другого — дефолт как в прототипе.
    stages_raw = _parse_json_field(body.stages_json, [])
    if not stages_raw and body.wf_template_id:
        tpl = await session.get(WfTemplate, body.wf_template_id)
        if tpl is None or not tpl.is_active:
            raise HTTPException(404, "Шаблон маршрута не найден или деактивирован")
        stages_raw = [{"name": n} for n in (tpl.steps or ["Подготовка", "Утверждение"])]
    if not stages_raw:
        stages_raw = [{"name": "Подготовка"}, {"name": "Утверждение"}]
    stages = _build_stages(stages_raw)
    # Межпространственный контекст проекта: участники получают EDIT и свой слот
    # ответственного (инвариант ТЗ «несколько ответственных»).
    mine = set((await session.scalars(
        select(DepartmentMember.department_id).where(
            DepartmentMember.user_id == user.id))).all())
    share_with: list[tuple[int, Permission]] = []
    for d in dict.fromkeys(body.share_with):
        if d == dept_id:
            continue
        if not await session.get(Department, d):
            raise HTTPException(404, f"Подразделение {d} не найдено")
        if user.role not in (Role.ADMIN, Role.COORDINATOR) and d not in mine:
            raise HTTPException(403, "Можно привлекать только свои подразделения-участники")
        share_with.append((d, Permission.EDIT))
    try:
        ent = await entity_service.create_entity(
            session, user, kind=EntityKind.PROJECT, module_key=MODULE_KEY,
            owner_department_id=dept_id, title=body.title.strip(),
            description=body.description or None, status_key=body.status_key,
            priority_key=body.priority_key, due_date=body.end_date or body.due_date or None,
            responsible_user_id=body.responsible_user_id or body.leader_user_id or user.id,
            executor_user_ids=body.executor_user_ids,
            share_with=share_with,
        )
    except ValueError as e:
        raise HTTPException(422, str(e))
    await _create_stage_tasks(session, user, ent, stages, dept_id, body.leader_user_id)
    session.add(ProjectPayload(
        entity_id=ent.id, stages=stages,
        customer=body.customer or None,
        links=_parse_json_field(body.links_json, []),
        parent_uid=body.parent_uid or None,
        code=(body.code or "").strip() or None,
        leader_user_id=body.leader_user_id,
        start_date=body.start_date or None, end_date=body.end_date or None,
        grant_entity_uid=body.grant_entity_uid or None,
        priority_indicator_uid=body.priority_indicator_uid or None))
    await session.commit()
    return {"uid": ent.uid, "ok": True, "stage_tasks": len([s for s in stages if s.get("taskId")])}


_PROJ_CORE = {"title", "description", "status_key", "priority_key", "due_date"}
_PROJ_PAYLOAD = {"customer", "stages", "links", "parent_uid", "code",
                 "leader_user_id", "start_date", "end_date",
                 "grant_entity_uid", "priority_indicator_uid"}


@router.patch("/{uid}")
async def patch_project(uid: str, body: dict, user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(session_dep)):
    ent = await session.scalar(
        select(Entity).where(Entity.uid == uid, Entity.kind == EntityKind.PROJECT))
    if not ent:
        raise HTTPException(404, "Проект не найден")
    ctx = await access.build_context(session, user)
    perm = await access.resolve_permission(session, ctx, ent)
    if not access.can_write(perm):
        raise HTTPException(403, "Нет прав на редактирование (доступ только на чтение)")
    unknown = set(body) - _PROJ_CORE - _PROJ_PAYLOAD - {"executor_user_ids", "responsible_user_id"}
    if unknown:
        raise HTTPException(422, f"Неизвестные поля: {sorted(unknown)}")
    await entity_service.update_entity(
        session, user, ent, {k: v for k, v in body.items() if k in _PROJ_CORE})
    payload = await _load_payload(session, ent.id)
    if payload is None:
        payload = ProjectPayload(entity_id=ent.id)
        session.add(payload)
    if "parent_uid" in body:
        new_parent = (body["parent_uid"] or "").strip() or None
        if new_parent:
            if new_parent == uid:
                raise HTTPException(422, "Проект не может быть частью самого себя")
            parent = await session.scalar(
                select(Entity).where(Entity.uid == new_parent,
                                     Entity.kind == EntityKind.PROJECT))
            if not parent:
                raise HTTPException(404, "Родительский проект не найден")
            if not await access.resolve_permission(session, ctx, parent):
                raise HTTPException(403, "Нет доступа к родительскому проекту")
            await _cycle_guard(session, new_parent, self_uid=uid)
        payload.parent_uid = new_parent
    # связанные сущности других модулей — с проверкой доступности
    if "grant_entity_uid" in body:
        g = (body["grant_entity_uid"] or "").strip() or None
        await _check_linked(session, ctx, g, EntityKind.GRANT, "Грант")
        payload.grant_entity_uid = g
    if "priority_indicator_uid" in body:
        pi = (body["priority_indicator_uid"] or "").strip() or None
        await _check_linked(session, ctx, pi, EntityKind.PRIORITY,
                            "Показатель Приоритет-2030")
        payload.priority_indicator_uid = pi
    if "leader_user_id" in body:
        lid = body["leader_user_id"] or None
        if lid and not await session.scalar(select(DepartmentMember.id).where(
                DepartmentMember.user_id == int(lid),
                DepartmentMember.department_id == ent.owner_department_id)):
            raise HTTPException(422, "Руководитель проекта должен быть участником подразделения")
        payload.leader_user_id = int(lid) if lid else None
    for f in (_PROJ_PAYLOAD - {"parent_uid", "grant_entity_uid",
                               "priority_indicator_uid", "leader_user_id",
                               "stages"}) & set(body):
        setattr(payload, f, body[f])
    # этапы маршрута через PATCH — с сохранением state/taskId/id/координат холста
    # (в отличие от POST, где маршрут строится заново и первый этап активен)
    if "stages" in body:
        old_by_key = {}
        for s in ((payload.stages or []) if payload else []):
            if isinstance(s, dict):
                if s.get("id"):
                    old_by_key[str(s["id"])] = s
                old_by_key.setdefault("n:" + str(s.get("name", "")).strip().lower(), s)
        merged = []
        for i, raw in enumerate(body["stages"] or []):
            if not isinstance(raw, dict) or not str(raw.get("name", "")).strip():
                continue
            prev = old_by_key.get(str(raw.get("id") or "")) \
                or old_by_key.get("n:" + str(raw.get("name", "")).strip().lower())
            st = {
                "id": str(raw.get("id") or (prev or {}).get("id") or _stage_uid()),
                "name": str(raw["name"]).strip(),
                "state": (raw.get("state") or (prev or {}).get("state")
                          or ("active" if i == 0 and not any(
                              (r.get("state") if isinstance(r, dict) else None)
                              in ("active", "done") for r in (body["stages"] or []))
                              else "pending")),
                "date": (prev or {}).get("date", ""),
                "deadline": str(raw.get("deadline") or raw.get("due")
                                or (prev or {}).get("deadline") or ""),
                "comment": (prev or {}).get("comment", ""),
                "taskId": raw.get("taskId") or (prev or {}).get("taskId") or "",
                "branches": raw.get("branches") or (prev or {}).get("branches") or [],
                "done": bool(raw.get("done")) or str(raw.get("state")) == "done"
                        or str((prev or {}).get("state")) == "done",
            }
            if st["done"] and st["state"] != "rejected":
                st["state"] = "done"
            for pos in ("x", "y"):
                v = raw.get(pos, (prev or {}).get(pos))
                if isinstance(v, (int, float)):
                    st[pos] = int(v)
            lo = [t for t in (raw.get("links_out") or (prev or {}).get("links_out") or [])
                  if isinstance(t, str)]
            if lo:
                st["links_out"] = lo
            merged.append(st)
        valid = {s["id"] for s in merged}
        for s in merged:
            if "links_out" in s:
                s["links_out"] = [t for t in s["links_out"] if t in valid]
                if not s["links_out"]:
                    del s["links_out"]
        payload.stages = merged
        await _create_stage_tasks(session, user, ent, merged,
                                  ent.owner_department_id, payload.leader_user_id)
    # «Дата окончания» проекта синхронизируется с календарной вехой due_date
    if body.get("end_date"):
        await entity_service.update_entity(
            session, user, ent, {"due_date": body["end_date"]})
    elif "due_date" in body and not body.get("end_date"):
        payload.end_date = body["due_date"] or None
    if "executor_user_ids" in body:
        try:
            await entity_service.set_executors(
                session, user, ent, [int(x) for x in (body["executor_user_ids"] or [])])
        except ValueError as e:
            raise HTTPException(422, str(e))
    if "responsible_user_id" in body:
        try:
            await entity_service.assign_responsible(
                session, user, ent, ent.owner_department_id, body["responsible_user_id"])
        except PermissionError as e:
            raise HTTPException(400, str(e))
    await session.commit()
    return {"ok": True}


@router.delete("/{uid}")
async def delete_project(uid: str, user: User = Depends(get_current_user),
                         session: AsyncSession = Depends(session_dep)):
    ent = await session.scalar(
        select(Entity).where(Entity.uid == uid, Entity.kind == EntityKind.PROJECT))
    if not ent:
        raise HTTPException(404, "Проект не найден")
    ctx = await access.build_context(session, user)
    perm = await access.resolve_permission(session, ctx, ent)
    if perm is None:
        raise HTTPException(403)
    if user.role == Role.MEMBER:
        mine = any(r.user_id == user.id for r in ent.responsibles)
        if not (mine and ent.owner_department_id in ctx.department_ids):
            raise HTTPException(403, "Участник удаляет только свои проекты")
    await entity_service.delete_entity(session, user, ent)
    await session.commit()
    return {"ok": True}
