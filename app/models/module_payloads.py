"""
Payload-модели модулей (специфичные поля сущностей).
Каждая таблица — 1:1 с ядром `entities` (общие поля title/status/due/responsible/shares живут в ядре).
Модуль «Задачи» — пример плагина: добавление нового модуля = новая payload-таблица + router + шаблон.
"""
from __future__ import annotations

from typing import Optional, List

from sqlalchemy import Boolean, ForeignKey, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin


class TaskPayload(TimestampMixin, Base):
    """Модуль 'tasks': поля карточки задачи из прототипа."""
    __tablename__ = "task_payloads"

    entity_id: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), primary_key=True)
    group_key: Mapped[Optional[str]] = mapped_column(String(50))      # taskGroups из демо
    project_entity_uid: Mapped[Optional[str]] = mapped_column(String(36))  # привязка к проекту
    assignee_names: Mapped[list] = mapped_column(JSON, default=list)  # историческое поле демо (ФИО)
    checklist: Mapped[list] = mapped_column(JSON, default=list)       # [{text, done}]
    wf_kind: Mapped[Optional[str]] = mapped_column(String(50))        # тип согласования (demo: t.wf)
    result_of: Mapped[Optional[str]] = mapped_column(Text)            # результат/комментарий
    cross_dept: Mapped[bool] = mapped_column(Boolean, default=False)  # признак межпространственного шеринга


class MeetingPayload(TimestampMixin, Base):
    """Модуль 'meetings': повестка и решения (m.agenda/m.decisions из демо)."""
    __tablename__ = "meeting_payloads"

    entity_id: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), primary_key=True)
    num: Mapped[Optional[str]] = mapped_column(String(64))            # номер протокола (m.num из демо)
    chair: Mapped[Optional[str]] = mapped_column(String(200))         # председатель
    secretary: Mapped[Optional[str]] = mapped_column(String(200))     # секретарь
    attendees: Mapped[Optional[str]] = mapped_column(Text)            # список участников (текст, как в демо)
    time_: Mapped[Optional[str]] = mapped_column("time", String(16))  # время начала 'HH:MM'
    agenda: Mapped[list] = mapped_column(JSON, default=list)          # [{q, who, minutes}]
    decisions: Mapped[list] = mapped_column(JSON, default=list)       # [{text, who, due, taskId}]
    linked_task_uids: Mapped[list] = mapped_column(JSON, default=list)
    project_entity_uid: Mapped[Optional[str]] = mapped_column(String(36), index=True)  # привязка к проекту (связанность категорий)
    location: Mapped[Optional[str]] = mapped_column(String(200))
    start_at: Mapped[Optional[str]] = mapped_column(String(32))       # ISO datetime для календаря
    end_at: Mapped[Optional[str]] = mapped_column(String(32))
    # ---- Раздел «Карточки» (по образцу модуля «Совещания» прототипа
    # ИС_Цезарь.html): карточки задач, заведённых на совещании.
    # Структура записи — как new_tasks в mBlank()/mRenderNewTaskList():
    # {uid, title, tag, priority, due, assignee_id, assignee_name, status_key}
    cards: Mapped[list] = mapped_column(JSON, default=list)


