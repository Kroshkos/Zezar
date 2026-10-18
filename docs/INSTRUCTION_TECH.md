# Инструкция для технических специалистов — «ЦЕЗАРЬ»

Для разработчиков, DevOps и технической поддержки: сборка, запуск, миграции,
тестирование, отладка типовых проблем прав доступа.

---

## 1. Стек и структура

* Python 3.12 + FastAPI (async), SQLAlchemy 2.x async + aiosqlite, Jinja2 SSR + Vanilla JS.
* SQLite строго в `/data` (путь из `DATABASE_PATH` в `.env`).
* Архитектура плагиновая: ядро `app/` не знает о модулях; каждый модуль —
  `modules/<key>/manifest.py` с `ModuleSpec` (роутер, страницы, payload-модель,
  календарный маппер). Реестр подключает модули на старте; роутер монтируется
  дважды: REST на `/api/<key>`, HTML на `/<key>`.

```
app/core/        config, security (сессии HMAC), access.py (RBAC), permcache.py,
                 events.py (аутлог+outbox), module_registry
app/models/      core.py (User/Role/Department/Workspace/Entity/AuditLog/OutboxEvent)
app/routers/     auth, departments, sharing, admin, pages, profile, notify, refdata
modules/         dashboard tasks projects meetings admin_dept docs softs grants schols priority
integrations/    yandex_calendar (WebDAV/CalDAV), telegram_service, workers (outbox + TG poller)
scripts/         e2e_test.py и др. автотесты (e2e_auth/e2e_rbac/e2e_refdata)
alembic/         миграции (baseline = вся схема ядра + payload модулей)
```

## 2. Аутентификация — актуальный контур

**OAuth через Яндекс ID полностью удалён из системы** (исторические колонки
`users.yandex_id`/`invites.yandex_id` вычищаются self-healing-логикой старта —
см. `_drop_legacy_auth_columns` в `app/main.py`). Единственный вход —
**логин + пароль** (`POST /auth/login`, форма):

* Администратор задаётся окружением: `ADMIN_USERNAME` + `ADMIN_PASSWORD`
  (хешируется при сиде `sync_admin_logins`, синхронизируется при каждом старте)
  или несколько — `ADMIN_LOGINS=user1:pass1,...`. Логика входа гарантирует роль
  ADMIN для env-аккаунта (защита от случайного понижения через API).
* Обычные пользователи создаются админом (`POST /admin/users`) с начальным
  паролем; флаг `must_change_password` заставляет сменить пароль при первом
  входе (`GET/POST /change-password`).
* Session-cookie: HMAC-подписанный токен `{uid, acl_epoch}`, срок 14 суток,
  `HttpOnly`, `SameSite=Lax`, `Secure` за HTTPS.
* Brute-force throttle: 10 неудачных попыток с IP за 15 минут → временная
  блокировка (`/login?error=too_many`).
* `DEV_LOGIN_ENABLED` — только для локальной разработки (в проде fail-fast).

Эндпоинты: `POST /auth/login`, `POST /auth/logout`, `POST /auth/change-password`,
`GET /auth/me`.

