"""Удаление колонок авторизации через Яндекс + журнал событий (event_log)

Авторизация через Яндекс ID полностью удалена из системы: единственный контур
входа — логин/пароль (администратор задаётся ADMIN_USERNAME/ADMIN_PASSWORD,
пользователей создаёт админ с принудительной сменой начального пароля).

Что делает ревизия (идемпотентно к любому состоянию легаси-БД):
  1. users.yandex_id -> users.login: перенос локальных логинов ('local:<login>')
     в колонку login; yandex_id удаляется вместе с индексом.
  2. must_change_password — недостающая колонка принудительной смены пароля
     при первом входе (в модели есть, ни одна ревизия не создавала).
  3. Чистка мусорных значений users.yandex_login ('local:*', 'dev:*'),
     оставшихся от прежних сидов; поле нужно только для CalDAV (Яндекс.Календарь).
  4. invites.yandex_id удаляется (таблица устарела, код к ней не обращается).
  5. Создаётся таблица event_log — журнал событий системы.

SQLite: все изменения через batch_alter_table (пересоздание таблицы сохраняет данные).

Revision ID: c9e4f18a7d02
Revises: 7d1a9c2f4b83
Create Date: 2026-10-16 18:30:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# Идентификаторы ревизий, используются Alembic.
revision: str = 'c9e4f18a7d02'
down_revision: Union[str, None] = '7d1a9c2f4b83'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _insp():
    return sa.inspect(op.get_bind())


def _tables():
    return set(_insp().get_table_names())


def _cols(table: str) -> set[str]:
    return {c["name"] for c in _insp().get_columns(table)}


def _copy_legacy_logins(from_col: str) -> None:
    """Перенос локальных логинов из легаси-колонки ('local:<login>') в users.login."""
    conn = op.get_bind()
    rows = conn.execute(sa.text(
        f"SELECT id, {from_col} FROM users WHERE {from_col} LIKE 'local:%'")).fetchall()
    for uid, val in rows:
        conn.execute(sa.text("UPDATE users SET login = :lg WHERE id = :uid"),
                     {"lg": val.split(":", 1)[1].lower(), "uid": uid})


def upgrade() -> None:
    tables = _tables()

    # ---------- users ----------
    if "users" in tables:
        cols = _cols("users")
        has_legacy = "yandex_id" in cols
        add_cols = []
        if "login" not in cols:
            add_cols.append(sa.Column("login", sa.String(length=64), nullable=True))
        if "password_hash" not in cols:
            add_cols.append(sa.Column("password_hash", sa.Text(), nullable=True))
        if "must_change_password" not in cols:
            # default=True в модели: новые пользователи (созданные админом с
            # начальным паролем) обязаны сменить пароль при первом входе.
            add_cols.append(sa.Column("must_change_password", sa.Boolean(),
                                      nullable=False, server_default=sa.false()))

        # 1) Сначала добавляем новые колонки и ПЕРЕНОСИМ данные из легаси-колок
        #    (пока они ещё существуют в таблице): 'local:<login>' -> login.
        if add_cols or has_legacy:
            with op.batch_alter_table("users", recreate="auto") as batch_op:
                for c in add_cols:
                    batch_op.add_column(c)
                if "login" not in cols:
                    batch_op.create_index(batch_op.f("ix_users_login"),
                                          ["login"], unique=True)
            if has_legacy:
                _copy_legacy_logins("yandex_id")
            # Чистка мусорных «сервисных» значений yandex_login от прежних сидов
            # ('local:*', 'dev:*'); реальные логины остаются — они нужны для
            # CalDAV (привязка Яндекс.Календаря).
            if "yandex_login" in cols:
                op.execute("UPDATE users SET yandex_login = NULL "
                           "WHERE yandex_login LIKE 'local:%' OR yandex_login LIKE 'dev:%'")

        # 2) Затем удаляем легаси-колонку yandex_id вместе с индексом.
        if has_legacy:
            user_indexes = {i["name"] for i in _insp().get_indexes("users")}
            with op.batch_alter_table("users", recreate="always") as batch_op:
                # Индекс удаляем только если он реально есть в схеме: batch-режим
                # падает с "No such index" на легаси-БД, где его никогда не создавали.
                for ix in ("ix_users_yandex_id",):
                    if ix in user_indexes:
                        batch_op.drop_index(ix)
                batch_op.drop_column("yandex_id")

    # ---------- invites (устаревшая таблица допусков OAuth) ----------
    if "invites" in tables:
        inv_cols = _cols("invites")
        drop_ix = "ix_invites_yandex_id" in {
            i["name"] for i in _insp().get_indexes("invites")}
        with op.batch_alter_table("invites", recreate="auto") as batch_op:
            if drop_ix:
                batch_op.drop_index("ix_invites_yandex_id")
            if "yandex_id" in inv_cols:
                batch_op.drop_column("yandex_id")

    # ---------- event_log: журнал событий системы ----------
    if "event_log" not in tables:
        op.create_table(
            "event_log",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("at", sa.DateTime(), nullable=False),
            sa.Column("user_id", sa.Integer(),
                      sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("user_name", sa.String(length=160), nullable=True),
            sa.Column("action", sa.String(length=60), nullable=False),
            sa.Column("category", sa.String(length=30), nullable=False,
                      server_default="system"),
            sa.Column("object", sa.String(length=200), nullable=True),
            sa.Column("ip", sa.String(length=64), nullable=True),
            sa.Column("level", sa.String(length=10), nullable=False,
                      server_default="info"),
            sa.Column("message", sa.Text(), nullable=True),
            sa.Column("details_json", sa.JSON(), nullable=False),
        )
        op.create_index("ix_event_log_at", "event_log", ["at"])
        op.create_index("ix_event_log_user_id", "event_log", ["user_id"])
        op.create_index("ix_event_log_action", "event_log", ["action"])
        op.create_index("ix_event_log_category", "event_log", ["category"])


def downgrade() -> None:
    tables = _tables()

    if "event_log" in tables:
        for ix in ("ix_event_log_category", "ix_event_log_action",
                   "ix_event_log_user_id", "ix_event_log_at"):
            op.drop_index(ix, table_name="event_log")
        op.drop_table("event_log")

    if "users" in tables:
        cols = _cols("users")
        with op.batch_alter_table("users", recreate="always") as batch_op:
            if "must_change_password" in cols:
                batch_op.drop_column("must_change_password")
            if "yandex_id" not in cols:
                batch_op.add_column(
                    sa.Column("yandex_id", sa.String(length=64), nullable=True))
                batch_op.create_index(batch_op.f("ix_users_yandex_id"),
                                      ["yandex_id"], unique=False)
        # возвращаем локальным аккаунтам прежний формат 'local:<login>'
        if "yandex_id" in _cols("users") and "login" in cols:
            op.execute("UPDATE users SET yandex_id = 'local:' || login "
                       "WHERE yandex_id IS NULL AND login IS NOT NULL")

    if "invites" in tables and "yandex_id" not in _cols("invites"):
        with op.batch_alter_table("invites", recreate="auto") as batch_op:
            batch_op.add_column(
                sa.Column("yandex_id", sa.String(length=64), nullable=True))
            batch_op.create_index(batch_op.f("ix_invites_yandex_id"),
                                  ["yandex_id"], unique=False)
