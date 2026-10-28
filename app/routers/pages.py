"""HTML-страницы ядра: вход, смена пароля, корень, профильные настройки.
Страницы модулей — у самих модулей."""
from __future__ import annotations

import secrets as _secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import decrypt_secret, encrypt_secret, get_current_user
from app.deps import session_dep
from app.models.core import Department, EntityKind, Role, User
from app.ui import render_page, templates as core_templates

router = APIRouter()
templates = core_templates

ERRORS = {"disabled": "Учётная запись заблокирована",
          "bad_credentials": "Неверный логин или пароль",
          "too_many": "Слишком много неудачных попыток — подождите 15 минут"}


@router.get("/login")
async def login_page(request: Request, error: str = "", next: str = ""):
    from app.core.config import get_settings
    msg = ERRORS.get(error, "Ошибка входа")
    # сохраняем цель редиректа только если это внутренний путь
    safe_next = next if (next.startswith("/") and not next.startswith("//")) else ""
    return templates.TemplateResponse(request, "login.html",
                                      {"error": bool(error),
                                       "error_msg": msg,
                                       "next": safe_next,
                                       "dev": bool(getattr(get_settings(), "dev_login_enabled", False))})


@router.get("/")
async def index(request: Request, user: User = Depends(get_current_user),
                session: AsyncSession = Depends(session_dep)):
    """Корень -> первая доступная страница (дашборд).

    Пользователь с не сменённым начальным паролем принудительно направляется
    на страницу смены пароля (защита от прямого захода по URL после первого входа)."""
    if user.must_change_password:
        return RedirectResponse("/change-password?forced=1", status_code=302)
    # Дашборд живёт в модуле-плагине (modules/dashboard): контекст агрегатов
    # строит build_dashboard_data. Рендерить dashboard.html без него нельзя —
    # шаблон обращается к totals/by_module/... и падает UndefinedError.
    from modules.dashboard.queries import build_dashboard_data
    data = await build_dashboard_data(session, user)
    return await render_page(request, "dashboard.html", module_key="dashboard",
                             page_title="Дашборд",
                             page_sub="Сводка по всем рабочим пространствам · данные обновляются при открытии",
                             user=user, session=session, **data)


# ---------------- Принудительная/добровольная смена пароля ----------------

CHANGE_ERRORS = {
    "wrong_current": "Текущий пароль указан неверно",
    "too_short": "Новый пароль слишком короткий (минимум 8 символов)",
    "mismatch": "Пароли в полях «Новый пароль» и «Повторите» не совпадают",
    "same_as_old": "Новый пароль совпадает с текущим — выберите другой",
    "done": "Пароль изменён. Вход выполнен.",
}


@router.get("/change-password")
async def change_password_page(request: Request, error: str = "", forced: str = "",
                               user: User = Depends(get_current_user)):
    """Страница смены пароля. forced=1 — первый вход с начальным паролем,
    пользователь обязан сменить его, до смены другие страницы недоступны."""
    msg = CHANGE_ERRORS.get(error, "Ошибка" if error else "")
    return templates.TemplateResponse(request, "change_password.html", {
        "user": user,
        "forced": forced == "1" or bool(user.must_change_password),
        "error_msg": msg,
        "ok": error == "done",
    })


# ---------------- Администрирование системы (пользователи + журнал событий) ----------------

def _require_system_admin(user: User) -> None:
    if user.role not in (Role.ADMIN, Role.COORDINATOR):
        raise HTTPException(403, "Раздел доступен администратору/координатору")