## 3. Локальный запуск / Docker

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # SECRET_KEY (≥32 символов), DATABASE_PATH, ADMIN_PASSWORD
uvicorn app.main:app --reload              # dev: доступны dev-вход и демо-сид
# или
docker compose up -d                       # прод: fail-fast без секретов не стартует
```

Production fail-fast (`ENVIRONMENT=production`): дефолтный/короткий `SECRET_KEY`,
включённый dev-вход/демо-сид, отсутствие `ADMIN_USERNAME/ADMIN_PASSWORD` →
приложение не стартует.

## 4. Миграции (Alembic)

* Контейнер при старте сам выполняет `alembic upgrade head`; легаси-БД штампуется
  (`stamp head`) автоматически.
* Изменили модель → `alembic revision --autogenerate -m "..."`, проверьте
  сгенерированное, коммитьте вместе с кодом.
* Новый модуль = новые таблицы payload: добавьте их в autogenerate-ревизию;
  базовая baseline пересоздавать не нужно.
* Ревизия `c9e4f18a7d02` удаляет легаси-колонки Яндекс-ID; если на проде БД была
  ошибочно заставлена раньше — self-healing при старте домусорит колонки сам.

## 5. Тесты

```bash
PYTHONPATH=. python scripts/e2e_test.py
PYTHONPATH=. python scripts/e2e_auth_test.py    # логин/пароль, смена пароля, throttle
PYTHONPATH=. python scripts/e2e_rbac_test.py    # роли, пространства, инварианты
```

Сценарии: RBAC, CRUD всех модулей, outbox-воркер, шеринг/изоляция пространств,
инвариант ответственных, ICS/время совещаний. При доработке прав доступа обязательно
добавляйте e2e-кейс (например: «HEAD создаёт задачу с явным `department_id` своего
пространства → 201»; «понижение последнего HEAD → 409 с текстом»).

## 6. Модель прав (app/core/access.py) — как отлаживать

Разрешения вычисляются `AccessContext`:

* глобально: `ADMIN/COORDINATOR` → всё во всех пространствах;
* в пространстве: роль членства `HEAD` (управление) / `MEMBER` (свои карточки);
* `_pick_department` (в 8 CRUD-модулях): явный `department_id` разрешён, если он
  входит в список пространств пользователя **или** пользователь ADMIN/COORDINATOR;
  иначе 403 «выбор чужого пространства…». Исторический баг (запрет выбора даже
  собственного пространства для HEAD — фронт всегда шлёт `department_id`) исправлен:
  membership-проверка выполняется до проверки глобальной роли.

Кэш прав: `permcache.py` — epoch в подписанном cookie + process-local mirror
(TTL ~5 c). При мутации роли сервер перевыпускает cookie с новым epoch прямо в
ответе (`Set-Cookie` в `/departments/{id}/members` и `/admin/users/{id}`), поэтому
права применяются сразу после перезагрузки страницы. Если у пользователя
«устаревшая» роль UI: (1) проверить, что mutation-ответ вернул `Set-Cookie` с
новым epoch; (2) при нескольких воркерах убедиться, что БД одна (SQLite в `/data`);
(3) инкогнито-перезагрузка/перелогин сбрасывает mirror.

Инварианты сервиса:

* ≥1 HEAD на подразделение: `set_department_role` возвращает **409**, фронт показывает
  текст ошибки (не молча перезагружает страницу); то же при удалении последнего HEAD;
* один ответственный от каждого участвующего подразделения — на уровне сервиса.

## 7. Фоновые задачи и интеграции

* **Transactional Outbox**: любое изменение пишет `OutboxEvent` в той же транзакции;
  asyncio-воркеры в lifespan доставляют Telegram/CalDAV с повторами. Воркер упал —
  события дождутся следующего запуска, идемпотентность по детерминированным UID.
* **Календарь**: WebDAV PROPFIND/PUT/DELETE на `webdav.yandex.ru`; app-пароль
  пользователя в БД только Fernet-шифротекстом. Область: Member — свои, Head —
  подразделение, Coordinator/Admin — система.
* **Telegram-бот**: polling-воркер; команды `/start <код>` (привязка), `/tasks`,
  `/mute`, `/unmute`. Сценарии рассылок редактируются в админ-разделе пространств
  (`/api/notify/scenarios`, базовые сценарии — `/api/admin/notify/base-scenarios`).

## 8. Деплой

Подробно — [DEPLOY.md](DEPLOY.md) (переменные окружения, бот, TLS/reverse-proxy, бэкапы).

| Вариант | Файл | Особенности |
|---|---|---|
| Amvera PaaS | `amvera.yaml` | python/pip, uvicorn :8080, persistenceMount → `/data` |
| Self-hosted VPS | `docker-compose.yml` | volume `caesar-data:/data`, свой nginx/TLS |

Обязательные переменные прода: `SECRET_KEY` (≥32), `ENVIRONMENT=production`,
`ADMIN_USERNAME`/`ADMIN_PASSWORD`; опционально `TELEGRAM_BOT_TOKEN`,
`YANDEX_CAL_*`. Бэкап = копия `/data/caesar.db` (sqlite3 `.backup` или hot-copy
при low-write).

## 9. Диагностика частых проблем

| Симптом | Где смотреть |
|---|---|
| 403 при создании в своём пространстве | membership пользователя в `workspace_members`; `_pick_department` |
| Роль в пространстве не обновилась в UI | epoch в cookie, TTL permcache, ответ мутации (200 vs 409) |
| Понижение HEAD «не работает» | ожиданное 409 (последний HEAD); UI обязан показать toast |
| Не может войти созданный пользователь | `must_change_password` (первый вход → смена пароля), `is_active`, регистр логина (нормализуется в lower) |
| Календарь не синхронизируется | статус `SyncState`, логи outbox-воркера, app-пароль пользователя |
| Нет уведомлений TG | привязка `telegram_chat_id`, `/mute`, доставка OutboxEvent |
| Приложение не стартует в проде | fail-fast проверки config: SECRET_KEY/ADMIN/dev-вход |
| IntegrityError yandex_id на старте | легаси-колонки: самоизлечивается `_drop_legacy_auth_columns`; проверьте историю alembic (`stamp` без `upgrade`) |
