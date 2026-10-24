"""
API Token аутентификация для мобильного приложения Zezar-app.

Эндпоинты:
  POST /api/auth/token-login       Логин + пароль -> access_token + user
  POST /api/auth/token-refresh     Refresh token -> новый access_token
  POST /api/auth/token-revoke      Отозвать токен
  GET  /api/auth/token/me           Текущий пользователь (по Bearer token)
  GET  /api/auth/tokens             Список активных токенов
  POST /api/auth/tokens             Создать новый токен
  DELETE /api/auth/tokens/{id}       Отозвать токен

Механизм:
  - При логине генерируется случайный токен (32 байта hex)
  - Токен хешируется через SHA-256 и сохраняется в БД
  - При запросе токен извлекается из Authorization: Bearer <token>
  - Хешируется входящий токен и ищется в БД
  - Токен может быть создан отдельно через "Создать токен"
"""
from __future__ import annotations

import logging
import secrets
import hashlib
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.events import client_ip, log_event
from app.core.security import verify_password
from app.deps import session_dep
from app.models.core import ApiToken, User

logger = logging.getLogger("caesar.token_auth")

router = APIRouter(prefix="/api/auth", tags=["mobile-auth"])

security = HTTPBearer(auto_error=False)


# ======================= Pydantic модели =======================

class TokenLoginRequest(BaseModel):
    login: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = 86400 * 365  # 1 year
    user: dict


class TokenCreateRequest(BaseModel):
    name: str  # метка токена


class TokenInfo(BaseModel):
    id: int
    name: str
    is_active: bool
    created_at: datetime
    last_used_at: Optional[datetime]
    created_at_human: str


# ======================= Зависимости =======================

async def get_current_user_from_token(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    session: AsyncSession = Depends(session_dep),
) -> User:
    """Извлечь пользователя из Bearer token."""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Не авторизован: отсутствует token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token_value = credentials.credentials
    token_hash = hashlib.sha256(token_value.encode()).hexdigest()

    # Ищем активный токен
    result = await session.execute(
        select(ApiToken)
        .where(ApiToken.token_hash == token_hash)
        .where(ApiToken.is_active == True)  # noqa: E712
    )
    api_token = result.scalar_one_or_none()

    if api_token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Недействительный или отозванный токен",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Обновляем last_used_at
    api_token.last_used_at = datetime.now(timezone.utc)

    # Загружаем пользователя
    result = await session.execute(
        select(User).where(User.id == api_token.user_id)
    )
    user = result.scalar_one_or_none()

    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Учётная запись заблокирована",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user


async def get_current_user_optional_from_token(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    session: AsyncSession = Depends(session_dep),
) -> Optional[User]:
    """Опционально извлечь пользователя из token (может вернуть None)."""
    if credentials is None:
        return None

    try:
        return await get_current_user_from_token(credentials, session)
    except HTTPException:
        return None


# ======================= Эндпоинты =======================

@router.post("/token-login", response_model=TokenResponse)
async def token_login(
    request: TokenLoginRequest,
    req: Request,
    session: AsyncSession = Depends(session_dep),
):
    """Логин по логину и паролю -> возвращает access_token."""
    login_norm = (request.login or "").strip().lower()
    user = await session.scalar(
        select(User).where(func.lower(User.login) == login_norm)
    )

    if user is None or not user.is_active:
        await log_event(
            session, "token_login_failed", category="auth",
            actor_name=login_norm[:60] or None, ip=client_ip(req) or "?",
            level="warning",
            message=f"Неудачный вход по API: логин «{login_norm[:60] or '—'}»"
        )
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный логин или пароль",
        )

    if not verify_password(request.password, user.password_hash):
        await log_event(
            session, "token_login_failed", category="auth",
            actor_name=login_norm[:60] or None, ip=client_ip(req) or "?",
            level="warning",
            message=f"Неверный пароль для логина «{login_norm[:60] or '—'}»"
        )
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный логин или пароль",
        )

    # Генерируем токен
    raw_token = secrets.token_hex(32)  # 64 hex chars
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

    # Сохраняем токен
    api_token = ApiToken(
        user_id=user.id,
        token_hash=token_hash,
        name="Mobile app",
        is_active=True,
    )
    session.add(api_token)

    await log_event(
        session, "token_login", category="auth", user=user,
        ip=client_ip(req) or "?",
        message=f"Вход в мобильное приложение (логин «{user.login}»)"
    )
    await session.commit()

    logger.info("token-login: успешный вход '%s' с мобильного устройства", user.login)

    return TokenResponse(
        access_token=raw_token,
        user={
            "id": user.id,
            "name": user.display_name,
            "role": user.role.value,
            "login": user.login,
            "must_change_password": bool(user.must_change_password),
        },
    )