@router.get("/admin")
async def admin_users_page(request: Request, user: User = Depends(get_current_user),
                           session: AsyncSession = Depends(session_dep)):
    """Раздел «Администрирование»: учётные записи пользователей
    (Фамилия Имя, логин, начальный пароль, роль) + ссылки на журнал событий."""
    _require_system_admin(user)
    from app.db.seed import is_env_admin_login
    rows = (await session.scalars(select(User).order_by(User.display_name))).all()
    users = [{
        "id": u.id, "name": u.display_name, "login": u.login, "email": u.email or "",
        "role": u.role.value, "active": bool(u.is_active),
        "env_admin": bool(is_env_admin_login(u.login)),
        "must_change": bool(u.must_change_password),
        "no_password": not bool(u.password_hash),
        # Состояние Telegram-интеграции: none | active | muted (см. GET /admin/users)
        "telegram_state": ("none" if not u.telegram_chat_id else
                           ("muted" if (getattr(u, "telegram_optout", None) == "all"
                                        or not u.telegram_notify) else "active")),
    } for u in rows]
    depts = (await session.execute(
        select(Department.id, Department.name)
        .where(Department.is_active == True)  # noqa: E712
        .order_by(Department.name))).all()
    return await render_page(request, "admin_users.html", module_key="system_admin",
                             user=user, session=session,
                             page_title="Администрирование",
                             page_sub="Учётные записи пользователей и журнал событий системы",
                             counts={},
                             users=users,
                             departments=[{"id": d[0], "name": d[1]} for d in depts])


@router.get("/admin/events")
async def admin_events_page(request: Request, category: str = "", q: str = "",
                            limit: int = 200,
                            user: User = Depends(get_current_user),
                            session: AsyncSession = Depends(session_dep)):
    """Журнал событий: всё, что происходит в системе (вход/выход, действия админа,
    CRUD сущностей, подразделения, системные события). Фильтры: категория, поиск."""
    _require_system_admin(user)
    from app.core.events import read_events, event_stats
    items, total = await read_events(session, limit=max(1, min(limit, 1000)), offset=0,
                                     category=category or None, q=q or None)
    stats = await event_stats(session)
    return await render_page(request, "admin_events.html", module_key="system_admin",
                             user=user, session=session,
                             page_title="Журнал событий",
                             page_sub=f"Записей по фильтру: {total}",
                             counts={},
                             events=items, stats=stats,
                             filter_category=category, filter_q=q, filter_limit=limit)


# ---------------- Профиль / интеграции ----------------

class CalIn(BaseModel):
    app_password: str
    calendar_name: str = "Цезарь"
    cal_login: str | None = None   # логин аккаунта Яндекса для CalDAV (вводится вручную)


@router.put("/api/profile/calendar")
async def save_calendar(body: CalIn, request: Request,
                        user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(session_dep)):
    """Сохраняем app-пароль (Fernet) после проверки WebDAV-доступа; статус синхронизации сбрасываем на pending."""
    from integrations import yandex_calendar as yc
    # CalDAV Basic auth: имя пользователя = ЛОГИН аккаунта Яндекса (не email!).
    # После удаления OAuth логин вводится вручную на странице настроек.
    if body.cal_login and body.cal_login.strip():
        login = user.yandex_login = body.cal_login.strip()
    else:
        login = user.yandex_login or (user.email.split("@")[0] if user.email else None)
    if not login:
        return JSONResponse({"detail": "Укажите логин аккаунта Яндекса в поле «Логин CalDAV» "
                                       "(вход в системе к Яндекс.ID не относится)"}, 400)
    ok, code = await yc.verify_credentials(login, body.app_password)
    if not ok:
        detail = {
            401: "Яндекс отклонил логин или app-пароль. Проверьте: пароль создан на id.yandex.ru → «Пароли приложений» → сервис «Другие», введён без опечаток; логин CalDAV — это логин аккаунта Яндекса (без домена)",
            403: "App-пароль недействителен (просрочен/отозван) или для него не выбран сервис «Другие». Создайте новый пароль приложений",
            404: "WebDAV недоступен для этого аккаунта (проверьте, что календарь включён в Яндекс.ID)",
            405: "Яндекс закрыл старый CalDAV-адрес dav.yandex.ru — интеграция должна использовать webdav.yandex.ru. Обновите сервис и повторите попытку",
        }.get(code, f"Сервис Яндекс.Календаря вернул HTTP {code} — попробуйте позже")
        return JSONResponse({"detail": detail}, 400)
    user.yandex_cal_app_password_enc = encrypt_secret(body.app_password)
    user.yandex_cal_name = body.calendar_name or "Цезарь"
    await session.commit()
    # diff-проход воркера сам поставит все видимые пользователю PENDING-сущности в очередь
    return {"ok": True, "calendar": user.yandex_cal_name}


