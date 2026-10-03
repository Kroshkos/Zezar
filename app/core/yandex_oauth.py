"""
OAuth 2.0 (Authorization Code) через Яндекс ID.

Регистрация ЗАКРЫТА: callback принимает только тех, чей yandex_id уже внесён
администратором (таблица users / Invite). Неизвестный sub -> страница «доступ закрыт».

Flow:
  GET  /auth/login     -> redirect на oauth.yandex.ru/authorize (state в cookie)
  GET  /auth/callback  -> code->token -> userinfo -> session-cookie
  POST /auth/logout    -> сброс cookie
"""
from __future__ import annotations

import secrets
import urllib.parse
from dataclasses import dataclass

import httpx
from fastapi import HTTPException, Request, status

from app.core.config import get_settings

AUTH_URL = "https://oauth.yandex.ru/authorize"
TOKEN_URL = "https://oauth.yandex.ru/token"
USERINFO_URL = "https://login.yandex.ru/info?format=json"

STATE_COOKIE = "caesar_oauth_state"


@dataclass
class YandexUser:
    yandex_id: str
    display_name: str
    email: str | None
    default_email: str | None
    login: str | None = None   # логин аккаунта — нужен для CalDAV Basic auth


def authorize_url() -> str:
    s = get_settings()
    if not s.yandex_client_id:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "OAuth не настроен: заполните YA_CLIENT_ID в .env")
    state = secrets.token_urlsafe(16)
    q = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": s.yandex_client_id,
        "redirect_uri": s.yandex_redirect_uri,
        "force_confirm": "yes",
        "format": "json",
        "state": state,
    })
    return f"{AUTH_URL}?{q}", state


async def exchange_code(code: str) -> dict:
    """code -> access_token (ya_userinfo scope обязателен у приложения в OAuth-кабинете)."""
    s = get_settings()
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(TOKEN_URL, data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": s.yandex_client_id,
            "client_secret": s.yandex_client_secret,
            "redirect_uri": s.yandex_redirect_uri,
        })
        if r.status_code != 200:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                                f"Яндекс отклонил обмен кода: {r.text[:200]}")
        return r.json()


async def fetch_userinfo(access_token: str) -> YandexUser:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(USERINFO_URL, headers={"Authorization": f"OAuth {access_token}"})
        if r.status_code != 200:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Не удалось получить профиль Яндекса")
        j = r.json()
    emails = j.get("emails") or []
    return YandexUser(
        yandex_id=str(j["id"]),
        display_name=j.get("real_name") or j.get("display_name") or j.get("default_email") or "Без имени",
        email=(j.get("default_email") or (emails[0] if emails else None)),
        default_email=j.get("default_email"),
        login=j.get("login"),
    )


def check_state(request: Request, state: str | None) -> None:
    expected = request.cookies.get(STATE_COOKIE)
    if not state or not expected or not secrets.compare_digest(expected, state):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Несовпадение OAuth state (CSRF?)")
