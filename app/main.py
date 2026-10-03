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


def _import_all_models() -> None:
    """Ядро + payload-таблицы должны быть импортированы до create_all."""
    import app.models.core            # noqa: F401
    import app.models.module_payloads  # noqa: F401


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Схема БД (SQLite строго в /data — путь из .env). Модели импортируем ДО create_all,
    #    иначе payload-таблицы модулей не попадут в метаданные.
    _import_all_models()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # 2. Динамическая регистрация модулей-плагинов + таблица module_registry + bootstrap
    registry.autodiscover("modules")
    registry.mount(app)
    registry.mount_pages(app)
    from app.db.seed import bootstrap
    async with SessionLocal() as s:
        await registry.sync_table(s)
        await bootstrap(s)

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

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

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
