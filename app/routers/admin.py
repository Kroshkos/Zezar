"""
Администрирование системы (только admin/coordinator): пользователи, допуски (Invite),
Telegram-привязка пользователя, справка по интеграциям.

  GET    /api/core/admin/users
  POST   /api/core/admin/users            {yandex_id|email, display_name, role, department_id}
  PATCH  /api/core/admin/users/{id}       {role?, is_active?, telegram_chat_id?}
  GET    /api/core/admin/invites          POST /api/core/admin/invites
  DELETE /api/core/admin/invites/{id}
  POST   /api/core/admin/users/{id}/telegram-unbind
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import build_context
from app.core.security import get_current_user
from app.deps import session_dep
from app.models.core import DepartmentMember, Invite, Role, User

router = APIRouter()


async def _require_admin(session, user):
    ctx = await build_context(session, user)
    if not ctx.can_admin_system:
        raise HTTPException(403, "Только администратор/координатор")


class UserIn(BaseModel):
    yandex_id: str | None = None
    email: str | None = None
    display_name: str
    role: Role = Role.MEMBER
    department_id: int | None = None


@router.get("/admin/users")
async def list_users(user: User = Depends(get_current_user),
                     session: AsyncSession = Depends(session_dep)):
    await _require_admin(session, user)
    rows = (await session.scalars(select(User).order_by(User.display_name))).all()
    return [{"id": u.id, "name": u.display_name, "email": u.email,
             "yandex_id": u.yandex_id, "login": u.login,
             "account": "local" if u.login else "yandex",
             "role": u.role.value, "active": u.is_active,
             "telegram": bool(u.telegram_chat_id), "calendar": bool(u.yandex_cal_app_password_enc)}
            for u in rows]


@router.post("/admin/users")
async def create_user(body: UserIn, user: User = Depends(get_current_user),
                      session: AsyncSession = Depends(session_dep)):
    """Регистрация закрыта — учётку создаёт только админ (или через Invite при первом OAuth-входе)."""
    await _require_admin(session, user)
    if not body.yandex_id and not body.email:
        raise HTTPException(400, "Нужен yandex_id или email яндекс-аккаунта")
    if body.yandex_id and await session.scalar(select(User).where(User.yandex_id == body.yandex_id)):
        raise HTTPException(409, "Такой Яндекс-ID уже в системе")
    parts = body.display_name.split()
    u = User(yandex_id=body.yandex_id or f"pending:{body.email}",
             email=body.email, display_name=body.display_name,
             initials="".join(p[0].upper() for p in parts[:2]) if parts else "?",
             role=body.role)
    session.add(u)
    await session.flush()
    if body.department_id:
        session.add(DepartmentMember(user_id=u.id, department_id=body.department_id,
                                     role_in_dept=Role.HEAD if body.role == Role.HEAD else Role.MEMBER))
    await session.commit()
    return {"id": u.id}


@router.patch("/admin/users/{user_id}")
async def patch_user(user_id: int, body: dict, user: User = Depends(get_current_user),
                     session: AsyncSession = Depends(session_dep)):
    await _require_admin(session, user)
    u = await session.get(User, user_id)
    if not u:
        raise HTTPException(404)
    # Защита контура админов: локальный аккаунт из ADMIN_USERNAME/ADMIN_PASSWORD
    # (или ADMIN_LOGINS) нельзя понизить или заблокировать через API — отзыв доступа =
    # удаление строки из env + рестарт (см. app/db/seed.sync_admin_logins). Иначе
    # админ случайно «сломал» бы себе вход, а это ровно та проблема, из-за которой
    # переделывалась авторизация. Проверка выполняется ДО изменений, чтобы не
    # трогать ORM-объект при отказе (см. ниже про MissingGreenlet).
    if u.login and (body.get("role") not in (None, "admin") or body.get("is_active") is False):
        raise HTTPException(409, f"Локальный администратор '{u.login}' управляется переменными "
                                 "ADMIN_USERNAME/ADMIN_PASSWORD: роль всегда ADMIN, "
                                 "блокировка — удалением из env + рестарт")
    for k in ("role", "is_active", "telegram_chat_id", "display_name"):
        if k in body:
            setattr(u, k, Role(body[k]) if k == "role" else body[k])
    await session.commit()
    return {"ok": True}


class InviteIn(BaseModel):
    yandex_id: str | None = None
    email: str | None = None
    display_name: str
    role: Role = Role.MEMBER
    department_id: int | None = None
    role_in_dept: Role = Role.MEMBER


@router.get("/admin/invites")
async def list_invites(user: User = Depends(get_current_user),
                       session: AsyncSession = Depends(session_dep)):
    await _require_admin(session, user)
    rows = (await session.scalars(select(Invite).where(Invite.used_at.is_(None)))).all()
    return [{"id": i.id, "email": i.email, "yandex_id": i.yandex_id,
             "name": i.display_name, "role": i.role.value} for i in rows]


@router.post("/admin/invites")
async def create_invite(body: InviteIn, user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(session_dep)):
    await _require_admin(session, user)
    inv = Invite(**body.model_dump(), created_by_id=user.id)
    session.add(inv)
    await session.commit()
    return {"id": inv.id}


@router.delete("/admin/invites/{invite_id}")
async def delete_invite(invite_id: int, user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(session_dep)):
    await _require_admin(session, user)
    inv = await session.get(Invite, invite_id)
    if inv:
        await session.delete(inv)
        await session.commit()
    return {"ok": True}
