# Инструкция для технических специалистов — «ЦЕЗАРЬ»

Для разработчиков, DevOps и технической поддержки: сборка, запуск, миграции,
тестирование, отладка типовых проблем прав доступа.

---

## 1. Стек и структура

* Python 3.12 + FastAPI (async), SQLAlchemy 2.x async + aiosqlite, Jinja2 SSR + Vanilla JS.
* SQLite строго в `/data` (путь из `DATABASE_PATH` в `.env`).
* Архитектура плагиновая: ядро `app/` не знает о модулях; каждый модуль —
  `modules/<key>/manifest.py` с `ModuleSpec` (роутер, страницы, payload-модель,
  календарный маппер). Реестр подключает модули на старте.

```
app/core/        config, security (сессии HMAC), oauth, access.py (RBAC), permcache.py, registry
app/models/      core.py (User/Role/Department/Workspace/Entity/AuditLog/OutboxEvent)
app/routers/     auth, departments, sharing, admin, pages, profile, notify, refdata
modules/         dashboard tasks projects meetings admin_dept docs softs grants schols priority
integrations/    yandex_calendar (WebDAV/CalDAV), telegram_service, workers (outbox + TG poller)
scripts/e2e_test.py   16 сквозных сценариев
alembic/         миграции (baseline = вся схема ядра + payload модулей)
```

## 2. Локальный запуск / Docker

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # SECRET_KEY (≥32 символов), DATABASE_PATH
uvicorn app.main:app --reload              # dev: доступны dev-вход и демо-сид
# или
docker compose up -d                       # прод: fail-fast без секретов не стартует
```

Production fail-fast (`ENVIRONMENT=production`): дефолтный/короткий `SECRET_KEY`,
включённый dev-вход/демо-сид, отсутствие OAuth-кредов → приложение не стартует.

## 3. Миграции (Alembic)

* Контейнер при старте сам выполняет `alembic upgrade head`; легаси-БД штампуется
  (`stamp head`) автоматически.
* Изменили модель → `alembic revision --autogenerate -m "..."`, проверьте сгенерированное,
  коммитьте вместе с кодом.
* Новый модуль = новые таблицы payload: добавьте их в autogenerate-ревизию; базовая
  baseline пересоздавать не нужно.

## 4. Тесты

```bash
PYTHONPATH=. python scripts/e2e_test.py
```

Сценарии: RBAC, CRUD всех модулей, outbox-воркер, шеринг/изоляция пространств,
инвариант ответственных, ICS/время совещаний. При доработке прав доступа обязательно
добавляйте e2e-кейс (например: «HEAD создаёт задачу с явным `department_id` своего
пространства → 201»; «понижение последнего HEAD → 409 с текстом»).

## 5. Модель прав (app/core/access.py) — как отлаживать

Разрешения вычисляются `AccessContext`:

* глобально: `ADMIN/COORDINATOR` → всё во всех пространствах;
* в пространстве: роль членства `HEAD` (управление) / `MEMBER` (свои карточки);
* `_pick_department` (в 8 CRUD-модулях): явный `department_id` разрешён, если он
  входит в список пространств пользователя **или** пользователь ADMIN/COORDINATOR;
  иначе 403 «выбор чужого пространства…».

Кэш прав: `permcache.py` — epoch в подписанном cookie + process-local mirror
(TTL ~5 c). При мутации роли сервер перевыпускает cookie с новым epoch, поэтому
права применяются сразу после перезагрузки страницы. Если у пользователя «устаревшая»
роль UI: (1) проверить, что mutation-ответ вернул `Set-Cookie` с новым epoch;
(2) при нескольких воркерах убедиться, что БД одна (SQLite в `/data`); (3) инкогнито-перезагрузка/перелогин сбрасывает mirror.

Инварианты сервиса:

* ≥1 HEAD на подразделение: `set_department_role` возвращает **409**, фронт показывает
  текст ошибки (не молча перезагружает страницу);
* один ответственный от каждого участвующего подразделения — на уровне сервиса.

## 6. Фоновые задачи и интеграции

* **Transactional Outbox**: любое изменение пишет `OutboxEvent` в той же транзакции;
  asyncio-воркеры в lifespan доставляют Telegram/CalDAV с повторами. Воркер упал —
  события дождутся следующего запуска, идемпотентность по детерминированным UID.
* **Календарь**: WebDAV PROPFIND/PUT/DELETE на `calendar.yandex.ru/wda/...`; app-пароль
  в БД только Fernet-шифротекстом. Область: Member — свои, Head — подразделение,
  Coordinator/Admin — система.
* **Telegram-бот**: polling-воркер; команды `/start <код>` (привязка), `/tasks`,
  `/mute`, `/unmute`.

## 7. Деплой

Подробно — [DEPLOY.md](DEPLOY.md) (OAuth Яндекс ID, бот, TLS/reverse-proxy, бэкапы).

| Вариант | Файл | Особенности |
|---|---|---|
| Amvera PaaS | `amvera.yaml` | python/pip, uvicorn :8080, persistenceMount → `/data` |
| Self-hosted VPS | `docker-compose.yml` | volume `caesar-data:/data`, свой nginx/TLS |

Бэкап = копия `/data/caesar.db` (sqlite3 `.backup` или hot-copy при low-write).

## 8. Диагностика частых проблем

| Симптом | Где смотреть |
|---|---|
| 403 при создании в своём пространстве | membership пользователя в `workspace_members`; `_pick_department` |
| Роль в пространстве не обновилась в UI | epoch в cookie, TTL permcache, ответ мутации (200 vs 409) |
| Понижение HEAD «не работает» | ожиданное 409 (последний HEAD); UI обязан показать toast |
| Календарь не синхронизируется | статус `SyncState`, логи outbox-воркера, app-пароль пользователя |
| Нет уведомлений TG | привязка `telegram_chat_id`, `/mute`, доставка OutboxEvent |
| Приложение не стартует в проде | fail-fast проверки config: SECRET_KEY/OAuth/dev-вход |
