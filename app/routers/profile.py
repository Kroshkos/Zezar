"""Профили сущностей (ядро) — модульное окно по клику на название сущности.

  GET /api/core/entities/{uid}/profile

По образу карточек/модалок sipm_lite_v3.html: возвращает ЯДРО (статус, приоритет,
сроки, ответственные, исполнители, шеринги, привязанный проект, история изменений)
+ СПЕЦИФИКУ модуля (payload: этапы маршрута проекта, повестка совещания, реквизиты
документа и т.п.). Данные — только из базы данных; подписи статусов/приоритетов —
через иерархический справочник refdata (преемственность глобальных шаблонов
администратора и копий подразделения).
"""
from __future__ import annotations

import json as _json
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import access, refdata
from app.core.security import get_current_user
from app.deps import session_dep
from app.models.core import (
    AuditLog, Department, DepartmentMember, Entity, EntityKind, EntityResponsible,
    EntityShare, Permission, PriorityLevel, Role, StatusSet, User,
)
from app.models.module_payloads import (
    DocPayload, GrantPayload, MeetingPayload, PriorityPayload,
    ProjectPayload, ScholarPayload, SoftPayload, TaskPayload,
)

router = APIRouter()

_PAYLOAD_MODEL = {
    EntityKind.TASK: TaskPayload,
    EntityKind.PROJECT: ProjectPayload,
    EntityKind.DOC: DocPayload,
    EntityKind.SOFT: SoftPayload,
    EntityKind.GRANT: GrantPayload,
    EntityKind.SCHOL: ScholarPayload,
    EntityKind.PRIORITY: PriorityPayload,
    EntityKind.MEETING: MeetingPayload,
}


def _iso(v):
    """datetime/date -> ISO-строка для JSON (аудит-история)."""
    if isinstance(v, datetime):
        return v.isoformat(timespec="seconds")
    if isinstance(v, date):
        return v.isoformat()
    return str(v) if v is not None else None


async def _entity_labels(session: AsyncSession, uids: list[str]) -> dict[str, str]:
    """uid -> title для связанных сущностей (проекты, гранты, показатели)."""
    uids = [u for u in dict.fromkeys(uids) if u]
    if not uids:
        return {}
    rows = (await session.execute(
        select(Entity.uid, Entity.title).where(Entity.uid.in_(uids)))).all()
    return {u: t for u, t in rows}


