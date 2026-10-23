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

from datetime import datetime, timedelta, timezone

from sqlalchemy import Text, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core import access
from app.db.base import utcnow
from app.models.core import (
    CardPrintLog, Department, Entity, EntityKind, PriorityLevel, StatusSet, User,
)
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


def _pick(refs: list, dept: int | None):
    """Справка подразделения важнее глобального шаблона (department_id=NULL)."""
    own = [r for r in refs if r.department_id == dept]
    return own[-1] if own else (refs[0] if refs else None)


async def cards_api(user: User, session: AsyncSession) -> dict:
    """Список печатных карточек с учетом прав (изоляция по подразделениям)."""
    ctx = await access.build_context(session, user)
    # Справочники статусов/приоритетов (метки и цвета — как в прототипе).
    # Глобальные шаблоны + собственные справочники подразделений; при коллизии
    # ключа приоритет у справки конкретного подразделения.
    labels: dict[tuple[str, str], str] = {}
    stcolor: dict[tuple[str, str], str] = {}
    prio_refs: dict[tuple[int | None, str], list] = {}
    for s in (await session.scalars(
            select(StatusSet).where(StatusSet.is_active == True))).all():  # noqa: E712
        k = s.kind.value if hasattr(s.kind, "value") else str(s.kind)
        labels[(k, s.key)] = s.label
        stcolor[(k, s.key)] = s.color or "b-gray"
    for p in (await session.scalars(
            select(PriorityLevel).where(PriorityLevel.is_active == True))).all():  # noqa: E712
        prio_refs.setdefault((p.department_id, p.key), []).append(p)

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

    # Журнал печати: последняя дата печати каждой сущности (фильтр
    # «ещё не печатались» на клиенте). Отдельный запрос — без изменения
    # updated_at самих сущностей.
    # CAST(... AS TEXT): без него SQLAlchemy возвращает datetime-объекты и
    # print-log не может записать в них ISO-строку (TypeError на сервере).
    printed_map = dict((await session.execute(
        select(CardPrintLog.entity_id,
               func.cast(func.max(CardPrintLog.printed_at), Text))
        .group_by(CardPrintLog.entity_id))).all())

    def _iso(dt):
        """DateTime (SQLite отдаёт naive) или строка -> ISO с меткой времени для JS."""
        if dt is None:
            return ""
        if isinstance(dt, str):
            return dt
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()

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
        pk = ent.priority_key or ""
        pref = _pick(prio_refs.get((ent.owner_department_id, pk), [])
                     + prio_refs.get((None, pk), []), ent.owner_department_id)
        out.append({
            "uid": ent.uid, "type": card_type, "kind": ent.kind.value,
            "module_key": ent.module_key, "tag": tag,
            "title": ent.title,
            "status_key": ent.status_key or "",
            "status_label": labels.get((ent.kind.value, ent.status_key or ""),
                                       ent.status_key or ""),
            "status_color": stcolor.get((ent.kind.value, ent.status_key or ""), "b-gray"),
            "priority_key": pk,
            "priority_label": (pref.label if pref else "") or pk,
            "priority_color": (pref.color if pref else "b-gray") or "b-gray",
            "due_date": ent.due_date or "",
            "responsible": resp[0] if resp else "",
            "responsibles": resp,
            "participants": sorted(set(executors + resp)),
            "stages": stages,
            "department_id": ent.owner_department_id,
            "cross_dept": cross,
            "can_edit": access.can_write(perm),
            # Для фильтров «изменения за день/неделю» и «ещё не печатались»
            "created_at": _iso(ent.created_at),
            "updated_at": _iso(ent.updated_at),
            "printed_at": _iso(printed_map.get(ent.id)),
            "url_path": f"/{ent.module_key}/",
            # QR ведёт на страницу модуля и открывает профиль сущности
            # (общий механизм ?entity=<uid> из static/js/profile.js)
            "profile_url": f"/{ent.module_key}/?entity={ent.uid}",
        })

    if ctx.is_global:
        drows = (await session.scalars(
            select(Department).where(Department.is_active == True))).all()  # noqa: E712
    else:
        drows = (await session.scalars(
            select(Department).where(Department.id.in_(ctx.department_ids or [-1])))).all()
    return {"is_global": ctx.is_global,
            "departments": [{"id": d.id, "name": d.name, "color": d.color or ""} for d in drows],
            "cards": out}


async def register_print(session: AsyncSession, user: User, uids: list[str]) -> int:
    """Записать факт печати карточек (журнал CardPrintLog).

    Пишем по entity_uid; чужие/недоступные uid просто не находятся в реестре.
    Возвращает количество записанных строк.
    """
    if not uids:
        return 0
    ids = dict((await session.execute(
        select(Entity.uid, Entity.id).where(Entity.uid.in_(uids[:2000])))).all())
    now = utcnow()
    n = 0
    for uid in set(uids):
        ent_id = ids.get(uid)
        if ent_id is None:
            continue
        session.add(CardPrintLog(entity_id=ent_id, user_id=user.id, printed_at=now))
        n += 1
    await session.commit()
    return n
