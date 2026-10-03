"""Манифест модуля-плагина «Гранты» (grants из прототипа)."""
from app.core.module_registry import ModuleSpec, registry
from modules.grants.router import router as grants_router

registry.register(ModuleSpec(
    key="grants", title="Гранты", icon="💰", order=70, is_core=False,
    entity_kinds=("GRANT",),
    router=grants_router,
    template_pages=("grants.html",),
))
