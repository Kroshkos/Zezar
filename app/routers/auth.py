"""
Роутер аутентификации: OAuth Яндекс ID с ЗАКРЫТОЙ регистрацией.

  GET  /auth/login            -> редирект на Яндекс
  GET  /auth/callback         -> код->токен->профиль; пользователь обязан быть в users/invites
  POST /auth/logout           -> сброс cookie
  GET  /auth/dev-login?email= -> DEV ONLY: вход без Яндекса (только если settings.dev_login_enabled)
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import yandex_oauth as yo
from app.core.config import get_settings
from app.core.security import SESSION_COOKIE, create_session_token, get_current_user
from app.deps import session_dep
from app.models.core import DepartmentMember, Invite, User
from app.core.yandex_oauth import STATE_COOKIE

router = APIRouter()


def _login_redirect(user: User) -> RedirectResponse:
    resp = RedirectResponse("/", status_code=302)
    resp.set_cookie(SESSION_COOKIE, create_session_token({"uid": user.id}),
                    httponly=True, samesite="lax", max_age=86400 * 14,
                    secure=get_settings().session_cookie_secure)
    return resp


@router.get("/auth/login")
async def login():
    url, state = yo.authorize_url()
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie(STATE_COOKIE, state, httponly=True, samesite="lax", max_age=600)
    return resp


@router.get("/auth/callback")
async def callback(request: Request, code: str = "", state: str = "",
                   session: AsyncSession = Depends(session_dep)):
    if not code:
        return RedirectResponse("/login?error=no_code", status_code=302)
    try:
        yo.check_state(request, state)
    except Exception:
        return RedirectResponse("/login?error=state", status_code=302)
    tok = await yo.exchange_code(code)
    ya = await yo.fetch_userinfo(tok["access_token"])

    user = await session.scalar(select(User).where(User.yandex_id == ya.yandex_id))
    if user is None:
        # закрытая регистрация: пускаем только по заранее созданному invite
        inv = await session.scalar(
            select(Invite).where(Invite.used_at.is_(None),
                                 (Invite.yandex_id == ya.yandex_id) |
                                 ((Invite.email != None) & (Invite.email == ya.email)))  # noqa: E711
        )
        if inv is None:
            return RedirectResponse("/login?error=not_allowed", status_code=302)
        user = User(yandex_id=ya.yandex_id, email=ya.email,
                    display_name=inv.display_name or ya.display_name,
                    role=inv.role, initials=_initials(inv.display_name or ya.display_name))
        session.add(user)
        await session.flush()
        if inv.department_id:
            session.add(DepartmentMember(user_id=user.id, department_id=inv.department_id,
                                         role_in_dept=inv.role_in_dept))
        inv.used_at = datetime.now(timezone.utc)
        await session.commit()
    elif not user.is_active:
        return RedirectResponse("/login?error=disabled", status_code=302)
    else:
        user.email = user.email or ya.email
        await session.commit()
    return _login_redirect(user)


@router.post("/auth/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@router.get("/auth/me")
async def me(user: User = Depends(get_current_user)):
    return {"id": user.id, "name": user.display_name, "role": user.role.value,
            "telegram_linked": bool(user.telegram_chat_id),
            "calendar_linked": bool(user.yandex_cal_app_password_enc)}


def _initials(name: str) -> str:
    parts = name.split()
    return "".join(p[0].upper() for p in parts[:2]) if parts else "?"


# ---------- DEV-вход (без Яндекса), отключён если не выставлен флаг ----------
try:
    DEV_ENABLED = bool(getattr(get_settings(), "dev_login_enabled", False))
except Exception:
    DEV_ENABLED = False

if DEV_ENABLED:
    @router.get("/auth/dev-login")
    async def dev_login(email: str, session: AsyncSession = Depends(session_dep)):
        user = await session.scalar(select(User).where(User.email == email))
        if user is None:
            return JSONResponse({"detail": "Пользователь не заведён"}, status_code=404)
        return _login_redirect(user)
