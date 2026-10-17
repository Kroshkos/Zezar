"""Группы задач для совещаний: критерии, матчинг, описания.

Реализует семантику конструктора групп из прототипа sipm_lite_v3.html:
criteria = {projectIds:[uid..], statuses:[key..], priorities:[key..], dues:[week|month|overdue]}.
Внутри параметра значения объединяются по «ИЛИ», между параметрами — по «И».
Пустой параметр = ограничение не применяется. Пустая группа = «все задачи».
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.core import Entity, EntityKind, PriorityLevel, StatusSet, TaskGroup
from app.models.module_payloads import TaskPayload

DUE_LABELS = {"week": "эта неделя", "month": "этот месяц", "overdue": "просроченные"}


def arr_of(v: Any) -> list:
    """Нормализация значения критерия в список (алиасы старых данных)."""
    if v is None:
        return []
    if isinstance(v, str):
        return [v] if v else []
    if isinstance(v, (list, tuple, set)):
        return [x for x in v if x not in (None, "")]
    return [v]


def norm_criteria(crit: Optional[dict]) -> dict:
    """Приведение criteria к каноническому виду {projectIds, statuses, priorities, dues}.

    Совместимость со старыми записями: ключ «projects» трактуется как projectIds.
    """
    c = crit or {}
    pids = arr_of(c.get("projectIds")) or arr_of(c.get("projects"))
    return {
        "projectIds": pids,
        "statuses": arr_of(c.get("statuses")),
        "priorities": arr_of(c.get("priorities")),
        "dues": arr_of(c.get("dues")),
    }


def _iso(d: date) -> str:
    return d.isoformat()


def match_due(deadline: Optional[str], du: str, today: Optional[date] = None) -> bool:
    """Срок исполнения задачи относительно сегодня (как matchDue в демо)."""
    if not deadline:
        return False
    today = today or date.today()
    t = _iso(today)
    if du == "week":
        return deadline >= t and deadline <= _iso(today + timedelta(days=7))
    if du == "month":
        return deadline >= t and deadline <= _iso(today + timedelta(days=30))
    if du == "overdue":
        return deadline < t  # статус done исключается вызывающим кодом
    return False


def task_matches(t: dict, crit: dict) -> bool:
    """Матчинг сериализованной задачи {projectId,status,priority,due_date,status_key} с критериями."""
    if crit["projectIds"] and (t.get("projectId") or "") not in crit["projectIds"]:
        return False
    if crit["statuses"] and t.get("status") not in crit["statuses"]:
        return False
    if crit["priorities"] and t.get("priority") not in crit["priorities"]:
        return False
    if crit["dues"]:
        done = t.get("status") == "done"
        if not any(match_due(t.get("due_date"), du) and not (du == "overdue" and done)
                   for du in crit["dues"]):
            return False
    return True


def _group_desc(crit: dict, proj_names: Optional[dict] = None,
                status_labels: Optional[dict] = None,
                prio_labels: Optional[dict] = None) -> str:
    """Человекочитаемое описание группы (как grpDesc в демо)."""
    parts = []
    if crit["projectIds"]:
        names = [((proj_names or {}).get(p) or p)[:18] for p in crit["projectIds"]]
        parts.append("проекты: " + ", ".join(names))
    if crit["statuses"]:
        labels = [((status_labels or {}).get(k) or k) for k in crit["statuses"]]
        parts.append("статусы: " + ", ".join(labels))
    if crit["priorities"]:
        labels = [((prio_labels or {}).get(k) or k) for k in crit["priorities"]]
        parts.append("приоритеты: " + ", ".join(labels))
    if crit["dues"]:
        parts.append("срок: " + ", ".join(DUE_LABELS.get(d, d) for d in crit["dues"]))
    return " · ".join(parts) if parts else "все задачи"


async def count_matches_for_groups(session: AsyncSession, dept_id: Optional[int],
                                   groups: Iterable[TaskGroup]) -> dict[int, int]:
    """Счётчик соответствующих задач для каждой активной группы.

    dept_id=None (режим «Вся система») — считаются задачи всех пространств;
    иначе — только задачи подразделения (owner_department_id).
    """
    groups = [g for g in groups if getattr(g, "is_active", True)]
    if not groups:
        return {}
    conds = [Entity.kind == EntityKind.TASK]
    if dept_id is not None:
        conds.append(Entity.owner_department_id == dept_id)
    rows = (await session.execute(
        select(TaskPayload.project_entity_uid, Entity.status_key,
               Entity.priority_key, Entity.due_date)
        .join(Entity, Entity.id == TaskPayload.entity_id)
        .where(*conds))).all()
    tasks = [{"projectId": pu, "status": sk, "priority": pk, "due_date": dd}
             for (pu, sk, pk, dd) in rows]
    out: dict[int, int] = {}
    for g in groups:
        crit = norm_criteria(g.criteria)
        out[g.id] = sum(1 for t in tasks if task_matches(t, crit))
    return out


async def group_options(session: AsyncSession, dept_id: Optional[int]) -> dict:
    """Данные для конструктора групп в refdata.js: проекты/статусы/приоритеты подразделения."""
    from app.models.core import Department  # локальный импорт во избе циклов
    
    # Проекты: uid + подпись (код · название)
    pq = (select(Entity.uid, Entity.title, Entity.extra_json)
          .where(Entity.kind == EntityKind.PROJECT))
    if dept_id is not None:
        pq = pq.where(Entity.owner_department_id == dept_id)
    projects = []
    for uid_, title, extra in (await session.execute(pq)).all():
        code = (extra or {}).get("code") or ""
        projects.append({"id": uid_,
                         "label": (f"{code} · {title}" if code else title)[:60]})

    # Статусы задач: подразделение + глобальные шаблоны (подразделение переопределяет по key)
    sq = select(StatusSet).where(StatusSet.kind == EntityKind.TASK,
                                 StatusSet.is_active.is_(True))
    if dept_id is not None:
        sq = sq.where(StatusSet.department_id.in_([dept_id, None]))
    statuses = [{"key": s.key, "label": s.label}
                for s in sorted((await session.scalars(sq.order_by(StatusSet.order))).all(),
                                key=lambda s: (s.department_id is None, s.order))]

    pq2 = select(PriorityLevel).where(PriorityLevel.is_active.is_(True))
    if dept_id is not None:
        pq2 = pq2.where(PriorityLevel.department_id.in_([dept_id, None]))
    priorities = [{"key": p.key, "label": p.label}
                  for p in (await session.scalars(pq2.order_by(PriorityLevel.weight))).all()]

    dues = [{"value": k, "label": v} for k, v in DUE_LABELS.items()]
    return {"projects": projects, "statuses": statuses,
            "priorities": priorities, "dues": dues}
