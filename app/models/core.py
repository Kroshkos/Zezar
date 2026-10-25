"""
Модели ядра: Пользователи, Подразделения (рабочие пространства), Модули,
Реестр сущностей, Ответственные, Шеринг, Роли/Права, Справочники статусов и workflow.

Ключевые решения:
- Таблица `entities` — полиморфный реестр ВСЕХ бизнес-сущностей всех модулей
  (tasks/docs/softs/projects/grants/schols/priority/meetings из прототипа).
  Это даёт: единый шеринг, единых ответственных, единую интеграцию с календарём
  и Telegram без переписывания ядра при добавлении нового модуля-плагина.
- Каждый модуль хранит свои специфичные поля в собственной таблице (`*_payload`)
  со связью 1:1 с `entities`.
- Права: role (глобальная) + department_members.role_in_dept (в пространстве)
  + EntityShare.permission (на уровне конкретной сущности).
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional, List

from sqlalchemy import (
    Boolean, DateTime, Enum, ForeignKey, Integer, JSON, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, utcnow


# ======================= Перечисления =======================

class Role(str, enum.Enum):
    ADMIN = "admin"            # Администратор (входит в группу «Координаторы»)
    COORDINATOR = "coordinator"  # Координатор: все пространства
    HEAD = "head"              # Руководитель подразделения
    MEMBER = "member"          # Участник


class Permission(str, enum.Enum):
    READ = "read"              # Только чтение
    EDIT = "edit"              # Редактирование
    FULL = "full"              # Полный доступ (в т.ч. перешеринг, назначение ответственных)


class EntityKind(str, enum.Enum):
    """Зарегистрированные виды сущностей. Новый модуль добавляет сюда значение
    и регистрируется в реестре модулей — ядро не меняется."""
    TASK = "task"
    DOC = "doc"
    SOFT = "soft"              # Проектная работа / ПО
    PROJECT = "project"
    GRANT = "grant"
    SCHOL = "scholarship"
    PRIORITY = "priority"
    MEETING = "meeting"


class SyncState(str, enum.Enum):
    NONE = "none"
    PENDING = "pending"        # сущность изменилась, нужна синхронизация
    SYNCED = "synced"
    ERROR = "error"
    DELETED = "deleted"        # событие удалено из календарей (после calendar.remove)


# ======================= Пользователи =======================

class User(TimestampMixin, Base):
    """Учётная запись. Авторизация — только локальная: логин + пароль.

    - Администратор задаётся переменными окружения (ADMIN_USERNAME/ADMIN_PASSWORD)
      и синхронизируется сидом при старте (app/db/seed.sync_admin_logins).
    - Обычные пользователи создаются администратором в разделе
      «Администрирование» (Фамилия Имя, логин, начальный пароль) и при первом
      входе обязаны сменить пароль (must_change_password).
    """
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[Optional[str]] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(160))   # «Фамилия Имя»
    # Логин аккаунта Яндекса (наследие прежней интеграции) — CalDAV Basic auth
    # требует его, а не email. Никчемные значения ('local:*', 'dev:*') очищены
    # миграцией; поле заполняется вручную только при привязке Яндекс.Календаря.
    yandex_login: Mapped[Optional[str]] = mapped_column(String(255), index=True)
    # Локальный логин учётной записи (уникальный). Единственный контур входа.
    login: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)  # pbkdf2_sha256$итерации$salt$hash
    # Принудительная смена пароля при первом входе (начальный пароль выдал админ).
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    initials: Mapped[Optional[str]] = mapped_column(String(8))
    color: Mapped[Optional[str]] = mapped_column(String(16))  # цвет аватара как в демо
    role: Mapped[Role] = mapped_column(Enum(Role), default=Role.MEMBER)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_confirm: Mapped[bool] = mapped_column(Boolean, default=False)  # «подтверждение требуется» из демо

    # Telegram-привязка
    telegram_chat_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    telegram_notify: Mapped[bool] = mapped_column(Boolean, default=True)
    telegram_bind_code: Mapped[Optional[str]] = mapped_column(String(16), unique=True)  # /start <код>
    telegram_optout: Mapped[Optional[str]] = mapped_column(String(20))  # NULL|'all' (/mute)

    # Яндекс.Календарь (app-пароль пользователя для WebDAV; шифруется на диске)
    yandex_cal_app_password_enc: Mapped[Optional[str]] = mapped_column(Text)
    yandex_cal_name: Mapped[str] = mapped_column(String(120), default="default")

    memberships: Mapped[List["DepartmentMember"]] = relationship(
        back_populates="user", cascade="all, delete-orphan")
    owned_entities: Mapped[List["EntityResponsible"]] = relationship(back_populates="user")


# ======================= Подразделения / Рабочие пространства =======================

class Department(TimestampMixin, Base):
    """Подразделение = изолированное рабочее пространство."""
    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)   # короткий ключ, как group-key в демо
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[Optional[str]] = mapped_column(Text)
    color: Mapped[Optional[str]] = mapped_column(String(16))     # HEX-цвет подразделения (печать карточек, задаёт админ)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    members: Mapped[List["DepartmentMember"]] = relationship(
        back_populates="department", cascade="all, delete-orphan")
    workspace: Mapped["Workspace"] = relationship(back_populates="department", uselist=False)


class Workspace(TimestampMixin, Base):
    """Рабочее пространство подразделения: набор включённых модулей + настройки."""
    __tablename__ = "workspaces"

    id: Mapped[int] = mapped_column(primary_key=True)
    department_id: Mapped[int] = mapped_column(
        ForeignKey("departments.id", ondelete="CASCADE"), unique=True)
    settings_json: Mapped[dict] = mapped_column(JSON, default=dict)  # темы/виджеты дашборда из демо

    department: Mapped[Department] = relationship(back_populates="workspace")
    modules: Mapped[List["WorkspaceModule"]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan")


class ModuleRegistry(Base):
    """Справочник плагинов-модулей, зарегистрированных ядром при старте."""
    __tablename__ = "module_registry"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(50), unique=True)     # 'dashboard','tasks',...
    title: Mapped[str] = mapped_column(String(120))
    icon: Mapped[str] = mapped_column(String(16), default="•")
    order: Mapped[int] = mapped_column(Integer, default=100)
    is_core: Mapped[bool] = mapped_column(Boolean, default=False)  # базовые нельзя отключить


class WorkspaceModule(TimestampMixin, Base):
    """Включение модуля в конкретное рабочее пространство (может быть отключено)."""
    __tablename__ = "workspace_modules"
    __table_args__ = (UniqueConstraint("workspace_id", "module_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    module_key: Mapped[str] = mapped_column(String(50))
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    config_json: Mapped[dict] = mapped_column(JSON, default=dict)

    workspace: Mapped[Workspace] = relationship(back_populates="modules")


class DepartmentMember(TimestampMixin, Base):
    """Членство пользователя в рабочем пространстве + роль внутри него."""
    __tablename__ = "department_members"
    __table_args__ = (UniqueConstraint("user_id", "department_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    department_id: Mapped[int] = mapped_column(
        ForeignKey("departments.id", ondelete="CASCADE"), index=True)
    role_in_dept: Mapped[Role] = mapped_column(Enum(Role), default=Role.MEMBER)

    user: Mapped[User] = relationship(back_populates="memberships")
    department: Mapped[Department] = relationship(back_populates="members")


# ======================= Реестр сущностей (ядро шеринга) =======================

class Entity(TimestampMixin, Base):
    """Полиморфная запись любой бизнес-сущности любого модуля.

    Собственник — подразделение (изоляция). Специфичные поля — в payload-таблице
    модуля, ссылка через entity_id. kind+payload_id позволяют загрузить данные модуля.
    """
    __tablename__ = "entities"

    id: Mapped[int] = mapped_column(primary_key=True)
    uid: Mapped[str] = mapped_column(String(36), unique=True, index=True)  # id из демо (localStorage)
    kind: Mapped[EntityKind] = mapped_column(Enum(EntityKind), index=True)
    module_key: Mapped[str] = mapped_column(String(50), index=True)
    owner_department_id: Mapped[int] = mapped_column(
        ForeignKey("departments.id", ondelete="CASCADE"), index=True)

    # Универсальные поля, общие для всех карточек прототипа
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[Optional[str]] = mapped_column(Text)
    status_key: Mapped[Optional[str]] = mapped_column(String(50))       # ссылка на StatusSet.key
    priority_key: Mapped[Optional[str]] = mapped_column(String(50))
    due_date: Mapped[Optional[str]] = mapped_column(String(32))         # ISO-дата (как в демо)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    extra_json: Mapped[dict] = mapped_column(JSON, default=dict)        # «остальное» без миграций

    # --- Синхронизация с Яндекс.Календарём ---
    sync_state: Mapped[SyncState] = mapped_column(
        Enum(SyncState), default=SyncState.NONE, index=True)
    cal_uid: Mapped[Optional[str]] = mapped_column(String(120))         # UID события в DAV
    cal_synced_at: Mapped[Optional[DateTime]] = mapped_column(DateTime)
    cal_error: Mapped[Optional[str]] = mapped_column(Text)

    owner_department: Mapped[Department] = relationship()
    responsibles: Mapped[List["EntityResponsible"]] = relationship(
        back_populates="entity", cascade="all, delete-orphan", lazy="selectin")
    # ВАЖНО: НЕ добавлять cascade="delete-orphan" к shares. Pending-объекты
    # EntityShare, созданные через session.add() до flush (share_with при
    # создании сущности), не имеют identity и orphan-детектор SQLAlchemy
    # помечал их к удалению — шеринг терялся, инвариант ответственных
    # нарушался, признак cross_dept в списках был всегда False.
    # viewonly=True: ORM-механику шерингов НЕ трогаем (pending-объекты до flush
    # не имеют identity, и любой sync-процесс коллекции их откатывал — шеринг
    # терялся). read-only коллекция для selectinload; запись — только через
    # session.add(EntityShare(...)) + явные SELECT-запросы.
    shares: Mapped[List["EntityShare"]] = relationship(
        lazy="selectin", foreign_keys="EntityShare.entity_id", viewonly=True)


class CardPrintLog(Base):
    """Журнал печати печатных карточек (раздел «Карточки» модуля «Совещания»).

    Одна строка на факт печати карточки конкретным пользователем. Используется
    для фильтра «Ещё не печатались» и не меняет updated_at сущности (иначе
    печать сбрасывала бы фильтр «изменения за день/неделю»).
    """
    __tablename__ = "card_print_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id", ondelete="CASCADE"),
                                           index=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    printed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class EntityResponsible(TimestampMixin, Base):
    """Ответственный за сущность. Инвариант: по одному от каждого участвующего
    подразделения (своё + все, кому расшарено)."""
    __tablename__ = "entity_responsibles"
    __table_args__ = (UniqueConstraint("entity_id", "department_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id", ondelete="CASCADE"), index=True)
    # NULL — слот создан при шеринге, ответственный ещё не назначен (назначает head подразделения)
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=True)
    department_id: Mapped[int] = mapped_column(
        ForeignKey("departments.id", ondelete="CASCADE"), index=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)  # основной (из подразделения-владельца)

    entity: Mapped[Entity] = relationship(back_populates="responsibles")
    user: Mapped[User] = relationship(back_populates="owned_entities")
    department: Mapped[Department] = relationship()


class EntityShare(TimestampMixin, Base):
    """Шеринг конкретной сущности другому подразделению с уровнем прав."""
    __tablename__ = "entity_shares"
    __table_args__ = (UniqueConstraint("entity_id", "target_department_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id", ondelete="CASCADE"), index=True)
    granted_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    target_department_id: Mapped[int] = mapped_column(
        ForeignKey("departments.id", ondelete="CASCADE"), index=True)
    permission: Mapped[Permission] = mapped_column(Enum(Permission), default=Permission.READ)

    entity: Mapped[Entity] = relationship(foreign_keys=[entity_id], lazy="raise")
    target_department: Mapped[Department] = relationship(foreign_keys=[target_department_id])


class EntityLink(TimestampMixin, Base):
    """Произвольная двусторонняя связь между двумя сущностями любого вида.

    Единый механизм «связать что угодно с чем угодно» (задача ↔ этап проекта,
    документ ↔ проект, совещание ↔ грант и т.п.). Одна запись = одна связь;
    направление не имеет значения — при создании нормализуем пару
    (min_id, max_id), поэтому дубликаты невозможны (UniqueConstraint).

    stage_index — необязательный указатель на этап маршрута (индекс в
    payload.stages той сущности, у которой есть маршрут: project/soft/doc),
    если связывают с конкретным этапом.
    """
    __tablename__ = "entity_links"
    __table_args__ = (UniqueConstraint("entity_a_id", "entity_b_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_a_id: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), index=True)
    entity_b_id: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), index=True)
    link_type: Mapped[str] = mapped_column(String(50), default="related")
    note: Mapped[Optional[str]] = mapped_column(String(500))
    stage_index: Mapped[Optional[int]] = mapped_column(Integer)
    created_by_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))


# ======================= Справочники (как statusSets/prioSet в демо) =======================

class StatusSet(TimestampMixin, Base):
    """Наборы статусов по видам сущностей; глобальные (department_id=NULL —
    шаблоны администратора) либо собственные справочники подразделения."""
    __tablename__ = "status_sets"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[EntityKind] = mapped_column(Enum(EntityKind))
    key: Mapped[str] = mapped_column(String(50))
    label: Mapped[str] = mapped_column(String(120))
    color: Mapped[str] = mapped_column(String(20), default="b-gray")   # класс badge из CSS демо
    order: Mapped[int] = mapped_column(Integer, default=0)
    department_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("departments.id", ondelete="CASCADE"), nullable=True)
    # Шаблон создан администратором и активирован руководителем в этом
    # подразделении (копия глобального шаблона). Деактивация копией НЕ удаляется,
    # а снимается is_active — при повторной активации восстанавливается та же
    # запись (преемственность: исторические status_key сущностей не «открепляются»).
    source_global_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("status_sets.id", ondelete="SET NULL"), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (UniqueConstraint("kind", "key", "department_id"),)


class PriorityLevel(TimestampMixin, Base):
    """Приоритеты задач. department_id=NULL — глобальный шаблон администратора;
    заполнен — собственный приоритет подразделения (или активированная копия
    глобального шаблона, см. source_global_id)."""
    __tablename__ = "priority_levels"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(50))
    label: Mapped[str] = mapped_column(String(120))
    color: Mapped[str] = mapped_column(String(20), default="b-blue")
    weight: Mapped[int] = mapped_column(Integer, default=0)
    department_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("departments.id", ondelete="CASCADE"), nullable=True)
    source_global_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("priority_levels.id", ondelete="SET NULL"), nullable=True)
    # См. StatusSet.is_active: деактивация копии шаблона = снять флаг, не удалить.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Скоуп-уникальность (key, department_id): в SQLite табличный UNIQUE с
    # NULL не защищает от дублей глобальных шаблонов (NULL != NULL) — дубли
    # global-шаблонов контролирует сервис app/core/refdata.py; для записей
    # подразделений работает частичный уникальный индекс
    # uq_priority_levels_scope_dept (WHERE department_id IS NOT NULL),
    # создаваемый ревизией a1b7c3d94e51.
    __table_args__ = ()


class RefType(TimestampMixin, Base):
    """Справочник типов: документы (doc_types), ПО (soft_types), стипендии
    (schol_types) — типы документов/ПО/стипендий из раздела администрирования
    прототипа sipm_lite_v3.html. department_id=NULL — шаблон администратора,
    иначе — тип подразделения (в т.ч. активированная копия глобального)."""
    __tablename__ = "ref_types"

    KIND_DOC = "doc_type"
    KIND_SOFT = "soft_type"
    KIND_SCHOL = "schol_type"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(30))            # doc_type | soft_type | schol_type
    label: Mapped[str] = mapped_column(String(200))
    department_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("departments.id", ondelete="CASCADE"), nullable=True)
    source_global_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("ref_types.id", ondelete="SET NULL"), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    __table_args__ = (UniqueConstraint("kind", "label", "department_id"),)


class TaskGroup(TimestampMixin, Base):
    """Группы задач для совещаний (taskGroups из демо): критерии отбора
    [{projects:[uid..], statuses:[key..], priorities:[key..], dues:[..]}]."""
    __tablename__ = "task_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    criteria: Mapped[dict] = mapped_column(JSON, default=dict)
    department_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("departments.id", ondelete="CASCADE"), nullable=True)
    source_global_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("task_groups.id", ondelete="SET NULL"), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    __table_args__ = (UniqueConstraint("name", "department_id"),)


class WfTemplate(TimestampMixin, Base):
    """Шаблоны маршрутов (wfTemplates из демо). department_id=NULL — шаблон
    администратора (применяется ко всем подразделениям согласно подключённым
    модулям); заполнен — собственный шаблон руководителя подразделения либо
    активированная копия глобального (source_global_id, деактивация = снятие
    is_active / удаление копии)."""
    __tablename__ = "wf_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    steps: Mapped[list] = mapped_column(JSON, default=list)             # ['Подготовка', ...]
    kind_scope: Mapped[Optional[str]] = mapped_column(String(50))       # для каких EntityKind
    department_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("departments.id", ondelete="CASCADE"), nullable=True)
    source_global_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("wf_templates.id", ondelete="SET NULL"), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class EntityWfState(Base):
    """Текущее состояние процесса согласования конкретной сущности."""
    __tablename__ = "entity_wf_states"

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_id: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), unique=True)
    template_id: Mapped[Optional[int]] = mapped_column(ForeignKey("wf_templates.id"))
    current_step: Mapped[int] = mapped_column(Integer, default=0)
    history: Mapped[list] = mapped_column(JSON, default=list)  # [{step, who, at, comment}]


# ======================= Журнал событий / аудит / очередь уведомлений =======================

class EventLog(Base):
    """Журнал событий системы: регистрируется всё, что происходит — вход/выход,
    смена пароля, действия администратора с пользователями и подразделениями,
    CRUD бизнес-сущностей модулей (пишется через app.core.events.log_event).

    user_id/name фиксируются денормализованно — запись остаётся читаемой даже
    после удаления учётной записи. level: info | warning | error."""
    __tablename__ = "event_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"),
                                                   nullable=True, index=True)
    user_name: Mapped[Optional[str]] = mapped_column(String(160))
    action: Mapped[str] = mapped_column(String(60), index=True)   # login/logout/create_user/...
    category: Mapped[str] = mapped_column(String(30), default="system", index=True)  # auth/admin/entity/dept/system
    object: Mapped[Optional[str]] = mapped_column(String(200))    # над чем: логин, название, uid сущности
    ip: Mapped[Optional[str]] = mapped_column(String(64))
    level: Mapped[str] = mapped_column(String(10), default="info")
    message: Mapped[Optional[str]] = mapped_column(Text)
    details_json: Mapped[dict] = mapped_column(JSON, default=dict)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(50))          # create/update/status/reassign/share
    entity_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("entities.id", ondelete="SET NULL"), index=True)
    details_json: Mapped[dict] = mapped_column(JSON, default=dict)


class AclState(Base):
    """Ключ-значение для состояния ядра RBAC (см. app/core/permcache.py).

    Единственный используемый ключ пока — ``acl_epoch``: монотонный счётчик,
    инкрементируемый при ЛЮБОМ изменении ролей/членств/шэров. Нужен, чтобы
    клиентские токены и DOM-рендер принудительно перечитывались после смены
    прав — без перезагрузки страницы пользователем."""
    __tablename__ = "acl_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class OutboxEvent(Base):
    """Транзакционная запись событий: фоновые воркеры (Telegram/Календарь)
    читают очередь и помечают статус — гарантии доставки без внешних брокеров."""
    __tablename__ = "outbox_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    topic: Mapped[str] = mapped_column(String(50), index=True)   # 'telegram.notify' | 'calendar.sync'
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)  # pending/done/error
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)


# ================= Сценарии уведомлений и рассылки (Telegram) =================

class NotifyScenario(Base):
    """Сценарий уведомления в Telegram.

    - Базовые (is_base=True) создаёт/редактирует/удаляет ТОЛЬКО администратор;
      они применимы ко всем пользователям и НЕ могут быть отключены пользователем
      (флаг включён принудительно).
    - Пользовательские сценарии создаёт пользователь в окне «Интеграции»;
      видимость: private (только автор), department (подразделение), global (все).
    - event_key — тип события ядра: assigned | executor_added | status_changed |
      due_soon | overdue | created | comment | custom.
    - entity_kinds — список видов сущностей (EntityKind), к которым применяется
      ('' пустой = все); module_keys — то же для модулей.
    - template — текст уведомления; плейсхолдеры: {title} {kind} {module}
      {action} {changed} {due_date} {actor} {link}.
    - department_id/source_global_id/is_active — та же модель преемственности
      шаблонов, что и в справочниках refdata.
    """
    __tablename__ = "notify_scenarios"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[Optional[str]] = mapped_column(Text)
    event_key: Mapped[str] = mapped_column(String(50), index=True)
    entity_kinds: Mapped[list] = mapped_column(JSON, default=list)
    module_keys: Mapped[list] = mapped_column(JSON, default=list)
    template: Mapped[str] = mapped_column(Text, default="")
    is_base: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_by: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    visibility: Mapped[str] = mapped_column(String(20), default="private")  # private|department|global
    department_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("departments.id", ondelete="CASCADE"), nullable=True, index=True)
    source_global_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("notify_scenarios.id", ondelete="SET NULL"), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class UserNotifyPref(Base):
    """Настройка пользователя по сценарию: включено/выключено.

    Для базовых (неотключаемых) сценариев запись не создаётся — эффективное
    значение всегда True (сервер защищает от выключения).
    """
    __tablename__ = "user_notify_prefs"
    __table_args__ = (UniqueConstraint("user_id", "scenario_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True)
    scenario_id: Mapped[int] = mapped_column(
        ForeignKey("notify_scenarios.id", ondelete="CASCADE"), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class NotifyTemplate(Base):
    """Шаблон ручного уведомления (администрирование). Плейсхолдеры: {name} {login}."""
    __tablename__ = "notify_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Notification(Base):
    """Уведомление ручной рассылки: черновик с датой-временем отправки или факт."""
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text, default="")
    send_at: Mapped[datetime] = mapped_column(DateTime, index=True)   # naive UTC
    audience_json: Mapped[dict] = mapped_column(JSON, default=dict)
    # {"mode":"all"} | {"mode":"users","user_ids":[..]} |
    # {"mode":"department","department_id":N,"include_head":true} |
    # {"mode":"heads"}
    status: Mapped[str] = mapped_column(String(20), default="scheduled", index=True)
    # scheduled | sent | canceled
    created_by: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    recipients_count: Mapped[int] = mapped_column(Integer, default=0)
    delivered_count: Mapped[int] = mapped_column(Integer, default=0)
    # Вложения рассылки: [{"file_id": "...", "type": "photo|video|document",
    # "caption": "..."}] — file_id загружен ботом через uploadMediaFile
    # (медиа, присланное администратором в чат бота).
    media_json: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class TgAdminLog(Base):
    """Журнал действий администратора через Telegram-бота (команды/рассылки)."""
    __tablename__ = "tg_admin_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True)
    action: Mapped[str] = mapped_column(String(50))     # broadcast | cancel | report ...
    details: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ======================= API Tokens (мобильное приложение) =======================

class ApiToken(TimestampMixin, Base):
    """API-токен для мобильного приложения Zezar-app.

    Токены хранятся в хешированном виде (SHA-256). При создании токена
    его значение возвращается ОДИН РАЗ — далее в ответе не попадает.
    """
    __tablename__ = "api_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # SHA-256 хеш токена (хранится на диске); сам токен — нет
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))  # метка для пользователя (напр. "iPhone")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
