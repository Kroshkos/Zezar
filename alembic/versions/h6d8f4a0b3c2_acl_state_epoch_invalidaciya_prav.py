"""ACL: таблица acl_state (epoch-инвалидация кэша прав)

Новая таблица (см. app/models/core.py, app/core/permcache.py):
  - acl_state — ключ-значение для состояния ядра RBAC;
    ключ 'acl_epoch' — монотонный счётчик, инкрементируется при любом
    изменении ролей/членств/шэров, чтобы токены и фронтенд перечитывали права.

Миграция идемпотентна: на чистой установке таблица уже создана create_all.

Revision ID: h6d8f4a0b3c2
Revises: g5c7e3f9a2b1
Create Date: 2026-10-18 10:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'h6d8f4a0b3c2'
down_revision: Union[str, None] = 'g5c7e3f9a2b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "acl_state" in insp.get_table_names():
        return
    op.create_table(
        "acl_state",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("key", name="pk_acl_state"),
    )


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "acl_state" in insp.get_table_names():
        op.drop_table("acl_state")
