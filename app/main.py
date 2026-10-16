"""
Ядро приложения «ЦЕЗАРЬ»: FastAPI + lifespan (БД в /data, динамический реестр модулей,
фоновые воркеры интеграций).

Запуск:  uvicorn app.main:app
Модуль-плагин = modules/<key>/manifest.py -> registry.register(ModuleSpec(...));
роутер монтируется ДВАЖДЫ: REST на /api/<key>, HTML-страницы на /<key>.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect as sa_inspect, schema, text, types as sa_types

from app.core.config import get_settings
from app.core.module_registry import registry
from app.db.base import Base
from app.db.engine import SessionLocal, engine

log = logging.getLogger("caesar")
settings = get_settings()

# Fail-fast: не даём поднять продакшен с дефолтным секретом / dev-входом / демо-сидом
if settings.is_production:
    settings.require_production_settings()


def _import_all_models() -> None:
    """Ядро + payload-таблицы должны быть импортированы до create_all."""
    import app.models.core            # noqa: F401
    import app.models.module_payloads  # noqa: F401


# ---------------------------------------------------------------------------
# Самовосстановление схемы БД. Вся интроспекция — ЧЕРЕЗ SQLAlchemy Inspector
# (sa_inspect(conn)), а не «сырые» PRAGMA: курсоры aiosqlite, оставленные
# недочитанными/незакрытыми внутри run_sync, роняли следующий запрос с
# MissingGreenlet на проде. Записи — только через conn.execute(text(...)).
# ---------------------------------------------------------------------------


def _exec(conn, sql: str) -> None:
    """Выполнить DDL/DML без строк результата (Inspector-безопасно)."""
    conn.execute(text(sql))


def _drop_legacy_auth_columns(conn) -> None:
    """Self-healing легаси-схемы: удаление колонок авторизации через Яндекс ID.

    На продакшене встречались БД, где миграция c9e4f18a7d02 не применялась
    (ошибочный `stamp head`). Колонки users.yandex_id / invites.yandex_id модели
    больше не объявляют, и create_all их НЕ чистит — leftover NOT NULL yandex_id
    ронял старт (IntegrityError при сиде администратора). SQLite < 3.35 может не
    поддерживать DROP COLUMN, поэтому в этом случае таблица аккуратно
    пересоздаётся по текущим метаданным с сохранением данных. Идемпотентно."""
    insp = sa_inspect(conn)

    def cols(table: str) -> set[str]:
        return {c["name"] for c in insp.get_columns(table)}

    def has_table(table: str) -> bool:
        return insp.has_table(table)

    def fks(table: str) -> list[tuple]:
        """Прямые FK-зависимости: (таблица-ребёнок, колонка ребёнка) на table.id."""
        out = []
        for t in insp.get_table_names():
            for fk in insp.get_foreign_keys(t):
                if fk["referred_table"] == table and \
                        fk["referred_columns"] == ["id"]:
                    for lc in fk["constrained_columns"]:
                        out.append((t, lc))
        return out

    # Очень старые схемы: users.full_name вместо display_name. ALTER TABLE ...
    # RENAME COLUMN поддерживается только SQLite >= 3.25; на более старых
    # версиях пробуем его, а при неудаче помечаем колонку к переносу — её
    # данные будут скопированы в display_name при пересоздании таблицы ниже.
    _legacy_col_rename: dict[str, tuple[str, str]] = {}  # table -> (старая, новая)
    if has_table("users"):
        ucols = cols("users")
        if "display_name" not in ucols and "full_name" in ucols:
            try:
                _exec(conn, "ALTER TABLE users RENAME COLUMN full_name TO display_name")
                log.warning("migrate: users.full_name переименован в display_name")
            except Exception:  # noqa: BLE001 — старый SQLite: перенос при recrate
                _legacy_col_rename["users"] = ("full_name", "display_name")
                log.warning("migrate: RENAME COLUMN не поддерживается — full_name "
                            "будет перенесён в display_name при пересоздании таблицы")
        # Нормализация role: Enum SQLAlchemy хранит ИМЕНА членов ('ADMIN'),
        # легаси-строки вроде 'admin'/'member' иначе роняют старт с LookupError
        # при любом SELECT (role -> None). CASE по lower(role) — чтобы чинить
        # и верхний, и нижний регистр легаси-значений.
        try:
            _exec(conn,
                "UPDATE users SET role = CASE lower(role)"
                " WHEN 'admin' THEN 'ADMIN'"
                " WHEN 'coordinator' THEN 'COORDINATOR'"
                " WHEN 'head' THEN 'HEAD'"
                " ELSE 'MEMBER' END"
                " WHERE role IS NULL OR role NOT IN"
                " ('ADMIN','COORDINATOR','HEAD','MEMBER')")
        except Exception:  # noqa: BLE001 — колонки role может не быть на убитых БД
            pass

    for table, legacy_col in (("users", "yandex_id"), ("invites", "yandex_id")):
        if not has_table(table) or legacy_col not in cols(table):
            continue
        try:  # сначала пробуем дешёвый DROP COLUMN (SQLite >= 3.35)
            _exec(conn, f"ALTER TABLE {table} DROP COLUMN {legacy_col}")
            log.warning("migrate: удалена легаси-колонка %s.%s", table, legacy_col)
            continue
        except Exception:  # noqa: BLE001 — старый SQLite: пересоздаём таблицу
            pass
        # Пересоздание: берём ПЕРЕСЕЧЕНИЕ колонок модели и фактической таблицы.
        # Раньше брались все колонки модели — на очень старых БД (например, с
        # users.full_name вместо display_name) INSERT...SELECT падал с
        # "no such column", и старт обрывался. Отсутствующие колонки после
        # пересоздания добивает _sync_missing_columns (NULL-дефолты).
        actual = cols(table)
        if table not in Base.metadata.tables:
            # Легаси-таблица, которой больше нет в моделях (invites): просто удаляем.
            conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
            log.warning("migrate: удалена легаси-таблица %s (её больше нет в модели)", table)
            continue
        # Пересечение колонок модели и фактической схемы, с учётом переименований
        # (очень старые БД: full_name -> display_name на SQLite < 3.25, где
        # RENAME COLUMN не удался выше).
        rename_src = {v: k for k, v in ([_legacy_col_rename[table]] if table in _legacy_col_rename else [])}
        src_cols, dst_cols = [], []
        for c in Base.metadata.tables[table].columns:
            if c.name == legacy_col:
                continue
            if c.name in actual:
                src_cols.append(c.name); dst_cols.append(c.name)
            elif c.name in rename_src and rename_src[c.name] in actual:
                src_cols.append(rename_src[c.name]); dst_cols.append(c.name)
        # Предзаполнение колонок, которых в старой таблице нет: INSERT...SELECT
        # с NULL в NOT NULL-колонку (login/password_hash на очень старых БД)
        # падает ещё ДО пересоздания. Отдельно: строки легаси-учёток (яндекс-
        # аккаунты без локального входа) имеют login/password_hash IS NULL даже
        # когда колонки уже добавлены — COALESCE даёт им временный служебный
        # логин 'user_<id>'; seed._backfill_missing_logins доводит такие учётки
        # при старте (пустой хеш + must_change_password). Идемпотентно.
        nullable_fill = {"login": "'user_' || COALESCE(id, 0)",
                         "password_hash": "''",
                         # datetime-колонки с server_default func.now(): ''
                         # недопустим (CHECK на типе), подставляем текущее время
                         "created_at": "datetime('now')",
                         "updated_at": "datetime('now')"}
        for c in Base.metadata.tables[table].columns:
            if c.name in dst_cols or not src_cols:
                continue
            d = c.default.arg if (c.default is not None and
                                  not callable(c.default.arg)) else None
            if isinstance(d, bool):
                fill = "1" if d else "0"
            elif isinstance(d, (int, float)):
                fill = str(d)
            elif isinstance(d, str):
                fill = "'" + d.replace("'", "''") + "'"
            else:
                fill = "''"
            src_cols.append(f"(SELECT {fill})")
            dst_cols.append(c.name)
        for i, name in enumerate(dst_cols):
            if name in nullable_fill:
                expr = nullable_fill[name]
                if src_cols[i].startswith("("):
                    src_cols[i] = f"(SELECT COALESCE({src_cols[i][1:-1]}, {expr}))"
                else:
                    src_cols[i] = f"(SELECT COALESCE(\"{name}\", {expr}))"
        collist_src = ", ".join(
            s if s.startswith("(") else f'"{s}"' for s in src_cols)
        collist_dst = ", ".join(f'"{n}"' for n in dst_cols)
        tmp = f"{table}__migrated"
        # Пересоздание таблицы с FK-ссылками (event_log.user_id -> users.id и т.п.)
        # на SQLite требует временного отключения foreign_keys; PRAGMA вне
        # транзакции, поэтому делаем это через raw driver-соединение.
        fk_children = fks(table)
        # PRAGMA foreign_keys — per-connection; выполняем через тот же
        # async-путь SQLAlchemy (run_sync уже внутри зелёной корутины),
        # курсоры закрываем явно (недочитанный курсор aiosqlite роняет
        # следующий запрос с MissingGreenlet).
        def _pragma(sql: str):
            cur = conn.connection.cursor()
            try:
                conn.dialect.do_execute(cur, sql, {}, None)
                return cur.fetchone()
            finally:
                with contextlib.suppress(Exception):
                    cur.close()

        fk_prev = None
        if fk_children:
            row = _pragma("PRAGMA foreign_keys")
            fk_prev = row[0] if row else None
            _pragma("PRAGMA foreign_keys=OFF")
        try:
            # DDL колонок берём из метаданных модели: «сырая» копия без типов
            # превращала id в колонку без автоинкремента (SQLite: PK работает
            # только как INTEGER PRIMARY KEY) — первый же INSERT нового
            # пользователя падал с "UNIQUE constraint failed: users.login"
            # (id=0 конфликтовал повторно).
            tmp_tbl = schema.Table(tmp, schema.MetaData())
            for name in dst_cols:
                cdef = Base.metadata.tables[table].columns[name]
                autoinc = None
                if cdef.primary_key and isinstance(cdef.type, sa_types.Integer):
                    # Явно INTEGER PRIMARY KEY (rowid) — иначе пересозданная
                    # таблица теряет автоинкремент и первый INSERT падает с
                    # "NOT NULL constraint failed: users.id".
                    autoinc = True
                tmp_tbl.append_column(schema.Column(
                    name, cdef.type, primary_key=cdef.primary_key,
                    nullable=cdef.nullable, autoincrement=autoinc))
            coldefs = [str(schema.CreateColumn(c).compile(dialect=conn.dialect))
                       for c in tmp_tbl.columns]
            conn.execute(text(
                f"CREATE TABLE {tmp} ({', '.join(coldefs)})"))
            conn.execute(text(
                f"INSERT INTO {tmp} ({collist_dst}) SELECT {collist_src} FROM {table}"))
            conn.execute(text(f"DROP TABLE {table}"))
            conn.execute(text(f"ALTER TABLE {tmp} RENAME TO {table}"))
        finally:
            if fk_children and fk_prev:
                _pragma("PRAGMA foreign_keys=ON")
        log.warning("migrate: таблица %s пересоздана без легаси-колонки %s "
                    "(данные сохранены)", table, legacy_col)
        # Пересозданная таблица — «сырая» копия колонок модели без PK/UNIQUE/
        # индексов. Раньше ограничения «доживались» отдельным UNIQUE INDEX
        # (id, login): он удовлетворяет проверке FK у детей, но НЕ делает id
        # rowid-первичным ключом — автоинкремент не работал, и первый же
        # ORM-INSERT нового пользователя падал с
        # "NOT NULL constraint failed: users.id".
        # Поэтому строим полный DDL из метаданных модели (PK INTEGER
        # AUTOINCREMENT, NOT NULL, server_default, FK) и переносим данные из
        # «сырой» копии вторым проходом. Ограничения UNIQUE из модели при этом
        # НЕ переносим: на легаси-данных они могут конфликтовать (например,
        # несколько NULL в telegram_bind_code при column-level UNIQUE); уникаль-
        # ные индексы добираются отдельно (create_all их создаёт корректно).
        md_tbl = Base.metadata.tables[table]
        full_cols = []
        for c in md_tbl.columns:
            autoinc = None
            if c.primary_key and isinstance(c.type, sa_types.Integer):
                autoinc = True
            full_cols.append(schema.Column(
                c.name, c.type, primary_key=c.primary_key,
                nullable=c.nullable,
                default=c.default, server_default=c.server_default,
                autoincrement=autoinc))
        fks_list = [schema.ForeignKeyConstraint(
            [fk.parent.name for fk in cons.columns],
            [fk.target_fullname for fk in cons.referenced_columns],
            ondelete=cons.ondelete, onupdate=cons.onupdate)
            for cons in md_tbl.constraints
            if isinstance(cons, schema.ForeignKeyConstraint)]
        tmp2 = f"{table}__final"
        full_tbl = schema.Table(tmp2, schema.MetaData(), *full_cols, *fks_list)
        create_ddl = str(schema.CreateTable(full_tbl).compile(dialect=conn.dialect))
        collist_all = ", ".join(f'"{c.name}"' for c in md_tbl.columns)
        try:
            conn.execute(text(create_ddl))
            conn.execute(text(
                f"INSERT INTO {tmp2} ({collist_all}) SELECT {collist_dst} FROM {table}"))
            conn.execute(text(f"DROP TABLE {table}"))
            conn.execute(text(f"ALTER TABLE {tmp2} RENAME TO {table}"))
            log.warning("migrate: схема %s восстановлена из метаданных "
                        "(PK/автоинкремент/FK)", table)
        except Exception:  # noqa: BLE001 — если DDL не подошёл, оставляем сырую копию + индексы
            log.warning("migrate: не удалось применить полный DDL для %s, "
                        "остаётся сырая копия с восстановленными индексами", table,
                        exc_info=True)
            with contextlib.suppress(Exception):
                conn.execute(text(f"DROP TABLE IF EXISTS {tmp2}"))
            if table == "users":
                with contextlib.suppress(Exception):
                    conn.execute(text(
                        'CREATE UNIQUE INDEX IF NOT EXISTS ix_users_id_login ON users ("id", "login")'))
        if table == "users":
            # Уникальные ИНДЕКСЫ по метаданным (login и т.п.) — как резерв на
            # случай отказа полного DDL; create_all позже досоздаст обычные.
            for c in Base.metadata.tables["users"].columns:
                if c.index or c.unique:
                    with contextlib.suppress(Exception):
                        conn.execute(text(
                            f'CREATE UNIQUE INDEX IF NOT EXISTS "ix_users_{c.name}" ON users ("{c.name}")'))


def _sync_missing_columns(conn) -> None:
    """Самовосстановление схемы для легаси-БД, созданных старым create_all.

    Проблема на продакшене (Amvera): entrypoint при alembic_version==head выполняет
    `stamp head` вместо `upgrade`, поэтому новые колонки (например users.login)
    так и не добавляются, а приложение падает с "no such column". Здесь сверяем
    фактические колонки SQLite с метаданными моделей и досоздаём отсутствующие
    (ALTER TABLE ADD COLUMN — единственная безопасная операция без пересоздания таблиц;
    индексы добираются через create_all). Идемпотентно."""
    for table in Base.metadata.sorted_tables:
        # Сначала проверяем существование таблицы (SELECT, а не PRAGMA — он не бросает
        # исключение на несуществующей таблице), иначе create_all создаст её целиком.
        exists = conn.execute(text(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=:n", {"n": table.name}
        )).fetchone()
        if not exists:
            continue
        existing = {r[1] for r in conn.execute(text(f"PRAGMA table_info({table.name})"))}
        missing = [c for c in table.columns if c.name not in existing]
        for col in missing:
            coltype = col.type.compile(conn.dialect)
            ddl = f"ALTER TABLE {table.name} ADD COLUMN {col.name} {coltype}"
            if not col.nullable:
                # SQLite требует DEFAULT для NOT NULL при ADD COLUMN; дефолт
                # берём из Python-default модели (bool/int/str), иначе NULL.
                d = col.default.arg if (col.default is not None and
                                        not callable(col.default.arg)) else None
                sqldef = {True: "1", False: "0"}.get(d)
                if sqldef is None and isinstance(d, (int, float)):
                    sqldef = str(d)
                if sqldef is None and isinstance(d, str):
                    sqldef = "'" + d.replace("'", "''") + "'"
                if sqldef is None and d is not None:
                    sqldef = str(d)
                if sqldef is not None:
                    ddl += f" NOT NULL DEFAULT {sqldef}"
            conn.execute(text(ddl))
            log.warning("migrate: добавлена отсутствующая колонка %s.%s (%s)",
                        table.name, col.name, coltype)
        if missing:
            # добрать индексы новой колонки, если они объявлены в модели
            for idx in table.indexes:
                if all(c.name in {m.name for m in missing} for c in idx.columns):
                    idx.create(conn, checkfirst=True)


def _reconcile_event_log_fk(conn) -> None:
    """Самовосстановление «битых» FK у event_log (sqlite3.OperationalError:
    foreign key mismatch - "event_log" referencing "users").

    Причина на проде: users была пересоздана self-healing-кодом (или легаси-
    миграцией) с PK id_user вместо id, а event_log остался ссылаться на
    users(id). SQLite при INSERT проверяет, что ссылка FK совпадает с PK/
    UNIQUE-индексом родительской таблицы — несовпадение роняло любой логин
    (запись login_failed в журнал давала 500).

    Лечим идемпотентно и без потери данных:
      1. Если PK users называется не 'id' — переименовываем его в 'id'
         (ALTER TABLE ... RENAME COLUMN поддерживается SQLite >= 3.25; после
         rename ссылки всех детей автоматически указывают на users(id)).
      2. Иначе, если PK корректный, но таблица-ребёнок ссылается на несущест-
         вующую колонку — пересоздаём ребёнка по метаданным модели.
    """
    def cols(table: str) -> list[str]:
        return [r[1] for r in _rows(f"PRAGMA table_info({table})")]

    def _rows(sql: str, params=None):
        """Безопасное чтение PRAGMA/SELECT через run_sync на aiosqlite.

        SQLAlchemy 2.x + aiosqlite: курсор, оставленный недочитанным, роняет
        следующий запрос с 'MissingGreenlet' (зелёный корутины-воркера
        закрывается при возврате из run_sync). Поэтому вычитываем ВСЕ строки
        сразу в list и закрываем курсор.
        """
        cur = conn.execute(text(sql, params)) if params is not None \
            else conn.execute(text(sql))
        try:
            return list(cur.fetchall())
        finally:
            with contextlib.suppress(Exception):
                cur.close()

    def _write_ddl(sql: str) -> None:
        """DDL/DML без строк результата: выполняем и ЯВНО закрываем курсор.

        На aiosqlite недозакрытый курсор от «молчащего» exec_driver_sql
        (ALTER/UPDATE/CREATE...) оставляет зелёный DB-worker в состоянии, при
        котором следующий запрос внутри того же run_sync падает с
        MissingGreenlet ('greenlet_spawn has not been called'). Закрытие
        сбрасывает cursor_ до следующей команды.
        """
        cur = conn.execute(text(sql))
        with contextlib.suppress(Exception):
            cur.close()

    def pk_col(table: str) -> Optional[str]:
        for row in _rows(f"PRAGMA table_info({table})"):
            if row[5]:  # (cid, name, type, notnull, dflt_value, pk)
                return row[1]
        return None

    def has_table(name: str) -> bool:
        return _rows(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=:n",
            {"n": name}) != []

    if not has_table("users") or not has_table("event_log"):
        return

    # 1. PK users должен называться 'id' — иначе ни одна FK-ссылка users.id
    #    не резолвится (в т.ч. у freshly-created create_all таблиц).
    ucols = cols("users")
    if ucols and "id" not in ucols:
        pk = pk_col("users")
        if pk:
            try:
                _write_ddl(f'ALTER TABLE users RENAME COLUMN "{pk}" TO "id"')
                log.warning("migrate: PK users.%s переименован в users.id "
                            "(чинит FK-ссылки event_log и других детей)", pk)
            except Exception:  # noqa: BLE001 — старый SQLite: чиним детей ниже
                log.warning("migrate: не удалось переименовать users.%s в id",
                            pk, exc_info=True)

    # 2. Проверить прямые ссылки event_log -> users: колонка цели должна быть
    #    реальным PK/UNIQUE-колонкой parents. Не совпало — пересоздать event_log.
    target = None
    for r in _rows("PRAGMA foreign_key_list(event_log)"):
        if r[2] == "users":
            target = r[3]  # referenced column
            break
    if target is None:
        return  # FK нет (например, создан без ограничений) — ничего не делаем
    ucols_now = cols("users")
    pk_now = pk_col("users")
    valid_targets = {c for c in ucols_now if c == pk_now}
    # уникальные индексы тоже удовлетворяют проверке SQLite
    with contextlib.suppress(Exception):
        for row in _rows("PRAGMA index_list(users)"):
            uniq = row[2]
            if uniq:
                icols = [i[2] for i in _rows(
                    f"PRAGMA index_info('{row[1]}')")]
                if len(icols) == 1:
                    valid_targets.update(icols)
    if target in valid_targets:
        return

    log.warning("migrate: FK event_log->users(%s) не совпадает со схемой "
                "users (PK=%s) — пересоздаю event_log по модели", target, pk_now)
    md_tbl = Base.metadata.tables["event_log"]
    tmp = "event_log__fkfix"
    dbapi = conn.connection.connection if hasattr(conn.connection, "connection") else None
    fk_prev = None
    if dbapi is not None:
        fk_prev = dbapi.execute("PRAGMA foreign_keys").fetchone()[0]
        dbapi.execute("PRAGMA foreign_keys=OFF")
    try:
        fresh = schema.Table(tmp, schema.MetaData())
        for c in md_tbl.columns:
            autoinc = True if (c.primary_key and isinstance(c.type, sa_types.Integer)) else None
            fresh.append_column(schema.Column(
                c.name, c.type, primary_key=c.primary_key,
                nullable=c.nullable, default=c.default,
                server_default=c.server_default, autoincrement=autoinc))
        fks_list = [schema.ForeignKeyConstraint(
            [fk.parent.name for fk in cons.columns],
            [fk.target_fullname for fk in cons.referenced_columns],
            ondelete=cons.ondelete, onupdate=cons.onupdate)
            for cons in md_tbl.constraints
            if isinstance(cons, schema.ForeignKeyConstraint)]
        for cons in fks_list:
            fresh.append_constraint(cons)
        _write_ddl(str(schema.CreateTable(fresh).compile(dialect=conn.dialect)))
        old_cols = set(cols("event_log"))
        dst = [c.name for c in md_tbl.columns if c.name in old_cols]
        collist = ", ".join(f'"{n}"' for n in dst)
        _write_ddl(
            f"INSERT INTO {tmp} ({collist}) SELECT {collist} FROM event_log")
        _write_ddl("DROP TABLE event_log")
        _write_ddl(f"ALTER TABLE {tmp} RENAME TO event_log")
        for ix in md_tbl.indexes:
            with contextlib.suppress(Exception):
                ix.create(conn, checkfirst=True)
        log.warning("migrate: event_log пересоздана с корректным FK -> users.id "
                    "(данные журнала сохранены)")
    except Exception:  # noqa: BLE001 — худший случай: журнал остаётся битым, но старт не падал раньше
        log.exception("migrate: не удалось пересоздать event_log")
        with contextlib.suppress(Exception):
            _write_ddl(f"DROP TABLE IF EXISTS {tmp}")
    finally:
        if dbapi is not None and fk_prev:
            dbapi.execute("PRAGMA foreign_keys=ON")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Схема БД (SQLite строго в /data — путь из .env). Модели импортируем ДО create_all,
    #    иначе payload-таблицы модулей не попадут в метаданные.
    _import_all_models()
    async with engine.begin() as conn:
        # Сначала досоздаём недостающие колонки в существующих таблицах (легаси-БД после
        # `alembic stamp head`), затем create_all добавляет отсутствующие таблицы/индексы.
        await conn.run_sync(_drop_legacy_auth_columns)
        await conn.run_sync(_sync_missing_columns)
        await conn.run_sync(Base.metadata.create_all)
        # После create_all: чиним FK-несоответствия event_log -> users
        # (sqlite3 "foreign key mismatch", ронявший логин с 500 на проде).
        await conn.run_sync(_reconcile_event_log_fk)

    # 2. Динамическая регистрация модулей-плагинов + таблица module_registry + bootstrap
    registry.autodiscover("modules")
    registry.mount(app)
    registry.mount_pages(app)
    from app.db.seed import bootstrap
    async with SessionLocal() as s:
        await registry.sync_table(s)
        await bootstrap(s)

    # Диагностика доступа сразу при старте (видно в логах Amvera без входа):
    # сколько админов по логину/паролю видит процесс и какие учётки лежат в базе.
    from app.core.config import get_settings as _gs
    from app.models.core import User as _User
    from sqlalchemy import select as _select
    _admins = _gs().admin_login_entries
    async with SessionLocal() as s:
        _uids = (await s.execute(_select(_User.login, _User.role))).all()
    _st = _gs()
    log.info("access check at startup: admin logins=%s (ADMIN_USERNAME=%r, ADMIN_PASSWORD %s); "
             "users в БД: %s",
             [a[0] for a in _admins] or "не задан",
             _st.admin_username, "задан" if _st.admin_password else "НЕ задан",
             [(lgn, r.value) for lgn, r in _uids] or "—")
    if not _admins:
        log.warning("access check at startup: НЕ задано ни одной учётной записи администратора! "
                    "Задайте ADMIN_USERNAME + ADMIN_PASSWORD (вход по логину/паролю) — "
                    "иначе в систему нельзя войти как админ.")

    # 3. Jinja: подключить папки шаблонов модулей
    from app.ui import init_templates
    init_templates()

    # 4. Фоновые интеграции (не блокируют HTTP): outbox Telegram/Календарь + TG long-polling
    tasks: list[asyncio.Task] = []
    stop = asyncio.Event()
    if settings.workers_enabled:
        try:
            from integrations.workers import outbox_worker, tg_poller
            tasks.append(asyncio.create_task(outbox_worker(stop)))
            if settings.telegram_bot_token:
                tasks.append(asyncio.create_task(tg_poller(stop)))
        except Exception as e:  # noqa: BLE001
            log.warning("workers disabled: %s", e)

    yield

    stop.set()
    for t in tasks:
        t.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await t


app = FastAPI(title=settings.app_name, lifespan=lifespan, docs_url="/api/docs-openapi",
              redoc_url=None, openapi_url="/api/openapi.json")


@app.middleware("http")
async def security_headers(request, call_next):
    """Базовые заголовки безопасности (без CDN-зависимостей, CSP под локальные ресурсы)."""
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        # script-src 'unsafe-inline': интерфейс наследует вёрстку прототипа с inline-обработчиками
        # onclick="..." (сохранены 1:1 по ТЗ); скрипты и данные — только локальные (/static, JSON-блоки).
        "Content-Security-Policy",
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'",
    )
    return response

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
if STATIC_DIR.is_dir():
    class NoCacheStaticFiles(StaticFiles):
        """Статика без кэша: после деплоя браузер сразу получает свежий JS/CSS
        (иначе пользователи видят старый core.js из кэша — классика «кнопки не работают»)."""
        async def get_response(self, path, scope):
            resp = await super().get_response(path, scope)
            resp.headers["Cache-Control"] = "no-cache, must-revalidate"
            return resp

    app.mount("/static", NoCacheStaticFiles(directory=str(STATIC_DIR)), name="static")

# Роутеры ядра
from app.routers import admin, auth, departments, pages, sharing  # noqa: E402

app.include_router(auth.router)                    # /auth/*
app.include_router(pages.router)                   # /login, /, /api/profile/*
app.include_router(departments.router, prefix="/api/core", tags=["core-departments"])
app.include_router(sharing.router, prefix="/api/core", tags=["core-sharing"])
app.include_router(admin.router, prefix="/api/core", tags=["core-admin"])


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("unhandled error on %s", request.url.path)
    if request.url.path.startswith("/api"):
        return JSONResponse({"detail": "Внутренняя ошибка"}, status_code=500)
    return HTMLResponse("<h3>Ошибка сервера</h3>", status_code=500)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "modules": [s.key for s in registry.all()]}
