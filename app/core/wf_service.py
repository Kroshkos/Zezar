"""Общий сервис маршрутов (workflow) для модулей «Документы» и «ПО».

Структура этапа — идентична проектной (ProjectPayload.stages), поэтому холст
wf_canvas.js и профильные виджеты работают без изменений:
  {id, name, state(pending|active|done|rejected), date, deadline, comment,
   taskId, branches[], x, y, links_out[]}

API:
  wf_build_stages(raw)          — нормализация этапов при создании (POST)
  wf_merge_stages(old, new)     — слияние при редактировании (PATCH): сохраняет
                                  id/state/date/comment/taskId/координаты по id/имени
  wf_create_stage_tasks(...)    — автозадачи новых этапов (как в модуле projects)
"""
from __future__ import annotations

import uuid as _uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import entity_service
from app.models.core import Entity, EntityKind, User
from app.models.module_payloads import TaskPayload


def _stage_uid() -> str:
    return _uuid.uuid4().hex[:10]


def wf_build_stages(raw: list) -> list[dict]:
    """Нормализация «сырых» этапов: id/state/deadline + координаты холста.
    Первый этап активен, остальные — в ожидании (как в projects._build_stages)."""
    out: list[dict] = []
    for i, s in enumerate(raw or []):
        if not isinstance(s, dict) or not str(s.get("name", "")).strip():
            continue
        st = {
            "id": str(s.get("id") or _stage_uid()),
            "name": str(s["name"]).strip(),
            "state": ("active" if i == 0 else "pending"),
            "date": "", "deadline": str(s.get("deadline") or s.get("due") or ""),
            "comment": "", "taskId": "", "branches": s.get("branches") or [],
        }
        for pos in ("x", "y"):
            v = s.get(pos)
            if isinstance(v, (int, float)):
                st[pos] = int(v)
        lo = [t for t in (s.get("links_out") or []) if isinstance(t, str)]
        if lo:
            st["links_out"] = lo
        out.append(st)
    valid = {st["id"] for st in out}
    for st in out:
        if "links_out" in st:
            st["links_out"] = [t for t in st["links_out"] if t in valid]
            if not st["links_out"]:
                del st["links_out"]
    return out


