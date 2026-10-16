"""
Ядро приложения «ЦЕЗАРЬ»: FastAPI + lifespan (БД в /data, динамический реестр модулей,
фоновые воркеры интеграций).

Запуск:  uvicorn app.main:app
Модуль-плагин = modules/<key>/manifest.py -> registry.register(ModuleSpec(...));
роутер монтируется ДВАЖДЫ: REST на /api/<key>, HTML-страницы на /<key>.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import get_settings
from app.core.module_registry import registry
from app.db.base import Base
from app.db.engine import SessionLocal, engine

log = logging.getLogger("caesar")
settings = get_settings()

# Fail-fast: не даём поднять продакшен с дефолтным секретом / dev-входом / демо-сидом
if settings.is_production:
    settings.require_production_settings()


def _import_all_models() -> None:
    """Ядро + payload-таблицы должны быть импортированы до create_all."""
    import app.models.core            # noqa: F401
    import app.models.module_payloads  # noqa: F401


def _sync_missing_columns(conn) -> None:
    """Самовосстановление схемы для легаси-БД, созданных старым create_all.

    Проблема на продакшене (Amvera): entrypoint при alembic_version==head выполняет
    `stamp head` вместо `upgrade`, поэтому новые колонки (например users.yandex_login)
    так и не добавляются, а приложение падает с "no such column". Здесь сверяем
    фактические колонки SQLite с метаданными моделей и досоздаём отсутствующие
    (ALTER TABLE ADD COLUMN — единственная безопасная операция без пересоздания таблиц;
    индексы добираются через create_all). Идемпотентно."""
    for table in Base.metadata.sorted_tables:
        # Сначала проверяем существование таблицы (SELECT, а не PRAGMA — он не бросает
        # исключение на несуществующей таблице), иначе create_all создаст её целиком.
        exists = conn.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=:n", {"n": table.name}
        ).fetchone()
        if not exists:
            continue
        existing = {r[1] for r in conn.exec_driver_sql(f"PRAGMA table_info({table.name})")}
        missing = [c for c in table.columns if c.name not in existing]
        for col in missing:
            coltype = col.type.compile(conn.dialect)
            conn.exec_driver_sql(
                f"ALTER TABLE {table.name} ADD COLUMN {col.name} {coltype} NULL"
            )
            log.warning("migrate: добавлена отсутствующая колонка %s.%s (%s)",
                        table.name, col.name, coltype)
        if missing:
            # добрать индексы новой колонки, если они объявлены в модели
            for idx in table.indexes:
                if all(c.name in {m.name for m in missing} for c in idx.columns):
                    idx.create(conn, checkfirst=True)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Схема БД (SQLite строго в /data — путь из .env). Модели импортируем ДО create_all,
    #    иначе payload-таблицы модулей не попадут в метаданные.
    _import_all_models()
    async with engine.begin() as conn:
        # Сначала досоздаём недостающие колонки в существующих таблицах (легаси-БД после
        # `alembic stamp head`), затем create_all добавляет отсутствующие таблицы/индексы.
        await conn.run_sync(_sync_missing_columns)
        await conn.run_sync(Base.metadata.create_all)

    # 2. Динамическая регистрация модулей-плагинов + таблица module_registry + bootstrap
    registry.autodiscover("modules")
    registry.mount(app)
    registry.mount_pages(app)
    from app.db.seed import bootstrap
    async with SessionLocal() as s:
        await registry.sync_table(s)
        await bootstrap(s)

    # Диагностика доступа сразу при старте (видно в логах Amvera без входа):
    # сколько админов по логину/паролю видит процесс и какие учётки лежат в базе.
    from app.core.config import get_settings as _gs
    from app.models.core import User as _User
    from sqlalchemy import select as _select
    _admins = _gs().admin_login_entries
    async with SessionLocal() as s:
        _uids = (await s.execute(_select(_User.login, _User.role))).all()
    _st = _gs()
    log.info("access check at startup: admin logins=%s (ADMIN_USERNAME=%r, ADMIN_PASSWORD %s); "
             "users в БД: %s",
             [a[0] for a in _admins] or "не задан",
             _st.admin_username, "задан" if _st.admin_password else "НЕ задан",
             [(lgn, r.value) for lgn, r in _uids] or "—")
    if not _admins:
        log.warning("access check at startup: НЕ задано ни одной учётной записи администратора! "
                    "Задайте ADMIN_USERNAME + ADMIN_PASSWORD (вход по логину/паролю) — "
                    "иначе в систему нельзя войти как админ.")

    # 3. Jinja: подключить папки шаблонов модулей
    from app.ui import init_templates
    init_templates()

    # 4. Фоновые интеграции (не блокируют HTTP): outbox Telegram/Календарь + TG long-polling
    tasks: list[asyncio.Task] = []
    stop = asyncio.Event()
    if settings.workers_enabled:
        try:
            from integrations.workers import outbox_worker, tg_poller
            tasks.append(asyncio.create_task(outbox_worker(stop)))
            if settings.telegram_bot_token:
                tasks.append(asyncio.create_task(tg_poller(stop)))
        except Exception as e:  # noqa: BLE001
            log.warning("workers disabled: %s", e)

    yield

    stop.set()
    for t in tasks:
        t.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await t


app = FastAPI(title=settings.app_name, lifespan=lifespan, docs_url="/api/docs-openapi",
              redoc_url=None, openapi_url="/api/openapi.json")


@app.middleware("http")
async def security_headers(request, call_next):
    """Базовые заголовки безопасности (без CDN-зависимостей, CSP под локальные ресурсы)."""
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        # script-src 'unsafe-inline': интерфейс наследует вёрстку прототипа с inline-обработчиками
        # onclick="..." (сохранены 1:1 по ТЗ); скрипты и данные — только локальные (/static, JSON-блоки).
        "Content-Security-Policy",
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'",
    )
    return response

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
if STATIC_DIR.is_dir():
    class NoCacheStaticFiles(StaticFiles):
        """Статика без кэша: после деплоя браузер сразу получает свежий JS/CSS
        (иначе пользователи видят старый core.js из кэша — классика «кнопки не работают»)."""
        async def get_response(self, path, scope):
            resp = await super().get_response(path, scope)
            resp.headers["Cache-Control"] = "no-cache, must-revalidate"
            return resp

    app.mount("/static", NoCacheStaticFiles(directory=str(STATIC_DIR)), name="static")

# Роутеры ядра
from app.routers import admin, auth, departments, pages, sharing  # noqa: E402

app.include_router(auth.router)                    # /auth/*
app.include_router(pages.router)                   # /login, /, /api/profile/*
app.include_router(departments.router, prefix="/api/core", tags=["core-departments"])
app.include_router(sharing.router, prefix="/api/core", tags=["core-sharing"])
app.include_router(admin.router, prefix="/api/core", tags=["core-admin"])


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("unhandled error on %s", request.url.path)
    if request.url.path.startswith("/api"):
        return JSONResponse({"detail": "Внутренняя ошибка"}, status_code=500)
    return HTMLResponse("<h3>Ошибка сервера</h3>", status_code=500)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "modules": [s.key for s in registry.all()]}
