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
