"""Манифест модуля-плагина «Проектная работа» (реестр ядра подхватывает через autodiscover)."""
from app.core.module_registry import ModuleSpec, registry
from modules.projects.router import router as projects_router

registry.register(ModuleSpec(
    key="projects", title="Проектная работа", icon="📊", order=30, is_core=True,
    entity_kinds=("PROJECT",),
    router=projects_router,
    template_pages=("projects.html",),
))
