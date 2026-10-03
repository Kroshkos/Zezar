"""Манифест модуля-плагина «Администрирование подразделения»."""
from app.core.module_registry import ModuleSpec, registry
from modules.admin_dept.router import router as admin_dept_router

registry.register(ModuleSpec(
    key="admin_dept", title="Администрирование", icon="⚙️", order=90, is_core=True,
    entity_kinds=(),
    router=admin_dept_router,
    template_pages=("admin_dept.html",),
))
