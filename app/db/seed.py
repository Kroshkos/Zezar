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
from app.core.security import hash_password
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


async def sync_admin_logins(session: AsyncSession) -> None:
    """Идемпотентная синхронизация локальных администраторов из ADMIN_LOGINS.

    Для каждой записи "логин:хеш:ФИО":
      * аккаунта нет -> создаётся User(login, password_hash, role=ADMIN, yandex_id=NULL);
      * аккаунт есть -> хеш и ФИО обновляются (смена пароля = правка env + рестарт),
        роль гарантированно ADMIN.
    Аккаунты, которые были созданы из ADMIN_LOGINS, но исчезли из переменной,
    деактивируются (is_active=False) — пароль «в воздухе» оставаться не должен.
    """
    entries = get_settings().admin_login_entries
    logins = {login for login, _, _ in entries}
    for login, pwhash, name in entries:
        user = await session.scalar(select(User).where(User.login == login))
        if user is None:
            session.add(User(login=login, password_hash=pwhash, display_name=name,
                             role=Role.ADMIN, initials=_initials_of(name), is_active=True))
            log.info("admin-logins: создана учётная запись администратора '%s' (%s)", login, name)
        else:
            changed = []
            if user.password_hash != pwhash:
                user.password_hash = pwhash
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
    # отзыв локальных админов, удалённых из ADMIN_LOGINS (только login-аккаунты!)
    stale = (await session.execute(
        select(User).where(User.login.is_not(None)))).scalars().all()
    for u in stale:
        if u.login not in logins and u.is_active:
            u.is_active = False
            log.warning("admin-logins: аккаунт '%s' удалён из ADMIN_LOGINS — отключён", u.login)
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

    # --- Администраторы по логину/паролю (ADMIN_LOGINS) ---
    # Независимый от Яндекса контур доступа: аккаунты создаются/синхронизируются
    # при каждом старте идемпотентно. Это гарантирует, что админка доступна даже
    # если OAuth-обход не настроен или id в BOOTSTRAP_ADMIN_YA_IDS указан неверно.
    await sync_admin_logins(session)

    # --- legacy: bootstrap через Яндекс-id (BOOTSTRAP_ADMIN_YA_IDS) ---
    # Оставлен как второй способ: id из списка получает ADMIN при OAuth-входе
    # (см. app/routers/auth.py). Bootstrap-invites больше не создаются — вход
    # админа теперь либо по логину/паролю, либо по ya_id из списка напрямую.
    bootstrap_ids = s.bootstrap_ids
    for ya_id in bootstrap_ids:
        existing = await session.scalar(select(User).where(func.lower(User.yandex_id) == ya_id))
        if existing is not None and existing.role != Role.ADMIN:
            existing.role = Role.ADMIN
            log.info("bootstrap: пользователю %s (id=%s) повышена роль до ADMIN",
                     existing.display_name, ya_id)
    # очистка незадействованных bootstrap-invites (созданы старым сидом)
    for inv in (await session.execute(
            select(Invite).where(Invite.used_at.is_(None),
                                 Invite.created_by_id.is_(None),
                                 Invite.email.is_(None)))).scalars():
        await session.delete(inv)

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
