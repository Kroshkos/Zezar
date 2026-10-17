"""Payload-таблицы модулей: привязка сущностей к проекту (project_entity_uid)

Обеспечивает связанность и подчинённость категорий в рамках подключённых
модулей: документы, ПО, гранты, стипендии и приоритетные показатели могут
быть привязаны к проекту (аналогично уже существующей привязке задач).
Ссылка хранится как uid сущности kind=project (без FK — устойчивость при
переупорядочивании id и отсутствие каскадного удаления проекта).

Все изменения идемпотентны (проверка через sa.inspect).

Revision ID: e3a5c7d91f02
Revises: d2f8a1b6c904
Create Date: 2026-10-17 20:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = 'e3a5c7d91f02'
down_revision: Union[str, None] = 'd2f8a1b6c904'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("doc_payloads", "soft_payloads", "grant_payloads",
           "scholar_payloads", "priority_payloads")


def _insp():
    return sa.inspect(op.get_bind())


def upgrade() -> None:
    insp = _insp()
    names = set(insp.get_table_names())
    for table in _TABLES:
        if table not in names:
            continue  # чистая установка через create_all — колонка уже в модели
        cols = {c["name"] for c in insp.get_columns(table)}
        if "project_entity_uid" not in cols:
            op.add_column(
                table, sa.Column("project_entity_uid", sa.String(length=36),
                                 nullable=True))
        idx = {i["name"] for i in _insp().get_indexes(table)}
        ix = f"ix_{table}_project_entity_uid"
        if ix not in idx:
            op.create_index(ix, table, ["project_entity_uid"])


def downgrade() -> None:
    insp = _insp()
    names = set(insp.get_table_names())
    for table in _TABLES:
        if table not in names:
            continue
        idx = {i["name"] for i in insp.get_indexes(table)}
        ix = f"ix_{table}_project_entity_uid"
        if ix in idx:
            op.drop_index(ix, table_name=table)
        cols = {c["name"] for c in insp.get_columns(table)}
        if "project_entity_uid" in cols:
            with op.batch_alter_table(table) as batch:
                batch.drop_column("project_entity_uid")
