"""Манифест модуля-плагина «Задачи» (реестр ядра подхватывает его через autodiscover)."""
from app.core.module_registry import ModuleSpec, registry
from modules.tasks.router import router as tasks_router

registry.register(ModuleSpec(
    key="tasks", title="Задачи", icon="✅", order=20, is_core=True,
    entity_kinds=("TASK",),
    router=tasks_router,
    template_pages=("tasks.html",),
))
