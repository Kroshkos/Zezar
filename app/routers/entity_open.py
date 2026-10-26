"""QR-шлюз для сущностей: «открой в приложении или на сайте».

Эндпоинт:
  GET /open/e/{uid}  — универсальная HTTPS-ссылка, которая кладётся в QR-код.

Поведение при сканировании QR (встроенным сканером камеры или браузером):
  1. Если мобильное приложение Zezar установлено — страница пытается открыть
     deep link ``zezar://entity/{uid}``; ОС перехватывает схему и запускает
     приложение (страница уходит в фон, редирект не происходит).
  2. Если приложения нет — через ~1.4 сек выполняется редирект на веб-версию:
       - авторизованный пользователь -> /{module_key}?focus={uid}
         (карточка открывается в модуле сайта);
       - гость -> /login?next=… (после входа попадёт туда же).

Шлюз проверяет существование карточки и доступ пользователя (RBAC), поэтому
чужой/несуществующий uid не раскрывается: отдаётся 404 без редиректа.
"""
from __future__ import annotations

import hashlib
import logging
import re
import secrets
import time

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import access
from app.core.config import get_settings
from app.core.events import client_ip, log_event
from app.core.security import SESSION_COOKIE
from app.deps import session_dep
from app.models.core import ApiToken, Entity, User

logger = logging.getLogger("caesar.open")

router = APIRouter()

UID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Ссылка шлюза должна быть как можно короче: размер QR-кода растёт с длиной
# данных, а компактные коды (версия 2–3, 25–29 модулей) надёжно распознаются
# камерой телефона с печатной карточки даже при плохом освещении.
# Для боевого домена zezar-patfiz.waw0.amvera.tech + uid(12) короткая форма
# /o/{uid} занимает 50 символов (QR версии 2) — короче уже не сделать.
MAX_GATEWAY_LEN = 50


def gateway_url_from_base(base: str, uid: str) -> str:
    """Ссылка для QR-кода: /open/e/{uid}, а если не помещается — короткий
    вариант /o/{uid}. Обе формы понимают шлюз и мобильный клиент."""
    b = base.rstrip("/")
    full = f"{b}/open/e/{uid}"
    if len(full) <= MAX_GATEWAY_LEN:
        return full
    return f"{b}/o/{uid}"

# ---------------------------------------------------------------------------
# Гостевые токены для deep link из QR-шлюза.
#
# Сканер камеры открывает HTTPS-ссылку шлюза в браузере — у браузера нет
# сессии мобильного приложения, и ОС не знает, установлено ли оно. Поэтому
# вместе с deep link передаётся одноразовый гостевой токен:
#   zezar://entity/{uid}?t=<token>
# Приложение обменивает его (POST /api/mobile/guest-login) на обычный
# Bearer access_token того пользователя, который распечатал/показал QR,
# и открывает карточку в рамках его RBAC-прав.
#
# Формат токена: "{user_id}.{sig}", sig = HMAC-SHA256(secret, payload), TTL 15 мин,
# stateless (переживает рестарт процесса и несколько воркеров). Секрет —
# SECRET_KEY приложения. Реvoke API-токенов на гостевой вход не распространяется:
# это короткоживущий доступ по физическому скану QR самой карточки.
# ---------------------------------------------------------------------------

GUEST_TOKEN_TTL = 900  # секунд


def _guest_secret() -> bytes:
    return (get_settings().secret_key or "zezar-dev-secret").encode()


def _guest_sig(payload: str) -> str:
    return hashlib.sha256(_guest_secret() + payload.encode()).hexdigest()[:32]


_used_nonces: dict[str, float] = {}  # nonce -> время выдачи (одноразовость)


def issue_guest_token(user_id: int) -> str:
    exp = int(time.time()) + GUEST_TOKEN_TTL
    nonce = secrets.token_urlsafe(9)
    payload = f"{user_id}.{exp}.{nonce}"
    # чистим реестр использованных нонсов старше TTL
    for n in [n for n, t in _used_nonces.items() if time.time() - t > GUEST_TOKEN_TTL]:
        _used_nonces.pop(n, None)
    return f"{payload}.{_guest_sig(payload)}"


