"""Манифест модуля-плагина «Документооборот» (реестр ядра подхватывает через autodiscover)."""
from app.core.module_registry import ModuleSpec, registry
from modules.docs.router import router as docs_router

registry.register(ModuleSpec(
    key="docs", title="Документы", icon="📄", order=50, is_core=False,
    entity_kinds=("DOC",),
    router=docs_router,
    template_pages=("docs.html",),
))
