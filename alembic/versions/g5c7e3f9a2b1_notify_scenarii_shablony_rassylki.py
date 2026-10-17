"""Уведомления: сценарии, пользовательские настройки, шаблоны рассылок, рассылки

Новые таблицы (см. app/models/core.py):
  - notify_scenarios   — базовые (неотключаемые) и пользовательские сценарии
                         уведомлений; преемственность шаблонов через
                         department_id / source_global_id / is_active;
  - user_notify_prefs  — вкл/выкл сценария конкретным пользователем
                         (для базовых записей нет — они всегда включены);
  - notify_templates   — шаблоны ручных уведомлений администратора;
  - notifications      — сама рассылка (черновик/запланировано/отправлено).

Миграция идемпотентна: на чистой установке таблицы уже созданы create_all,
поэтому существующие таблицы просто пропускаются.

Revision ID: g5c7e3f9a2b1
Revises: f4b6d2a8c107
Create Date: 2026-10-17 23:40:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'g5c7e3f9a2b1'
down_revision: Union[str, None] = 'f4b6d2a8c107'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _insp():
    return sa.inspect(op.get_bind())


def upgrade() -> None:
    insp = _insp()
    tables = set(insp.get_table_names())

    if "notify_scenarios" not in tables:
        op.create_table(
            "notify_scenarios",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(length=200), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("event_key", sa.String(length=50), nullable=False),
            sa.Column("entity_kinds", sa.JSON(), nullable=False),
            sa.Column("module_keys", sa.JSON(), nullable=False),
            sa.Column("template", sa.Text(), nullable=False),
            sa.Column("is_base", sa.Boolean(), nullable=False),
            sa.Column("created_by", sa.Integer(), nullable=True),
            sa.Column("visibility", sa.String(length=20), nullable=False),
            sa.Column("department_id", sa.Integer(), nullable=True),
            sa.Column("source_global_id", sa.Integer(), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["department_id"], ["departments.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["source_global_id"], ["notify_scenarios.id"], ondelete="SET NULL"),
        )
        op.create_index("ix_notify_scenarios_event_key", "notify_scenarios", ["event_key"])
        op.create_index("ix_notify_scenarios_is_base", "notify_scenarios", ["is_base"])
        op.create_index("ix_notify_scenarios_department_id", "notify_scenarios", ["department_id"])
        op.create_index("ix_notify_scenarios_is_active", "notify_scenarios", ["is_active"])

    if "user_notify_prefs" not in tables:
        op.create_table(
            "user_notify_prefs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("scenario_id", sa.Integer(), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("user_id", "scenario_id"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["scenario_id"], ["notify_scenarios.id"], ondelete="CASCADE"),
        )
        op.create_index("ix_user_notify_prefs_user_id", "user_notify_prefs", ["user_id"])
        op.create_index("ix_user_notify_prefs_scenario_id", "user_notify_prefs", ["scenario_id"])

    if "notify_templates" not in tables:
        op.create_table(
            "notify_templates",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(length=200), nullable=False),
            sa.Column("title", sa.String(length=300), nullable=False),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("created_by", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        )

    if "notifications" not in tables:
        op.create_table(
            "notifications",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("title", sa.String(length=300), nullable=False),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("send_at", sa.DateTime(), nullable=False),
            sa.Column("audience_json", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("created_by", sa.Integer(), nullable=True),
            sa.Column("sent_at", sa.DateTime(), nullable=True),
            sa.Column("recipients_count", sa.Integer(), nullable=False),
            sa.Column("delivered_count", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        )
        op.create_index("ix_notifications_send_at", "notifications", ["send_at"])
        op.create_index("ix_notifications_status", "notifications", ["status"])


def downgrade() -> None:
    for tbl in ("notifications", "user_notify_prefs", "notify_templates", "notify_scenarios"):
        if tbl in _insp().get_table_names():
            op.drop_table(tbl)
