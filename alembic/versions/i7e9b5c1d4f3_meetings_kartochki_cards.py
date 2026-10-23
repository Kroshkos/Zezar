"""Meetings: раздел «Карточки» — поле cards в meeting_payloads

Раздел «Карточки» модуля «Совещания» по образу и подобию прототипа
ИС_Цезарь.html (new_tasks формы совещания / mPushNewTasksToKanban):
список карточек задач, заведённых на совещании, хранится в JSON-колонке
payload'а. Изменения идемпотентны (проверка через sa.inspect).

Revision ID: i7e9b5c1d4f3
Revises: h6d8f4a0b3c2
Create Date: 2026-10-24 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = 'i7e9b5c1d4f3'
down_revision: Union[str, None] = 'h6d8f4a0b3c2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _insp():
    return sa.inspect(op.get_bind())


def upgrade() -> None:
    insp = _insp()
    if "meeting_payloads" not in set(insp.get_table_names()):
        return  # чистая установка через create_all — колонка уже в модели
    cols = {c["name"] for c in insp.get_columns("meeting_payloads")}
    if "cards" not in cols:
        op.add_column(
            "meeting_payloads",
            sa.Column("cards", sa.JSON(), nullable=False, server_default="[]"))


def downgrade() -> None:
    insp = _insp()
    if "meeting_payloads" not in set(insp.get_table_names()):
        return
    cols = {c["name"] for c in insp.get_columns("meeting_payloads")}
    if "cards" in cols:
        with op.batch_alter_table("meeting_payloads") as batch:
            batch.drop_column("cards")
