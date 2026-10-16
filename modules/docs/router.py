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

from app.core import access, entity_service, refdata
from app.core.security import get_current_user
from app.deps import session_dep
from app.models.core import (
    Department, Entity, EntityKind, EntityShare, Permission, StatusSet, User,
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
    due_date: str | None = None              # ISO-дата документа (поле ядра -> календарь)
    doc_type: str | None = ""                # вид документа (docTypes из демо)
    number: str | None = ""                  # номер документа
    issued_at: str | None = ""               # дата выдачи/регистрации
    file_ref: str | None = ""                # ссылка на файл
    description: str | None = ""
    department_id: int | None = None         # для админа/координатора: чьё пространство
    responsible_user_id: int | None = None
    share_with: list[int] = []               # межпространственный контекст: dept_id[]


async def _load_payload(session: AsyncSession, ent_id: int) -> DocPayload | None:
    return await session.scalar(select(DocPayload).where(DocPayload.entity_id == ent_id))


def _doc_dict(ent: Entity, p: DocPayload | None, perm: Permission | None,
              resp_names: list[str], cross_dept: bool = False) -> dict:
    return {
        "uid": ent.uid, "title": ent.title, "description": ent.description or "",
        "status_key": ent.status_key, "due_date": ent.due_date,
        "department_id": ent.owner_department_id,
        "cross_dept": cross_dept,
        "doc_type": p.doc_type if p else "", "number": p.number if p else "",
        "issued_at": p.issued_at if p else "", "file_ref": p.file_ref if p else "",
        "responsibles": resp_names,
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
    if body.department_id:
        if user.role not in (Role.ADMIN, Role.COORDINATOR):
            raise HTTPException(403, "Выбор чужого пространства доступен только админу/координатору")
        if not await session.get(Department, body.department_id):
            raise HTTPException(404, "Подразделение не найдено")
        return body.department_id
    if not mine:
        raise HTTPException(400, "Вы не состоите ни в одном подразделении")
    return mine[0]


# ---------------- HTML ----------------

@router.get("")
@router.get("/")
async def docs_page(request: Request, user: User = Depends(get_current_user),
                    session: AsyncSession = Depends(session_dep)):
    ctx = await access.build_context(session, user)
    ents = (await session.scalars(
        select(Entity).where(Entity.kind == EntityKind.DOC, access.entity_filter(ctx))
        .options(selectinload(Entity.shares), selectinload(Entity.responsibles))
        .order_by(Entity.due_date.desc()))).all()
    items = []
    for ent in ents:
        perm = await access.resolve_permission(session, ctx, ent)
        if perm is None:
            continue
        cross = await _has_shares(session, ent)
        if not ctx.is_global and not cross \
                and ent.owner_department_id not in ctx.department_ids:
            continue
        resp_names = []
        for r in ent.responsibles:
            if r.user_id:
                u = await session.get(User, r.user_id)
                if u:
                    resp_names.append(u.display_name)
        items.append(_doc_dict(ent, await _load_payload(session, ent.id), perm,
                               resp_names, cross_dept=cross))
    # Справочник — иерархический (ядро app/core/refdata): глобальные шаблоны
    # администратора + собственные/активированные записи подразделения; набор
    # ограничен подключённым модулем данного workspace.
    dept_ctx = await refdata.context_dept(session, user, None)
    mods = await refdata.enabled_modules(session, dept_ctx)
    statuses = await refdata.effective_statuses(
        session, EntityKind.DOC, dept_ctx, mods)
    depts: list[dict] = []
    if ctx.is_global:
        depts = [{"id": d.id, "name": d.name} for d in
                 (await session.scalars(
                     select(Department).where(Department.is_active == True))).all()]  # noqa: E712
    return await render_page(
        request, "docs.html", module_key=MODULE_KEY, user=user, session=session,
        page_title="Документы",
        page_sub="Реестр документов со статусами; события синхронизируются с Яндекс.Календарём",
        counts={MODULE_KEY: len(items)},
        docs=items,
        statuses=[{"key": s.key, "label": s.label, "color": s.color} for s in statuses],
        departments=depts,
    )


# ---------------- REST ----------------

@router.get("/api-list")
async def list_docs(user: User = Depends(get_current_user),
                    session: AsyncSession = Depends(session_dep)):
    ctx = await access.build_context(session, user)
    ents = (await session.scalars(
        select(Entity).where(Entity.kind == EntityKind.DOC, access.entity_filter(ctx))
        .options(selectinload(Entity.shares), selectinload(Entity.responsibles)))).all()
    out = []
    for ent in ents:
        perm = await access.resolve_permission(session, ctx, ent)
        if perm is None:
            continue
        cross = await _has_shares(session, ent)
        if not ctx.is_global and not cross \
                and ent.owner_department_id not in ctx.department_ids:
            continue
        resp_names = []
        for r in ent.responsibles:
            if r.user_id:
                u = await session.get(User, r.user_id)
                if u:
                    resp_names.append(u.display_name)
        out.append(_doc_dict(ent, await _load_payload(session, ent.id), perm,
                             resp_names, cross_dept=cross))
    return out


@router.post("")
async def create_doc(body: DocIn, user: User = Depends(get_current_user),
                     session: AsyncSession = Depends(session_dep)):
    dept_id = await _pick_department(session, user, body)
    ent = await entity_service.create_entity(
        session, user, kind=EntityKind.DOC, module_key=MODULE_KEY,
        owner_department_id=dept_id, title=body.title.strip(),
        description=body.description or None, status_key=body.status_key,
        due_date=body.due_date or None,
        responsible_user_id=body.responsible_user_id or user.id,
        share_with=[(d, Permission.EDIT) for d in dict.fromkeys(body.share_with)]
        if body.share_with else None,
    )
    session.add(DocPayload(
        entity_id=ent.id, doc_type=body.doc_type or None, number=body.number or None,
        issued_at=body.issued_at or None, file_ref=body.file_ref or None))
    await session.flush()
    await session.commit()
    return {"uid": ent.uid, "ok": True}


_DOC_FIELDS = {"doc_type", "number", "issued_at", "file_ref"}


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
    payload = await _load_payload(session, ent.id)
    if payload:
        for f in _DOC_FIELDS:
            if f in body:
                setattr(payload, f, body[f])
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
