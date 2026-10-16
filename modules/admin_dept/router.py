"""Модуль-плагин «Администрирование подразделения» (базовый для каждого workspace).

Управление пространством без обращения к ядру-админу:
  - участники подразделения (Head/Координатор добавляет MEMBER, меняет роль_in_dept);
  - включение/выключение НЕбазовых модулей в рабочем пространстве;
  - обзор расшаренных сущностей пространства ( outgoing / incoming ) и ответственных.

Права (ролевая модель ТЗ):
  - Admin/Coordinator — любое подразделение;
  - Head — своё подразделение (role_in_dept=HEAD);
  - Member — только просмотр своего состава участников (без mutations).
Данные берутся из существующих эндпоинтов ядра (/api/core/departments/*) — здесь
только HTML-страница + агрегирующий JSON для неё, чтобы не дублировать бизнес-логику.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core import access
from app.core.module_registry import registry
from app.core.security import get_current_user
from app.deps import session_dep
from app.models.core import (
    Department, DepartmentMember, Entity, EntityShare, Permission, Role, User,
    Workspace, WorkspaceModule,
)
from app.ui import render_page

router = APIRouter()

MODULE_KEY = "admin_dept"


async def _pick_department(session: AsyncSession, ctx: access.AccessContext,
                           dept_id: int | None) -> Department:
    """Выбор управляемого подразделения с проверкой прав (изоляция пространств).

    Если department_id не передан — подразделение определяется по контексту:
    HEAD/MEMBER — своё единственное членство; ADMIN/COORDINATOR — первое из своих
    членств, а если членств нет — самое раннее активное подразделение системы.
    Переключить пространство админ/координатор может выпадающим списком на странице.
    """
    if dept_id is None:
        if ctx.is_global:
            # Глобальные роли (ADMIN/COORDINATOR) не обязаны состоять в
            # подразделениях — открываем самое раннее активное пространство;
            # если подразделений нет — пустое состояние со ссылкой создания.
            dept_id = await session.scalar(
                select(Department.id)
                .where(Department.is_active == True)  # noqa: E712
                .order_by(Department.id).limit(1))
            if dept_id is None:
                return None      # пустое состояние: страница предложит создать пространство
        elif ctx.department_ids:
            dept_id = next(iter(sorted(ctx.department_ids)))
        else:
            raise HTTPException(403, "Вы не состоите ни в одном подразделении")
    dept = await session.get(Department, dept_id)
    if dept is None or not dept.is_active:
        # Пространство удалено/деактивировано (например, устаревший bookmark или
        # ссылка из кэшированной страницы) — для глобальных ролей возвращаем
        # None, чтобы страница показала актуальный список и форму создания,
        # вместо «сырого» 404.
        if ctx.is_global:
            return None
        raise HTTPException(404, "Подразделение не найдено")
    if not ctx.is_global and dept.id not in ctx.department_ids:
        raise HTTPException(403, "Нет доступа к этому подразделению")
    return dept


@router.get("/")
async def admin_dept_page(request: Request, department_id: int | None = None,
                          tab: str | None = None,
                          user: User = Depends(get_current_user),
                          session: AsyncSession = Depends(session_dep)):
    """Единая административная панель (всё в одном окне — вкладки).

    Наполнение зависит от роли: администратор/координатор видит все
    подразделения, учётные записи и журнал целиком; руководитель — только
    своё подразделение (участники, модули, шеринг, состав своего отдела,
    журнал своего подразделения)."""
    ctx = await access.build_context(session, user)
    dept = await _pick_department(session, ctx, department_id)

    async def _dept_list(include_inactive=False):
        """Агрегаты по всем подразделениям для вкладки «Подразделения»."""
        dsel = select(Department)
        if not include_inactive:
            dsel = dsel.where(Department.is_active == True)  # noqa: E712
        rows = (await session.scalars(dsel.order_by(Department.name))).all()
        total_mods = len(registry.all())
        out = []
        for d in rows:
            mc = await session.scalar(
                select(func.count(DepartmentMember.id)).where(
                    DepartmentMember.department_id == d.id)) or 0
            ws = await session.scalar(select(Workspace).where(Workspace.department_id == d.id))
            en = 0
            if ws:
                wm = (await session.scalars(select(WorkspaceModule).where(
                    WorkspaceModule.workspace_id == ws.id))).all()
                en = sum(1 for x in wm if x.is_enabled)
            out.append({"id": d.id, "code": d.code, "name": d.name,
                        "description": d.description or "", "is_active": bool(d.is_active),
                        "member_count": mc, "modules_enabled": en, "modules_total": total_mods})
        return out

    # --- пустое состояние ----------------------------------------------------
    if dept is None:
        all_users = (await session.scalars(
            select(User).where(User.is_active == True)  # noqa: E712
            .order_by(User.display_name))).all()
        depts_all = await _dept_list(include_inactive=True) if ctx.is_global else []
        return await render_page(
            request, "admin_dept.html", module_key=MODULE_KEY, user=user,
            session=session,
            page_title="Администрирование",
            page_sub="Единая панель управления системой и рабочими пространствами",
            counts={MODULE_KEY: 0},
            is_global_admin=ctx.can_admin_system,
            can_manage=ctx.can_admin_system,
            active_tab=tab or ("departments" if ctx.can_admin_system else "overview"),
            department=None,
            departments=[{"id": d["id"], "name": d["name"]} for d in depts_all if d["is_active"]],
            dept_list=depts_all,
            dept_list_simple=[{"id": d["id"], "name": d["name"]} for d in depts_all if d["is_active"]],
            members=[], modules=[], shares_out=[], shares_in=[],
            users=[{"id": u.id, "name": u.display_name} for u in all_users],
        )

    # --- все подразделения ---------------------------------------------------
    # Админ/координатор: все активные пространства системы (переключатель +
    # вкладка «Подразделения»). Руководитель: только свои подразделения.
    if ctx.is_global:
        scope_depts = [{"id": d.id, "name": d.name} for d in
                       (await session.scalars(
                           select(Department).where(Department.is_active == True)  # noqa: E712
                           .order_by(Department.name))).all()]
        dept_list = await _dept_list(include_inactive=True)
    else:
        mine = (await session.scalars(
            select(Department).where(Department.id.in_(ctx.department_ids or [-1]),
                                     Department.is_active == True)  # noqa: E712
            .order_by(Department.name))).all()
        scope_depts = [{"id": d.id, "name": d.name} for d in mine]
        dept_list = []

    # --- состав участников --------------------------------------------------
    members = (await session.scalars(
        select(DepartmentMember)
        .where(DepartmentMember.department_id == dept.id)
        .options(selectinload(DepartmentMember.user)))).all()
    member_rows = [{
        "user_id": m.user_id,
        "name": m.user.display_name if m.user else f"#{m.user_id}",
        "email": (m.user.email if m.user else "") or "",
        "global_role": m.user.role.value if m.user else "",
        "dept_role": m.role_in_dept.value,
        "tg_bound": bool(m.user and m.user.telegram_chat_id),
    } for m in members]

    # --- модули рабочего пространства ---------------------------------------
    ws = await session.scalar(select(Workspace).where(Workspace.department_id == dept.id))
    ws_mods = []
    if ws:
        ws_mods = (await session.scalars(
            select(WorkspaceModule).where(WorkspaceModule.workspace_id == ws.id))).all()
    enabled = {wm.module_key: wm.is_enabled for wm in ws_mods}
    module_rows = []
    for spec in registry.all():
        module_rows.append({
            "key": spec.key, "title": spec.title, "icon": spec.icon,
            "is_core": spec.is_core,
            "enabled": enabled.get(spec.key, spec.enabled_by_default),
        })

    # --- шеринг пространства (исходящий / входящий) --------------------------
    out_q = (select(EntityShare)
             .join(Entity, Entity.id == EntityShare.entity_id)
             .where(Entity.owner_department_id == dept.id)
             .options(selectinload(EntityShare.target_department)))
    inc_q = (select(EntityShare)
             .where(EntityShare.target_department_id == dept.id))
    shares_out = []
    for sh in (await session.scalars(out_q)).all():
        ent = await session.get(Entity, sh.entity_id)
        shares_out.append({
            "uid": ent.uid, "title": ent.title, "kind": ent.kind.value,
            "to_dept": sh.target_department.name if sh.target_department else str(sh.target_department_id),
            "permission": sh.permission.value,
        })
    shares_in = []
    for sh in (await session.scalars(inc_q)).all():
        ent = await session.get(Entity, sh.entity_id)
        owner = await session.get(Department, ent.owner_department_id)
        shares_in.append({
            "uid": ent.uid, "title": ent.title, "kind": ent.kind.value,
            "from_dept": owner.name if owner else "",
            "permission": sh.permission.value,
        })

    can_manage = ctx.is_global or any(
        m.user_id == user.id and m.role_in_dept == Role.HEAD for m in members)

    return await render_page(
        request, "admin_dept.html", module_key=MODULE_KEY, user=user, session=session,
        page_title="Администрирование",
        is_global_admin=ctx.can_admin_system,
        can_manage=can_manage,
        active_tab=tab or ("departments" if ctx.can_admin_system and dept is None else "overview"),
        page_sub="Единая панель: подразделения, участники, учётные записи, модули, шеринг, журнал",
        counts={MODULE_KEY: len(member_rows)},
        department={"id": dept.id, "name": dept.name},
        departments=scope_depts,
        dept_list=dept_list,
        dept_list_simple=[{"id": d["id"], "name": d["name"]} for d in dept_list if d["is_active"]],
        members=member_rows,
        modules=module_rows,
        shares_out=shares_out,
        shares_in=shares_in,
        users=[{"id": u.id, "name": u.display_name} for u in
               (await session.scalars(select(User).where(User.is_active == True)  # noqa: E712
                                      .order_by(User.display_name))).all()],
    )
