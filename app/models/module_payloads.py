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


class ProjectPayload(TimestampMixin, Base):
    """Модуль 'projects' (+ docs/softs используют похожую структуру этапов/wf)."""
    __tablename__ = "project_payloads"

    entity_id: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), primary_key=True)
    stages: Mapped[list] = mapped_column(JSON, default=list)   # [{name, branches:[...]}] — как в демо
    customer: Mapped[Optional[str]] = mapped_column(String(200))
    budget: Mapped[Optional[str]] = mapped_column(String(64))
    links: Mapped[list] = mapped_column(JSON, default=list)
    # Иерархия проектов: проект может быть частью другого проекта (родитель —
    # uid сущности kind=project; хранится uid, а не FK id — устойчивость при
    # репликации/синхронизации пространств и отсутствие каскадного удаления).
    parent_uid: Mapped[Optional[str]] = mapped_column(String(36), index=True)


class DocPayload(TimestampMixin, Base):
    __tablename__ = "doc_payloads"

    entity_id: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), primary_key=True)
    doc_type: Mapped[Optional[str]] = mapped_column(String(120))      # docTypes из демо
    number: Mapped[Optional[str]] = mapped_column(String(64))
    issued_at: Mapped[Optional[str]] = mapped_column(String(32))
    file_ref: Mapped[Optional[str]] = mapped_column(String(500))
    project_entity_uid: Mapped[Optional[str]] = mapped_column(String(36), index=True)  # привязка к проекту


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
