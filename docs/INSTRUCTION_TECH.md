---
title: Инструкция — Технические специалисты
order: 60
date: 2026-10-26
tag: Guide
---

# Инструкция для технических специалистов — система «ЦЕЗАРЬ»

Документ для разработчиков/DevOps: архитектура, запуск, миграции, RBAC-контуры,
интеграции, диагностика и эксплуатация.

---

## 1. Архитектура

Стек: **Python 3.12 + FastAPI + SQLAlchemy (async) + SQLite (aiosqlite)**, шаблоны
Jinja2, фоновые воркеры в процессе приложения.

```
app/                    # ядро
  core/                 # config (fail-fast), security (пароли/сессии), access (RBAC),
                        # entity_service, refdata (справочники), module_registry,
                        # permcache (acl_epoch), events (аудит), notify, meet_groups
  models/               # core.py (User/Department/Entity/Role...), module_payloads.py
  routers/              # auth, pages, admin, departments, sharing, refdata, notify, profile
  templates/            # login, base, admin_users, admin_events, integrations, change_password
modules/                # подключаемые модули (manifest + router + templates):
  dashboard tasks projects meetings admin_dept docs softs grants schols priority
integrations/           # yandex_calendar (WebDAV), telegram_service, tg_queries, workers
db/, alembic/           # движок БД, сиды, миграции
```

**Модель данных.** Единая полиморфная таблица `entities` (kind + payload_id) —
специфика модулей лежит в payload-таблицах (`modules/*/models`). Собственник
карточки — подразделение (изоляция пространств). Справочники — двухуровневые:
глобальные шаблоны (`department_id IS NULL`) и активированные копии пространств
(`source_global_id` обеспечивает преемственность при deactivate/activate).

**Реестр модулей:** новый модуль = пакет `modules/<name>/` с `manifest.py`,
вызывающим `registry.register(ModuleSpec(...))`; ядро не меняется. Реестр
синхронизируется с таблицей `module_registry` для UI администрирования; включение
модуля пространству — строка `workspace_modules.is_enabled`.

**Роли.** Глобальная роль пользователя (`users.role`: ADMIN / COORDINATOR / HEAD /
MEMBER) и роль в пространстве (`department_members.role_in_dept`: HEAD/MEMBER).
Синхронизацией занимается единственная точка входа `access.set_department_role`
(см. п. 4).

---

## 2. Аутентификация (актуальный контур)

**Только логин + пароль.** OAuth/Яндекс ID полностью удалены из кода; исторические
колонки (`users.yandex_id`, таблица `invites` и т.п.) пережитками не используются
и вычищаются self-healing-логикой старта/migrations.

* `POST /auth/login` — форма, проверка хеша, троттлинг (10 фейлов/IP за 15 мин →
  `/login?error=too_many`), события `login_failed`/`login_throttle` в аудит.
* При первом входе (`must_change_password`) — редирект на «Смену пароля».
* Сессия — подписаннный токен в cookie `HttpOnly/SameSite=Lax`, 14 дней, с полем
  `acl_epoch` (см. п. 4). `secure` включается в production или `COOKIE_SECURE=true`.
* Env-админ: `ADMIN_USERNAME`/`ADMIN_PASSWORD` (или `ADMIN_LOGINS=u:p,u2:p2`);
  сид `sync_admin_logins` при каждом старте создаёт/чинит учётку и **гарантирует
  роль ADMIN** (случайное понижение через API восстанавливается при следующем
  входе/старте).
* `POST /auth/logout`, `GET /auth/me`. Dev-логин (`/auth/dev-login`) существует
  только при `DEV_LOGIN_ENABLED=true` и заблокирован fail-fast в production.

Пароли — bcrypt-style хеши; app-пароли Яндекса — Fernet-шифротекст в БД.

---

## 3. Запуск и деплой

```bash
cp .env.example .env      # заполнить SECRET_KEY, ADMIN_USERNAME/PASSWORD
docker compose up -d --build
curl -s http://localhost:8000/healthz   # {"ok":true,"modules":[...]}
```

Локальная разработка без Docker: `uvicorn app.main:app --reload` (SQLite создаётся
в `DATABASE_PATH`, схема — `create_all` идемпотентно + bootstrap-сид). Демо-данные
только при `SEED_DEMO_DATA=true` в development.

**Fail-fast в production (`ENVIRONMENT=production`):** сервис не стартует, если
`SECRET_KEY` = `change-me` или короче 32 символов, включён `DEV_LOGIN` или
`SEED_DEMO_DATA`, не заданы `ADMIN_USERNAME`/`ADMIN_PASSWORD`.

Обязательные переменные (см. `.env.example`): `SECRET_KEY`, `ADMIN_USERNAME`,
`ADMIN_PASSWORD`, `DATABASE_PATH=/data/caesar.db`, `DATA_DIR=/data`; опционально
`TELEGRAM_BOT_TOKEN`, `YANDEX_CAL_*`, `SYNC_INTERVAL_SECONDS`, `COOKIE_SECURE`,
`PUBLIC_BASE_URL`.

Reverse-proxy с TLS (nginx/traefik/Caddy) обязателен в production: приложение
слушает HTTP :8000, uvicorn работает с `--proxy-headers`, чтобы cookie получала
флаг Secure. Сетевые зависимости исходящего трафика: `api.telegram.org`,
`webdav.yandex.ru` (OAuth-домены больше не нужны).

