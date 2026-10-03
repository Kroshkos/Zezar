"""
Реестр модулей-плагинов. Ядро НЕ знает содержимое модулей — только контракт ModuleSpec.

Добавление модуля:
  1) создать пакет modules/<name>/ (router.py, templates/, models.py при необходимости);
  2) объявить ModuleSpec и вызвать registry.register(spec) в modules/__init__.py
     (или дать ядру просканировать пакеты автоматически — см. autodiscover);
  3) ядро само подключит роутер, добавит пункт меню, включит модуль в новые workspace.
"""
from __future__ import annotations

import dataclasses
import importlib
from typing import Callable, Optional

from fastapi import APIRouter, FastAPI


@dataclasses.dataclass
class ModuleSpec:
    key: str                              # 'tasks'
    title: str                            # 'Задачи'
    icon: str = "•"                       # глиф для сайдбара (как в демо)
    order: int = 100
    is_core: bool = False                 # базовые: Дашборд/Задачи/Проектная/Совещания/Админ — нельзя отключить
    entity_kinds: tuple[str, ...] = ()    # какие EntityKind обслуживает (для реестра и интеграций)
    router: Optional[APIRouter] = None    # эндпоинты модуля (prefix='/api/{key}', tag=key)
    # хук жизненного цикла интеграций: собрать данные для календаря/Telegram из payload
    calendar_mapper: Optional[Callable] = None   # (entity, payload) -> EventDraft | None
    template_pages: tuple[str, ...] = ()         # html-страницы модуля ('tasks.html', ...)
    enabled_by_default: bool = True


class ModuleRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ModuleSpec] = {}

    def register(self, spec: ModuleSpec) -> None:
        if spec.key in self._specs:
            raise ValueError(f"Модуль {spec.key!r} уже зарегистрирован")
        self._specs[spec.key] = spec

    def unregister(self, key: str) -> None:
        self._specs.pop(key, None)

    def all(self) -> list[ModuleSpec]:
        return sorted(self._specs.values(), key=lambda s: s.order)

    def get(self, key: str) -> Optional[ModuleSpec]:
        return self._specs.get(key)

    def mount(self, app: FastAPI) -> None:
        """Динамическая регистрация в ядре: REST-роутеры на /api/<key>."""
        for spec in self.all():
            if spec.router is not None:
                app.include_router(spec.router, prefix=f"/api/{spec.key}", tags=[spec.key])

    def mount_pages(self, app: FastAPI) -> None:
        """HTML-страницы модулей — на /<key> (тот же роутер плагина)."""
        for spec in self.all():
            if spec.router is not None:
                app.include_router(spec.router, prefix=f"/{spec.key}",
                                   tags=[f"page-{spec.key}"], include_in_schema=False)

    async def sync_table(self, session) -> None:
        """Синхронизирует реестр с таблицей module_registry (для UI администрирования)."""
        from app.models.core import ModuleRegistry
        existing = {r.key: r for r in (await session.scalars(select(ModuleRegistry))).all()}
        changed = False
        for spec in self.all():
            row = existing.get(spec.key)
            if row is None:
                session.add(ModuleRegistry(key=spec.key, title=spec.title, icon=spec.icon,
                                           order=spec.order, is_core=spec.is_core))
                changed = True
            else:
                row.title, row.icon, row.order, row.is_core = (
                    spec.title, spec.icon, spec.order, spec.is_core)
        if changed:
            await session.commit()

    def autodiscover(self, package: str = "modules") -> None:
        """Импорт modules.<pkg>.manifest, каждый manifest вызывает registry.register."""
        import pkgutil
        mod = importlib.import_module(package)
        for _, name, _ in pkgutil.iter_modules(mod.__path__):
            try:
                importlib.import_module(f"{package}.{name}.manifest")
            except ModuleNotFoundError:
                pass  # пакет без manifest — не модуль


registry = ModuleRegistry()
