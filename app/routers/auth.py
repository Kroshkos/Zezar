"""
Роутер аутентификации: OAuth Яндекс ID с ЗАКРЫТОЙ регистрацией.

  GET  /auth/login            -> редирект на Яндекс
  GET  /auth/callback         -> код->токен->профиль; пользователь обязан быть в users/invites
  POST /auth/logout           -> сброс cookie
  GET  /auth/dev-login?email= -> DEV ONLY: вход без Яндекса (только если settings.dev_login_enabled)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger("caesar.auth")

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import yandex_oauth as yo
from app.core.config import get_settings
from app.core.security import SESSION_COOKIE, create_session_token, get_current_user
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

    # Диагностика bootstrap: видно ли в логах, что за id пришёл из Яндекса и что
    # реально лежит в BOOTSTRAP_ADMIN_YA_IDS (включая «мусор»: пробелы, email,
    # опечатки — сравнение идёт по точному совпадению sub).
    _raw_bootstrap = get_settings().bootstrap_admin_ya_ids
    logger.info(
        "auth callback: ya_id=%s login=%s email=%s | BOOTSTRAP_ADMIN_YA_IDS=%r",
        ya.yandex_id, ya.login, ya.email, _raw_bootstrap)

    user = await session.scalar(select(User).where(User.yandex_id == ya.yandex_id))
    if user is None:
        # закрытая регистрация: пускаем только по заранее созданному invite
        inv = await session.scalar(
            select(Invite).where(Invite.used_at.is_(None),
                                 (Invite.yandex_id == ya.yandex_id) |
                                 ((Invite.email != None) & (Invite.email == ya.email)))  # noqa: E711
        )
        if inv is None:
            # Подсказка для bootstrap-администратора: на первом входе логин
            # виден только ему (в браузере), поэтому по нему можно заполнить
            # BOOTSTRAP_ADMIN_YA_IDS. Для чужих аккаунтов id не раскрываем —
            # админ создаст Invite по email.
            logger.warning(
                "Yandex userinfo: id=%s default_email=%s login=%s — доступ закрыт",
                ya.yandex_id, ya.default_email, ya.email)
            return RedirectResponse(
                f"/login?error=not_allowed&ya_id={ya.yandex_id}", status_code=302)
        # Если id пользователя также в BOOTSTRAP_ADMIN_YA_IDS — вход даёт ADMIN,
        # даже когда приглашение было найдено по email с ролью ниже (или invite
        # создавал другой админ). Иначе получалось «зашёл, но админки нет».
        bootstrap_ids = [x.strip() for x in get_settings().bootstrap_admin_ya_ids.split(",")
                         if x.strip()]
        role = Role.ADMIN if ya.yandex_id in bootstrap_ids else inv.role
        user = User(yandex_id=ya.yandex_id, email=ya.email,
                    display_name=inv.display_name or ya.display_name,
                    yandex_login=ya.login,
                    role=role, initials=_initials(inv.display_name or ya.display_name))
        session.add(user)
        await session.flush()
        if inv.department_id:
            session.add(DepartmentMember(user_id=user.id, department_id=inv.department_id,
                                         role_in_dept=inv.role_in_dept))
        # Bootstrap-приглашение (создано seed.bootstrap из BOOTSTRAP_ADMIN_YA_IDS)
        # не помечаем использованным: идемпотентный bootstrap при следующем старте
        # иначе его не пересоздаст (он ищет только used_at IS NULL), и админ,
        # потерявший сессию, больше не сможет войти. Обычные приглашения — одноразовые.
        if inv.created_by_id is None and inv.email is None:
            await session.delete(inv)
        else:
            inv.used_at = datetime.now(timezone.utc)
        await session.commit()
    elif not user.is_active:
        return RedirectResponse("/login?error=disabled", status_code=302)
    else:
        user.email = user.email or ya.email
        user.yandex_login = ya.login or user.yandex_login  # CalDAV требует актуальный логин
        # Идемпотентный bootstrap на лету: если id в BOOTSTRAP_ADMIN_YA_IDS, но
        # учётная запись до сих пор не ADMIN (например, сервис не перезапускался
        # после добавления id в .env, или роль была понижена вручную) — повышаем
        # прямо при входе. Иначе «перезапустил, вошёл — а админки всё равно нет».
        bootstrap_ids = [x.strip() for x in get_settings().bootstrap_admin_ya_ids.split(",")
                         if x.strip()]
        if ya.yandex_id in bootstrap_ids and user.role != Role.ADMIN:
            user.role = Role.ADMIN
            logger.info("auth callback: пользователю %s (ya_id=%s) восстановлена роль ADMIN "
                        "по BOOTSTRAP_ADMIN_YA_IDS", user.display_name, ya.yandex_id)
        elif user.role != Role.ADMIN:
            # Пользователь НЕ из bootstrap-списка и не админ — фиксируем в логе
            # его реальный id: если человек рассчитывал на админку, это значит,
            # что в BOOTSTRAP_ADMIN_YA_IDS прописан другой (неверный) id.
            logger.info("auth callback: пользователь %s (ya_id=%s) вошёл с ролью %s; "
                        "его id отсутствует в BOOTSTRAP_ADMIN_YA_IDS — добавьте туда "
                        "именно этот ya_id, если нужен доступ администратора",
                        user.display_name, ya.yandex_id, user.role.value)
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