**Один экземпляр!** SQLite + in-process воркеры (notifier, outbox, tg_poller,
календарный diff-проход) рассчитаны на ровно один процесс сервиса; несколько копий
с общей БД дают `database is locked` и рассинхрон кэша прав. План роста >~50
пользователей: PostgreSQL + внешний воркер (SessionLocal изолирован).

Данные живут только в volume `caesar-data` (`/data`). Бэкап: `VACUUM INTO` снапшот
`caesar.db` ежедневно; восстановление — заменить файл, удалить `-wal/-shm`,
перезапустить. Обновление: `git pull && docker compose up -d --build`.

---

## 4. RBAC и кэш прав (acl_epoch)

`app/core/access.py` — вся модель прав; `permcache.py` — epoch инвалидации сессий:

* каждая мутация состава/ролей вызывает `bump_epoch` + перевыпуск cookie;
* ответ `add_member` дополнительно ставит свежую session-cookie с актуальным
  epoch (`force=True`, минуя process-local TTL-mirror ~5 сек) — иначе при
  нескольких процессах перезагрузка страницы могла рендериться по устаревшей
  эпохе («роль отображается старой»);
* `set_department_role` — единственная точка изменения `role_in_dept`:
  инвариант «≥1 HEAD в пространстве» (нарушение → HTTP 409 с текстом),
  синхронизация глобальной роли (HEAD ↔ users.role), запрет повышения до HEAD
  для не-глобальных акторов (`allow_head_promotion=ctx.is_global`).

**Проверка пространства при создании карточек** (`_pick_department` во всех
модулях): порядок такой — (1) если выбранное пространство входит в список
пространств пользователя (membership), доступ разрешён независимо от глобальной
роли; (2) явный выбор чужого пространства доступен только ADMIN/COORDINATOR
(иначе 403 «выбор чужого пространства…»). Исторический баг: раньше membership не
проверялся и руководитель получал 403 в собственном пространстве — следите, чтобы
новые модули наследовали исправленную логику.

Регистр ролей в шаблонах/API — значения enum (`admin`, `coordinator`, `head`,
`member`); нормализация входных значений — `_norm_role` в pydantic-моделях.

---

## 5. Миграции и self-healing схемы

* Схема управляется **Alembic**: базовая ревизия `baseline` создаёт ядро+модули.
  Entrypoint контейнера определяет состояние `/data`: новая БД → `upgrade head`;
  легаси-БД без `alembic_version` → `stamp baseline` + `upgrade head` (данные не
  трогаются). Приложение при старте дополнительно досоздаёт отсутствующие колонки
  (`ALTER TABLE ADD COLUMN`) — защита от ложного `stamp head`.
* Новая колонка/таблица: `alembic revision --autogenerate -m "..."`, проверить
  (SQLite — batch-режим), коммитить вместе с моделью.
* Удаление легаси-колонок OAuth/invites выполняется само-исцеляющейся логикой
  старта — не добавляйте обратных зависимостей.
* Тесты: `pytest` (e2e-кейсы RBAC: создание карточки HEAD в своём пространстве с
  явным `department_id`, мгновенная смена роли, 409 при понижении последнего HEAD).

---

## 6. Интеграции

**Telegram** (`integrations/telegram_service.py`): long-polling-воркер; привязка
по схеме «бот запросил логин → пароль» (без кодов), сообщения привязки удаляются
из чата, неизвестный логин молча игнорируется (защита от разведки). Команды:
`/mute`, `/unmute`, `/help`. Без `TELEGRAM_BOT_TOKEN` уведомления просто не
отправляются. Отправка — через outbox-таблицу (не теряет события при простое).

**Яндекс Календарь** (`integrations/yandex_calendar.py`): WebDAV/CalDAV
(`webdav.yandex.ru`), Basic-auth логином аккаунта Яндекса + паролем приложения
(Fernet в БД, вводится пользователем на странице «Интеграции» — вход в ЦЕЗАРЬ к
Яндексу не относится). Diff-проход воркера каждые `SYNC_INTERVAL_SECONDS`
переводит PENDING-карточки в SYNCED; состояния `SyncState`, ошибки вида 401/403
пишутся в статус пользователя, не блокируя остальных. Старый адрес `dav.yandex.ru`
не поддерживается.

**Уведомления-сценарии** (`app/routers/notify.py`): базовые сценарии
(неотключаемые) + пользовательские (событие→audience→шаблон), broadcast с
планированием/отменой, предпросмотр получателей.

---

## 7. Диагностика

| Симптом | Где искать |
|---|---|
| 403 при создании в своём пространстве | membership-часть `_pick_department`; роль в `department_members`; аудит `login/add_member` |
| Роль применилась, но UI старый | `acl_epoch` в cookie vs БД; несколько процессов сервиса; force-перевыпуск cookie в ответе мутации |
| Не понижается последний HEAD | Штатный 409 из `set_department_role`; текст должен показываться в UI |
| Контейнер не стартует | Логи fail-fast (SECRET_KEY/ADMIN_* /DEV-флаги) |
| `database is locked` | Два процесса на одном `/data` |
| Нет уведомлений TG | Статус воркера/токена, `telegram_chat_id`, optout, outbox |
| Календарь не обновляется | Привязка app-пароля, статус `cal_error`/PENDING, период SYNC_INTERVAL |
| Cookie слетает | HTTPS/`X-Forwarded-Proto`, `COOKIE_SECURE` |

Журнал событий: страница `/admin/events` (фильтры по категории/поиску) и таблица
events в БД; логи приложения — `docker compose logs -f caesar` (категории
`caesar.auth`, интеграции).