class ProjectPayload(TimestampMixin, Base):
    """Модуль 'projects' (+ docs/softs используют похожую структуру этапов/wf)."""
    __tablename__ = "project_payloads"

    entity_id: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), primary_key=True)
    # Маршрут (workflow): этапы [{name,state,date,deadline,comment,taskId,branches:[...]}]
    # — автозадачи создаются на каждый этап; ответвления и условия — на холсте маршрута
    # (по образу wfCanvas из sipm_lite_v3.html).
    stages: Mapped[list] = mapped_column(JSON, default=list)
    customer: Mapped[Optional[str]] = mapped_column(String(200))
    # бюджет/источник финансирования удалён из формы проекта по ТЗ (поле остаётся
    # в БД для исторических данных, но не заполняется и не отображается)
    budget: Mapped[Optional[str]] = mapped_column(String(64))
    links: Mapped[list] = mapped_column(JSON, default=list)
    # Иерархия проектов: проект может быть частью другого проекта (родитель —
    # uid сущности kind=project; хранится uid, а не FK id — устойчивость при
    # репликации/синхронизации пространств и отсутствие каскадного удаления).
    parent_uid: Mapped[Optional[str]] = mapped_column(String(36), index=True)
    code: Mapped[Optional[str]] = mapped_column(String(64))               # код проекта (П-01)
    leader_user_id: Mapped[Optional[int]] = mapped_column(                # руководитель проекта
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    start_date: Mapped[Optional[str]] = mapped_column(String(32))         # дата начала (ISO)
    end_date: Mapped[Optional[str]] = mapped_column(String(32))           # дата окончания (ISO)
    grant_entity_uid: Mapped[Optional[str]] = mapped_column(String(36), index=True)   # связанный грант
    priority_indicator_uid: Mapped[Optional[str]] = mapped_column(String(36), index=True)  # показатель Приоритет-2030


class DocPayload(TimestampMixin, Base):
    __tablename__ = "doc_payloads"

    entity_id: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), primary_key=True)
    doc_type: Mapped[Optional[str]] = mapped_column(String(120))      # docTypes из демо
    number: Mapped[Optional[str]] = mapped_column(String(64))
    issued_at: Mapped[Optional[str]] = mapped_column(String(32))
    file_ref: Mapped[Optional[str]] = mapped_column(String(500))
    project_entity_uid: Mapped[Optional[str]] = mapped_column(String(36), index=True)  # привязка к проекту
    # Маршрут (workflow) документа — та же структура этапов, что у проектов/ПО:
    # [{id,name,state,date,deadline,comment,taskId,branches,x,y,links_out}]
    stages: Mapped[list] = mapped_column(JSON, default=list)


class SoftPayload(TimestampMixin, Base):
    """Проектная работа / ПО (softTypes из демо)."""
    __tablename__ = "soft_payloads"

    entity_id: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), primary_key=True)
    soft_type: Mapped[Optional[str]] = mapped_column(String(120))
    repo_url: Mapped[Optional[str]] = mapped_column(String(500))
    env: Mapped[Optional[str]] = mapped_column(String(120))
    stages: Mapped[list] = mapped_column(JSON, default=list)
    project_entity_uid: Mapped[Optional[str]] = mapped_column(String(36), index=True)  # привязка к проекту


class GrantPayload(TimestampMixin, Base):
    __tablename__ = "grant_payloads"

    entity_id: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), primary_key=True)
    program: Mapped[Optional[str]] = mapped_column(String(200))
    amount: Mapped[Optional[str]] = mapped_column(String(64))
    submitted_at: Mapped[Optional[str]] = mapped_column(String(32))
    project_entity_uid: Mapped[Optional[str]] = mapped_column(String(36), index=True)  # привязка к проекту


class ScholarPayload(TimestampMixin, Base):
    __tablename__ = "scholar_payloads"

    entity_id: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), primary_key=True)
    person_name: Mapped[Optional[str]] = mapped_column(String(200))
    schol_type: Mapped[Optional[str]] = mapped_column(String(120))    # scholTypes из демо
    amount: Mapped[Optional[str]] = mapped_column(String(64))
    project_entity_uid: Mapped[Optional[str]] = mapped_column(String(36), index=True)  # привязка к проекту


class PriorityPayload(TimestampMixin, Base):
    """Показатели приоритетных проектов (prioSet/pc из демо)."""
    __tablename__ = "priority_payloads"

    entity_id: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), primary_key=True)
    direction: Mapped[Optional[str]] = mapped_column(String(200))
    metric: Mapped[Optional[str]] = mapped_column(String(200))
    target_value: Mapped[Optional[str]] = mapped_column(String(64))
    current_value: Mapped[Optional[str]] = mapped_column(String(64))
    project_entity_uid: Mapped[Optional[str]] = mapped_column(String(36), index=True)  # привязка к проекту
