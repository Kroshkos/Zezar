"""users.yandex_id -> NULLABLE + локальные колонки login/password_hash/yandex_login

Проблема на деплое (Amvera, SQLite): базовая ревизия 0ad0082f05da ошибочно создала
таблицу users с yandex_id NOT NULL, хотя модель (app/models/core.py) объявляет её
nullable=True — у локальных админ-аккаунтов из ADMIN_USERNAME/ADMIN_PASSWORD
(bootstrap в app/db/seed.py) yandex_id отсутствует. При старте вставки таких
аккаунтов получали:

    sqlalchemy.exc.IntegrityError: (sqlite3.IntegrityError)
    NOT NULL constraint failed: users.yandex_id

Дополнительно здесь добавляются колонки login/password_hash/yandex_login, которых
нет ни в одной предыдущей ревизии (модели они уже содержатся), чтобы схема после
`alembic upgrade head` полностью соответствовала метаданным и не требовалось
самовосстановления колонок из lifespan.

SQLite не умеет снимать NOT NULL через ALTER TABLE, поэтому пересоздание таблицы
выполняется через batch_alter_table (режим recreate).

Revision ID: 7d1a9c2f4b83
Revises: bf6f942b2cbf
Create Date: 2026-10-16 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# Идентификаторы ревизий, используются Alembic.
revision: str = '7d1a9c2f4b83'
down_revision: Union[str, None] = 'bf6f942b2cbf'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(name: str) -> bool:
    return name in set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    # --- users: снимаем NOT NULL с yandex_id + досоздаём недостающие колонки ---
    if _table_exists('users'):
        with op.batch_alter_table('users', recreate='always') as batch_op:
            batch_op.alter_column('yandex_id',
                                  existing_type=sa.String(length=64),
                                  nullable=True)
            existing_cols = {c['name'] for c in
                             sa.inspect(op.get_bind()).get_columns('users')}
            if 'login' not in existing_cols:
                batch_op.add_column(sa.Column('login', sa.String(length=64),
                                              nullable=True))
                batch_op.create_index(batch_op.f('ix_users_login'),
                                      ['login'], unique=True)
            if 'password_hash' not in existing_cols:
                batch_op.add_column(sa.Column('password_hash', sa.Text(),
                                              nullable=True))
            if 'yandex_login' not in existing_cols:
                batch_op.add_column(sa.Column('yandex_login',
                                              sa.String(length=255),
                                              nullable=True))
                batch_op.create_index(batch_op.f('ix_users_yandex_login'),
                                      ['yandex_login'], unique=False)

    # --- invites: yandex_id по модели необязателен (допуск может быть по email) ---
    if _table_exists('invites'):
        with op.batch_alter_table('invites', recreate='auto') as batch_op:
            batch_op.alter_column('yandex_id',
                                  existing_type=sa.String(length=64),
                                  nullable=True)


def downgrade() -> None:
    if _table_exists('users'):
        with op.batch_alter_table('users', recreate='always') as batch_op:
            existing_cols = {c['name'] for c in
                             sa.inspect(op.get_bind()).get_columns('users')}
            if 'login' in existing_cols:
                batch_op.drop_index(batch_op.f('ix_users_login'))
                batch_op.drop_column('login')
            if 'password_hash' in existing_cols:
                batch_op.drop_column('password_hash')
            batch_op.alter_column('yandex_id',
                                  existing_type=sa.String(length=64),
                                  nullable=False)
    if _table_exists('invites'):
        with op.batch_alter_table('invites', recreate='auto') as batch_op:
            batch_op.alter_column('yandex_id',
                                  existing_type=sa.String(length=64),
                                  nullable=False)
