# Инструкция по деплою и настройке сервиса «ЦЕЗАРЬ»

Документ для системного администратора/DevOps. Описывает полный путь от клонирования
репозитория до продакшен-инстанса с OAuth, Telegram и Яндекс.Календарём.

---

## 1. Требования к окружению

| Компонент | Минимум | Комментарий |
|---|---|---|
| ОС | Linux (любая с Docker 24+) | или Python 3.12 для bare-metal |
| CPU/RAM | 1 vCPU / 512 MB | SQLite + asyncio, запас по вертикали не нужен |
| Диск | ≥ 2 GB в разделе томов Docker | БД живёт на volume `caesar-data` (`/data`) |
| Сеть | исходящий HTTPS к `oauth.yandex.ru`, `api.telegram.org`, `webdav.yandex.ru` | интеграции работают из фоновых воркеров |
| TLS | reverse-proxy (nginx/traefik/Caddy) | приложение отдаёт HTTP на :8000, Secure-cookie требует HTTPS |

---

## 2. Получение кода и сборка

```bash
git clone <repo-url> caesar && cd caesar
docker compose build          # образ python:3.12-slim, non-root appuser, healthcheck /healthz
```

Схема БД создаётся автоматически при старте (`Base.metadata.create_all`), отдельная
инициализация не требуется. Для эволюции схемы на изменения колонок подготовьте
миграцию Alembic (`alembic init` уже добавлен в зависимости; шаблон миграций — TODO
при первом изменении модели).

---

## 3. Настройка `.env` (обязательный минимум)

Создайте файл `.env` **рядом с `docker-compose.yml`** (compose подставит значения в
контейнер; переменные помечены `:?` — без них сервис не стартует):

```bash
# --- Секрет подписи сессий (ОБЯЗАТЕЛЬНО, fail-fast в production) ---
SECRET_KEY=$(python -c "import secrets;print(secrets.token_hex(32))")

# --- Яндекс ID (OAuth 2.0) ---
YANDEX_CLIENT_ID=<из oauth.yandex.ru>
YANDEX_CLIENT_SECRET=<из oauth.yandex.ru>
YANDEX_REDIRECT_URI=https://caesar.example.org/auth/callback

# --- Первый администратор (при пустой БД эти Yandex ID допускаются без invite) ---
BOOTSTRAP_ADMIN_YA_IDS=<sub первого админа>[,<ещё sub через запятую>]

# --- Telegram-бот (опционально, но рекомендуется) ---
TELEGRAM_BOT_TOKEN=1234567890:AA...        # без токена уведомления просто выключены
TELEGRAM_NOTIFY_ENABLED=true

# --- Яндекс.Календарь ---
YANDEX_CAL_ENABLED=true
YANDEX_CAL_BASE=https://webdav.yandex.ru
SYNC_INTERVAL_SECONDS=300                  # период diff-прохода календаря
```

Полный список параметров и дефолты — в [.env.example](../.env.example).

⚠️ Правила продакшена, которые проверяет fail-fast при старте:
* `SECRET_KEY` ≠ `change-me` и длина ≥ 32;
* `DEV_LOGIN_ENABLED=false` и `SEED_DEMO_DATA=false`;
* заданы `YANDEX_CLIENT_ID` и `YANDEX_CLIENT_SECRET`.

Нарушение любого правила → контейнер падает с внятной ошибкой в логе. Это защита от
случайного запуска демо-режима на боевых данных.

---

## 4. Яндекс ID: регистрация приложения

1. Зайдите на <https://oauth.yandex.ru> → «Создать приложение».
2. Платформа: **Веб-сервисы**.
3. Поле «Имя приложения»: например, `Цезарь УСИП`.
4. В блоке **«Яндекс.ID»** включите доступ *«Доступ к логину пользователя»*
   (scope `login:info` / `login:email`) — этого достаточно.
5. В **«Redirect URI»** добавьте точный адрес: `https://<ваш-домен>/auth/callback`.
   Протокол/домен/путь должны совпадать с `YANDEX_REDIRECT_URI` символ в символ.
6. Сохраните `ClientID` и `ClientSecret` в `.env` (см. п. 3).

Регистрация пользователей **закрыта**: вход возможен только если
* пользователь уже есть в БД (добавлен админом), либо
* его Yandex `sub` указан в `BOOTSTRAP_ADMIN_YA_IDS` (первый запуск), либо
* админ создал Invite на конкретного человека (страница Администрирования).

Узнать свой `sub`: после входа открыть `/auth/me` (поле `yandex_id`).

---

## 5. Telegram-бот

