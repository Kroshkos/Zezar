"""Манифест модуля-плагина «Совещания» (реестр ядра подхватывает через autodiscover)."""
from app.core.module_registry import ModuleSpec, registry
from modules.meetings.router import router as meetings_router

registry.register(ModuleSpec(
    key="meetings", title="Совещания", icon="🤝", order=40, is_core=True,
    entity_kinds=("MEETING",),
    router=meetings_router,
    template_pages=("meetings.html",),
))
