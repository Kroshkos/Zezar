"""
Bootstrap/seed базы (идемпотентен): справочники статусов и приоритетов — как defaults()
из прототипа sipm_lite_v3.html; демо-данные только в dev-режиме.

Авторизация — только локальная (логин + пароль):
  * Администратор задаётся переменными окружения ADMIN_USERNAME/ADMIN_PASSWORD
    (или резервным ADMIN_LOGINS="логин:хеш[:ФИО];...").
  * Обычные пользователи создаются администратором в разделе
    «Администрирование» с принудительной сменой пароля при первом входе.

Вызывается из lifespan app/main.py после create_all + registry.sync_table.
"""
from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.core import (
    Department, DepartmentMember, EntityKind, PriorityLevel, Role, StatusSet, User,
)

log = logging.getLogger("caesar.seed")


def is_env_admin_login(login: str) -> bool:
    """Принадлежит ли логин к списку администраторов из переменных окружения
    (ADMIN_USERNAME / ADMIN_LOGINS). Используется как источник истины роли ADMIN
    при входе — защита от случайного понижения env-админа через API."""
    if not login:
        return False
    norm = login.strip().lower()
    return any(login_l == norm for login_l, _, _ in get_settings().admin_login_entries)

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


async def sync_admin_logins(session: AsyncSession) -> None:
    """Идемпотентная синхронизация локальных администраторов из env.

    Источник — Settings.admin_login_entries: ADMIN_USERNAME/ADMIN_PASSWORD
    (пароль хешируется системой, pbkdf2_sha256) + резервный ADMIN_LOGINS
    ("логин:хеш[:ФИО];..."). Для каждой записи:
      * аккаунта нет -> создаётся User(login, password_hash, role=ADMIN);
      * аккаунт есть -> хеш и ФИО обновляются (смена пароля = правка env + рестарт),
        роль гарантированно ADMIN.
    ВНИМАНИЕ: отзыв аккаунтов, отсутствующих в env, здесь НЕ выполняется —
    пользователи создаются администратором через админку и живут в БД;
    из env управляются только администраторы.
    """
    entries = get_settings().admin_login_entries
    for login, pwhash, name in entries:
        user = await session.scalar(select(User).where(User.login == login))
        if user is None:
            session.add(User(login=login, password_hash=pwhash, display_name=name,
                             role=Role.ADMIN, initials=_initials_of(name), is_active=True,
                             must_change_password=False))
            log.info("admin-logins: создана учётная запись администратора '%s' (%s)", login, name)
        else:
            changed = []
            if user.password_hash != pwhash:
                user.password_hash = pwhash
                user.must_change_password = False  # env-пароль актуален, смена не требуется
                changed.append("пароль обновлён")
            if user.display_name != name:
                user.display_name = name
                changed.append("имя обновлено")
            if user.role != Role.ADMIN:
                user.role = Role.ADMIN
                changed.append("роль восстановлена до ADMIN")
            if not user.is_active:
                user.is_active = True
                changed.append("аккаунт активирован")
            if changed:
                log.info("admin-logins: администратор '%s': %s", login, ", ".join(changed))
    await session.commit()


def _initials_of(name: str) -> str:
    parts = (name or "").split()
    return "".join(p[0].upper() for p in parts[:2]) if parts else "?"


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

    # --- Администраторы по логину/паролю (ADMIN_USERNAME + ADMIN_PASSWORD) ---
    # Единственный контур входа в систему: аккаунты создаются/синхронизируются
    # при каждом старте идемпотентно, пароль хешируется системой (pbkdf2_sha256).
    await sync_admin_logins(session)

    # --- демо-данные для локальной разработки (как в прототипе) ---
    # В продакшене запрещены (fail-fast в main.py), но дополнительно защищаемся и здесь:
    # seed включается только явным флагом SEED_DEMO_DATA=true (или dev-режимом).
    if (s.seed_demo_data or s.dev_login_enabled) and not s.is_production:
        await _demo(session)

    await session.commit()


DEMO_DEPTS = [("dep-strat", "Управление стратегических инициатив"),
              ("dep-it", "Департамент цифрового развития"),
              ("dep-hr", "Управление кадров и организационного развития")]
DEMO_USERS = [  # (login, email, name, role, dept_code)
    ("ivanov", "admin@caesar.local", "Иванов И.И.", Role.ADMIN, "dep-strat"),
    ("petrova", "coord@caesar.local", "Петрова А.С.", Role.COORDINATOR, "dep-strat"),
    ("sidorov", "head-it@caesar.local", "Сидоров В.В.", Role.HEAD, "dep-it"),
    ("kuznetsov", "member-it@caesar.local", "Кузнецов Д.А.", Role.MEMBER, "dep-it"),
    ("smirnova", "member-strat@caesar.local", "Смирнова Е.П.", Role.MEMBER, "dep-strat"),
    ("morozova", "head-hr@caesar.local", "Морозова Т.И.", Role.HEAD, "dep-hr"),
    ("gavrilov", "member-hr@caesar.local", "Гаврилов П.С.", Role.MEMBER, "dep-hr"),
]


async def _demo(session: AsyncSession) -> None:
    if await session.scalar(select(func.count(Department.id))):
        return  # уже засеяно
    from app.core.module_registry import registry
    from app.core.security import hash_password
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
    # Демо-вход: логин из колонки login, пароль demo12345 (только dev-режим).
    for login, email, name, role, dept_code in DEMO_USERS:
        u = User(login=login, email=email, display_name=name,
                 password_hash=hash_password("demo12345"),
                 must_change_password=False,
                 initials="".join(w[0] for w in name.split()[:2]),
                 color="#4c7dff", role=role)
        session.add(u)
        await session.flush()
        session.add(DepartmentMember(user_id=u.id, department_id=depts[dept_code].id,
                                     role_in_dept=Role.HEAD if role == Role.HEAD else Role.MEMBER))
        users[email] = u
    await session.flush()