def consume_guest_token(token: str) -> int | None:
    """Проверить подпись/TTL/одноразовость; вернуть user_id или None.
    Успешная проверка помечает токен использованным."""
    try:
        payload, sig = token.rsplit(".", 1)
        uid_s, exp_s, nonce = payload.split(".")
        exp = int(exp_s)
    except (ValueError, AttributeError):
        return None
    if not secrets.compare_digest(sig, _guest_sig(payload)):
        return None
    if time.time() > exp:
        return None
    if nonce in _used_nonces:  # одноразовость
        return None
    _used_nonces[nonce] = exp - GUEST_TOKEN_TTL
    try:
        return int(uid_s)
    except ValueError:
        return None


async def get_optional_user(request: Request):
    """Пользователь из session-cookie или Bearer-токена мобильного приложения;
    для гостя — None (шлюз не бросает 401: QR сканируют и без входа)."""
    from app.core.security import get_current_user

    auth_header = request.headers.get("authorization") or ""
    if not auth_header.startswith("Bearer ") and not request.cookies.get(SESSION_COOKIE):
        return None  # заведомый гость — без похода в БД
    try:
        return await get_current_user(request)
    except Exception:  # noqa: BLE001 — протухшая/неподписанная кука = гость
        return None


def _web_url(request: Request, path: str) -> str:
    """Абсолютный веб-адрес с учётом возможного reverse-proxy (X-Forwarded-*)."""
    forwarded_host = request.headers.get("x-forwarded-host")
    if forwarded_host:
        proto = request.headers.get("x-forwarded-proto") or request.url.scheme
        base = f"{proto}://{forwarded_host}"
    else:
        base = str(request.base_url).rstrip("/")
    return f"{base}{path}"


