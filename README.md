# ЦЕЗАРЬ — модульный веб-сервис управления подразделениями

Веб-сервис, превращающий статический прототип `sipm_lite_v3.html` в полноценное
многопользовательское приложение с ролевой моделью, изолированными рабочими
пространствами (подразделениями), межпространственным шерингом сущностей и
интеграциями с Яндекс ID / Яндекс.Календарём / Telegram.

## Технологии

| Слой | Решение |
|---|---|
| Backend | Python 3.12, FastAPI (async) |
| БД | SQLite (файл строго в `/data`, путь через `.env`), SQLAlchemy 2.x async + aiosqlite |
| Frontend | Оригинальная вёрстка прототипа + Jinja2 SSR + Vanilla JS (без React/Vue) |
| Auth | OAuth 2.0 через Яндекс ID; регистрация закрыта (допуск по invite/списку админа) |
| Фоновые задачи | Transactional Outbox + asyncio-воркеры в lifespan (Telegram, CalDAV-синхронизация) |
| Инфраструктура | Docker / docker-compose, non-root контейнер, healthcheck |

## Архитектура

Приложение построено по принципу плагинов. Ядро (`app/`) ничего не знает о
содержимом модулей: каждый модуль — пакет `modules/<key>/manifest.py`,
объявляющий `ModuleSpec` (название, иконка, порядок, REST-роутер, HTML-страницы,
payload-модель, календарный маппер). Реестр подключает их динамически при старте.

**Базовые модули каждого рабочего пространства** (нельзя отключить):
Дашборд, Задачи, Проектная работа, Совещания, Администрирование подразделения.
**Дополнительные модули** (включаются/выключаются на пространство):
Документы, ПО и разработки, Гранты, Стипендии, Приоритеты.

### Ключевые сущности БД

* `User` — роль `ADMIN | COORDINATOR | HEAD | MEMBER`, `yandex_id`, привязки
  `telegram_chat_id` и зашифрованный (Fernet) app-пароль Яндекс.Диска/Календаря.
* `Department → Workspace → WorkspaceModule` — изолированное пространство.
* `Entity` — полиморфный реестр карточек всех модулей (title/status/priority/due/tags);
  специфика модуля — в его `*_payload` (1:1). Единый механизм шеринга, ответственных,
  аудита и интеграций работает для любого модуля без правок ядра.
* `EntityShare` — шеринг сущности другому подразделению (`READ | EDIT`).
* `EntityResponsible` — **по одному ответственному от каждого участвующего
  подразделения** (инвариант обеспечивается на уровне сервиса).
* `AuditLog` + `OutboxEvent` — транзакционная очередь фоновых задач.

### Интеграции

* **Яндекс.Календарь (WebDAV/CalDAV):** сущности ответственных автоматически
  создаются/обновляются/удаляются как события в личном календаре «Цезарь».
  UID детерминирован → идемпотентность. Область: Member — свои, Head — подразделение,
  Coordinator/Admin — система.
* **Telegram:** уведомления о создании/редактировании/смене статуса/назначении
  ответственного с той же областью видимости; бот поддерживает `/start <код>`
  (привязка), `/tasks`, `/mute`, `/unmute`.

Подробная архитектура — в [docs/STEP1_ARCHITECTURE.md](docs/STEP1_ARCHITECTURE.md).

## Структура проекта

```
app/                # ядро: config, security, oauth, RBAC (access.py), реестр модулей,
                    # модели, роутеры (auth/departments/sharing/admin/pages), шаблоны
modules/            # плагины: dashboard, tasks, projects, meetings, admin_dept,
                    # docs, softs, grants, schols, priority
integrations/       # yandex_calendar (WebDAV), telegram_service, workers (outbox + TG poller)
scripts/            # e2e_test.py — автотесты (16 сценариев)
static/             # CSS/JS из прототипа
docs/               # архитектурные заметки
Dockerfile, docker-compose.yml, requirements.txt, .env.example
```

## Быстрый старт (development)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                      # заполните SECRET_KEY и опционально креды
mkdir -p data && export DATABASE_PATH=./data/caesar.db   # локально вне Docker
uvicorn app.main:app --reload
# http://localhost:8000  (dev-вход и демо-сид включены только в development)
```

## Тесты

```bash
PYTHONPATH=. python scripts/e2e_test.py
```

16 сквозных сценариев: OAuth/dev-вход, RBAC, CRUD модулей, outbox-воркер,
шеринг и изоляция пространств, инвариант ответственных, ICS/время совещаний.

## Деплой в продакшен

См. подробную инструкцию: **[docs/DEPLOY.md](docs/DEPLOY.md)**
(OAuth Яндекс ID, Telegram-бот, TLS/reverse-proxy, бэкапы).

**Миграции схемы — Alembic**: базовая ревизия `alembic/versions/*baseline*` создаёт всю
схему (ядро + payload-таблицы модулей); контейнер при старте сам применяет
`alembic upgrade head` (легаси-БД автоматически штампуется через `stamp head`).
Новая колонка в модели → `alembic revision --autogenerate -m "..."` вместе с кодом.

Готовые варианты развёртывания:

| Вариант | Файлы | Особенности |
|---|---|---|
| **Amvera** (облачный PaaS) | `amvera.yaml` (окружение `python`/toolchain `pip`) | сборка через venv+pip по `requirements.txt`; ASGI-запуск uvicorn на порту 8080 (`containerPort`); стандартный путь БД `/data/caesar.db` — постоянное хранилище монтируется в `/data` параметром `run.persistenceMount` (по доке [Хранилище и пути](https://docs.amvera.ru/applications/storage.html)). `Dockerfile` — для self-hosted. Инструкция — DEPLOY.md, п. 10 |
| **Self-hosted VPS** | `docker-compose.yml` + `Dockerfile` | свой nginx/TLS, volume `caesar-data:/data` |

Инструкция для конечных пользователей: **[docs/USER_GUIDE.md](docs/USER_GUIDE.md)**.

## Безопасность

* В `ENVIRONMENT=production` приложение **не стартует** (fail-fast), если
  `SECRET_KEY` дефолтный/короткий (<32 символов), включён dev-вход или демо-сид,
  не заданы OAuth-креденшелы.
* Session-cookie подписан HMAC-SHA256, флаг `Secure` обязателен за HTTPS.
* App-пароли Яндекс хранятся в БД только в виде Fernet-шифротекста.
* CSP/XFO/nosniff выдаются middleware'ом; секреты — только через `.env`.