@router.get("/api/profile/telegram/steps")
async def telegram_steps(user: User = Depends(get_current_user)):
    """Инструкция привязки Telegram: сценарий «логин → пароль», без кодов."""
    from app.core.config import get_settings as _gs
    s = _gs()
    bot = ("@" + s.telegram_bot_username) if getattr(s, "telegram_bot_username", None) \
          else (getattr(s, "telegram_bot_name", None) or "бот ЦЕЗАРЬ")
    return {
        "bot": bot,
        "login": user.login,
        "bot_configured": bool(s.telegram_bot_token),
        "public_base_url": s.public_base_url,
        "steps": [
            f"Откройте в Telegram чат с ботом {bot} и нажмите /start (или сразу напишите сообщение).",
            "Первым сообщением отправьте ваш логин от системы ЦЕЗАРЬ — вот он: " + user.login,
            "Бот попросит пароль — отправьте его ОДНИМ сообщением.",
            "Бот проверит данные, немедленно удалит оба сообщения из диалога "
            "и пришлёт приветствие. Готово ✅",
        ],
        "note": "После привязки бот только отправляет уведомления; ваши сообщения он "
                "игнорирует и удаляет. Команды: /mute — отключить уведомления, "
                "/unmute — включить.",
    }


@router.post("/api/profile/calendar/unbind")
async def calendar_unbind(user: User = Depends(get_current_user),
                          session: AsyncSession = Depends(session_dep)):
    """Отвязать Яндекс.Календарь (app-пароль стирается, синхронизация останавливается)."""
    user.yandex_cal_app_password_enc = None
    await session.commit()
    return {"ok": True}


@router.post("/api/profile/telegram/unbind")
async def telegram_unbind(user: User = Depends(get_current_user),
                          session: AsyncSession = Depends(session_dep)):
    """Отвязать Telegram от учётной записи."""
    user.telegram_chat_id = None
    user.telegram_bind_code = None
    user.telegram_optout = None
    await session.commit()
    return {"ok": True}


@router.get("/api/profile/integrations")
async def integrations_state(user: User = Depends(get_current_user)):
    return {"telegram_linked": bool(user.telegram_chat_id),
            "telegram_muted": user.telegram_optout == "all",
            "calendar_linked": bool(decrypt_secret(user.yandex_cal_app_password_enc)),
            "calendar_name": user.yandex_cal_name,
            "email": user.email}


@router.get("/integrations")
async def integrations_page(request: Request,
                            user: User = Depends(get_current_user),
                            session: AsyncSession = Depends(session_dep)):
    """Полноценное окно «Интеграции» (не модалка): Telegram-привязка, сценарии
    уведомлений (базовые неотключаемые + пользовательские), тест доставки."""
    from app.core import notify as notify_core
    scenarios = await notify_core.scenarios_for_user(session, user)
    return templates.TemplateResponse(request, "integrations.html", {
        "user": user,
        "page_title": "Интеграции",
        "scenarios": scenarios,
        "event_keys": ["assigned", "executor_added", "status_changed", "due_soon",
                       "overdue", "created", "updated", "shared", "done", "custom"],
        "entity_kinds": [k.value for k in EntityKind],
        "entity_kind_labels": notify_core.ENTITY_KIND_LABELS,
        "telegram_linked": bool(user.telegram_chat_id),
        "telegram_muted": user.telegram_optout == "all",
        "calendar_linked": bool(decrypt_secret(user.yandex_cal_app_password_enc)),
        "calendar_name": user.yandex_cal_name or "Цезарь",
        "public_base_url": get_settings().public_base_url,
    })
