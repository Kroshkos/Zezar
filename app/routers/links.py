"""Произвольные связи между сущностями (ядро).

  GET    /api/core/entities/{uid}/links                 — список связей сущности
  POST   /api/core/links                                {source_uid, target_uid, link_type?, note?, stage_index?}
  DELETE /api/core/links/{link_id}                       — удалить связь

Связь двусторонняя и симметричная: запись нормализуется парой (min_id, max_id),
дубликаты запрещены на уровне БД (UniqueConstraint entity_a_id+entity_b_id).
Права: чтобы связать — нужен доступ READ к обеим сущностям; чтобы удалить —
WRITE к любой из них (или автор связи).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import access
from app.core.security import get_current_user
from app.deps import session_dep
from app.models.core import Department, Entity, EntityLink, User

router = APIRouter()

# человекочитаемые подписи видов для чипов в UI
KIND_LABELS = {
    "task": "Задача", "doc": "Документ", "soft": "ПО/разработка",
    "project": "Проект", "grant": "Грант", "scholarship": "Стипендия",
    "priority": "Приоритет-2030", "meeting": "Совещание",
}


async def _get_entity(session: AsyncSession, uid: str) -> Entity:
    ent = await session.scalar(select(Entity).where(Entity.uid == uid))
    if not ent:
        raise HTTPException(404, f"Сущность «{uid}» не найдена")
    return ent


class LinkIn(BaseModel):
    source_uid: str
    target_uid: str
    link_type: str = Field(default="related", max_length=50)
    note: str | None = Field(default=None, max_length=500)
    stage_index: int | None = None


def _norm_pair(a_id: int, b_id: int) -> tuple[int, int]:
    return (a_id, b_id) if a_id < b_id else (b_id, a_id)


@router.get("/entities/{uid}/links")
async def list_links(uid: str, user: User = Depends(get_current_user),
                     session: AsyncSession = Depends(session_dep)):
    ent = await _get_entity(session, uid)
    ctx = await access.build_context(session, user)
    if await access.resolve_permission(session, ctx, ent) is None:
        raise HTTPException(403)
    rows = (await session.scalars(select(EntityLink).where(
        or_(EntityLink.entity_a_id == ent.id, EntityLink.entity_b_id == ent.id)))).all()
    out = []
    for ln in rows:
        other_id = ln.entity_b_id if ln.entity_a_id == ent.id else ln.entity_a_id
        other = await session.get(Entity, other_id)
        if other is None:
            continue
        out.append({
            "id": ln.id,
            "uid": other.uid,
            "title": other.title,
            "kind": other.kind.value,
            "kind_label": KIND_LABELS.get(other.kind.value, other.kind.value),
            "module_key": other.module_key,
            "status_key": other.status_key,
            "due_date": other.due_date,
            "link_type": ln.link_type,
            "note": ln.note,
            # этап актуален только если ЦЕЛЬ имеет маршрут; индекс храним как есть
            "stage_index": ln.stage_index,
            "stage_name": None,
        })
    # подписи этапов для связей, указывающих на этап проекта/софта (у них есть
    # маршрут в payload.stages)
    for item in out:
        if item["stage_index"] is None:
            continue
        stages = await _stages_of(session, item["uid"])
        if stages and 0 <= item["stage_index"] < len(stages):
            item["stage_name"] = (stages[item["stage_index"]] or {}).get("name")
    return out


async def _stages_of(session: AsyncSession, uid: str) -> list | None:
    """stages маршрута сущности по uid, если у вида есть маршрут (project/soft)."""
    from app.models.module_payloads import ProjectPayload, SoftPayload
    ent = await session.scalar(select(Entity).where(Entity.uid == uid))
    if ent is None:
        return None
    model = {"project": ProjectPayload, "soft": SoftPayload}.get(ent.kind.value)
    if model is None:
        return None
    row = await session.scalar(select(model).where(model.entity_id == ent.id))
    return list(row.stages or []) if row else None


@router.get("/links/search")
async def search_linkable(q: str = "", user: User = Depends(get_current_user),
                          session: AsyncSession = Depends(session_dep)):
    """Поиск доступных пользователю сущностей для связывания (по названию).

    Возвращает до 30 карточек: uid, title, kind + подписи модуля/пространства —
    достаточно для комбобокса в модалке «Связать с…».
    """
    ctx = await access.build_context(session, user)
    stmt = select(Entity).where(access.entity_filter(ctx)).order_by(Entity.id.desc()).limit(120)
    if q.strip():
        stmt = select(Entity).where(
            access.entity_filter(ctx),
            Entity.title.ilike(f"%{q.strip()[:120]}%")).order_by(Entity.id.desc()).limit(120)
    ents = (await session.scalars(stmt)).all()
    out = []
    for ent in ents:
        perm = await access.resolve_permission(session, ctx, ent)
        if perm is None:
            continue
        dept = await session.get(Department, ent.owner_department_id)
        out.append({"uid": ent.uid, "title": ent.title, "kind": ent.kind.value,
                    "kind_label": KIND_LABELS.get(ent.kind.value, ent.kind.value),
                    "module_key": ent.module_key, "status_key": ent.status_key,
                    "due_date": ent.due_date,
                    "department": dept.name if dept else "",
                    "has_stages": ent.kind.value in ("project", "soft")})
        if len(out) >= 30:
            break
    return out


@router.post("/links")
async def create_link(body: LinkIn, user: User = Depends(get_current_user),
                      session: AsyncSession = Depends(session_dep)):
    src = await _get_entity(session, body.source_uid.strip())
    tgt = await _get_entity(session, body.target_uid.strip())
    if src.id == tgt.id:
        raise HTTPException(422, "Нельзя связать сущность саму с собой")
    ctx = await access.build_context(session, user)
    if await access.resolve_permission(session, ctx, src) is None:
        raise HTTPException(403, "Нет доступа к исходной сущности")
    if await access.resolve_permission(session, ctx, tgt) is None:
        raise HTTPException(403, "Нет доступа к целевой сущности")
    if body.stage_index is not None:
        stages = await _stages_of(session, tgt.uid)
        if stages is None:
            raise HTTPException(422, "У целевой сущности нет маршрута этапов")
        if not (0 <= body.stage_index < len(stages)):
            raise HTTPException(422, "Некорректный индекс этапа")
    a, b = _norm_pair(src.id, tgt.id)
    existing = await session.scalar(select(EntityLink).where(
        EntityLink.entity_a_id == a, EntityLink.entity_b_id == b))
    if existing:
        raise HTTPException(409, "Такая связь уже существует")
    ln = EntityLink(entity_a_id=a, entity_b_id=b,
                    link_type=(body.link_type or "related").strip()[:50] or "related",
                    note=(body.note or "").strip()[:500] or None,
                    stage_index=body.stage_index, created_by_user_id=user.id)
    session.add(ln)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "Такая связь уже существует")
    return {"id": ln.id, "ok": True}


@router.delete("/links/{link_id}")
async def delete_link(link_id: int, user: User = Depends(get_current_user),
                      session: AsyncSession = Depends(session_dep)):
    ln = await session.get(EntityLink, link_id)
    if not ln:
        raise HTTPException(404, "Связь не найдена")
    ctx = await access.build_context(session, user)
    allowed = ln.created_by_user_id == user.id or ctx.is_global
    for eid in (ln.entity_a_id, ln.entity_b_id):
        ent = await session.get(Entity, eid)
        if ent is not None and access.can_write(await access.resolve_permission(session, ctx, ent)):
            allowed = True
    if not allowed:
        raise HTTPException(403, "Нет прав на удаление связи")
    await session.delete(ln)
    await session.commit()
    return {"ok": True}
