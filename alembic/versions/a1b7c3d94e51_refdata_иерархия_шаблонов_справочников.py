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


def _target_constraints(table: str) -> list:
    """Табличные ограничения целевой схемы модели (PK/UNIQUE/FK) с именами из
    naming convention — ровно те, что создаёт create_all, поэтому следующие
    `--autogenerate` ревизии не будут пересоздавать таблицу."""
    import app.models.core  # noqa: F401
    from app.db.base import Base
    out = []
    for c in Base.metadata.tables[table].constraints:
        if isinstance(c, sa.PrimaryKeyConstraint):
            out.append(sa.PrimaryKeyConstraint(*[col.name for col in c.columns],
                                               name=c.name))
        elif isinstance(c, sa.UniqueConstraint):
            out.append(sa.UniqueConstraint(*[col.name for col in c.columns],
                                           name=c.name))
        elif isinstance(c, sa.ForeignKeyConstraint):
            cols = [col.name for col in c.columns]
            # SQLAlchemy 2.x: у ещё не привязанного (unbound) ForeignKey
            # атрибута _table_name нет — целевая таблица берётся из
            # элемент-спецификации ссылки ("departments.id" / "status_sets.id").
            targets = []
            for el in c.elements:
                spec = el._target_fullname if hasattr(el, "_target_fullname") \
                    else str(el._colspec)
                targets.append(spec)
            out.append(sa.ForeignKeyConstraint(cols, targets,
                                               name=c.name,
                                               ondelete=c.ondelete,
                                               onupdate=c.onupdate))
    return out


def _rebuild(table: str) -> None:
    """Полное приведение таблицы к целевой схеме модели (move-and-copy).

    Механика Alembic batch с copy_from=<целевая таблица>: временная таблица
    создаётся по ЦЕЛЕВОЙ схеме (Column(...) моделей ядра, где ForeignKey
    указан явно на уровне колонки — без дефекта self-FK из
    batch.create_foreign_key()), а INSERT..SELECT переносит данные только по
    колонкам, реально присутствующим в СТАРОЙ таблице. Поэтому недостающие
    колонки иерархии добавляем отдельным простым ALTER ADD COLUMN ДО
    пересоздания; после recreate целевые FK/UNIQUE уже на месте.

    legacy UNIQUE(key) у priority_levels в целевую схему НЕ переносится:
    скоуп-уникальность (key, department_id) объявлена в модели ядра и входит
    в copy_from вместе с FK.
    """
    cur_cols = _cols(table)
    add_cols = []
    if "department_id" not in cur_cols:
        add_cols.append(sa.Column("department_id", sa.Integer(), nullable=True))
    if "source_global_id" not in cur_cols:
        add_cols.append(sa.Column("source_global_id", sa.Integer(), nullable=True))
    if "is_active" not in cur_cols:
        add_cols.append(sa.Column("is_active", sa.Boolean(), nullable=False,
                                  server_default=sa.true()))
    # legacy create_all-БД могут не иметь и других колонок целевой модели
    # (например wf_templates.kind_scope) — пересоздание с copy_from делает
    # INSERT..SELECT по ВСЕМ колонкам целевой схемы, поэтому досоздаём всё
    # недостающее из модели до recreate.
    target_colnames = {c.name for c in _target_defs(table)}
    for name in sorted(target_colnames - cur_cols - {c.name for c in add_cols}):
        add_cols.append(sa.Column(name, sa.String(length=50), nullable=True))
    if add_cols:
        # plain ALTER (batch без recreate='always' выполняет их напрямую)
        with op.batch_alter_table(table) as batch:
            for c in add_cols:
                batch.add_column(c)
        cur_cols |= {c.name for c in add_cols}

    cols = _target_defs(table)
    # Целевая схема = ровно метаданные моделей ядра. В режиме copy_from Alembic
    # переносит в tmp-таблицу только ограничения, которые нашёл у переданной
    # copy_from-таблицы; отдельно собранный sa.Table без них терял FK (дефект
    # третьей редакции: status_sets осталась вообще без department_id FK).
    # Поэтому собираем target С ОГРАНИЧЕНИЯМИ модели (PK/UNIQUE/FK с именами из
    # naming convention — те же, что создаёт create_all, чтобы следующие
    # `--autogenerate` ревизии не «пересоздавали» таблицы из-за имён).
    constraints = _target_constraints(table)
    if table == "priority_levels":
        # legacy UNIQUE(key) несовместим с иерархией (один key глобально + в
        # подразделениях); целевой скоуп-UNIQUE (key, department_id) объявлен в
        # модели. Перед снятием старого ограничения чистим коллизии по целевому
        # ключу, иначе CREATE нового UNIQUE упадёт на существующих данных.
        bind = op.get_bind()
        dups = bind.exec_driver_sql(
            "SELECT key, COALESCE(department_id, -1) AS dep, COUNT(*) "
            "FROM priority_levels GROUP BY key, dep HAVING COUNT(*) > 1"
        ).fetchall()
        for key, _dep, _n in dups:
            bind.exec_driver_sql(
                "DELETE FROM priority_levels WHERE key = ? AND id NOT IN ("
                "SELECT MIN(id) FROM priority_levels WHERE key = ?)",
                (key, key))
    target = sa.Table(table, sa.MetaData(), *cols, *constraints)
    with op.batch_alter_table(table, recreate="always", copy_from=target):
        pass
    # Пересобранная таблица обязана иметь честные FK (защита от тихой потери
    # ограничений в batch-режиме и от self-FK дефекта прошлых редакций).
    fkmap = _fk_map(table)
    if fkmap.get(("department_id",)) != ("departments", ("id",)):
        raise RuntimeError(
            f"MIGRATION BUG: {table}.department_id FK отсутствует/битый: {fkmap}")
    if fkmap.get(("source_global_id",)) != (table, ("id",)):
        raise RuntimeError(
            f"MIGRATION BUG: {table}.source_global_id self-FK отсутствует: {fkmap}")


