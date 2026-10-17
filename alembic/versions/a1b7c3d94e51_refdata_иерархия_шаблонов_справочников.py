"""Справочники: иерархия шаблонов (администратор -> подразделения)

Ревизия приводит схему таблиц справочников в соответствие с моделью
преемственности шаблонов из app/models/core.py (StatusSet, PriorityLevel,
RefType, TaskGroup, WfTemplate):

  1. status_sets / priority_levels / wf_templates — недостающие колонки:
       department_id   NULL = глобальный шаблон администратора,
       source_global_id id глобального шаблона, копией которого является запись
                        подразделения (активация руководителем);
       is_active       деактивация копии НЕ удаляет запись (исторические
                        status_key/priority_key сущностей не «открепляются»).
  2. ref_types / task_groups — новых таблиц нет ни в baseline, ни в create_all
     легаси-БД: создаются целиком (идемпотентно).

Ограничение UNIQUE базовой ревизии uq_priority_levels_key(key) мешает хранить
одинаковый key глобально и в подразделении — снимается; вместо него вводится
уникальный индекс uq_priority_levels_key_department_id(key, department_id)
(в SQLite NULL != NULL, повторные глобальные шаблоны с тем же key контролируются
сервисом app/core/refdata.py).

ИСПРАВЛЕНИЕ ДЕФЕКТА ПЕРВОЙ РЕДАКЦИИ РЕВИЗИИ: вызовы batch.create_foreign_key()
внутри batch_alter_table игнорируют referent_table и всегда референцируют
изменяемую таблицу (ограничение Alembic BatchOperations) — department_id
получал self-FK на собственную таблицу вместо departments.id. Теперь таблицы
пересоздаются ЯВНО: batch_alter_table(recreate="always", copy_from=<отражённая
таблица>) + add_constraint(ForeignKeyConstraint(...)) с явно указанными
referenced tables. Для уже применённой битой версии ревизии добавлена
самодиагностика (_has_broken_self_fk), которая пересобирает таблицы заново.

Все изменения идемпотентны к любому состоянию БД (проверка через sa.inspect).

Revision ID: a1b7c3d94e51
Revises: c9e4f18a7d02
Create Date: 2026-10-17 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = 'a1b7c3d94e51'
down_revision: Union[str, None] = 'c9e4f18a7d02'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _insp():
    return sa.inspect(op.get_bind())


def _tables() -> set[str]:
    return set(_insp().get_table_names())


def _cols(table: str) -> set[str]:
    return {c["name"] for c in _insp().get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {i["name"] for i in _insp().get_indexes(table)}


def _fk_map(table: str) -> dict[str, tuple[str, tuple]]:
    """local column tuple -> (referred_table, referred columns)."""
    out = {}
    for fk in _insp().get_foreign_keys(table):
        out[tuple(fk["constrained_columns"])] = (
            fk["referred_table"], tuple(fk["referred_columns"]))
    return out


def _uq_names(table: str) -> set[str]:
    """Имена табличных UNIQUE-ограничений (sqlite_master DDL)."""
    ddl = op.get_bind().exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).scalar() or ""
    names = set()
    import re as _re
    for m in _re.finditer(r"CONSTRAINT\s+([\"\w]+)\s+UNIQUE", ddl):
        names.add(m.group(1).strip('"'))
    return names


def _target_defs(table: str) -> list:
    """Колонки целевой схемы (порядок и типы — из моделей ядра Base.metadata;
    тот же metadata, что использует autogenerate/env.py)."""
    import app.models.core  # noqa: F401  регистрация таблиц в Base.metadata
    from app.db.base import Base
    return [c.copy() for c in Base.metadata.tables[table].columns]


def _rebuild(table: str, *, drop_unique_key: bool = False) -> None:
    """Полное приведение таблицы к целевой схеме модели (move-and-copy).

    copy_from — целевой sa.Table, собранный из Column(...) моделей ядра, где
    ForeignKey("departments.id") указан ЯВНО на уровне колонки. Alembic сам
    создаёт временную таблицу, копирует данные SELECT'ом по ПЕРЕСЕЧЕНИЮ колонок
    (старые + добавленные read как NULL) и переименовывает. Так мы обходим
    дефект batch.create_foreign_key(): он игнорирует referent_table и строит
    self-FK (первая редакция ревизии — см. docstring модуля).

    drop_unique_key: legacy UNIQUE(key) у priority_levels не переносится
    (скоуп-уникальность обеспечит уникальный индекс на шаге 3).
    """
    cols = _target_defs(table)
    constraints: list = []
    if table == "status_sets":
        constraints.append(sa.UniqueConstraint("kind", "key", "department_id",
                                               name="uq_status_sets_kind"))
    args = [*cols, *constraints]
    target = sa.Table(table, sa.MetaData(), *args)
    if drop_unique_key:
        # исключаем перенос legacy-ограничения (для priority_levels его в
        # целевой схеме и так нет — UniqueConstraint("key","department_id")
        # из модели не переносим табличным ограничением, чтобы не конфликтовать
        # с SQLite NULL!=NULL; контроль — сервисом + уникальным индексом шага 3)
        pass
    with op.batch_alter_table(table, recreate="always", copy_from=target):
        pass


def _ensure_columns(table: str) -> None:
    """Недостающие колонки иерархии + корректные FK одним безопасным путём:

    - если хватает только ADD COLUMN (FK уже корректны, UNIQUE не мешает) —
      делаем простой ALTER (без пересоздания);
    - иначе (нет FK / битый self-FK / legacy UNIQUE(key) у приоритетов) —
      полное пересоздание по целевой схеме модели (_rebuild).
    """
    cols = _cols(table)
    fks = _fk_map(table)
    add_cols = []
    if "department_id" not in cols:
        add_cols.append(sa.Column("department_id", sa.Integer(), nullable=True))
    if "source_global_id" not in cols:
        add_cols.append(sa.Column("source_global_id", sa.Integer(), nullable=True))
    if "is_active" not in cols:
        add_cols.append(sa.Column("is_active", sa.Boolean(), nullable=False,
                                  server_default=sa.true()))

    fk_ok = (fks.get(("department_id",)) == ("departments", ("id",))
             and (("source_global_id",) not in cols
                  or fks.get(("source_global_id",)) == (table, ("id",))))
    need_uniq_drop = (table == "priority_levels"
                      and "uq_priority_levels_key" in _uq_names(table))

    if add_cols and fk_ok and not need_uniq_drop:
        # редкий случай: колонки есть частично, схема FK уже правильная
        with op.batch_alter_table(table) as batch:
            for c in add_cols:
                batch.add_column(c)
    elif not fk_ok or need_uniq_drop:
        _rebuild(table, drop_unique_key=need_uniq_drop)
    # если add_cols пуст, fk_ok и UNIQUE не мешает — таблица уже в целевом виде


def upgrade() -> None:
    tables = _tables()

    # ---------- 1. Новые таблицы справочников (типы + группы задач) ---------
    if "ref_types" not in tables:
        op.create_table(
            "ref_types",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("kind", sa.String(length=30), nullable=False),
            sa.Column("label", sa.String(length=200), nullable=False),
            sa.Column("department_id", sa.Integer(), nullable=True),
            sa.Column("source_global_id", sa.Integer(), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False,
                      server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(), nullable=False,
                      server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False,
                      server_default=sa.func.now()),
            sa.ForeignKeyConstraint(
                ["department_id"], ["departments.id"],
                name="fk_ref_types_department_id_departments", ondelete="CASCADE"),
            sa.ForeignKeyConstraint(
                ["source_global_id"], ["ref_types.id"],
                name="fk_ref_types_source_global_id_ref_types", ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id", name="pk_ref_types"),
            sa.UniqueConstraint("kind", "label", "department_id",
                                name="uq_ref_types_kind_label_department_id"),
        )

    if "task_groups" not in tables:
        op.create_table(
            "task_groups",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=200), nullable=False),
            sa.Column("criteria", sa.JSON(), nullable=False),
            sa.Column("department_id", sa.Integer(), nullable=True),
            sa.Column("source_global_id", sa.Integer(), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False,
                      server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(), nullable=False,
                      server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False,
                      server_default=sa.func.now()),
            sa.ForeignKeyConstraint(
                ["department_id"], ["departments.id"],
                name="fk_task_groups_department_id_departments", ondelete="CASCADE"),
            sa.ForeignKeyConstraint(
                ["source_global_id"], ["task_groups.id"],
                name="fk_task_groups_source_global_id_task_groups", ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id", name="pk_task_groups"),
            sa.UniqueConstraint("name", "department_id",
                                name="uq_task_groups_name_department_id"),
        )

    # ---------- 2. Колонки иерархии + корректные FK --------------------------
    # (включая самодиагностику: если предыдущая редакция ревизии оставила
    # self-FK на department_id — _fk_map её не увидит как корректную, и таблица
    # будет пересобрана заново с честными ограничениями)
    for table in ("status_sets", "priority_levels", "wf_templates"):
        if table in _tables():
            _ensure_columns(table)

    # ---------- 3. priority_levels: уникальность key в разрезе scope ---------
    if "priority_levels" in _tables():
        idx = _indexes("priority_levels")
        if "ix_priority_levels_key" in idx:
            with op.batch_alter_table("priority_levels") as batch:
                batch.drop_index("ix_priority_levels_key")
        if "uq_priority_levels_key_department_id" not in idx:
            op.create_index(
                "uq_priority_levels_key_department_id", "priority_levels",
                ["key", "department_id"], unique=True)

    # ---------- 4. Индексы для выборок по подразделениям --------------------
    for table in ("status_sets", "priority_levels", "ref_types",
                  "task_groups", "wf_templates"):
        if table in _tables() and f"ix_{table}_department_id" not in _indexes(table):
            op.create_index(f"ix_{table}_department_id", table, ["department_id"])

    # ---------- 5. Проверка результата (fail-fast вместо тихой поломки) ------
    for table in ("status_sets", "priority_levels", "wf_templates",
                  "ref_types", "task_groups"):
        if table not in _tables():
            continue
        for fk in _insp().get_foreign_keys(table):
            if ("department_id" in fk["constrained_columns"]
                    and fk["referred_table"] != "departments"):
                raise RuntimeError(
                    f"MIGRATION BUG: {table}.department_id FK указывает на "
                    f"{fk['referred_table']}, ожидался departments")


def downgrade() -> None:
    # Данные справочников не восстанавливаются: возвращаем только структуру.
    if "priority_levels" in _tables():
        try:
            op.drop_index("uq_priority_levels_key_department_id",
                          table_name="priority_levels")
        except Exception:
            pass
        existing = sa.Table("priority_levels", sa.MetaData(),
                            autoload_with=op.get_bind())
        with op.batch_alter_table("priority_levels", recreate="always",
                                  copy_from=existing) as batch:
            batch.add_constraint(sa.UniqueConstraint("key",
                                                     name="uq_priority_levels_key"))
    for table in ("status_sets", "priority_levels", "wf_templates"):
        if table in _tables():
            cols = _cols(table)
            with op.batch_alter_table(table) as batch:
                for c in ("is_active", "source_global_id", "department_id"):
                    if c in cols:
                        batch.drop_column(c)
    for table in ("task_groups", "ref_types"):
        if table in _tables():
            op.drop_table(table)
