"""Раздел «Карточки» модуля «Совещания»: печатные карточки для всех сущностей.

Два типа карточек (по мотивам pcardHTML() из прототипа ИС_Цезарь.html):

1. entity — сущности с workflow (проекты, ПО, документы и т.п., у payload'ов
   которых есть этапы `stages`, а также задачи проекта — этап/автозадача):
   название карточки = название сущности; содержание — чек-лист этапов
   (наименование этапа, дедлайн, ответственный); внизу — общий дедлайн,
   ответственный, участники и QR-код на страницу сущности.

2. task — автономная задача (kind=TASK без project_entity_uid): обычный вид
   карточки из примера, но вместо содержания — место для рукописных заметок;
   внизу — дедлайн, ответственный, участники и QR-код на задачу.

Права: обычные пользователи получают карточки только своего подразделения
(включая расшаренные сущности — через access.entity_filter + resolve_permission);
администраторы и координаторы (ctx.is_global) — по всей системе.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core import access
from app.models.core import Department, Entity, EntityKind, PriorityLevel, StatusSet, User
from app.models.module_payloads import (
    DocPayload, ProjectPayload, SoftPayload, TaskPayload,
)

# kind -> таблица payload'а с полем stages (workflow-сущности). Задачи (TASK)
# обрабатываются отдельно: они либо этапы проекта (тип entity), либо
# автономные задачи (тип task).
_STAGE_MODELS = {
    EntityKind.PROJECT: ProjectPayload,
    EntityKind.SOFT: SoftPayload,
    EntityKind.DOC: DocPayload,
}

KIND_LABEL = {
    "project": "Проект", "soft": "ПО / разработка", "doc": "Документ",
    "grant": "Грант", "scholarship": "Стипендия", "priority": "Показатель",
    "meeting": "Совещание", "task": "Задача",
}


async def _resp_names(session: AsyncSession, ent: Entity) -> list[str]:
    out: list[str] = []
    for r in ent.responsibles:
        if r.user_id:
            u = await session.get(User, r.user_id)
            if u and u.display_name not in out:
                out.append(u.display_name)
    return out


def _stage_rows(stages: list | None) -> list[dict]:
    """Чек-лист этапов: наименование, дедлайн, ответственный."""
    rows = []
    for s in stages or []:
        if not isinstance(s, dict):
            continue
        rows.append({
            "name": s.get("name") or "",
            "deadline": s.get("deadline") or s.get("date") or "",
            "responsible": s.get("responsible") or s.get("who") or "",
            "state": s.get("state") or "",
        })
    return rows


async def cards_api(user: User, session: AsyncSession) -> dict:
    """Список печатных карточек с учетом прав (изоляция по подразделениям)."""
    ctx = await access.build_context(session, user)
    # Справочники статусов/приоритетов (метки и цвета — как в прототипе)
    labels: dict[tuple[str, str], str] = {}
    colors: dict[str, str] = {}
    prlabels: dict[str, str] = {}
    for s in (await session.scalars(select(StatusSet))).all():
        k = s.kind.value if hasattr(s.kind, "value") else str(s.kind)
        labels[(k, s.key)] = s.label
    for p in (await session.scalars(select(PriorityLevel))).all():
        colors[p.key] = p.color
        prlabels.setdefault(p.key, p.label)

    ents = (await session.scalars(
        select(Entity).where(access.entity_filter(ctx))
        .options(selectinload(Entity.shares), selectinload(Entity.responsibles))
        .order_by(Entity.due_date.asc(), Entity.id))).all()

    # Автономные задачи (без привязки к проекту) исключаем из чек-листов;
    # заодно собираем project_uid -> [задачи-этапы] одним запросом.
    task_payloads = (await session.scalars(select(TaskPayload))).all()
    tp_by_ent = {t.entity_id: t for t in task_payloads}
    stages_by_project: dict[str, list[tuple]] = {}
    for t in task_payloads:
        if t.project_entity_uid:
            stages_by_project.setdefault(t.project_entity_uid, []).append(t.entity_id)
    task_ids = {e.id: e for e in ents if e.kind == EntityKind.TASK}

    out: list[dict] = []
    for ent in ents:
        perm = await access.resolve_permission(session, ctx, ent)
        if perm is None:
            continue
        # Инвариант изоляции пространств (см. meetings/router): MEMBER видит
        # только свои подразделения (+ расшаренное); admin/coordinator — всё.
        cross = bool(ent.shares)
        if not ctx.is_global and not cross \
                and ent.owner_department_id not in ctx.department_ids:
            continue

        payload = None
        Model = _STAGE_MODELS.get(ent.kind)
        if Model is not None:
            payload = await session.scalar(
                select(Model).where(Model.entity_id == ent.id))

        executors = [e.get("name", "") for e in (ent.extra_json or {}).get("executors") or []
                     if isinstance(e, dict) and e.get("name")]

        if ent.kind == EntityKind.TASK:
            tp = tp_by_ent.get(ent.id)
            if tp and tp.project_entity_uid:
                # Этап (автозадача) проекта — часть карточки проекта:
                # попадает в чек-лист соответствующей сущности, своей карточки нет.
                continue
            card_type, tag = "task", "Задача"
            stages: list[dict] = []
        else:
            stages = _stage_rows(getattr(payload, "stages", None)) if payload else []
            # Задачи-этапы, привязанные к сущности, тоже попадают в чек-лист
            for child_id in stages_by_project.get(ent.uid, []):
                cent = task_ids.get(child_id)
                if cent is None:
                    continue  # задача недоступна пользователю — не в чек-листе
                cresp = [r.user_id for r in cent.responsibles if r.user_id]
                cname = ""
                if cresp:
                    u = await session.get(User, cresp[0])
                    cname = u.display_name if u else ""
                stages.append({"name": cent.title,
                               "deadline": cent.due_date or "",
                               "responsible": cname,
                               "state": cent.status_key or ""})
            if not stages:
                # Сущность без workflow и без этапов — карточка-чек-лист не формируется.
                continue
            card_type = "entity"
            tag = KIND_LABEL.get(ent.kind.value, ent.kind.value)

        resp = await _resp_names(session, ent)
        out.append({
            "uid": ent.uid, "type": card_type, "kind": ent.kind.value,
            "module_key": ent.module_key, "tag": tag,
            "title": ent.title,
            "status_key": ent.status_key or "",
            "status_label": labels.get((ent.kind.value, ent.status_key or ""),
                                       ent.status_key or ""),
            "priority_key": ent.priority_key or "",
            "priority_label": prlabels.get(ent.priority_key or "", ent.priority_key or ""),
            "priority_color": colors.get(ent.priority_key or "", "b-gray"),
            "due_date": ent.due_date or "",
            "responsible": resp[0] if resp else "",
            "responsibles": resp,
            "participants": sorted(set(executors + resp)),
            "stages": stages,
            "department_id": ent.owner_department_id,
            "cross_dept": cross,
            "can_edit": access.can_write(perm),
            "url_path": f"/{ent.module_key}/",
            "profile_url": f"/{ent.module_key}/?open={ent.uid}",
        })

    if ctx.is_global:
        drows = (await session.scalars(
            select(Department).where(Department.is_active == True))).all()  # noqa: E712
    else:
        drows = (await session.scalars(
            select(Department).where(Department.id.in_(ctx.department_ids or [-1])))).all()
    return {"is_global": ctx.is_global,
            "departments": [{"id": d.id, "name": d.name} for d in drows],
            "cards": out}
