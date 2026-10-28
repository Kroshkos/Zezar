"""
Администрирование системы (только admin/coordinator): учётные записи пользователей,
журнал событий, Telegram-привязка.

Авторизация через Яндекс ID удалена — единственный контур входа: логин + пароль.
  * Администратор задаётся переменными окружения ADMIN_USERNAME/ADMIN_PASSWORD
    (синхронизируется сидом при старте; через API не создаётся и не удаляется).
  * Пользователей создаёт администратор: Фамилия Имя, логин, НАЧАЛЬНЫЙ ПАРОЛЬ.
    При первом входе пользователь обязан сменить пароль (must_change_password).

Эндпоинты:
  GET    /api/core/admin/users
  POST   /api/core/admin/users            {display_name, login, initial_password, role?, department_id?}
  PATCH  /api/core/admin/users/{id}       {role?, is_active?, display_name?, email?, telegram_chat_id?}
  POST   /api/core/admin/users/{id}/reset-password  {new_password}   (сброс -> принудительная смена при входе)
  DELETE /api/core/admin/users/{id}
  GET    /api/core/admin/events           журнал событий (?limit&offset&category&action&user_id&q)
  GET    /api/core/admin/events/stats     сводка по журналу
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import build_context
from app.core.events import client_ip, event_stats, log_event, read_events
from app.core.security import get_current_user, hash_password
from app.db.seed import is_env_admin_login
from app.deps import session_dep
from app.models.core import (
    Department, DepartmentMember, EntityResponsible, EventLog, Role, User,
)
from sqlalchemy.orm import selectinload

router = APIRouter()

_LOGIN_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")


async def _require_admin(session, user):
    ctx = await build_context(session, user)
    if not ctx.can_admin_system:
        raise HTTPException(403, "Только администратор/координатор")


def _norm_role_value(v):
    """Принять роль в любом регистре ('HEAD'/'head'/Role); пустое/неизвестное — MEMBER.

    Пустая строка раньше бросала ValueError прямо в валидаторе — клиент получал
    422, и создание пользователя «не работало» без внятной ошибки. Теперь это
    безопасное значение по умолчанию (UI всегда шлёт конкретную роль)."""
    if isinstance(v, str):
        s = v.strip().lower()
        if not s:
            return Role.MEMBER
        try:
            return Role(s)
        except ValueError:
            raise HTTPException(400, f"Неизвестная роль '{v}'. "
                                     "Допустимы: admin, coordinator, head, member")
    return v


class UserIn(BaseModel):
    display_name: str = Field(min_length=2, max_length=160)   # «Фамилия Имя»
    login: str = Field(min_length=2, max_length=64)
    initial_password: str = Field(min_length=8, max_length=128)
    role: Role = Role.MEMBER
    email: str | None = None
    department_id: int | None = None

    @field_validator("role", mode="before")
    @classmethod
    def _norm_role(cls, v):
        return _norm_role_value(v)


class UserPatch(BaseModel):
    display_name: str | None = None
    email: str | None = None
    role: Role | None = None
    is_active: bool | None = None
    telegram_chat_id: str | None = None

    @field_validator("role", mode="before")
    @classmethod
    def _norm_role(cls, v):
        # Принять "HEAD"/"MEMBER" в любом регистре; None оставить как есть (PATCH)
        if isinstance(v, str):
            return _norm_role_value(v)
        return v


class ResetPasswordIn(BaseModel):
    new_password: str = Field(min_length=8, max_length=128)


@router.get("/admin/users")
async def list_users(q: str | None = None, department_id: int | None = None,
                     user: User = Depends(get_current_user),
                     session: AsyncSession = Depends(session_dep)):
    """Список учётных записей. Полностью — администратору/координатору;
    руководителю подразделения — только состав его подразделения (для назначения)."""
    ctx = await build_context(session, user)
    scope_depts: list[int] | None = None      # None = без ограничения (глобал)
    if not ctx.can_admin_system:
        heads = await session.scalars(
            select(DepartmentMember.department_id).where(
                DepartmentMember.user_id == user.id,
                DepartmentMember.role_in_dept == Role.HEAD))
        scope_depts = list(set(heads.all()))
        if not scope_depts:
            raise HTTPException(403, "Раздел доступен администратору/координатору "
                                     "или руководителю подразделения")
    stmt = select(User).order_by(User.display_name)
    if q and q.strip():
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(or_(func.lower(User.display_name).like(like),
                              func.lower(User.login).like(like),
                              func.lower(User.email).like(like)))
    rows = list((await session.scalars(stmt)).all())
    # карта членств user_id -> [(dept_id, dept_name)]
    mem_stmt = (select(DepartmentMember.user_id, Department.id, Department.name)
                .join(Department, Department.id == DepartmentMember.department_id))
    if scope_depts is not None:
        mem_stmt = mem_stmt.where(DepartmentMember.department_id.in_(scope_depts))
    memberships: dict[int, list[tuple[int, str]]] = {}
    for uid, did, dname in (await session.execute(mem_stmt)).all():
        memberships.setdefault(uid, []).append((did, dname))
    out = []
    for u in rows:
        pairs = memberships.get(u.id, [])
        if scope_depts is not None and not pairs:
            continue                          # руководитель видит только своих
        # Состояние интеграции с Telegram-ботом для таблицы учётных записей:
        #   none      — не подключен (нет привязанного chat_id)
        #   active    — подключен, уведомления активны
        #   muted     — подключен, но пользователь отключил звук/уведомления (/mute)
        if not u.telegram_chat_id:
            tg_state = "none"
        elif getattr(u, "telegram_optout", None) == "all" or not u.telegram_notify:
            tg_state = "muted"
        else:
            tg_state = "active"
        out.append({"id": u.id, "name": u.display_name, "email": u.email,
                    "login": u.login, "role": u.role.value, "active": u.is_active,
                    "env_admin": bool(is_env_admin_login(u.login)),
                    "must_change_password": bool(u.must_change_password),
                    "no_password": not bool(u.password_hash),
                    "telegram": bool(u.telegram_chat_id),
                    "telegram_state": tg_state,
                    "calendar": bool(u.yandex_cal_app_password_enc),
                    "departments": [{"id": d[0], "name": d[1]} for d in pairs]})
    if department_id:
        out = [x for x in out if any(d["id"] == department_id for d in x["departments"])]
    return out


@router.post("/admin/users")
async def create_user(body: UserIn, request: Request,
                      user: User = Depends(get_current_user),
                      session: AsyncSession = Depends(session_dep)):
    """Создание учётной записи администратором: ФИО, логин, начальный пароль.

    Пароль хранится хешем (pbkdf2_sha256); must_change_password=True — при первом
    входе система принудительно потребует сменить пароль."""
    await _require_admin(session, user)
    login = body.login.strip().lower()
    if not _LOGIN_RE.match(login):
        raise HTTPException(400, "Логин: минимум 3 символа, только строчные латинские "
                                 "буквы/цифры/`.` `_` `-`, без пробелов")
    name = body.display_name.strip()
    if len(name) < 2:
        raise HTTPException(400, "Укажите Фамилию и Имя пользователя")
    if is_env_admin_login(login):
        raise HTTPException(409, f"Логин '{login}' зарезервирован администратором из "
                                 "переменных окружения")
    if await session.scalar(select(User).where(User.login == login)):
        raise HTTPException(409, "Такой логин уже занят")

    parts = name.split()
    u = User(display_name=name, login=login,
             password_hash=hash_password(body.initial_password),
             must_change_password=True,
             initials="".join(p[0].upper() for p in parts[:2]) if parts else "?",
             email=(body.email or "").strip() or None,
             role=Role.ADMIN if is_env_admin_login(login) else body.role)
    if u.role == Role.ADMIN and not is_env_admin_login(login):
        pass  # админа вручную создавать можно — но он НЕ управляется env
    session.add(u)
    await session.flush()
    if body.department_id:
        session.add(DepartmentMember(user_id=u.id, department_id=body.department_id,
                                     role_in_dept=Role.HEAD if body.role == Role.HEAD else Role.MEMBER))
    await log_event(session, "create_user", category="admin", actor_name=user.display_name,
                    user=user, object=f"{name} ({login})", ip=client_ip(request),
                    level="warning",
                    message=f"Создан пользователь '{name}' с логином '{login}' "
                            f"(роль {u.role.value}); выдан начальный пароль")
    await session.commit()
    return {"id": u.id, "login": u.login}


@router.patch("/admin/users/{user_id}")
async def patch_user(user_id: int, body: UserPatch, request: Request,
                     user: User = Depends(get_current_user),
                     session: AsyncSession = Depends(session_dep)):
    await _require_admin(session, user)
    u = await session.scalar(
        select(User).options(selectinload(User.memberships)).where(User.id == user_id))
    if not u:
        raise HTTPException(404)
    changes: list[str] = []
    if body.display_name is not None and body.display_name.strip() != u.display_name:
        u.display_name = body.display_name.strip()
        changes.append("ФИО изменено")
    if body.email is not None:
        u.email = body.email.strip() or None
        changes.append("email изменён")
    if body.role is not None and body.role != u.role:
        # Синхронизация с ролями в пространствах (иначе users.role и
        # department_members.role_in_dept расходятся — см. admin_dept UI):
        #   повышение до head -> HEAD во всех его подразделениях;
        #   понижение с head -> снимаем HEAD везде (member остаётся);
        #   admin/coordinator -> глобальная роль, role_in_dept не трогаем.
        from app.core.permcache import bump_epoch

        if body.role == Role.HEAD:
            await session.execute(
                update(DepartmentMember)
                .where(DepartmentMember.user_id == user_id)
                .values(role_in_dept=Role.HEAD))
        elif u.role == Role.HEAD:
            await session.execute(
                update(DepartmentMember)
                .where(DepartmentMember.user_id == user_id,
                       DepartmentMember.role_in_dept == Role.HEAD)
                .values(role_in_dept=Role.MEMBER))
        await bump_epoch(session)
        changes.append(f"роль -> {body.role.value}")
    # ВАЖНО: присваиваем новую роль ПОСЛЕ синхронизации role_in_dept — ветка
    # понижения опирается на старое значение u.role == Role.HEAD.
    if body.role is not None and body.role != u.role:
        u.role = body.role
    if body.is_active is not None and body.is_active != u.is_active:
        u.is_active = body.is_active
        changes.append("аккаунт активирован" if body.is_active else "аккаунт заблокирован")
    if "telegram_chat_id" in body.model_fields_set:
        u.telegram_chat_id = body.telegram_chat_id
        changes.append("telegram-привязка изменена")
    # Защита контура админов: локальный аккаунт из ADMIN_USERNAME/ADMIN_PASSWORD
    # нельзя понизить или заблокировать через API — отзыв доступа = удаление
    # строки из env + рестарт (см. app/db/seed.sync_admin_logins).
    # ВАЖНО: блокируем только ПОПЫТКУ реально изменить роль/статус env-админа.
    # Раньше условие смотрело на итоговое состояние (u.role != ADMIN or
    # not u.is_active), из-за чего любое сохранение ФИО/email у env-админа
    # возвращало 409 и кнопка «Сохранить» «не работала».
    tried_role_downgrade = (body.role is not None and body.role != Role.ADMIN)
    tried_deactivate = (body.is_active is not None and not body.is_active)
    if is_env_admin_login(u.login) and (tried_role_downgrade or tried_deactivate):
        # Откатываем НЕ всю транзакцию (session.rollback отменил бы и refresh
        # detached-объекта при формировании 409 -> MissingGreenlet), а только
        # свои изменения: возвращаем роль/активность env-админа.
        if body.role is not None:
            u.role = Role.ADMIN
        if body.is_active is not None:
            u.is_active = True
        await session.commit()
        raise HTTPException(409, f"Локальный администратор '{u.login}' управляется переменными "
                                 "ADMIN_USERNAME/ADMIN_PASSWORD: роль всегда ADMIN, "
                                 "блокировка — удалением из env + рестарт")
    if changes:
        target_name, target_login = u.display_name, u.login
        await log_event(session, "update_user", category="admin", actor_name=user.display_name,
                        object=f"{target_name} ({target_login})", ip=client_ip(request),
                        level="warning",
                        message=f"Изменения пользователя '{target_login}': {', '.join(changes)}")
    await session.commit()
    return {"ok": True}


@router.post("/admin/users/{user_id}/reset-password")
async def reset_password(user_id: int, body: ResetPasswordIn, request: Request,
                         user: User = Depends(get_current_user),
                         session: AsyncSession = Depends(session_dep)):
    """Сброс пароля администратором: новый (начальный) пароль + принудительная
    смена при первом входе владельца учётной записи."""
    await _require_admin(session, user)
    u = await session.scalar(
        select(User).options(selectinload(User.memberships)).where(User.id == user_id))
    if not u:
        raise HTTPException(404)
    if is_env_admin_login(u.login):
        raise HTTPException(409, "Пароль env-администратора меняется только через "
                                 "ADMIN_PASSWORD + рестарт сервиса")
    u.password_hash = hash_password(body.new_password)
    u.must_change_password = True
    await log_event(session, "reset_password", category="admin", user=user,
                    object=f"{u.display_name} ({u.login})", ip=client_ip(request),
                    level="warning",
                    message=f"Администратор сбросил пароль пользователя '{u.login}' "
                            "— при следующем входе потребуется смена пароля")
    await session.commit()
    return {"ok": True}


@router.delete("/admin/users/{user_id}")
async def delete_user(user_id: int, request: Request,
                      user: User = Depends(get_current_user),
                      session: AsyncSession = Depends(session_dep)):
    """Удаление учётной записи. Нельзя удалить себя, env-администратора и
    пользователя, назначенного ответственным по сущностям (сначала передайте дела)."""
    await _require_admin(session, user)
    if user_id == user.id:
        raise HTTPException(409, "Нельзя удалить собственную учётную запись")
    u = await session.scalar(
        select(User).options(selectinload(User.memberships)).where(User.id == user_id))
    if not u:
        raise HTTPException(404)
    if is_env_admin_login(u.login):
        raise HTTPException(409, f"Администратор '{u.login}' задан переменными окружения — "
                                 "удаление только через env + рестарт")
    resp_cnt = await session.scalar(
        select(EntityResponsible.id).where(EntityResponsible.user_id == user_id).limit(1))
    if resp_cnt:
        raise HTTPException(409, f"Пользователь '{u.login}' назначен ответственным по сущностям — "
                                 "сначала передайте ответственность или отключите аккаунт")
    await session.execute(delete(DepartmentMember).where(DepartmentMember.user_id == user_id))
    name, login = u.display_name, u.login
    await session.delete(u)
    await log_event(session, "delete_user", category="admin", user=user,
                    object=f"{name} ({login})", ip=client_ip(request), level="error",
                    message=f"Удалён пользователь '{name}' (логин '{login}')")
    await session.commit()
    return {"ok": True}


# ---------------- Журнал событий ----------------

@router.get("/admin/events")
async def list_events(limit: int = Query(200, ge=1, le=1000),
                      offset: int = Query(0, ge=0),
                      category: str | None = None, action: str | None = None,
                      user_id: int | None = None, q: str | None = None,
                      user: User = Depends(get_current_user),
                      session: AsyncSession = Depends(session_dep)):
    await _require_admin(session, user)
    items, total = await read_events(session, limit=limit, offset=offset,
                                     category=category, action=action,
                                     user_id=user_id, q=q)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/admin/events/stats")
async def events_stats(user: User = Depends(get_current_user),
                       session: AsyncSession = Depends(session_dep)):
    await _require_admin(session, user)
    return await event_stats(session)


async def _head_dept_ids(session, ctx, user):
    """Подразделения, которыми руководит текущий пользователь (для scoped-журнала)."""
    rows = await session.scalars(
        select(DepartmentMember.department_id).where(
            DepartmentMember.user_id == user.id,
            DepartmentMember.role_in_dept == Role.HEAD))
    ids = list(set(rows.all()))
    if not ids and not ctx.can_admin_system:
        raise HTTPException(403, "Раздел доступен администратору или руководителю подразделения")
    return ids


@router.get("/admin/dept-events")
async def dept_events(department_id: int,
                      limit: int = Query(100, ge=1, le=500),
                      offset: int = Query(0, ge=0),
                      q: str | None = None,
                      user: User = Depends(get_current_user),
                      session: AsyncSession = Depends(session_dep)):
    """Журнал событий в рамках одного подразделения (для руководителя).

    Фильтр по «причастности»: событие относится к подразделению, если в тексте
    (объект/сообщение) упоминается его название, либо действие касается
    пользователей, состоящих в этом подразделении."""
    ctx = await build_context(session, user)
    if not ctx.is_global:
        my_head = await session.scalar(select(DepartmentMember).where(
            DepartmentMember.department_id == department_id,
            DepartmentMember.user_id == user.id,
            DepartmentMember.role_in_dept == Role.HEAD))
        if not my_head:
            raise HTTPException(403, "Журнал доступен только руководителю этого подразделения")
    dept = await session.get(Department, department_id)
    if not dept:
        raise HTTPException(404, "Подразделение не найдено")
    member_ids = list(await session.scalars(
        select(DepartmentMember.user_id).where(
            DepartmentMember.department_id == department_id)))
    conds = [EventLog.object.ilike(f"%{dept.name}%"),
             EventLog.message.ilike(f"%{dept.name}%"),
             EventLog.user_name.ilike(f"%{dept.name}%")]
    if member_ids:
        conds.append(EventLog.user_id.in_(member_ids))
    stmt = select(EventLog).where(or_(*conds))
    count_stmt = select(func.count()).select_from(stmt.subquery())
    if q and q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(EventLog.object.ilike(like), EventLog.message.ilike(like),
                              EventLog.action.ilike(like), EventLog.user_name.ilike(like)))
        count_stmt = select(func.count()).select_from(stmt.subquery())
    total = await session.scalar(count_stmt) or 0
    rows = (await session.scalars(
        stmt.order_by(EventLog.at.desc(), EventLog.id.desc())
        .limit(max(1, min(limit, 500))).offset(max(0, offset)))).all()
    return {"items": [{
        "id": e.id,
        "at": e.at.isoformat(timespec="seconds") + "Z" if e.at else "",
        "user": e.user_name or ("—" if not e.user_id else f"#{e.user_id}"),
        "action": e.action, "category": e.category, "object": e.object or "",
        "ip": e.ip or "", "level": e.level, "message": e.message or "",
    } for e in rows], "total": total, "department": dept.name}
