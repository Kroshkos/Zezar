"""
Роутер аутентификации «ЦЕЗАРЬ» — два независимых контура входа:

1) АДМИНИСТРАТОРЫ — локальный логин/пароль (без Яндекса):
  POST /auth/admin-login   форма login+password; учётные записи синхронизируются
                           из переменных окружения при старте
                           (ADMIN_USERNAME + ADMIN_PASSWORD, резерв — ADMIN_LOGINS;
                           app/db/seed.sync_admin_logins). Пароль хешируется
                           системой (pbkdf2_sha256), в env задаётся открытым.

2) УЧАСТНИКИ — OAuth Яндекс ID с закрытой регистрацией (по приглашению админа):
  GET  /auth/login         -> редирект на Яндекс
  GET  /auth/callback      -> код->токен->профиль; вход только если пользователь
                              уже в users или есть Invite по yandex_id/email
  POST /auth/logout        -> сброс cookie
  GET  /auth/me            -> профиль текущей сессии

Legacy-контур (BOOTSTRAP_ADMIN_YA_IDS) сохранён: ya_id из списка получает ADMIN
прямо при OAuth-входе — совместимость с ранее выданными допусками.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import datetime, timezone

logger = logging.getLogger("caesar.auth")

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import yandex_oauth as yo
from app.core.config import get_settings
from app.core.security import (
    SESSION_COOKIE, create_session_token, get_current_user, verify_password,
)
from app.deps import session_dep
from app.models.core import DepartmentMember, Invite, Role, User
from app.core.yandex_oauth import STATE_COOKIE

router = APIRouter()


def _login_redirect(user: User) -> RedirectResponse:
    resp = RedirectResponse("/", status_code=302)
    resp.set_cookie(SESSION_COOKIE, create_session_token({"uid": user.id}),
                    httponly=True, samesite="lax", max_age=86400 * 14,
                    secure=get_settings().session_cookie_secure)
    return resp


# ======================= Контур 1: администраторы (логин/пароль) =======================

# Простейшая защита от перебора: не более 10 неудачных попыток с одного IP за 15 минут.
_ATTEMPTS: dict[str, list[float]] = defaultdict(list)
_WINDOW_S = 900
_MAX_FAILS = 10


def _throttled(ip: str) -> bool:
    now = time.time()
    fails = [t for t in _ATTEMPTS[ip] if now - t < _WINDOW_S]
    _ATTEMPTS[ip] = fails
    return len(fails) >= _MAX_FAILS


@router.post("/auth/admin-login")
async def admin_login(request: Request, login: str = Form(""), password: str = Form(""),
                      session: AsyncSession = Depends(session_dep)):
    ip = request.client.host if request.client else "?"
    if _throttled(ip):
        logger.warning("admin-login: перебор паролей с %s — временная блокировка", ip)
        return RedirectResponse("/login?error=too_many&mode=admin", status_code=302)

    login_norm = (login or "").strip().lower()
    user = await session.scalar(select(User).where(User.login == login_norm)) \
        if login_norm else None
    ok = bool(user and user.password_hash and verify_password(password or "", user.password_hash))
    if not ok:
        _ATTEMPTS[ip].append(time.time())
        logger.info("admin-login: неудачная попытка '%s' с %s", login_norm[:40], ip)
        return RedirectResponse("/login?error=bad_credentials&mode=admin", status_code=302)

    if not user.is_active:
        return RedirectResponse("/login?error=disabled&mode=admin", status_code=302)
    # Гарантийство роли: админ-аккаунт обязан быть ADMIN (защита от ручного понижения
    # в обход env — источник истины для локальных админов всё-таки ADMIN_USERNAME/
    # ADMIN_PASSWORD).
    if user.role != Role.ADMIN:
        user.role = Role.ADMIN
        logger.info("admin-login: пользователю '%s' восстановлена роль ADMIN", user.login)
    _ATTEMPTS.pop(ip, None)
    logger.info("admin-login: успешный вход администратора '%s' (%s)", user.login, ip)
    await session.commit()
    return _login_redirect(user)


# ======================= Контур 2: участники (OAuth Яндекс ID) =======================

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
        return RedirectResponse("/login?error=no_code&mode=yandex", status_code=302)
    try:
        yo.check_state(request, state)
    except Exception:
        return RedirectResponse("/login?error=state&mode=yandex", status_code=302)
    tok = await yo.exchange_code(code)
    ya = await yo.fetch_userinfo(tok["access_token"])

    _st = get_settings()
    logger.info("auth callback: ya_id=%s login=%s email=%s | bootstrap_ids=%s",
                ya.yandex_id, ya.login, ya.email, _st.bootstrap_ids or "—")

    user = await session.scalar(select(User).where(User.yandex_id == ya.yandex_id))
    if user is None:
        # закрытая регистрация: пускаем только по заранее созданному invite
        inv = await session.scalar(
            select(Invite).where(Invite.used_at.is_(None),
                                 (Invite.yandex_id == ya.yandex_id) |
                                 ((Invite.email != None) & (Invite.email == ya.email)))  # noqa: E711
        )
        if inv is None:
            # Подсказка: на первом входе свой id видит только сам входящий — по нему
            # можно заполнить BOOTSTRAP_ADMIN_YA_IDS, если нужен доступ администратора.
            logger.warning("Yandex userinfo: id=%s default_email=%s login=%s — доступа нет "
                           "(нет Invite). Админ создаст допуск в разделе «Пользователи».",
                           ya.yandex_id, ya.default_email, ya.email)
            return RedirectResponse(
                f"/login?error=not_allowed&ya_id={ya.yandex_id}&mode=yandex", status_code=302)
        # Legacy-допуск: id также в BOOTSTRAP_ADMIN_YA_IDS -> ADMIN независимо от роли invite.
        role = Role.ADMIN if ya.yandex_id.lower() in _st.bootstrap_ids else inv.role
        user = User(yandex_id=ya.yandex_id, email=ya.email,
                    display_name=inv.display_name or ya.display_name,
                    yandex_login=ya.login,
                    role=role, initials=_initials(inv.display_name or ya.display_name))
        session.add(user)
        await session.flush()
        if inv.department_id:
            session.add(DepartmentMember(user_id=user.id, department_id=inv.department_id,
                                         role_in_dept=inv.role_in_dept))
        inv.used_at = datetime.now(timezone.utc)
        await session.commit()
    elif not user.is_active:
        return RedirectResponse("/login?error=disabled&mode=yandex", status_code=302)
    else:
        user.email = user.email or ya.email
        user.yandex_login = ya.login or user.yandex_login  # CalDAV требует актуальный логин
        # Идемпотентный legacy-bootstrap на лету: id в списке -> ADMIN при входе.
        if ya.yandex_id.lower() in _st.bootstrap_ids and user.role != Role.ADMIN:
            user.role = Role.ADMIN
            logger.info("auth callback: пользователю %s (ya_id=%s) восстановлена роль ADMIN "
                        "по BOOTSTRAP_ADMIN_YA_IDS", user.display_name, ya.yandex_id)
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
            "account": "local" if user.login else "yandex",
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
