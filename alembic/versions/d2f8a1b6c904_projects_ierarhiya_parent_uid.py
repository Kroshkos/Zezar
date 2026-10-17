"""Проекты: иерархия (проект может быть частью другого проекта)

Добавляет в project_payloads колонку parent_uid (uid родительского проекта,
ссылка на entities.uid без FK — устойчивость к переупорядочиванию id при
синхронизации пространств и отсутствие каскадного удаления родителя).
Исполнители сущностей хранятся денормализованно в entities.extra_json
(JSON-колонка существует с baseline) — отдельная миграция не требуется.

Все изменения идемпотентны (проверка через sa.inspect).

Revision ID: d2f8a1b6c904
Revises: a1b7c3d94e51
Create Date: 2026-10-17 18:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = 'd2f8a1b6c904'
down_revision: Union[str, None] = 'a1b7c3d94e51'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _insp():
    return sa.inspect(op.get_bind())


def upgrade() -> None:
    insp = _insp()
    if "project_payloads" not in set(insp.get_table_names()):
        return  # таблицы нет (чистая установка через create_all) — ничего делать не нужно
    cols = {c["name"] for c in insp.get_columns("project_payloads")}
    if "parent_uid" not in cols:
        op.add_column(
            "project_payloads",
            sa.Column("parent_uid", sa.String(length=36), nullable=True))
    idx = {i["name"] for i in _insp().get_indexes("project_payloads")}
    if "ix_project_payloads_parent_uid" not in idx:
        op.create_index(
            "ix_project_payloads_parent_uid", "project_payloads", ["parent_uid"])


def downgrade() -> None:
    insp = _insp()
    if "project_payloads" not in set(insp.get_table_names()):
        return
    idx = {i["name"] for i in insp.get_indexes("project_payloads")}
    if "ix_project_payloads_parent_uid" in idx:
        op.drop_index("ix_project_payloads_parent_uid",
                      table_name="project_payloads")
    cols = {c["name"] for c in insp.get_columns("project_payloads")}
    if "parent_uid" in cols:
        with op.batch_alter_table("project_payloads") as batch:
            batch.drop_column("parent_uid")