async def _module_block(session: AsyncSession, ent: Entity, p) -> dict:
    """Специфика модуля для профиля (по образцу карточек прототипа)."""
    kind = ent.kind
    if kind == EntityKind.TASK:
        return {"group_key": p.group_key if p else None,
                "checklist": (p.checklist if p else []) or [],
                "wf_kind": (p.wf_kind if p else "") or "",
                "result_of": (p.result_of if p else "") or ""}
    if kind == EntityKind.PROJECT:
        stages = (p.stages if p else []) or []
        done = sum(1 for s in stages if s.get("done") or s.get("state") == "done")
        return {"code": (p.code if p else "") or "",
                "customer": (p.customer if p else "") or "",
                "start_date": (p.start_date if p else None),
                "end_date": (p.end_date if p else None),
                "leader_user_id": (p.leader_user_id if p else None),
                "parent_uid": (p.parent_uid if p else None),
                "grant_entity_uid": (p.grant_entity_uid if p else None),
                "priority_indicator_uid": (p.priority_indicator_uid if p else None),
                "stages": stages, "stage_done": done, "stage_total": len(stages),
                "progress": round(done / len(stages) * 100) if stages else 0,
                "links": (p.links if p else []) or []}
    if kind == EntityKind.DOC:
        dstages = (p.stages if p else []) or []
        ddone = sum(1 for s in dstages if s.get("done") or s.get("state") == "done")
        return {"doc_type": (p.doc_type if p else "") or "",
                "number": (p.number if p else "") or "",
                "issued_at": (p.issued_at if p else "") or "",
                "file_ref": (p.file_ref if p else "") or "",
                "stages": dstages, "stage_done": ddone, "stage_total": len(dstages),
                "progress": round(ddone / len(dstages) * 100) if dstages else 0}
    if kind == EntityKind.SOFT:
        sstages = (p.stages if p else []) or []
        sdone = sum(1 for s in sstages if s.get("done") or s.get("state") == "done")
        return {"soft_type": (p.soft_type if p else "") or "",
                "repo_url": (p.repo_url if p else "") or "",
                "env": (p.env if p else "") or "",
                "stages": sstages, "stage_done": sdone, "stage_total": len(sstages),
                "progress": round(sdone / len(sstages) * 100) if sstages else 0}
    if kind == EntityKind.GRANT:
        return {"program": (p.program if p else "") or "",
                "amount": (p.amount if p else "") or "",
                "submitted_at": (p.submitted_at if p else "") or ""}
    if kind == EntityKind.SCHOL:
        return {"person_name": (p.person_name if p else "") or "",
                "schol_type": (p.schol_type if p else "") or "",
                "amount": (p.amount if p else "") or ""}
    if kind == EntityKind.PRIORITY:
        return {"direction": (p.direction if p else "") or "",
                "metric": (p.metric if p else "") or "",
                "target_value": (p.target_value if p else "") or "",
                "current_value": (p.current_value if p else "") or ""}
    if kind == EntityKind.MEETING:
        return {"num": (p.num if p else "") or "",
                "chair": (p.chair if p else "") or "",
                "secretary": (p.secretary if p else "") or "",
                "attendees": (p.attendees if p else "") or "",
                "time": ((p.time_ if p else "") or ""),
                "location": (p.location if p else "") or "",
                "agenda": (p.agenda if p else []) or [],
                "decisions": (p.decisions if p else []) or [],
                "linked_task_uids": (p.linked_task_uids if p else []) or []}
    return {}


