"""HTML-страницы ядра: вход, корень, профильные настройки. Страницы модулей — у самих модулей."""
from __future__ import annotations

import secrets as _secrets

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decrypt_secret, encrypt_secret, get_current_user
from app.deps import session_dep
from app.models.core import User
from app.ui import render_page, templates as core_templates

router = APIRouter()
templates = core_templates

ERRORS = {"no_code": "Яндекс не вернул код", "state": "Сессия входа истекла, попробуйте ещё раз",
          "not_allowed": "Доступ закрыт: обратитесь к администратору за приглашением",
          "disabled": "Учётная запись заблокирована"}


@router.get("/login")
async def login_page(request: Request, error: str = ""):
    from app.core.config import get_settings
    return templates.TemplateResponse(request, "login.html",
                                      {"error": bool(error),
                                       "error_msg": ERRORS.get(error, "Ошибка входа"),
                                       "dev": bool(getattr(get_settings(), "dev_login_enabled", False))})


@router.get("/")
async def index(request: Request, user: User = Depends(get_current_user),
                session: AsyncSession = Depends(session_dep)):
    """Корень -> первая доступная страница (дашборд)."""
    return await render_page(request, "dashboard.html", module_key="dashboard",
                             page_title="Дашборд", page_sub="",
                             user=user, session=session)


# ---------------- Профиль / интеграции ----------------

class CalIn(BaseModel):
    app_password: str
    calendar_name: str = "Цезарь"


@router.put("/api/profile/calendar")
async def save_calendar(body: CalIn, request: Request,
                        user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(session_dep)):
    """Сохраняем app-пароль (Fernet) после проверки WebDAV-доступа; статус синхронизации сбрасываем на pending."""
    from integrations import yandex_calendar as yc
    if not user.email:
        return JSONResponse({"detail": "В профиле Яндекса нет email — привяжите аккаунт с почтой"}, 400)
    ok = await yc.verify_credentials(user.email, body.app_password)
    if not ok:
        return JSONResponse({"detail": "Яндекс отклонил app-пароль"}, 400)
    user.yandex_cal_app_password_enc = encrypt_secret(body.app_password)
    user.yandex_cal_name = body.calendar_name or "Цезарь"
    await session.commit()
    # diff-проход воркера сам поставит все видимые пользователю PENDING-сущности в очередь
    return {"ok": True, "calendar": user.yandex_cal_name}


@router.post("/api/profile/telegram/bind")
async def telegram_bind(user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(session_dep)):
    """Генерирует одноразовый код: отправьте боту «/start <код>» (воркер long-polling привяжет chat_id)."""
    code = _secrets.token_hex(4)
    user.telegram_bind_code = code
    await session.commit()
    return {"code": code, "hint": "Напишите боту: /start " + code}


@router.get("/api/profile/integrations")
async def integrations_state(user: User = Depends(get_current_user)):
    return {"telegram_linked": bool(user.telegram_chat_id),
            "telegram_muted": user.telegram_optout == "all",
            "calendar_linked": bool(decrypt_secret(user.yandex_cal_app_password_enc)),
            "calendar_name": user.yandex_cal_name,
            "email": user.email}
