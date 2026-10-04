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
    """
    from sqlalchemy.orm import selectinload

    from app.db.engine import SessionLocal
    from app.models.core import DepartmentMember, User

    data = read_session_token(request.cookies.get(SESSION_COOKIE))
    if not data or "uid" not in data:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Не авторизован")
    async with SessionLocal() as session:
        user = await session.scalar(
            select(User).options(selectinload(User.memberships)).where(User.id == data["uid"]))
        if user is not None:
            for m in user.memberships:
                _ = m.department_id  # скаляр уже загружен; связь department не трогаем
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Учётная запись заблокирована")
    return user


require_login = get_current_user  # семантический алиас


def optional_user_from_request(request: Request):
    """Для Jinja-роутеров: пользователь или None (без исключения)."""
    data = read_session_token(request.cookies.get(SESSION_COOKIE))
    return data.get("uid") if data else None
