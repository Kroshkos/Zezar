"""
Bootstrap/seed базы (идемпотентен): справочники статусов и приоритетов — как defaults()
из прототипа sipm_lite_v3.html; демо-данные только при dev_login_enabled.

Вызывается из lifespan app/main.py после create_all + registry.sync_table.
"""
from __future__ import annotations

import secrets

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.core import (
    Department, DepartmentMember, EntityKind, Invite, PriorityLevel, Role, StatusSet, User,
)

# statusSets['task'] / prioSet из defaults() прототипа
TASK_STATUSES = [
    ("new", "Новая", "b-gray", 0),
    ("in_work", "В работе", "b-blue", 1),
    ("review", "На проверке", "b-amber", 2),
    ("done", "Исполнена", "b-green", 3),
    ("canceled", "Снята", "b-red", 4),
]
MEETING_STATUSES = [
    ("planned", "Запланировано", "b-blue", 0),
    ("held", "Проведено", "b-green", 1),
    ("postponed", "Перенесено", "b-amber", 2),
    ("canceled", "Отменено", "b-red", 3),
]
# статусы проектной работы — по образцу wfTemplates/statusSets прототипа
PROJECT_STATUSES = [
    ("init", "Инициация", "b-gray", 0),
    ("planning", "Планирование", "b-blue", 1),
    ("exec", "Выполнение", "b-amber", 2),
    ("monitor", "Мониторинг", "b-blue", 3),
    ("done", "Завершён", "b-green", 4),
    ("canceled", "Остановлен", "b-red", 5),
]
PRIORITIES = [
    ("critical", "Критическая", "b-red", 30),
    ("high", "Высокая", "b-amber", 20),
    ("medium", "Средняя", "b-blue", 10),
    ("low", "Низкая", "b-gray", 5),
]


async def bootstrap(session: AsyncSession) -> None:
    s = get_settings()

    # --- справочники (глобальные наборы) ---
    for kind, rows in ((EntityKind.TASK, TASK_STATUSES), (EntityKind.MEETING, MEETING_STATUSES),
                       (EntityKind.PROJECT, PROJECT_STATUSES)):
        cnt = await session.scalar(
            select(func.count(StatusSet.id)).where(StatusSet.kind == kind)) or 0
        if not cnt:
            for key, label, color, order in rows:
                session.add(StatusSet(kind=kind, key=key, label=label, color=color, order=order))

    if not await session.scalar(select(func.count(PriorityLevel.id))):
        for key, label, color, weight in PRIORITIES:
            session.add(PriorityLevel(key=key, label=label, color=color, weight=weight))

    # --- первый администратор: bootstrap_admin_ya_ids или invite-коды ---
    if s.bootstrap_admin_ya_ids and not await session.scalar(
            select(func.count(User.id)).where(User.role == Role.ADMIN)):
        for ya_id in [x.strip() for x in s.bootstrap_admin_ya_ids.split(",") if x.strip()]:
            if not await session.scalar(select(User).where(User.yandex_id == ya_id)):
                session.add(Invite(yandex_id=ya_id, display_name="Администратор",
                                   role=Role.ADMIN, code=secrets.token_hex(4)))

    # --- демо-данные для локальной разработки (как в прототипе) ---
    if s.dev_login_enabled:
        await _demo(session)

    await session.commit()


DEMO_DEPTS = [("dep-strat", "Управление стратегических инициатив"),
              ("dep-it", "Департамент цифрового развития"),
              ("dep-hr", "Управление кадров и организационного развития")]
DEMO_USERS = [  # (email, name, role, dept_code)
    ("admin@caesar.local", "Иванов И.И.", Role.ADMIN, "dep-strat"),
    ("coord@caesar.local", "Петрова А.С.", Role.COORDINATOR, "dep-strat"),
    ("head-it@caesar.local", "Сидоров В.В.", Role.HEAD, "dep-it"),
    ("member-it@caesar.local", "Кузнецов Д.А.", Role.MEMBER, "dep-it"),
    ("member-strat@caesar.local", "Смирнова Е.П.", Role.MEMBER, "dep-strat"),
    ("head-hr@caesar.local", "Морозова Т.И.", Role.HEAD, "dep-hr"),
]


async def _demo(session: AsyncSession) -> None:
    if await session.scalar(select(func.count(Department.id))):
        return  # уже засеяно
    from app.core.module_registry import registry
    from app.models.core import Workspace, WorkspaceModule

    depts = {}
    for code, name in DEMO_DEPTS:
        d = Department(code=code, name=name)
        session.add(d)
        await session.flush()
        ws = Workspace(department_id=d.id)
        session.add(ws)
        await session.flush()
        for spec in registry.all():
            if spec.enabled_by_default:
                session.add(WorkspaceModule(workspace_id=ws.id, module_key=spec.key))
        depts[code] = d

    users = {}
    for email, name, role, dept_code in DEMO_USERS:
        u = User(yandex_id=f"dev:{email}", email=email, display_name=name,
                 initials="".join(w[0] for w in name.split()[:2]),
                 color="#4c7dff", role=role)
        session.add(u)
        await session.flush()
        session.add(DepartmentMember(user_id=u.id, department_id=depts[dept_code].id,
                                     role_in_dept=Role.HEAD if role == Role.HEAD else Role.MEMBER))
        users[email] = u
    await session.flush()
