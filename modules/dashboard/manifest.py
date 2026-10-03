"""Манифест модуля-плагина «Дашборд»."""
from app.core.module_registry import ModuleSpec, registry
from modules.dashboard.router import router as dash_router

registry.register(ModuleSpec(
    key="dashboard", title="Дашборд", icon="📊", order=10, is_core=True,
    entity_kinds=(),
    router=dash_router,
    template_pages=("dashboard.html",),
))
