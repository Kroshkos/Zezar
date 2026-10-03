"""Манифест модуля-плагина «Программное обеспечение» (softs из прототипа)."""
from app.core.module_registry import ModuleSpec, registry
from modules.softs.router import router as softs_router

registry.register(ModuleSpec(
    key="softs", title="ПО и разработки", icon="💻", order=60, is_core=False,
    entity_kinds=("SOFT",),
    router=softs_router,
    template_pages=("softs.html",),
))