_GATEWAY_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ЦЕЗАРЬ — открытие карточки…</title>
<style>
  body { font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
         background:#0f172a; color:#e2e8f0; display:flex; align-items:center;
         justify-content:center; min-height:100vh; margin:0; }
  .box { text-align:center; padding:24px; max-width:420px; }
  .logo { font-size:30px; letter-spacing:3px; color:#93c5fd; margin-bottom:12px; }
  a.btn { display:inline-block; margin-top:18px; padding:12px 22px; border-radius:10px;
          background:#2563eb; color:#fff; text-decoration:none; font-weight:600; }
  a.secondary { display:block; margin-top:14px; color:#94a3b8; font-size:13px; }
  #hint { color:#94a3b8; font-size:14px; }
</style>
</head>
<body>
  <div class="box">
    <div class="logo">ЦЕЗАРЬ</div>
    <p id="hint">Открываем карточку «__TITLE__» в мобильном приложении…</p>
    <noscript><p>Для перехода включите JavaScript или откройте ссылку вручную:</p>
      <a class="secondary" href="__WEB_URL__">Открыть на сайте →</a></noscript>
    <div id="manual" style="display:none">
      <p>Приложение не запущено?</p>
      <a class="btn" id="retry" href="#">Открыть в приложении</a>
      <a class="secondary" id="webfallback" href="#">Открыть на сайте →</a>
    </div>
  </div>
<script>
(function () {
  var DEEP = "__DEEP_LINK__";
  var WEB  = "__WEB_URL__";
  var FALLBACK_MS = 1400;
  var done = false;

  function openWeb() {
    if (done) return;
    done = true;
    location.replace(WEB);
  }

  function tryApp() {
    // Скрытый iframe — способ вызвать custom scheme без «всплывашки»:
    // если приложение установлено, ОС перехватит zezar:// и страница уйдёт в фон.
    try {
      var f = document.createElement("iframe");
      f.style.display = "none";
      f.src = DEEP;
      document.body.appendChild(f);
      setTimeout(function () { f.remove(); }, 3000);
    } catch (e) { /* ignore */ }
    // Страховка: прямой переход (надёжнее на десктопе).
    setTimeout(function () { if (!done) location.href = DEEP; }, 50);
  }

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) done = true; // приложение открылось поверх вкладки
  });
  window.addEventListener("pagehide", function () { done = true; });

  tryApp();
  setTimeout(function () {
    if (done) return;
    // Приложение не отозвалось: показываем кнопки + уходим на сайт.
    document.getElementById("hint").textContent = "Мобильное приложение не найдено.";
    document.getElementById("manual").style.display = "block";
    document.getElementById("retry").href = DEEP;
    document.getElementById("webfallback").href = WEB;
    openWeb();
  }, FALLBACK_MS);
})();
</script>
</body>
</html>
"""


async def _render_gateway(uid: str, request: Request, user, session, go: str = ""):
    """Шлюз для QR/share-ссылок: deep link в приложение, иначе — карточка сайта.

    go — необязательный «веб-адрес по умолчанию» из ссылки (например /okr/
    для печатных карточек модулей): если задан и это внутренний путь,
    браузер после скана попадёт туда вместо дефолтного фокуса на /tasks.
    """
    if not UID_RE.match(uid or ""):
        return HTMLResponse("<h1>Некорректный адрес карточки</h1>", status_code=404)

    from app.routers.qr_codes import build_deep_link
    deep_link = build_deep_link(uid)

    # Разрешаем только относительные пути нашего сайта (?entity=<uid> шлюз
    # добавит сам); всё остальное (внешние URL, //host, схемы) игнорируем.
    go_path = ""
    if go and go.startswith("/") and not go.startswith("//"):
        go_path = go.split("?")[0].split("#")[0]

    title = ""
    if user is None:
        # Гость (приложения нет): на сайт нельзя пускать анонимно — сначала вход,
        # после которого попадёт прямо на карточку (?next — внутренний путь).
        # Ответ идентичен для любых uid, включая несуществующие: сканирование QR
        # не должно быть способом разведки «есть ли такая карточка».
        web_path = f"{go_path}?entity={quote(uid)}" if go_path else f"/tasks?focus={quote(uid)}"
        web = _web_url(request, f"/login?next={quote(web_path, safe='')}")
    else:
        ent = await session.scalar(select(Entity).where(Entity.uid == uid))
        if ent is None:
            return HTMLResponse("<h1>Карточка не найдена</h1>", status_code=404)
        # RBAC как в мобильных эндпоинтах: недоступная карточка — тоже 404,
        # чтобы шлюз не стал каналом утечки факта существования чужих сущностей.
        ctx = await access.build_context(session, user)
        perm = await access.resolve_permission(session, ctx, ent)
        if perm is None:
            return HTMLResponse("<h1>Карточка не найдена</h1>", status_code=404)
        if go_path:
            web_path = f"{go_path}?entity={quote(uid)}"
        elif ent.module_key == "tasks":
            web_path = f"/tasks?focus={quote(uid)}"
        else:
            web_path = f"/{ent.module_key}/?entity={quote(uid)}"
        web = _web_url(request, web_path)
        title = (ent.title or "")[:80]
        # Авторизованный сканер: передаём в приложение одноразовый гостевой
        # токен — оно откроет карточку без ручного логина (TTL 15 минут).
        deep_link = f"{deep_link}?t={quote(issue_guest_token(user.id), safe='')}"

    html = (
        _GATEWAY_HTML
        .replace("__DEEP_LINK__", deep_link)
        .replace("__WEB_URL__", web)
        .replace("__TITLE__", title.replace("<", "&lt;"))
    )
    # Шлюз не кешируем: решение зависит от устройства пользователя и хоста.
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@router.get("/open/e/{uid}", response_class=HTMLResponse, include_in_schema=False)
async def entity_open_gateway(
    uid: str,
    request: Request,
    go: str = "",
    user: User | None = Depends(get_optional_user),
    session: AsyncSession = Depends(session_dep),
):
    return await _render_gateway(uid, request, user, session, go)


# Короткий алиас для QR-кодов: /o/{uid} вместо /open/e/{uid}. Нужен, когда
# полный путь с доменом не влезает в компактный QR (см. gateway_url_from_base) —
# маленький код лучше распознаётся камерой с печатной карточки.
@router.get("/o/{uid}", response_class=HTMLResponse, include_in_schema=False)
async def entity_open_gateway_short(
    uid: str,
    request: Request,
    go: str = "",
    user: User | None = Depends(get_optional_user),
    session: AsyncSession = Depends(session_dep),
):
    return await _render_gateway(uid, request, user, session, go)


# ---------------------------------------------------------------------------
# Прямой скан deep link (если кто-то распечатал/сохранил zezar://-ссылку):
# сервер сам выдаёт гостевой токен авторизованного пользователя и редиректит
# в приложение. Без входа — на страницу с инструкцией (QR приложения нет).
# ---------------------------------------------------------------------------

@router.get("/open/e/{uid}/deep", include_in_schema=False)
async def entity_open_deep(
    uid: str,
    request: Request,
    user: User | None = Depends(get_optional_user),
    session: AsyncSession = Depends(session_dep),
):
    if not UID_RE.match(uid or ""):
        raise HTTPException(status_code=404, detail="Некорректный адрес карточки")
    from app.routers.qr_codes import build_deep_link

    if user is None:
        # Не 401: страница-ответ идентична для любых uid (в т.ч. несуществующих),
        # чтобы эндпоинт не стал каналом разведки. Скан zezar:// без входа в
        # приложение невозможен — просим войти и отсканировать обычный QR.
        return HTMLResponse(
            "<h1>ЦЕЗАРЬ</h1><p>Для открытия карточки в приложении войдите на сайте "
            "и отсканируйте QR-код карточки камерой телефона.</p>", status_code=200)
    ent = await session.scalar(select(Entity).where(Entity.uid == uid))
    if ent is None:
        raise HTTPException(status_code=404, detail="Карточка не найдена")
    ctx = await access.build_context(session, user)
    perm = await access.resolve_permission(session, ctx, ent)
    if perm is None:
        raise HTTPException(status_code=404, detail="Карточка не найдена")
    guest = issue_guest_token(user.id)
    return RedirectResponse(
        url=f"{build_deep_link(uid)}?t={quote(guest, safe='')}", status_code=302
    )


# ---------------------------------------------------------------------------
# Обмен гостевого QR-токена на обычный Bearer access_token (как token-login,
# но без пароля — доверие основано на физическом скане QR самой карточки).
# Создаётся ApiToken с меткой «QR guest» — его видно и можно отозвать в
# списке токенов пользователя, как и любой мобильный токен.
# ---------------------------------------------------------------------------

from pydantic import BaseModel  # noqa: E402


class GuestLoginRequest(BaseModel):
    token: str


@router.post("/api/mobile/guest-login", include_in_schema=False)
async def guest_login(
    payload: GuestLoginRequest,
    req: Request,
    session: AsyncSession = Depends(session_dep),
):
    user_id = consume_guest_token(payload.token or "")
    if user_id is None:
        raise HTTPException(status_code=401, detail="Ссылка устарела — отсканируйте QR заново")
    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Учётная запись недоступна")

    raw_token = secrets.token_hex(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    session.add(ApiToken(
        user_id=user.id, token_hash=token_hash,
        name="Mobile app (QR)", is_active=True,
    ))
    await log_event(
        session, "guest_login", category="auth", user=user,
        ip=client_ip(req) or "?",
        message=f"Гостевой вход по QR-коду карточки (логин «{user.login}»)",
    )
    await session.commit()
    logger.info("guest-login: вход '%s' по одноразовому QR-токену", user.login)
    return {
        "access_token": raw_token,
        "token_type": "bearer",
        "guest": True,
        "user": {
            "id": user.id,
            "name": user.display_name,
            "role": user.role.value,
            "login": user.login,
            "must_change_password": bool(user.must_change_password),
        },
    }
