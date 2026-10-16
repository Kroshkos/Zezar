#!/bin/sh
# Entrypoint сервиса «ЦЕЗАРЬ»: миграция схемы + запуск uvicorn.
#
# Логика инициализации SQLite-файла в /data:
#   1) файла БД нет                          -> `alembic upgrade head` (чистая схема из миграций);
#      приложение дополнительно выполнит create_all (идемпотентен) и bootstrap/seed;
#   2) файл есть, но таблицы users отсутствуют-> пустой/битый файл: upgrade head создаст схему;
#   3) файл есть, схема есть, alembic_version нет
#                                            -> легаси-БД, созданная create_all до Alembic:
#      привязываем её к базовой ревизии (`stamp head`) без изменения данных;
#   4) файл есть и alembic_version есть      -> обычный апгрейд до head.
set -e

DB="${DATABASE_PATH:-/data/caesar.db}"

# Проверки состояния файла БД — через встроенный sqlite3 модуль Python
# (CLI-утилита sqlite3 в образе python:*-slim отсутствует).
db_has_schema() {
    python - "$1" <<'PY'
import sqlite3, sys
try:
    con = sqlite3.connect(sys.argv[1])
    con.execute("SELECT 1 FROM users LIMIT 1")
except Exception:
    sys.exit(1)
sys.exit(0)
PY
}

db_has_alembic_version() {
    python - "$1" <<'PY'
import sqlite3, sys
try:
    con = sqlite3.connect(sys.argv[1])
    con.execute("SELECT 1 FROM alembic_version LIMIT 1")
except Exception:
    sys.exit(1)
sys.exit(0)
PY
}

# Состояние легаси-БД: alembic_version == head, но схема НЕ соответствует
# текущим ревизиям. Признаки: в users есть колонка yandex_id (авторизация через
# Яндекс ID полностью удалена ревизией c9e4f18a7d02) либо отсутствует колонка
# login (локальный вход по логину/паролю). Такой БД нужен реальный upgrade
# с baseline, а не stamp.
db_is_legacy_schema() {
    python - "$1" <<'PYEOF'
import sqlite3, sys
try:
    con = sqlite3.connect(sys.argv[1])
    cols = {r[1] for r in con.execute("PRAGMA table_info(users)")}
except Exception:
    sys.exit(0)  # не смогли прочитать — считаем не легаси, upgrade сам разберётся
sys.exit(0 if ("yandex_id" in cols or "login" not in cols) else 1)
PYEOF
}

if [ ! -f "$DB" ]; then
    echo "[entrypoint] БД $DB отсутствует — применяю миграции с нуля"
    alembic upgrade head
elif ! db_has_schema "$DB"; then
    echo "[entrypoint] В $DB нет схемы (новая/пустая база) — применяю миграции"
    alembic upgrade head
elif ! db_has_alembic_version "$DB"; then
    echo "[entrypoint] Легаси-БД без alembic_version — штамвую базовую ревизию (данные не трогаю)"
    alembic stamp 0ad0082f05da
    echo "[entrypoint] Применяю последующие ревизии"
    alembic upgrade head
elif db_is_legacy_schema "$DB"; then
    # Ситуация с продакшена: версия=head, но колонки новых ревизий отсутствуют
    # (БД была наштампована на head целиком). Ставим корректную базовую ревизию
    # и догоняем схему upgrade'ом; миграции идемпотентны к данным.
    echo "[entrypoint] Обнаружена легаси-БД (head со старой схемой auth) — перештамповываю на baseline и выполняю upgrade"
    alembic stamp 0ad0082f05da
    alembic upgrade head
else
    echo "[entrypoint] Обновляю схему до head"
    alembic upgrade head
fi

echo "[entrypoint] Запуск uvicorn на порту ${PORT:-8000}"
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" \
    --proxy-headers --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}"
