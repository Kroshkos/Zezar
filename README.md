# ЦЕЗАРЬ — модульный веб-сервис управления подразделениями

Веб-сервис, превращающий статический прототип `sipm_lite_v3.html` в полноценное
многопользовательское приложение с ролевой моделью, изолированными рабочими
пространствами (подразделениями), межпространственным шерингом сущностей и
интеграциями с Яндекс.Календарём и Telegram.

Автор: Коршунов Константин Сергеевич

## Технологии

| Слой | Решение |
|---|---|
| Backend | Python 3.12, FastAPI (async) |
| БД | SQLite (файл строго в `/data`, путь через `.env`), SQLAlchemy 2.x async + aiosqlite |
| Frontend | Оригинальная вёрстка прототипа + Jinja2 SSR + Vanilla JS (без React/Vue) |
| Auth | Логин + пароль (OAuth Яндекс ID удалён из системы); регистрация закрыта — учётные записи создаёт администратор; env-админ через `ADMIN_USERNAME`/`ADMIN_PASSWORD` |
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

* `User` — роль `ADMIN | COORDINATOR | HEAD | MEMBER`, логин и хеш пароля,
  привязка `telegram_chat_id` и зашифрованный (Fernet) app-пароль Яндекс.Календаря.
* `Department → Workspace → WorkspaceModule` — изолированное пространство.
* `Entity` — полиморфный реестр карточек всех модулей (title/status/priority/due/tags);
  специфика модуля — в его `*_payload` (1:1). Единый механизм шеринга, ответственных,
  аудита и интеграций работает для любого модуля без правок ядра.
* `EntityShare` — шеринг сущности другому подразделению (`READ | EDIT | FULL`).
* `EntityResponsible` — **по одному ответственному от каждого участвующего
  подразделения** (инвариант обеспечивается на уровне сервиса).
* `AuditLog` + `OutboxEvent` — транзакционная очередь фоновых задач.

### Интеграции

* **Яндекс.Календарь (WebDAV/CalDAV):** сущности ответственных автоматически
  создаются/обновляются/удаляются как события в личном календаре «Цезарь».
  UID детерминирован → идемпотентность. Область: Member — свои, Head — подразделение,
  Coordinator/Admin — система.
* **Telegram:** уведомления о создании/редактировании/смене статуса/назначении
  ответственного с той же областью видимости; привязка без кодов — бот запрашивает
  логин и пароль ЦЕЗАРЬ и удаляет сообщения из диалога; команды `/mute`, `/unmute`,
  `/help`.

Подробная архитектура — в [docs/STEP1_ARCHITECTURE.md](docs/STEP1_ARCHITECTURE.md).

## Структура проекта

```
app/                # ядро: config, security (логин+пароль), RBAC (access.py), реестр модулей,
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

16 сквозных сценариев: вход по логину/паролю, RBAC, CRUD модулей, outbox-воркер,
шеринг и изоляция пространств, инвариант ответственных, ICS/время совещаний.

## Деплой в продакшен

См. подробную инструкцию: **[docs/DEPLOY.md](docs/DEPLOY.md)**
(переменные окружения, Telegram-бот, TLS/reverse-proxy, бэкапы).

**Миграции схемы — Alembic**: базовая ревизия `alembic/versions/*baseline*` создаёт всю
схему (ядро + payload-таблицы модулей); контейнер при старте сам применяет
`alembic upgrade head` (легаси-БД автоматически штампуется через `stamp head`).
Новая колонка в модели → `alembic revision --autogenerate -m "..."` вместе с кодом.

Готовые варианты развёртывания:

| Вариант | Файлы | Особенности |
|---|---|---|
| **Amvera** (облачный PaaS) | `amvera.yaml` (окружение `python`/toolchain `pip`) | сборка через venv+pip по `requirements.txt`; ASGI-запуск uvicorn на порту 8080 (`containerPort`); стандартный путь БД `/data/caesar.db` — постоянное хранилище монтируется в `/data` параметром `run.persistenceMount` (по доке [Хранилище и пути](https://docs.amvera.ru/applications/storage.html)). `Dockerfile` — для self-hosted. Инструкция — DEPLOY.md, п. 10 |
| **Self-hosted VPS** | `docker-compose.yml` + `Dockerfile` | свой nginx/TLS, volume `caesar-data:/data` |

## Инструкции по ролям

| Аудитория | Документ | Ключевые темы |
|---|---|---|
| Пользователь (участник, MEMBER) | [docs/INSTRUCTION_USER.md](docs/INSTRUCTION_USER.md) | вход по логину и паролю, свои карточки и статусы, ответственные, Telegram/Календарь, частые ситуации |
| Руководитель подразделения (HEAD) | [docs/INSTRUCTION_HEAD.md](docs/INSTRUCTION_HEAD.md) | создание задач/проектов в своих пространствах, администрирование подразделения, роли участников (повышение/понижение), шеринг, контроль потока |
| Координатор (COORDINATOR) | [docs/INSTRUCTION_COORDINATOR.md](docs/INSTRUCTION_COORDINATOR.md) | агрегированный дашборд, работа в любом пространстве, помощь руководителям, инварианты системы |
| Администратор (ADMIN) | [docs/INSTRUCTION_ADMIN.md](docs/INSTRUCTION_ADMIN.md) | общая админка `/admin`: пользователи, пароли и глобальные роли, подразделения, аудит, безопасность |
| Технические специалисты (dev/devops/support) | [docs/INSTRUCTION_TECH.md](docs/INSTRUCTION_TECH.md) | сборка и запуск, миграции Alembic, тесты, RBAC-модель и кэш прав (`permcache`), диагностика типовых проблем |

Все инструкции учитывают актуальное поведение системы:
руководитель может создавать сущности в любом пространстве, где он состоит
(явный выбор своего пространства разрешён); роль в пространстве применяется
немедленно (перевыпуск cookie с новым `acl_epoch`); понижение последнего
руководителя запрещено и сопровождается понятным сообщением в интерфейсе.

Полное руководство пользователя: **[docs/USER_GUIDE.md](docs/USER_GUIDE.md)**.

## Безопасность

* В `ENVIRONMENT=production` приложение **не стартует** (fail-fast), если
  `SECRET_KEY` дефолтный/короткий (<32 символов), включён dev-вход или демо-сид,
  не задан администратор (`ADMIN_USERNAME`/`ADMIN_PASSWORD`).
* Session-cookie подписан HMAC-SHA256, флаг `Secure` обязателен за HTTPS.
* App-пароли Яндекс хранятся в БД только в виде Fernet-шифротекста.
* CSP/XFO/nosniff выдаются middleware'ом; секреты — только через `.env`.

## Сайт документации и новостей

Сайт вынесен в **отдельный репозиторий**: https://github.com/Kroshkos/zezar.docs

Схема работы (по желанию владельца):
* папка `news/*.md` в том репозитории → раздел «Новости» на сайте;
* папка `docs/*.md` → раздел «Документация»;
* при деплое на Amvera генератор `content_site.py` собирает всё в `static/content.json`.

Формат front-matter (`title`, `date`, `order`, `tag`) и инструкция по добавлению
материалов — в README того репозитория. В этом приложении путь `/docs` делает
редирект на внешний сайт (URL задаётся в константе `_DOCS_SITE_URL` в `app/main.py`).

логика — в `app/content_site.py`, эндпоинт данных — `GET /docs/content.json`.
