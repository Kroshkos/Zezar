---
title: Деплой и эксплуатация
order: 70
date: 2026-10-26
tag: DevOps
---

# Инструкция по деплою и эксплуатации сервиса «ЦЕЗАРЬ»

Документ для системного администратора/DevOps: полный путь от клонирования
репозитория до продакшен-инстанса. Авторизация в системе — **только логин +
пароль** (OAuth/Яндекс ID удалены); интеграции — Telegram-бот и Яндекс Календарь
(WebDAV).

---

## 1. Требования к окружению

| Компонент | Минимум | Комментарий |
|---|---|---|
| ОС | Linux с Docker 24+ | или Python 3.12 для bare-metal |
| CPU/RAM | 1 vCPU / 512 MB | SQLite + asyncio |
| Диск | ≥ 2 GB под volume `caesar-data` (`/data`) | БД, WAL, бэкапы |
| Сеть | исходящий HTTPS к `api.telegram.org`, `webdav.yandex.ru` | фоновые воркеры; OAuth-домены не нужны |
| TLS | reverse-proxy (nginx/traefik/Caddy) | приложение отдаёт HTTP на :8000 |

---

## 2. Получение кода и сборка

```bash
git clone <repo-url> caesar && cd caesar
docker compose build   # python:3.12-slim, non-root appuser, healthcheck /healthz
```

Схема БД управляется **Alembic** (базовая ревизия `baseline`). Entrypoint при
запуске определяет состояние `/data`: новая база → `alembic upgrade head`;
легаси-база без таблицы `alembic_version` → штамп `baseline` + `upgrade head`
(данные не трогаются). Приложение при старте также досоздаёт отсутствующие
колонки (`ALTER TABLE ADD COLUMN`) — self-healing поверх старых баз. Для локальной
разработки без Docker схема создаётся `create_all` идемпотентно.

---

## 3. Настройка `.env` (рядом с docker-compose.yml)

```bash
# --- Секрет подписи сессий (ОБЯЗАТЕЛЬНО) ---
SECRET_KEY=<python -c "import secrets;print(secrets.token_hex(32))">

# --- Администратор: единственный контур входа — логин + пароль ---
ADMIN_USERNAME=admin
ADMIN_PASSWORD=<стойкий пароль>          # система сама хеширует при сиде
# ADMIN_LOGINS=user1:pass1,user2:pass2   # несколько env-админов (необязательно)

# --- Telegram-бот (опционально; без токена уведомления просто выключены) ---
TELEGRAM_BOT_TOKEN=1234567890:AA...
TELEGRAM_NOTIFY_ENABLED=true

# --- Яндекс.Календарь (WebDAV) ---
YANDEX_CAL_ENABLED=true
YANDEX_CAL_BASE=https://webdav.yandex.ru
SYNC_INTERVAL_SECONDS=300                # период diff-прохода

# --- Продакшен ---
ENVIRONMENT=production                   # включает fail-fast (см. ниже)
# COOKIE_SECURE=true                     # если TLS терминируется балансировщиком
SEED_DEMO_DATA=false
DEV_LOGIN_ENABLED=false
```

Полный список параметров и дефолты — в [.env.example](../.env.example).

**Fail-fast правила production** (нарушение любого → контейнер не стартует с
понятной ошибкой в логе):

* `SECRET_KEY` ≠ `change-me` и длина ≥ 32;
* заданы `ADMIN_USERNAME` и `ADMIN_PASSWORD`;
* `DEV_LOGIN_ENABLED=false` и `SEED_DEMO_DATA=false`.

Переменные `YANDEX_CLIENT_ID/SECRET/REDIRECT_URI` и `BOOTSTRAP_ADMIN_YA_IDS` из
старых версий **больше не используются** — удалите их из `.env`.

---

## 4. Первый запуск

```bash
docker compose up -d
docker compose ps                       # состояние healthy (healthcheck /healthz)
curl -s https://caesar.example.org/healthz
# {"ok":true,"modules":["dashboard","tasks","projects","meetings",...]}
```

Первый вход: откройте `/login` и войдите под `ADMIN_USERNAME`/`ADMIN_PASSWORD`
(сид создаёт/синхронизирует эту учётную запись при старте; роль ADMIN гарантирована).

