"""
Bootstrap/seed базы (идемпотентен): справочники статусов и приоритетов — как defaults()
из прототипа sipm_lite_v3.html; демо-данные только при dev_login_enabled.

Вызывается из lifespan app/main.py после create_all + registry.sync_table.
"""
from __future__ import annotations

import logging
import secrets

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.core import (
    Department, DepartmentMember, EntityKind, Invite, PriorityLevel, Role, StatusSet, User,
)

log = logging.getLogger("caesar.seed")

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
# статусы документооборота (docs из прототипа)
DOC_STATUSES = [
    ("draft", "Проект", "b-gray", 0),
    ("signed", "Подписан", "b-green", 1),
    ("in_work", "В работе", "b-blue", 2),
    ("archived", "В архиве", "b-amber", 3),
    ("canceled", "Аннулирован", "b-red", 4),
]
# статусы проектной работы / ПО (softs из прототипа)
SOFT_STATUSES = [
    ("backlog", "Бэклог", "b-gray", 0),
    ("dev", "Разработка", "b-blue", 1),
    ("test", "Тестирование", "b-amber", 2),
    ("prod", "В эксплуатации", "b-green", 3),
    ("support", "Поддержка", "b-gray", 4),
    ("deprecated", "Устарело", "b-red", 5),
]
# статусы грантов (grants из прототипа)
GRANT_STATUSES = [
    ("prep", "Подготовка", "b-gray", 0),
    ("submitted", "Подана", "b-blue", 1),
    ("won", "Выигран", "b-green", 2),
    ("exec", "Исполняется", "b-amber", 3),
    ("closed", "Закрыт", "b-gray", 4),
    ("rejected", "Отклонена", "b-red", 5),
]
# статусы стипендий (schols из прототипа)
SCHOLAR_STATUSES = [
    ("nomination", "Номинация", "b-gray", 0),
    ("approved", "Одобрена", "b-green", 1),
    ("paid", "Выплачивается", "b-blue", 2),
    ("terminated", "Прекращена", "b-red", 3),
]
# статусы показателей приоритетных проектов (priority из прототипа)
PRIORITY_STATUSES = [
    ("plan", "Плановое", "b-gray", 0),
    ("track", "На контроле", "b-blue", 1),
    ("risk", "Риск отставания", "b-amber", 2),
    ("achieved", "Достигнуто", "b-green", 3),
    ("missed", "Не достигнуто", "b-red", 4),
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
                       (EntityKind.PROJECT, PROJECT_STATUSES), (EntityKind.DOC, DOC_STATUSES),
                       (EntityKind.SOFT, SOFT_STATUSES), (EntityKind.GRANT, GRANT_STATUSES),
                       (EntityKind.SCHOL, SCHOLAR_STATUSES), (EntityKind.PRIORITY, PRIORITY_STATUSES)):
        cnt = await session.scalar(
            select(func.count(StatusSet.id)).where(StatusSet.kind == kind)) or 0
        if not cnt:
            for key, label, color, order in rows:
                session.add(StatusSet(kind=kind, key=key, label=label, color=color, order=order))

    if not await session.scalar(select(func.count(PriorityLevel.id))):
        for key, label, color, weight in PRIORITIES:
            session.add(PriorityLevel(key=key, label=label, color=color, weight=weight))

    # --- первый администратор: bootstrap_admin_ya_ids или invite-коды ---
    # Ключевое свойство: bootstrap должен сработать при ПЕРВОМ входе администратора.
    # Раньше условие опиралось на отсутствие пользователя с ролью ADMIN: если к моменту
    # запуска уже существовал любой админ (демо-сид, созданный ранее), Invite не
    # создавался вовсе — и человек из BOOTSTRAP_ADMIN_YA_IDS входил обычным участником
    # без прав администрирования (нельзя добавлять пользователей и подразделения).
    # Теперь:
    #   * приглашение создаётся для каждого id из списка, пока у него нет учётной записи;
    #   * если учётная запись уже есть (например, создана до того, как в .env появился
    #     его ya_id, или после отката БД) — роль повышается до ADMIN идемпотентно;
    #   * bootstrap-приглашение удаляется из таблицы сразу после использования
    #     (см. app/routers/auth.py), поэтому при случайной очистке cookie админ
    #     получит новый доступ на следующем перезапуске.
    bootstrap_ids = s.bootstrap_ids
    for ya_id in bootstrap_ids:
        existing = await session.scalar(select(User).where(func.lower(User.yandex_id) == ya_id))
        if existing is not None:
            if existing.role != Role.ADMIN:
                existing.role = Role.ADMIN
                log.info("bootstrap: пользователю %s (id=%s) повышена роль до ADMIN",
                         existing.display_name, ya_id)
            continue
        inv = await session.scalar(
            select(Invite).where(Invite.used_at.is_(None), Invite.yandex_id == ya_id)
        )
        if inv is None:
            session.add(Invite(yandex_id=ya_id, display_name="Администратор",
                               role=Role.ADMIN, code=secrets.token_hex(4)))

    # --- демо-данные для локальной разработки (как в прототипе) ---
    # В продакшене запрещены (fail-fast в main.py), но дополнительно защищаемся и здесь:
    # seed включается только явным флагом SEED_DEMO_DATA=true (или dev-режимом).
    if (s.seed_demo_data or s.dev_login_enabled) and not s.is_production:
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
    ("member-hr@caesar.local", "Гаврилов П.С.", Role.MEMBER, "dep-hr"),
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
