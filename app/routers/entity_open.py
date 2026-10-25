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

import logging
import re

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import access
from app.core.config import get_settings
from app.core.security import SESSION_COOKIE
from app.deps import session_dep
from app.models.core import Entity, User

logger = logging.getLogger("caesar.open")

router = APIRouter()

UID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


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


@router.get("/open/e/{uid}", response_class=HTMLResponse, include_in_schema=False)
async def entity_open_gateway(
    uid: str,
    request: Request,
    user: User | None = Depends(get_optional_user),
    session: AsyncSession = Depends(session_dep),
):
    """Шлюз для QR/share-ссылок: deep link в приложение, иначе — карточка сайта."""
    if not UID_RE.match(uid or ""):
        return HTMLResponse("<h1>Некорректный адрес карточки</h1>", status_code=404)

    settings = get_settings()
    from app.routers.qr_codes import build_deep_link
    deep_link = build_deep_link(uid)

    title = ""
    if user is None:
        # Гость (приложения нет): на сайт нельзя пускать анонимно — сначала вход,
        # после которого попадёт прямо на карточку (?next — внутренний путь).
        # Ответ идентичен для любых uid, включая несуществующие: сканирование QR
        # не должно быть способом разведки «есть ли такая карточка».
        web_path = f"/tasks?focus={quote(uid)}"
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
        web_path = f"/{ent.module_key or 'tasks'}?focus={quote(uid)}"
        web = _web_url(request, web_path)
        title = (ent.title or "")[:80]

    html = (
        _GATEWAY_HTML
        .replace("__DEEP_LINK__", deep_link)
        .replace("__WEB_URL__", web)
        .replace("__TITLE__", title.replace("<", "&lt;"))
    )
    # Шлюз не кешируем: решение зависит от устройства пользователя и хоста.
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})
