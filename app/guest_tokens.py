"""Одноразовые гостевые токены для QR-шлюза (v1.5).

Сценарий: пользователь сканирует QR карточки камерой -> шлюз /open/e/{uid}
открывает мобильное приложение по deep link zezar://entity/{uid}?t=<гостевой
токен>. Приложение обменивает токен на обычный API-токен (POST
/api/auth/token-exchange) и показывает карточку — без ручного ввода пароля.

Токен:
  "guest:" + base64url(json{u,e,exp,sig})   (подпись HMAC-SHA256 на SECRET_KEY)
  * привязан к конкретной карточке (e=uid) — украденный токен открывает только её;
  * короткоживущий (GUEST_TOKEN_TTL секунд, по умолчанию 900);
  * выдаётся только тому, кто уже авторизован в шлюзе и имеет RBAC-доступ к uid;
  * при обмене помечается использованным в in-memory黑名单 (однократность best-effort:
    перезапуск сервера сбрасывает список, но TTL+привязка к uid ограничивают окно).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from app.core.config import get_settings

PREFIX = "guest:"

# Best-effort список уже потраченных токенов (подпись уникальна из-за exp).
_used: set[str] = set()


def _sign(payload_b64: str) -> str:
    key = get_settings().secret_key.encode()
    return hmac.new(key, payload_b64.encode(), hashlib.sha256).hexdigest()


def make_guest_token(user_id: int, entity_uid: str) -> str:
    ttl = int(getattr(get_settings(), "guest_token_ttl", 900))
    payload = {"u": user_id, "e": entity_uid, "exp": int(time.time()) + ttl}
    body = base64.urlsafe_b64(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    return PREFIX + body + "." + _sign(body)


def parse_guest_token(token: str) -> dict | None:
    """Возвращает {"user_id", "entity_uid"} для валидного живого токена, иначе None."""
    if not token or not token.startswith(PREFIX):
        return None
    raw = token[len(PREFIX):]
    body, sep, sig = raw.rpartition(".")
    if not sep or not body or not sig:
        return None
    if not hmac.compare_digest(sig, _sign(body)):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except Exception:  # noqa: BLE001
        return None
    if int(payload.get("exp", 0)) < time.time():
        return None
    if token in _used:
        return None
    return {"user_id": payload.get("u"), "entity_uid": payload.get("e")}


def mark_used(token: str) -> None:
    _used.add(token)
    if len(_used) > 100_000:  # защита от разрастания памяти
        _used.clear()