def _ensure_columns(table: str) -> None:
    """Приводит таблицу справочника к целевой схеме модели: недостающие
    колонки иерархии + корректные FK (+ снятие legacy UNIQUE(key)).

    ИСПРАВЛЕНИЕ ДЕФЕКТА ВТОРОЙ РЕДАКЦИИ: в legacy-БД (create_all до ревизии)
    таблица priority_levels уже содержала колонку key с табличным
    UNIQUE(uq_priority_levels_key), но колонки department_id/source_global_id
    отсутствовали. Прежняя логика считала fk_ok=True (FK на department_id
    просто не существовал — сравнение fks.get(...) == (...) не выполнялось для
    отсутствующей колонки «как будто всё ок»), попадала в ветку add-column без
    пересоздания — и UNIQUE(key) оставался в DDL таблицы, мешая хранить
    одинаковый key глобально и в подразделении. Теперь наличие колонок
    иерархии обязательно для fk_ok: если их нет — таблица пересобирается
    полностью, что заодно снимает legacy UNIQUE (в целевую схему он не входит).
    """
    cols = _cols(table)
    has_hierarchy_cols = {"department_id", "source_global_id",
                          "is_active"} <= cols
    fks = _fk_map(table)
    fk_ok = (has_hierarchy_cols
             and fks.get(("department_id",)) == ("departments", ("id",))
             and fks.get(("source_global_id",)) == (table, ("id",)))
    need_uniq_drop = (table == "priority_levels"
                      and "uq_priority_levels_key" in _uq_names(table))

    if not fk_ok or need_uniq_drop:
        # NB: сигнатура _rebuild(table) — legacy UNIQUE(key) у priority_levels
        # не переносится в целевую схему автоматически (см. docstring _rebuild),
        # отдельный аргумент drop_unique_key не требуется и ранее вызывал
        # TypeError при миграции легаси-БД.
        _rebuild(table)
    else:
        cols = _cols(table)
        add_cols = []
        if "department_id" not in cols:
            add_cols.append(sa.Column("department_id", sa.Integer(), nullable=True))
        if "source_global_id" not in cols:
            add_cols.append(sa.Column("source_global_id", sa.Integer(),
                                      nullable=True))
        if "is_active" not in cols:
            add_cols.append(sa.Column("is_active", sa.Boolean(), nullable=False,
                                      server_default=sa.true()))
        if add_cols:
            with op.batch_alter_table(table) as batch:
                for c in add_cols:
                    batch.add_column(c)


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
        # Скоуп-уникальность для записей подразделений (key, department_id):
        # в SQLite табличный UNIQUE с NULL не защищает от дублей глобальных
        # шаблонов — для них контроль дублей выполняет сервис app/core/refdata.py.
        # Удалённые имена: legacy-табличный UNIQUE(uq_priority_levels_key) и
        # ошибочный полный UNIQUE uq_priority_levels_key_department_id из
        # предыдущих редакций (он блокировал легитимную пару "глобальный key +
        # копия подразделения", т.к. индекс без WHERE покрывал и NULL-строки).
        for legacy_idx in ("uq_priority_levels_key_department_id",
                           "uq_priority_levels_scope_dept"):
            if legacy_idx in idx:
                op.drop_index(legacy_idx, table_name="priority_levels")
                idx.discard(legacy_idx)
        if "uq_priority_levels_scope_dept" not in idx:
            bind = op.get_bind()
            # защита от уже существующих дублей в legacy-данных
            dups = bind.exec_driver_sql(
                "SELECT key, department_id, COUNT(*) FROM priority_levels "
                "WHERE department_id IS NOT NULL "
                "GROUP BY key, department_id HAVING COUNT(*) > 1").fetchall()
            for key, dep_id, _n in dups:
                bind.exec_driver_sql(
                    "DELETE FROM priority_levels WHERE department_id IS NOT ? "
                    "AND key = ? AND id NOT IN ("
                    "SELECT MIN(id) FROM priority_levels "
                    "WHERE department_id IS NOT ? AND key = ?)",
                    (dep_id, key, dep_id, key))
            op.create_index(
                "uq_priority_levels_scope_dept", "priority_levels",
                ["key", "department_id"], unique=True,
                postgresql_where=sa.text("department_id IS NOT NULL"),
                sqlite_where=sa.text("department_id IS NOT NULL"))

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
        for idx_name in ("uq_priority_levels_scope_dept",
                         "uq_priority_levels_key_department_id"):
            if idx_name in _indexes("priority_levels"):
                op.drop_index(idx_name, table_name="priority_levels")
        bind = op.get_bind()
        # legacy UNIQUE(key) может быть восстановлен только при отсутствии
        # дублей по key (например, глобальный шаблон + активированная копия);
        # чистим дубли, оставляя запись с минимальным id.
        dups = bind.exec_driver_sql(
            "SELECT key FROM priority_levels GROUP BY key HAVING COUNT(*) > 1"
        ).fetchall()
        for (key,) in dups:
            bind.exec_driver_sql(
                "DELETE FROM priority_levels WHERE key = ? AND id NOT IN ("
                "SELECT MIN(id) FROM priority_levels WHERE key = ?)",
                (key, key))
        # Возврат legacy-структуры: без колонок иерархии + табличный UNIQUE(key).
        # BatchOperations.add_constraint в Alembic 1.20 не существует —
        # пересоздаём таблицу ЯВНО по целевой схеме (как в _rebuild), колонки
        # copy_from собираем вручную (dept-колонки из неё исключаем).
        cur_cols = {c.name: c for c in sa.Table(
            "priority_levels", sa.MetaData(),
            autoload_with=bind).columns}
        keep = [sa.Column(name, c.type.copy(), nullable=c.nullable,
                          primary_key=(name == "id"),
                          server_default=c.server_default.arg
                          if c.server_default else None)
                for name, c in cur_cols.items()
                if name not in ("department_id", "source_global_id", "is_active")]
        keep[0] = sa.Column("id", sa.Integer(), nullable=False, primary_key=True)
        legacy = sa.Table("priority_levels", sa.MetaData(), *keep,
                          sa.UniqueConstraint("key", name="uq_priority_levels_key"))
        with op.batch_alter_table("priority_levels", recreate="always",
                                  copy_from=legacy):
            pass
    for table in ("status_sets", "wf_templates"):
        if table in _tables():
            cols = _cols(table)
            with op.batch_alter_table(table) as batch:
                for c in ("is_active", "source_global_id", "department_id"):
                    if c in cols:
                        batch.drop_column(c)
    for table in ("task_groups", "ref_types"):
        if table in _tables():
            op.drop_table(table)