def wf_merge_stages(old: list | None, new_raw: list | None) -> list[dict]:
    """Слияние изменённого маршрута со старым (по образцу PATCH /api/projects):
    сохраняются state/date/comment/taskId/id и координаты холста; новые этапы
    получают id и pending-state."""
    old_by_key: dict[str, dict] = {}
    for s in (old or []):
        if isinstance(s, dict):
            if s.get("id"):
                old_by_key[str(s["id"])] = s
            old_by_key.setdefault("n:" + str(s.get("name", "")).strip().lower(), s)
    merged: list[dict] = []
    rows = [r for r in (new_raw or []) if isinstance(r, dict)
            and str(r.get("name", "")).strip()]
    any_active = any(str(r.get("state")) in ("active", "done") for r in rows)
    for i, raw in enumerate(rows):
        prev = old_by_key.get(str(raw.get("id") or "")) \
            or old_by_key.get("n:" + str(raw.get("name", "")).strip().lower())
        prev = prev or {}
        st = {
            "id": str(raw.get("id") or prev.get("id") or _stage_uid()),
            "name": str(raw["name"]).strip(),
            "state": (raw.get("state") or prev.get("state")
                      or ("active" if i == 0 and not any_active else "pending")),
            "date": prev.get("date", ""),
            "deadline": str(raw.get("deadline") or raw.get("due")
                            or prev.get("deadline") or ""),
            "comment": prev.get("comment", ""),
            "taskId": raw.get("taskId") or prev.get("taskId") or "",
            "branches": raw.get("branches") or prev.get("branches") or [],
            "done": bool(raw.get("done")) or str(raw.get("state")) == "done"
                    or str(prev.get("state")) == "done",
        }
        if st["done"] and st["state"] != "rejected":
            st["state"] = "done"
        for pos in ("x", "y"):
            v = raw.get(pos, prev.get(pos))
            if isinstance(v, (int, float)):
                st[pos] = int(v)
        lo = [t for t in (raw.get("links_out") or prev.get("links_out") or [])
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
    return merged


async def wf_create_stage_tasks(session: AsyncSession, user: User, ent: Entity,
                                stages: list[dict], dept_id: int,
                                title_prefix: str = "") -> int:
    """Автозадачи для этапов БЕЗ taskId (и шагов ответвлений). Возвращает число
    созданных задач. Исполнитель — автор сущности; срок — дедлайн этапа."""
    created = 0

    async def _mk_task(title: str, desc: str, due: str | None, resp_id: int):
        nonlocal created
        t = await entity_service.create_entity(
            session, user, kind=EntityKind.TASK, module_key="tasks",
            owner_department_id=dept_id, title=title, description=desc,
            status_key="new", due_date=due or None, responsible_user_id=resp_id)
        session.add(TaskPayload(entity_id=t.id, project_entity_uid=None))
        created += 1
        return t

    for s in stages:
        if s.get("taskId"):
            continue
        t = await _mk_task(
            f"{title_prefix}{ent.title} — этап: {s['name']}",
            "Автозадача этапа «" + s["name"] + "». Завершение задачи закрывает этап.",
            s.get("deadline") or None, user.id)
        s["taskId"] = t.uid
    for s in stages:
        for br in (s.get("branches") or []):
            if not isinstance(br, dict):
                continue
            for bs in (br.get("steps") or []):
                if isinstance(bs, dict) and not bs.get("taskId") \
                        and str(bs.get("name", "")).strip():
                    t = await _mk_task(
                        f"{title_prefix}{ent.title} — ветка «{br.get('name', '')}»:"
                        f" {bs.get('name', '')}",
                        f"Автозадача этапа ветки «{br.get('name', '')}».",
                        bs.get("deadline") or None, user.id)
                    bs["taskId"] = t.uid
    return created


def wf_stats(stages: Any) -> tuple[int, int]:
    """(done, total) по списку этапов — для карточек/прогресса."""
    st = [s for s in (stages or []) if isinstance(s, dict)]
    done = sum(1 for s in st if s.get("done") or s.get("state") == "done")
    return done, len(st)


# --- Автозакрытие этапов маршрута при завершении автозадачи -----------------
# Замыкание «Задача → этап»: taskId хранится в payload.stages сущности-носителя
# маршрута (project/doc/soft). При переводе задачи в done этап должен
# закрываться и маршрут должен переходить на следующий этап — раньше этого
# не происходило, потому что модуль tasks вообще не знал о существовании
# этапов (пользовательский баг: «отмечаю автозадачу выполненной, а следующий
# этап не активируется»).

_TASK_DONE_KEYS = {"done", "closed", "completed", "finished"}


def is_task_done(status_key: str | None) -> bool:
    return str(status_key or "").strip().lower() in _TASK_DONE_KEYS


def _is_closed(s: dict) -> bool:
    return bool(s.get("done")) or str(s.get("state")) == "done"


def _activate_next(stages: list[dict], from_idx: int) -> bool:
    """Активирует первый незакрытый этап после from_idx (в порядке холста;
    связи links_out учитываются только если образуют однозначную цепочку)."""
    ids = [s.get("id") for s in stages]
    order = list(range(from_idx + 1, len(stages)))
    # если у этапа есть явные связи — идём по ним первыми
    src = stages[from_idx]
    linked = [i for i, sid in enumerate(ids)
              if sid in (src.get("links_out") or [])]
    for i in linked + order:
        s = stages[i]
        if not _is_closed(s) and s.get("state") != "rejected":
            if s.get("state") != "active":
                s["state"] = "active"
                return True
            return False  # следующий уже активен — ничего менять не нужно
    return False


def wf_close_stage_by_task(stages: list[dict], task_uid: str) -> dict | None:
    """Закрывает этап с taskId==task_uid и активирует следующий.
    Возвращает {'stage': <имя закрытого>, 'next': <имя следующего>|None} или
    None, если этап не найден (ничего не меняем)."""
    rows = [s for s in (stages or []) if isinstance(s, dict)]
    idx = next((i for i, s in enumerate(rows)
                if s.get("taskId") and str(s["taskId"]) == str(task_uid)), None)
    if idx is None:
        return None
    cur = rows[idx]
    changed = False
    if not _is_closed(cur):
        cur["state"] = "done"
        cur["done"] = True
        changed = True
    # шаги ответвлений того же этапа
    for br in (cur.get("branches") or []):
        if not isinstance(br, dict):
            continue
        for bs in (br.get("steps") or []):
            if isinstance(bs, dict) and str(bs.get("taskId") or "") == str(task_uid) \
                    and not _is_closed(bs):
                bs["state"] = "done"
                bs["done"] = True
                changed = True
    nxt_name = None
    if _activate_next(rows, idx):
        changed = True
        nxt = next((s for s in rows[idx + 1:] if not _is_closed(s)), None)
        nxt_name = str(nxt.get("name") or "") if nxt else None
    if changed:
        return {"stage": str(cur.get("name") or ""), "next": nxt_name}
    # этап уже был закрыт — вернём имя для корректного ответа API
    return {"stage": str(cur.get("name") or ""), "next": None, "noop": True} \
        if _is_closed(cur) else None


def wf_find_owner_payload_cls(kind: Any) -> Any | None:
    """payload-модель сущности-носителя маршрута по EntityKind."""
    from app.models.core import EntityKind
    from app.models.module_payloads import DocPayload, ProjectPayload, SoftPayload
    return {EntityKind.PROJECT: ProjectPayload, EntityKind.DOC: DocPayload,
            EntityKind.SOFT: SoftPayload}.get(kind)


async def wf_link_task_to_stage(session: AsyncSession, task_uid: str,
                                owner_uid: str, stage_id: str) -> bool:
    """Ручная привязка задачи к этапу маршрута (замыкает «Задача → этап», если
    автозадача была создана отдельно или taskId у этапа пуст). Возвращает True,
    если привязка выполнена."""
    from app.models.core import Entity
    ent = await session.scalar(select(Entity).where(Entity.uid == owner_uid))
    if ent is None:
        return False
    cls = wf_find_owner_payload_cls(ent.kind)
    if cls is None:
        return False
    pl = await session.scalar(select(cls).where(cls.entity_id == ent.id))
    if pl is None:
        return False
    stages = [s for s in (pl.stages or []) if isinstance(s, dict)]
    hit = False
    for s in stages:
        if str(s.get("id") or "") == str(stage_id):
            s["taskId"] = task_uid
            hit = True
        for br in (s.get("branches") or []):
            if not isinstance(br, dict):
                continue
            for bs in (br.get("steps") or []):
                if isinstance(bs, dict) and str(bs.get("id") or "") == str(stage_id):
                    bs["taskId"] = task_uid
                    hit = True
    if hit:
        pl.stages = stages
        await session.flush()
    return hit


def wf_payload_entity_join():
    """Вспомогательное: payload-таблица связана с entities через entity_id."""
    from app.models.module_payloads import ProjectPayload
    return ProjectPayload.entity_id


async def wf_close_stage_for_task(session: AsyncSession, user: User,
                                  task: Entity) -> dict | None:
    """Замыкает «Задача → этап»: ищет носителя маршрута (project/doc/soft),
    у которого есть этап с taskId == uid задачи, закрывает его и активирует
    следующий. Возвращает {'owner': <Entity>, 'stage': ..., 'next': ...} или
    None, если связь не найдена.

    Пузы поиска (по приоритету):
      1) TaskPayload.project_entity_uid — задача создана как автозадача этапа;
      2) описания задач вида «… — этап: …» / «автозадача этапа «…»» — легаси,
         где project_entity_uid не заполнялся;
      3) EntityLink задача ↔ носитель маршрута (ручная привязка).
    """
    from app.models.core import Entity, EntityLink
    from app.models.module_payloads import TaskPayload

    owner = None
    tp = await session.scalar(
        select(TaskPayload).where(TaskPayload.entity_id == task.id))
    if tp and getattr(tp, "project_entity_uid", None):
        owner = await session.scalar(
            select(Entity).where(Entity.uid == tp.project_entity_uid))

    # 2) эвристика по описанию автозадачи (легаси-данные без project_entity_uid)
    desc = str(task.description or "")
    m_name = ""
    if not owner:
        import re
        m = re.search(r"— этап:\s*(.+)$", desc) or \
            re.search(r"автозадача этапа «(.+?)»", desc) or \
            re.search(r"ветки «(.+?)»", desc)
        if m:
            m_name = (m.group(1) or "").strip()

    candidates: list[Entity] = []
    if owner is not None:
        candidates.append(owner)
    if m_name or not candidates:
        # все носители маршрутов того же подразделения + связанные через EntityLink
        linked_ids = set()
        for link in await session.scalars(select(EntityLink).where(
                (EntityLink.entity_a_id == task.id)
                | (EntityLink.entity_b_id == task.id))):
            linked_ids.add(link.entity_b_id if link.entity_a_id == task.id
                           else link.entity_a_id)
        q = select(Entity).where(Entity.kind.in_(
            ("project", "doc", "soft")))
        if linked_ids:
            q = q.where(Entity.id.in_(sorted(linked_ids)))
        elif task.owner_department_id:
            q = select(Entity).where(
                Entity.kind.in_(("project", "doc", "soft")),
                Entity.owner_department_id == task.owner_department_id)
        for e in await session.scalars(q):
            if e not in candidates:
                candidates.append(e)

    for ent in candidates:
        cls = wf_find_owner_payload_cls(ent.kind)
        if cls is None:
            continue
        pl = await session.scalar(select(cls).where(cls.entity_id == ent.id))
        if pl is None or not pl.stages:
            continue
        stages = [s for s in pl.stages if isinstance(s, dict)]
        hit = _find_stage_by_task_or_name(stages, task.uid, m_name)
        if hit is None:
            continue
        res = wf_close_stage_by_task(stages, task.uid)
        if res is None and m_name:
            # этап найден по имени, но taskId пуст — привяжем и закроем вручную
            res = _close_stage_row(stages, hit)
        pl.stages = stages
        await session.flush()
        return {"owner": ent, **res}
    return None


def _find_stage_by_task_or_name(stages: list[dict], task_uid: str,
                                name: str) -> dict | None:
    for s in stages:
        if str(s.get("taskId") or "") == str(task_uid):
            return s
        if name and str(s.get("name", "")).strip().lower() == name.lower():
            return s
        for br in (s.get("branches") or []):
            if not isinstance(br, dict):
                continue
            for bs in (br.get("steps") or []):
                if isinstance(bs, dict) and (
                        str(bs.get("taskId") or "") == str(task_uid)
                        or (name and str(bs.get("name", "")).strip().lower()
                            == name.lower())):
                    return bs
    return None


def _close_stage_row(stages: list[dict], row: dict) -> dict:
    """Закрывает конкретный этап/шаг (найденный по имени) и активирует
    следующий незакрытый этап."""
    idx = next((i for i, s in enumerate(stages)
                if s is row or row in (s.get("branches") or [])
                or any(bs is row for br in (s.get("branches") or [])
                       if isinstance(br, dict)
                       for bs in (br.get("steps") or []))), None)
    if idx is None:
        idx = len(stages) - 1
    cur = stages[idx]
    if not _is_closed(cur):
        cur["state"] = "done"
        cur["done"] = True
    _activate_next(stages, idx)
    nxt = next((s for s in stages[idx + 1:] if not _is_closed(s)), None)
    return {"stage": str(cur.get("name") or ""),
            "next": str(nxt.get("name") or "") if nxt else None}
