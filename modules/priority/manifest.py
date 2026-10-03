"""Манифест модуля-плагина «Приоритетные показатели» (priority из прототипа)."""
from app.core.module_registry import ModuleSpec, registry
from modules.priority.router import router as priority_router

registry.register(ModuleSpec(
    key="priority", title="Приоритеты", icon="🎯", order=85, is_core=False,
    entity_kinds=("PRIORITY",),
    router=priority_router,
    template_pages=("priority.html",),
))
