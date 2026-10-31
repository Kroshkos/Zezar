"""Задачи: иерархия (задача может быть подзадачей другой задачи)

Добавляет в task_payloads колонку parent_task_uid (uid родительской задачи,
ссылка на entities.uid без FK — по образцу иерархии проектов d2f8a1b6c904:
устойчивость к переупорядочиванию id при синхронизации пространств и
отсутствие каскадного удаления родителя).

Все изменения идемпотентны (проверка через sa.inspect).

Revision ID: o3k5h1d7e0f9
Revises: n2j4g0c6d9e8
Create Date: 2026-10-31 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision = 'o3k5h1d7e0f9'
down_revision = 'n2j4g0c6d9e8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _insp():
    return sa.inspect(op.get_bind())


def upgrade() -> None:
    insp = _insp()
    if "task_payloads" not in set(insp.get_table_names()):
        return  # таблицы нет (чистая установка через create_all) — ничего делать не нужно
    cols = {c["name"] for c in insp.get_columns("task_payloads")}
    if "parent_task_uid" not in cols:
        op.add_column(
            "task_payloads",
            sa.Column("parent_task_uid", sa.String(length=36), nullable=True))
    idx = {i["name"] for i in _insp().get_indexes("task_payloads")}
    if "ix_task_payloads_parent_task_uid" not in idx:
        op.create_index(
            "ix_task_payloads_parent_task_uid", "task_payloads",
            ["parent_task_uid"])


def downgrade() -> None:
    insp = _insp()
    if "task_payloads" not in set(insp.get_table_names()):
        return
    idx = {i["name"] for i in insp.get_indexes("task_payloads")}
    if "ix_task_payloads_parent_task_uid" in idx:
        op.drop_index("ix_task_payloads_parent_task_uid",
                      table_name="task_payloads")
    cols = {c["name"] for c in insp.get_columns("task_payloads")}
    if "parent_task_uid" in cols:
        with op.batch_alter_table("task_payloads") as batch:
            batch.drop_column("parent_task_uid")