@router.post("/token-revoke")
async def revoke_current_token(
    req: Request,
    user: User = Depends(get_current_user_from_token),
    session: AsyncSession = Depends(session_dep),
):
    """Отозвать текущий токен (выход из мобильного приложения)."""
    credentials = req.headers.get("authorization")
    if credentials and credentials.startswith("Bearer "):
        token_value = credentials[7:]
        token_hash = hashlib.sha256(token_value.encode()).hexdigest()

        result = await session.execute(
            select(ApiToken)
            .where(ApiToken.token_hash == token_hash)
            .where(ApiToken.user_id == user.id)
        )
        api_token = result.scalar_one_or_none()
        if api_token:
            api_token.is_active = False
            await log_event(
                session, "token_revoke", category="auth", user=user,
                ip=client_ip(req) or "?",
                message="Токен мобильного приложения отозван"
            )

    await session.commit()
    return {"ok": True}


@router.get("/token/me", response_model=dict)
async def me_from_token(
    user: User = Depends(get_current_user_from_token),
):
    """Профиль текущего пользователя (по Bearer token)."""
    return {
        "id": user.id,
        "name": user.display_name,
        "role": user.role.value,
        "login": user.login,
        "must_change_password": bool(user.must_change_password),
    }


@router.get("/tokens", response_model=list[TokenInfo])
async def list_tokens(
    user: User = Depends(get_current_user_from_token),
    session: AsyncSession = Depends(session_dep),
):
    """Список активных API токенов текущего пользователя."""
    result = await session.execute(
        select(ApiToken)
        .where(ApiToken.user_id == user.id)
        .order_by(ApiToken.created_at.desc())
    )
    tokens = result.scalars().all()

    return [
        TokenInfo(
            id=t.id,
            name=t.name,
            is_active=t.is_active,
            created_at=t.created_at,
            last_used_at=t.last_used_at,
            created_at_human=t.created_at.strftime("%d.%m.%Y %H:%M"),
        )
        for t in tokens
    ]


@router.post("/tokens", response_model=dict)
async def create_token(
    req: TokenCreateRequest,
    user: User = Depends(get_current_user_from_token),
    session: AsyncSession = Depends(session_dep),
):
    """Создать новый API токен для текущего пользователя.

    Возвращает токен ОДИН РАЗ — он попадёт в ответ только при создании.
    """
    if not req.name or not req.name.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Укажите название токена",
        )

    raw_token = secrets.token_hex(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

    api_token = ApiToken(
        user_id=user.id,
        token_hash=token_hash,
        name=req.name.strip()[:120],
        is_active=True,
    )
    session.add(api_token)

    await log_event(
        session, "token_create", category="auth", user=user,
        ip=client_ip(req),
        message=f"Создан API токен: «{req.name.strip()[:60]}»"
    )
    await session.commit()

    logger.info("token-create: пользователь '%s' создал токен «%s»", user.login, req.name)

    return {
        "access_token": raw_token,
        "token_type": "bearer",
        "name": req.name.strip(),
        "expires_in": 86400 * 365,
        "warning": "Сохраните токен сейчас — он больше не будет показан",
    }


@router.delete("/tokens/{token_id}")
async def delete_token(
    token_id: int,
    user: User = Depends(get_current_user_from_token),
    session: AsyncSession = Depends(session_dep),
):
    """Отозвать (удалить) API токен."""
    result = await session.execute(
        select(ApiToken)
        .where(ApiToken.id == token_id)
        .where(ApiToken.user_id == user.id)
    )
    api_token = result.scalar_one_or_none()

    if api_token is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Токен не найден",
        )

    api_token.is_active = False

    await log_event(
        session, "token_delete", category="auth", user=user,
        ip=client_ip(req),
        message=f"Токен «{api_token.name}» отозван"
    )
    await session.commit()

    return {"ok": True}