1. В Telegram откройте [@BotFather](https://t.me/BotFather) → `/newbot` → получите токен.
2. Запишите токен в `TELEGRAM_BOT_TOKEN`.
3. Отправьте боту команду `setMyCommands` (необязательно) через BotFather:
   `/start` — привязка, `/tasks` — мои задачи, `/mute`/`/unmute` — рассылка.
4. Пользователи привязываются сами: в профиле сервиса («Настройки → Интеграции»)
   генерируется код, пользователь отправляет боту `/start <код>` (см. USER_GUIDE).

Без токена сервис работает: outbox-события Telegram накапливаются и игнорируются
воркером, long-polling не запускается.

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

Uvicorn внутри контейнера запущен с `--proxy-headers --forwarded-allow-ips *`, поэтому
корректно определяет HTTPS и ставит cookie c флагом `Secure`
(`COOKIE_SECURE=true` в compose по умолчанию в production).

---

## 7. Запуск и проверка

```bash
docker compose up -d
docker compose ps                     # состояние healthy (healthcheck /healthz)
curl -s https://caesar.example.org/healthz
# ожидаем: {"ok":true,"modules":["dashboard","tasks","projects","meetings",...]}
```

Первый вход: откройте `/login` → «Войти через Яндекс ID». Если `BOOTSTRAP_ADMIN_YA_IDS`
содержит ваш `sub`, вы станете первым администратором. Дальше:
1. Создайте подразделения (Администрирование → Подразделения) — каждому пространству
   автоматически подключаются базовые модули.
2. Добавьте пользователей по их Yandex ID / e-mail, назначьте роли (Head/Member) и подразделения.
3. Руководители включают нужные дополнительные модули и добавляют участников.

Логи воркеров и ошибок интеграций: `docker compose logs -f caesar`.

---

## 8. Данные, бэкапы, обновление

**Все данные — только в Docker volume `caesar-data` (внутри контейнера `/data`):**
SQLite-файл `caesar.db` (+ WAL). Пересборка/пересоздание контейнера данные НЕ трогает.

```bash
# Бэкап (cron ежедневно; sqlite3 обязателен для консистентного снапшота при WAL)
docker compose exec caesar python -c "import sqlite3;sqlite3.connect('/data/caesar.db').execute(\"VACUUM INTO '/data/backup-$(date +%F).db'\")"
docker run --rm -v caesar_caesar-data:/data -v $(pwd):/out alpine cp /data/backup-*.db /out/

# Восстановление: остановить сервис, заменить caesar.db, удалить .db-wal/.db-shm, запустить
```

Обновление:
```bash
git pull && docker compose up -d --build
```
Совместимость схемы: `create_all` добавляет новые таблицы, но **не изменяет существующие**.
При релизе, содержащем новые колонки, выполните миграцию Alembic
(после `alembic revision --autogenerate`) или одноразовый `ALTER TABLE` в
`docker compose exec caesar python`, затем `alembic stamp head`.

Масштабирование: сервис stateful-однопоточный по дизайну (SQLite + in-process воркеры).
Запускать ровно **один** экземпляр контейнера; горизонтальное масштабирование нескольких
копий с общей файловой БД недопустимо. При росте нагрузки > ~50 активных пользователей
план миграции на PostgreSQL + внешний ARQ-воркер заложен архитектурно (SessionLocal и
workers изолированы).

---

## 9. Диагностика типовых проблем

| Симптом | Причина / решение |
|---|---|
| Контейнер падает на старте с сообщением fail-fast | Нарушены правила продакшена п. 3 — исправьте `.env` |
| OAuth: `redirect_uri doesn't match` | Расхождение `YANDEX_REDIRECT_URI` и записи в консоли Яндекс ID (протокол/слэш) |
| Вход → «доступ закрыт» | Пользователя нет в БД и он не в `BOOTSTRAP_ADMIN_YA_IDS`; создайте Invite |
| Cookie слетает между запросами | Нет HTTPS / не проброшен `X-Forwarded-Proto`; проверьте proxy и `COOKIE_SECURE` |
| События не появляются в Яндекс.Календаре | Пользователь не привязал app-пароль (Настройки→Интеграции); смотрите `cal_error`/статус `PENDING` в логах воркера |
| Telegram-уведомления не приходят | Не задан токен, либо пользователь не прошёл `/start <код>`; проверьте `/api/profile/integrations` |
| `database is locked` | Несколько процессов пишут в один файл — убедитесь, что запущен один контейнер |

---

## 10. Чек-лист перед вводом в эксплуатацию

- [ ] `SECRET_KEY` длинный случайный, ротация предусмотрена
- [ ] `ENVIRONMENT=production`, dev-вход и демо-сид выключены (fail-fast проходит)
- [ ] TLS включён, HSTS желателен, `COOKIE_SECURE=true`
- [ ] `YANDEX_REDIRECT_URI` = боевой домен, зарегистрирован в OAuth-консоли
- [ ] `BOOTSTRAP_ADMIN_YA_IDS` заполнен; после создания админов — очищен
- [ ] Telegram-токен проверен: бот отвечает `/help`
- [ ] Настроен cron-бэкап `/data` (проверено восстановление!)
- [ ] E2E-тесты зелёные на стенде: `docker compose exec caesar python scripts/e2e_test.py`