Дальше по INSTRUCTION_ADMIN.md, разд. «Первые шаги»: создать подразделения →
пользователей с начальными паролями → назначить руководителям роли HEAD →
настроить глобальные справочники и уведомления. Обычные пользователи получают
от администратора адрес, логин и начальный пароль; при первом входе система
принудительно требует смену пароля. Самостоятельная регистрация и приглашения по
ссылкам отсутствуют.

---

## 5. Telegram-бот

1. [@BotFather](https://t.me/BotFather) → `/newbot` → токен в `TELEGRAM_BOT_TOKEN`.
2. Команды бота (по желанию зарегистрировать через BotFather): `/start`,
   `/mute`, `/unmute`, `/help`.
3. Пользователи привязываются сами: бот запрашивает логин и пароль ЦЕЗАРЬ и
   удаляет эти сообщения из диалога (без кодов подтверждения). Без токена сервис
   работает штатно, рассылка выключена.

---

## 6. Reverse-proxy и TLS (nginx пример)

```nginx
server {
    listen 443 ssl http2;
    server_name caesar.example.org;
    ssl_certificate     /etc/letsencrypt/live/caesar.example.org/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/caesar.example.org/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host              $host;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;   # важно: uvicorn --proxy-headers
    }
}
server { listen 80; server_name caesar.example.org; return 301 https://$host$request_uri; }
```

Uvicorn внутри контейнера запущен с `--proxy-headers --forwarded-allow-ips *` и
корректно ставит cookie с флагом `Secure` за HTTPS-прокси.

---

## 7. Данные, бэкапы, обновление

Все данные — только в Docker volume `caesar-data` (внутри контейнера `/data`):
SQLite `caesar.db` (+WAL). Пересборка контейнера данные не трогает.

```bash
# Ежедневный консистентный снапшот (sqlite3 обязателен при WAL)
docker compose exec caesar python -c "import sqlite3;sqlite3.connect('/data/caesar.db').execute(\"VACUUM INTO '/data/backup-$(date +%F).db'\")"
docker run --rm -v caesar_caesar-data:/data -v $(pwd):/out alpine cp /data/backup-*.db /out/

# Восстановление: остановить сервис, заменить caesar.db, удалить .db-wal/.db-shm, запустить
```

Обновление:

```bash
git pull && docker compose up -d --build
```

Совместимость схемы: при добавлении колонок/таблиц коммитьте alembic-ревизию —
контейнер применит `upgrade head` сам.

**Масштабирование:** сервис stateful-однопоточный по дизайну (SQLite + in-process
воркеры: notifier, outbox, tg_poller, календарный diff). Запускайте ровно **один**
экземпляр контейнера; несколько копий с общей файловой БД недопустимы (`database is
locked`, рассинхрон кэша прав acl_epoch). При росте > ~50 активных пользователей —
миграция на PostgreSQL + внешний воркер (архитектурно заложена).

---

## 8. Диагностика типовых проблем

| Симптом | Причина / решение |
|---|---|
| Контейнер падает на старте | Нарушены fail-fast-правила п. 3 — исправьте `.env` |
| Вход: «Неверный логин или пароль» | Пользователя нет в БД / неверные данные; создайте учётку на странице «Администрирование» |
| «Слишком много неудачных попыток» | Троттлинг IP (10 фейлов/15 мин) — подождите или разберитесь в журнале событий |
| Cookie слетает между запросами | Нет HTTPS / не проброшен `X-Forwarded-Proto`; проверьте proxy и `COOKIE_SECURE` |
| События не появляются в Яндекс Календаре | Пользователь не привязал пароль приложения (страница «Интеграции»); смотрите статусы `cal_error`/`PENDING` |
| Telegram-уведомления не приходят | Не задан токен, пользователь не привязал чат или включил `/mute` |
| `database is locked` | Несколько процессов пишут в один файл — оставьте один контейнер |
| После смены роли интерфейс «старый» | Проверьте единство экземпляра/БД; ответ мутации перевыпускает cookie с актуальным epoch |

Логи: `docker compose logs -f caesar`; аудит действий — страница `/admin/events`.
