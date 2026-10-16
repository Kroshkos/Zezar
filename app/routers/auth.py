"""
Роутер аутентификации «ЦЕЗАРЬ» — единый контур входа: логин + пароль.

Авторизация через Яндекс ID полностью удалена. Учётные записи:
  * Администратор задаётся переменными окружения ADMIN_USERNAME/ADMIN_PASSWORD
    (синхронизируется сидом app/db/seed.sync_admin_logins при каждом старте).
  * Обычные пользователи создаются администратором в разделе
    «Администрирование» с ФИО, логином и НАЧАЛЬНЫМ ПАРОЛЕМ; при первом входе
    такой пользователь обязан сменить пароль (must_change_password).

Эндпоинты:
  POST /auth/login          форма login+password -> session-cookie
  POST /auth/change-password принудительная/добровольная смена пароля
  POST /auth/logout         сброс cookie
  GET  /auth/me             профиль текущей сессии
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.events import client_ip, log_event
from app.core.security import (
    SESSION_COOKIE, create_session_token, get_current_user, hash_password,
    verify_password,
)
from app.deps import session_dep
from app.models.core import Role, User

logger = logging.getLogger("caesar.auth")

router = APIRouter()


def _login_redirect(user: User) -> RedirectResponse:
    resp = RedirectResponse("/", status_code=302)
    resp.set_cookie(SESSION_COOKIE, create_session_token({"uid": user.id}),
                    httponly=True, samesite="lax", max_age=86400 * 14,
                    secure=get_settings().session_cookie_secure)
    return resp


# Простейшая защита от перебора: не более 10 неудачных попыток с одного IP за 15 минут.
_ATTEMPTS: dict[str, list[float]] = defaultdict(list)
_WINDOW_S = 900
_MAX_FAILS = 10


def _throttled(ip: str) -> bool:
    now = time.time()
    fails = [t for t in _ATTEMPTS[ip] if now - t < _WINDOW_S]
    _ATTEMPTS[ip] = fails
    return len(fails) >= _MAX_FAILS


@router.post("/auth/login")
async def login(request: Request, login: str = Form(""), password: str = Form(""),
                session: AsyncSession = Depends(session_dep)):
    ip = client_ip(request) or (request.client.host if request.client else "?")
    if _throttled(ip):
        logger.warning("login: перебор паролей с %s — временная блокировка", ip)
        await log_event(session, "login_throttle", category="auth",
                        actor_name=(login or "").strip().lower()[:60], ip=ip,
                        level="warning",
                        message="Слишком много неудачных попыток входа — IP временно заблокирован")
        await session.commit()
        return RedirectResponse("/login?error=too_many", status_code=302)

    login_norm = (login or "").strip().lower()
    user = await session.scalar(select(User).where(User.login == login_norm)) \
        if login_norm else None
    ok = bool(user and user.password_hash and verify_password(password or "", user.password_hash))
    if not ok:
        _ATTEMPTS[ip].append(time.time())
        logger.info("login: неудачная попытка '%s' с %s", login_norm[:40], ip)
        await log_event(session, "login_failed", category="auth",
                        actor_name=login_norm[:60] or None, ip=ip, level="warning",
                        message=f"Неудачная попытка входа: логин «{login_norm[:60] or '—'}»")
        await session.commit()
        return RedirectResponse("/login?error=bad_credentials", status_code=302)

    if not user.is_active:
        await log_event(session, "login_disabled", category="auth", user=user, ip=ip,
                        level="warning",
                        message=f"Попытка входа в заблокированную учётную запись «{user.login}»")
        await session.commit()
        return RedirectResponse("/login?error=disabled", status_code=302)

    # Гарантийство роли: env-аккаунт обязан быть ADMIN (источник истины —
    # ADMIN_USERNAME/ADMIN_PASSWORD, защита от случайного понижения через API).
    from app.db.seed import is_env_admin_login
    if user.login and is_env_admin_login(user.login) and user.role != Role.ADMIN:
        user.role = Role.ADMIN
        logger.info("login: пользователю '%s' восстановлена роль ADMIN (из env)", user.login)

    _ATTEMPTS.pop(ip, None)
    first_login = bool(user.must_change_password)
    logger.info("login: успешный вход '%s' (%s) с %s", user.login, user.id, ip)
    await log_event(session, "password_change_required" if first_login else "login",
                    category="auth", user=user, ip=ip,
                    message=("Первый вход — требуется смена начального пароля"
                             if first_login else f"Вход в систему (логин «{user.login}»)"))
    await session.commit()
    # Первый вход с начальным паролем — принудительно на страницу смены пароля.
    if first_login:
        resp = RedirectResponse("/change-password?forced=1", status_code=302)
        resp.set_cookie(SESSION_COOKIE, create_session_token({"uid": user.id}),
                        httponly=True, samesite="lax", max_age=86400 * 14,
                        secure=get_settings().session_cookie_secure)
        return resp
    return _login_redirect(user)


@router.post("/auth/change-password")
async def change_password(request: Request,
                          current_password: str = Form(""),
                          new_password: str = Form(""),
                          repeat_password: str = Form(""),
                          user: User = Depends(get_current_user),
                          session: AsyncSession = Depends(session_dep)):
    """Смена пароля: текущий пароль проверяется; новый — минимум 8 символов.
    После успешной смены must_change_password снимается и ставится новая сессия."""
    if not user.password_hash or not verify_password(current_password or "", user.password_hash):
        await log_event(session, "password_change_failed", category="auth", user=user,
                        ip=client_ip(request), level="warning",
                        message="Смена пароля отклонена: неверный текущий пароль")
        await session.commit()
        return RedirectResponse("/change-password?error=wrong_current", status_code=302)
    np = (new_password or "").strip()
    if len(np) < 8:
        return RedirectResponse("/change-password?error=too_short", status_code=302)
    if np != (repeat_password or "").strip():
        return RedirectResponse("/change-password?error=mismatch", status_code=302)
    if np == current_password:
        return RedirectResponse("/change-password?error=same_as_old", status_code=302)

    from sqlalchemy import update as sql_update  # локальный импорт: name 'update' занят формой
    new_hash = hash_password(np)
    was_forced = bool(user.must_change_password)
    # user здесь detached (загружается в отдельной сессии зависимостью
    # get_current_user) — присваивание атрибутов ничего бы не записало в БД.
    # Поэтому явный Core-update по id: новый хеш + снятие флага принудительной
    # смены (без него пользователь зацикливался на /change-password).
    await session.execute(
        sql_update(User).where(User.id == user.id)
        .values(password_hash=new_hash, must_change_password=False))
    user.password_hash = new_hash
    user.must_change_password = False
    await log_event(session, "password_changed", category="auth", user=user,
                    ip=client_ip(request),
                    message=("Пароль сменён при первом входе (принудительно)"
                             if was_forced else "Пользователь сменил пароль"))
    await session.commit()
    logger.info("change-password: пользователь '%s' сменил пароль (forced=%s)",
                user.login, was_forced)
    return _login_redirect(user)


@router.get("/auth/dev-login")
async def dev_login(login: str, session: AsyncSession = Depends(session_dep)):
    """Вход по логину без пароля — ТОЛЬКО локальная разработка (DEV_LOGIN_ENABLED=true).

    В продакшене отключён (404 + fail-fast require_production_settings). Нужен для
    E2E-тестов и ручной проверки RBAC/шеринга под разными участниками. Пользователь
    должен существовать (демо-сид создаёт их с фиксированными логинами).
    """
    if not get_settings().dev_login_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Не найдено")
    user = await session.scalar(
        select(User).where(func.lower(User.login) == login.strip().lower()))
    if user is None or not user.is_active:
        raise HTTPException(404, "Пользователь с таким логином не найден (dev-режим)")
    return _login_redirect(user)


@router.post("/auth/logout")
async def logout(request: Request, user: User = Depends(get_current_user),
                 session: AsyncSession = Depends(session_dep)):
    await log_event(session, "logout", category="auth", user=user, ip=client_ip(request),
                    message="Выход из системы")
    await session.commit()
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@router.get("/auth/me")
async def me(user: User = Depends(get_current_user)):
    return {"id": user.id, "name": user.display_name, "role": user.role.value,
            "login": user.login, "account": "local",
            "must_change_password": bool(user.must_change_password),
            "telegram_linked": bool(user.telegram_chat_id),
            "calendar_linked": bool(user.yandex_cal_app_password_enc)}


def _initials(name: str) -> str:
    parts = name.split()
    return "".join(p[0].upper() for p in parts[:2]) if parts else "?"
