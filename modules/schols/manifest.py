"""Манифест модуля-плагина «Стипендии» (schols из прототипа)."""
from app.core.module_registry import ModuleSpec, registry
from modules.schols.router import router as schols_router

registry.register(ModuleSpec(
    key="schols", title="Стипендии", icon="🎓", order=80, is_core=False,
    entity_kinds=("SCHOL",),
    router=schols_router,
    template_pages=("schols.html",),
))
