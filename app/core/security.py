"""
Безопасность ядра: подписанные session-cookie (без JWT-клиентов на фронте),
Fernet-шифрование секретов пользователей (app-пароли Яндекс.Календаря).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, Request, status
from sqlalchemy import select

from app.core.config import get_settings


# ---------- подписанные session-cookie (HMAC-SHA256, формат: base64(json).sig) ----------

def create_session_token(data: dict) -> str:
    body = base64.urlsafe_b64encode(
        json.dumps({**data, "iat": int(time.time())}, ensure_ascii=False).encode()
    ).decode()
    sig = hmac.new(get_settings().secret_key.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def read_session_token(token: str | None) -> dict | None:
    if not token or "." not in token:
        return None
    body, _, sig = token.rpartition(".")
    expected = hmac.new(get_settings().secret_key.encode(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(body.encode()))
    except Exception:
        return None
    max_age = 86400 * 14  # 2 недели
    if time.time() - data.get("iat", 0) > max_age:
        return None
    return data


def gen_confirm_code() -> str:
    """Код привязки Telegram (/start <code>) и одноразовые коды допуска."""
    return secrets.token_hex(4)


# ---------- Пароли локальных администраторов (pbkdf2_sha256, stdlib) ----------
# Формат хранения: pbkdf2_sha256$<итерации>$<salt-hex>$<hash-hex>.
# Реализация на hashlib + hmac.compare_digest — без внешних зависимостей
# (passlib/bcrypt не входят в образ; pbkdf2 с 240k итераций соответствует OWASP).

PBKDF2_ITERATIONS = 240_000


def hash_password(plain: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", plain.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(plain: str, stored: str | None) -> bool:
    if not stored or not plain:
        return False
    try:
        scheme, iters_s, salt_hex, hash_hex = stored.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", plain.encode(), bytes.fromhex(salt_hex), int(iters_s))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


# ---------- шифрование secrets (app-пароль календаря и т.п.) ----------

def _fernet() -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(get_settings().secret_key.encode()).digest())
    return Fernet(key)


def encrypt_secret(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt_secret(token: str | None) -> str | None:
    if not token:
        return None
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return None


# ---------- FastAPI-зависимости ----------

SESSION_COOKIE = "caesar_session"


async def get_current_user(request: Request):
    """Возвращает User из session-cookie или 401. Импортирует модели лениво,
    чтобы модуль безопасности не зависел от порядка загрузки ORM.

    Сессия закрывается с expire_on_commit=False, а отношения (memberships и др.)
    подгружаются eagerly — иначе роутеры получают DetachedInstanceError на lazy-load.

    Инвалидация кэша прав (acl_epoch): если сервер поднял эпоху ACL (смена
    ролей/участников), кука со старым epoch протухла. Вместо жёсткого 401
    применяется мягкая инвалидация: пользователь получает актуальные права на
    этом же запросе, а на следующий ответ вешается свежая cookie с текущим
    epoch (см. ``attach_fresh_session_cookie``). Принудительный перевход
    остаётся только для токенов, подписанных другим secret_key. Куки без поля
    acl_epoch (выпущены до внедрения epoch) принимаются как раньше.
    """
    from sqlalchemy.orm import selectinload

    from app.db.engine import SessionLocal
    from app.models.core import DepartmentMember, User

    # --- Мобильный контур: Authorization: Bearer <API-токен> ---
    # Тот же пользователь и те же права, что и в веб-сессии; логика токена
    # вынесена в app/routers/token_auth (кэш last_used_at не критичен).
    auth_header = request.headers.get("authorization") or ""
    if auth_header.startswith("Bearer "):
        token_value = auth_header[7:].strip()
        if token_value:
            import hashlib as _hashlib
            from sqlalchemy import select as _select
            from app.models.core import ApiToken
            token_hash = _hashlib.sha256(token_value.encode()).hexdigest()
            async with SessionLocal() as session:
                api_token = await session.scalar(
                    _select(ApiToken).where(ApiToken.token_hash == token_hash,
                                             ApiToken.is_active == True))  # noqa: E712
                if api_token is not None:
                    user = await session.scalar(
                        _select(User).options(selectinload(User.memberships))
                        .where(User.id == api_token.user_id))
                    if user is None or not user.is_active:
                        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                                            "Учётная запись заблокирована")
                    request.state.auth_via_bearer = True
                    return user
            raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                                "Недействительный или отозванный токен",
                                headers={"WWW-Authenticate": "Bearer"})

    data = read_session_token(request.cookies.get(SESSION_COOKIE))
    if not data or "uid" not in data:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Не авторизован")
    stale = False
    async with SessionLocal() as session:
        user = await session.scalar(
            select(User).options(selectinload(User.memberships)).where(User.id == data["uid"]))
        if user is not None:
            for m in user.memberships:
                _ = m.department_id  # скаляр уже загружен; связь department не трогаем
        if user is not None and "acl_epoch" in data:
            from app.core.permcache import get_epoch, token_needs_refresh
            stale = token_needs_refresh(data, await get_epoch(session))
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Учётная запись заблокирована")
    # Помечаем пользователя «протухшей» кукой: middleware-обработчик ниже
    # перезапишет cookie свежим epoch в этом же ответе. Права при этом всегда
    # читаются из БД (user выше загружен заново), поэтому ложных прав нет.
    request.state.acl_stale = bool(stale)
    return user


async def attach_fresh_session_cookie(request: Request, response) -> None:
    """Перевыпустить session-cookie с текущим acl_epoch, если get_current_user
    пометил куку протухшей (``request.state.acl_stale``).

    Это закрывает UX-дефект: эндпоинты, которые сами поднимают epoch
    (patch_user, смена пароля, смена состава), больше не выбрасывают
    легитимного пользователя на /login — его собственная сессия обновляется
    в том же ответе. Вызывается из хука @app.after_request в main.py."""
    try:
        if not getattr(request.state, "acl_stale", False):
            return
        uid = getattr(request.state, "current_uid", None)
        if uid is None:
            from app.core.security import read_session_token
            data = read_session_token(request.cookies.get(SESSION_COOKIE))
            uid = (data or {}).get("uid")
        if uid is None:
            return
        from app.core.permcache import get_epoch
        from app.db.engine import SessionLocal
        async with SessionLocal() as s:
            # force=True: перевыпуск куки не должен опираться на TTL-mirror —
            # иначе после смены роли в другом воркере cookie переживает
            # перезагрузку со старым epoch и страница рендерит устаревшие права.
            epoch = await get_epoch(s, force=True)
        response.set_cookie(
            SESSION_COOKIE, create_session_token({"uid": uid, "acl_epoch": epoch}),
            httponly=True, samesite="lax", max_age=86400 * 14,
            secure=get_settings().session_cookie_secure)
        request.state.acl_stale = False
    except Exception:  # никогда не ломаем ответ из-за косметической куки
        pass


require_login = get_current_user  # семантический алиас


def optional_user_from_request(request: Request):
    """Для Jinja-роутеров: пользователь или None (без исключения)."""
    data = read_session_token(request.cookies.get(SESSION_COOKIE))
    return data.get("uid") if data else None
