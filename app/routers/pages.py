"""HTML-страницы ядра: вход, профильные настройки. Страницы модулей — у самих модулей."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates
from pathlib import Path

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

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