@router.get("/entities/{uid}/profile")
async def entity_profile(uid: str, user: User = Depends(get_current_user),
                         session: AsyncSession = Depends(session_dep)):
    ent = await session.scalar(select(Entity).where(Entity.uid == uid))
    if not ent:
        raise HTTPException(404, "Сущность не найдена")
    ctx = await access.build_context(session, user)
    perm = await access.resolve_permission(session, ctx, ent)
    if perm is None:
        raise HTTPException(403, "Нет доступа к сущности")

    # --- справочники (refdata: глобальные шаблоны + копии подразделения) ---
    dept = ent.owner_department_id
    mods = await refdata.enabled_modules(session, dept)
    statuses = await refdata.effective_statuses(session, ent.kind, dept, mods)
    priorities = await refdata.effective_priorities(session, dept, mods)
    st = next((s for s in statuses if s.key == ent.status_key), None)
    pr = next((q for q in priorities if q.key == ent.priority_key), None)

    # --- payload модуля ---
    pm = _PAYLOAD_MODEL.get(ent.kind)
    p = await session.scalar(select(pm).where(pm.entity_id == ent.id)) if pm else None
    module = await _module_block(session, ent, p)

    # --- связанные сущности (подчинённость/связанность категорий) ---
    linked: dict[str, str] = {}
    project_uid = getattr(p, "project_entity_uid", None) if p else None
    if ent.kind != EntityKind.PROJECT and project_uid:
        titles = await _entity_labels(session, [project_uid])
        if project_uid in titles:
            linked["project"] = titles[project_uid]
    if ent.kind == EntityKind.PROJECT and p:
        need = [x for x in (p.parent_uid, p.grant_entity_uid, p.priority_indicator_uid) if x]
        titles = await _entity_labels(session, need)
        if p.parent_uid and p.parent_uid in titles:
            linked["parent_project"] = titles[p.parent_uid]
        if p.grant_entity_uid and p.grant_entity_uid in titles:
            linked["grant"] = titles[p.grant_entity_uid]
        if p.priority_indicator_uid and p.priority_indicator_uid in titles:
            linked["priority_indicator"] = titles[p.priority_indicator_uid]
    if ent.kind == EntityKind.MEETING and p:
        lt = [t for t in ((p.linked_task_uids if p else []) or []) if t]
        if lt:
            tl = await _entity_labels(session, lt)
            linked["tasks"] = ", ".join(tl[t] for t in lt if t in tl)
    module["linked"] = linked

    # --- ответственные (по подразделениям) ---
    resp_out = []
    rrows = (await session.scalars(
        select(EntityResponsible).where(EntityResponsible.entity_id == ent.id)
        .order_by(EntityResponsible.is_primary.desc(), EntityResponsible.id))).all()
    for r in rrows:
        u = await session.get(User, r.user_id) if r.user_id else None
        d = await session.get(Department, r.department_id)
        resp_out.append({"department": d.name if d else "",
                         "user_name": u.display_name if u else "— не назначен —",
                         "is_primary": bool(r.is_primary)})

    # --- исполнители (denormalized extra_json, как в карточках) ---
    executors = (ent.extra_json or {}).get("executors") or []

    # --- шеринги ---
    shares = []
    for s in (await session.scalars(
            select(EntityShare).where(EntityShare.entity_id == ent.id))).all():
        d = await session.get(Department, s.target_department_id)
        shares.append({"department_id": s.target_department_id,
                       "department_name": d.name if d else "",
                       "permission": s.permission.value})

    # --- участники подразделения-владельца (для назначения в окне профиля) ---
    people = []
    mrows = (await session.execute(
        select(User.id, User.display_name, DepartmentMember.role_in_dept)
        .join(DepartmentMember, DepartmentMember.user_id == User.id)
        .where(DepartmentMember.department_id == dept)
        .order_by(User.display_name))).all()
    for uid_, name_, role_ in mrows:
        people.append({"id": uid_, "name": name_, "is_head": role_ == Role.HEAD})

    # --- история изменений (AuditLog в транзакции ядра) ---
    history = []
    arows = (await session.scalars(
        select(AuditLog).where(AuditLog.entity_id == ent.id)
        .order_by(AuditLog.at.desc(), AuditLog.id.desc()).limit(50))).all()
    for a in arows:
        actor = await session.get(User, a.user_id) if a.user_id else None
        try:
            det = a.details_json if isinstance(a.details_json, dict) \
                else (_json.loads(a.details_json) if a.details_json else {})
        except Exception:
            det = {}
        changed = det.get("changed") or {}
        history.append({"at": _iso(a.at), "actor": actor.display_name if actor else "система",
                        "action": a.action,
                        "fields": list(changed.keys()) if isinstance(changed, dict) else []})

    department = await session.get(Department, dept)
    return {
        "uid": ent.uid, "kind": ent.kind.value, "module_key": ent.module_key,
        "title": ent.title, "description": ent.description or "",
        "status": {"key": ent.status_key,
                   "label": st.label if st else (ent.status_key or "—"),
                   "color": st.color if st else "b-gray"},
        "priority": ({"key": ent.priority_key, "label": pr.label if pr else ent.priority_key,
                      "color": pr.color if pr else "b-gray"}
                     if ent.priority_key else None),
        "due_date": ent.due_date, "tags": ent.tags or [],
        "created_at": _iso(ent.created_at), "updated_at": _iso(ent.updated_at),
        "sync_state": ent.sync_state.value,
        "department_id": dept, "department_name": department.name if department else "",
        "cross_dept": bool(shares),
        "perm": perm.value, "can_edit": access.can_write(perm),
        "can_share": access.can_manage_sharing(perm),
        "responsibles": resp_out, "executors": executors,
        "shares": shares, "people": people,
        "statuses": [{"key": s.key, "label": s.label, "color": s.color} for s in statuses],
        "priorities": [{"key": q.key, "label": q.label, "color": q.color} for q in priorities],
        "module": module,
        "history": history,
    }
