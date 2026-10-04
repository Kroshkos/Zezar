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

Схема БД управляется **Alembic-миграциями** (базовая ревизия `baseline` создаёт всю схему
ядра и модулей). При запуске контейнера `entrypoint.sh` автоматически определяет состояние
БД в `/data`: новая база — `alembic upgrade head`; легаси-база, созданная старым `create_all`
без таблицы `alembic_version`, — штампуется на базовую ревизию и догоняется `upgrade head`
(данные не трогаются). Дополнительно приложение при старте само досоздаёт отсутствующие
колонки (`ALTER TABLE ADD COLUMN`) — защита от ложного `stamp head` на старых базах.
Для локальной разработки без Docker схема по-прежнему создаётся `create_all` при старте
приложения (идемпотентно поверх смитгрированной базы).

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

Как узнать свой `sub` (числовой идентификатор аккаунта Яндекса):

**Способ 1 — через сам сервис (рекомендуется, ничего настраивать не нужно):**
1. Задеплойте приложение с заполненными `YANDEX_CLIENT_ID/SECRET/REDIRECT_URI`,
   но **пустым** `BOOTSTRAP_ADMIN_YA_IDS`.
2. Откройте `/login` и нажмите «Войти через Яндекс ID» под нужным аккаунтом.
3. OAuth отработает штатно, но на странице появится сообщение вида:
   *«Доступ закрыт… Ваш Yandex ID (sub): 123456789 — сообщите его администратору»*.
   Этот же id пишется в логи сервера (`caesar.auth`).
4. Впишите полученный `sub` в `BOOTSTRAP_ADMIN_YA_IDS`, перезадеплойте — при
   следующем входе пользователь станет ADMIN (seed создаёт Invite только если
   в БД ещё нет ни одного админа).

**Способ 2 — напрямую через API Яндекс ID:** получите любой OAuth-токен для
scope `login:info` и выполните:
```bash
curl -H "Authorization: OAuth <ACCESS_TOKEN>" \
     "https://login.yandex.ru/info?format=json" | jq '.id'
```
Поле `id` в ответе — это и есть `sub` (он же значение `yandex_id` в БД; совпадает
с числом из ссылки профиля `https://ya.ru/users/<id>`).

**Способ 3 — для уже входящих пользователей:** админ видит `yandex_id` всех
пользователей в списке `/api/core/admin/users` страницы Администрирования.

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
Совместимость схемы: при сборке новой колонки/таблицы выполните
`alembic revision --autogenerate -m "<описание>"`, проверьте сгенерированный файл
(для SQLite используется batch-режим) и коммитьте его вместе с моделью. Контейнер
при старте сам применит `alembic upgrade head` (см. `entrypoint.sh`); легаси-БД без
`alembic_version` будет автоматически «штампована» на базовую ревизию без потери данных.

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

## 10. Деплой на хостинг Amvera (PaaS, python/pip-окружение)

Подготовлен под требования платформы. Amvera поддерживает два взаимоисключающих
режима: **Dockerfile** или **файл конфигурации `amvera.yaml`** (toolchain-сборка).
В проекте выбран второй режим — конфигурация окружения `python` + toolchain `pip`
(докa: https://docs.amvera.ru/applications/environments/python-pip.html):

| Требование Amvera | Как выполнено в проекте |
|---|---|
| Секция `meta`: `environment: python`, `toolchain.name: pip`, версия Python | `amvera.yaml` → `python` / `pip` / `3.12` |
| Зависимости ставятся из `requirements.txt` (`build.requirementsPath` по умолчанию) | секция `build` не нужна; все пакеты (fastapi, uvicorn[standard], alembic…) зафиксированы в `requirements.txt`. ВАЖНО: async-движок SQLAlchemy требует нативную библиотеку **greenlet** — в `requirements.txt` он указан явно и через extra `sqlalchemy[asyncio]` (без него приложение падает на старте с `ImportError: The SQLAlchemy asyncio module requires ... 'greenlet'`) |
| ASGI-приложение запускается через `run.command` (официальный рецепт «FastAPI» из доки) | `uvicorn --host 0.0.0.0 --port 8080 --proxy-headers --forwarded-allow-ips '*' app.main:app` |
| Порт задаётся **только** параметром `containerPort` (переменная `PORT` платформой не инжектируется) | `containerPort: 8080` = тот же порт в команде uvicorn |
| Постоянное хранилище монтируется в путь из `run.persistenceMount` (по умолчанию `/data`) — параметр поддерживается и для python/pip ([дока](https://docs.amvera.ru/applications/environments/python-pip.html)) | `amvera.yaml` → `run.persistenceMount: /data`; база пишется по абсолютному пути `/data/caesar.db` (дефолт в коде, `DATABASE_PATH` задавать не нужно). Секция `build` с `diskSizeGb` для python **невалидна** (отклоняется валидатором) и не используется. Подробности — [Хранилище и пути](https://docs.amvera.ru/applications/storage.html), см. п. 10 ниже |
| `docker-compose.yml` **не поддерживается** | compose остаётся только для self-hosted VPS; на Amvera используется `amvera.yaml` |
| TLS терминируется платформой (внутрь контейнера приходит HTTP) | `--proxy-headers` + `TRUST_PROXY_HEADERS=true`; cookies Secure/HttpOnly |

> **Про Dockerfile:** он нужен для self-hosted VPS (`docker build`/compose). На Amvera
> при наличии `amvera.yaml` сборка идёт через venv+pip. Если предпочитаете docker-режим
> платформы — создайте приложение типа «Docker» и удалите `amvera.yaml` из корня
> репозитория (порт 8000 из CMD тогда потребует `run.containerPort: 8000`).

### Шаги деплоя

1. **Создайте приложение** в панели Amvera и привяжите git-репозиторий
   (`https://github.com/Kroshkos/Zezar`). Файл `amvera.yaml` в корне будет подхвачен
   автоматически; конфигурация берётся из него.
2. **Переменные окружения** — добавьте в настройках приложения (не в git!):

   ```
   ENVIRONMENT=production
   SECRET_KEY=<python -c "import secrets;print(secrets.token_hex(32))">
   YANDEX_CLIENT_ID=...
   YANDEX_CLIENT_SECRET=...
   YANDEX_REDIRECT_URI=https://<ваш-домен>.amvera.app/auth/callback
   BOOTSTRAP_ADMIN_YA_IDS=<ваш yandex sub>
   COOKIE_SECURE=false        # см. пояснение ниже
   TRUST_PROXY_HEADERS=true
   DATABASE_PATH=/data/caesar.db
   ```

   Переменная `PORT` **не требуется**: порт жёстко задан в `amvera.yaml`
   (`containerPort: 8080` и та же цифра в команде uvicorn).

   ⚠️ Важно про путь БД и хранилище (по доке «Хранилище и пути»:
   https://docs.amvera.ru/applications/storage.html). Amvera использует три папки:
   **Code** (git-код, доступен только при сборке), **Artifacts** (результат сборки,
   монтируется в `/app`; содержимое теряется при пересборке/заморозке — хранить
   данные там нельзя) и **Data** (постоянное хранилище — монтируется при каждом
   запуске контейнера в точку `run.persistenceMount`, по умолчанию `/data`, и НЕ
   очищается между деплоями). В проекте база пишется именно туда: стандартный
   абсолютный путь `/data/caesar.db` (значение по умолчанию в коде — переменную
   `DATABASE_PATH` задавать не нужно; дока требует именно абсолютные пути, что и
   сделано). Точка монтирования закреплена в `amvera.yaml`: `persistenceMount: /data`.
   Секция `build.diskSizeGb` для python/pip **невалидна** (валидатор отклоняет её
   «unknown fields … in build section») и в конфиге отсутствует. Если точка
   монтирования в вашем проекте изменена (панель → Хранилище), укажите этот путь в
   `DATABASE_PATH`. Приложение само создаёт недостающие папки по пути к файлу БД.

   Важно про cookies: Amvera терминирует TLS на своём балансировщике и внутрь
   контейнера отдаёт HTTP. Флаг `Secure` у session-cookie в этом случае безопасен
   (внешний трафик всегда HTTPS), но если браузер почему-то видит http-origin,
   вход можно починить значением `COOKIE_SECURE=false`. Fail-fast продакшена при
   этом сохраняется: без длинного `SECRET_KEY` и OAuth-кредов контейнер не стартует.
3. **OAuth-консоль Яндекс ID**: зарегистрируйте приложение с redirect URI из п. 2
   (см. раздел 4 этой инструкции). Домен `.amvera.app` выдаётся бесплатно после деплоя.
4. **Деплой**: push в ветку → Amvera собирает образ и запускает контейнер.
   Проверка готовности: `curl https://<домен>/healthz` → `{"ok": true, ...}`.
5. **Первый вход**: откройте `/auth/login`, войдите через Яндекс ID — ваш `sub` из
   `BOOTSTRAP_ADMIN_YA_IDS` получит роль ADMIN (далее очистите переменную и
   перезадеплойте).

### Ограничения платформы (учесть)

- **Ресурсы**: бесплатного тарифа хватает на пилот (~десятки пользователей); SQLite WAL
  на одном экземпляре контейнера — обязательное условие (Amvera и так запускает один
  контейнер на приложение).
- **Бэкапы**: файл БД лежит на постоянном диске в точке монтирования `persistenceMount` (стандартно `/data`; диск изолирован от образа и живёт между деплоями; доступен из exec-сессии). Скачивайте его из панели «Хранилище» или делайте `VACUUM INTO` через exec. Для локальной копии удобно `amvera app fs download <appId> /data/caesar.db ./backup.db`. Загрузку файлов в папку Data можно делать из интерфейса после первого успешного деплоя (папка создаётся при развертывании).
- **Telegram long-polling и воркеры календаря** работают внутри lifespan того же
  контейнера — дополнительных сервисов (как в compose-варианте) разворачивать не нужно.
- **Alembic-миграции подключены**: базовая ревизия создаёт всю схему, а `entrypoint.sh`
  при каждом старте контейнера автоматически применяет `alembic upgrade head`
  (легаси-БД без таблицы `alembic_version` безопасно штампуется — данные не трогаются).
  При изменении колонок в моделях просто добавьте новую ревизию
  (`alembic revision --autogenerate -m "..."`) и задеплойте — миграция применится сама.

---

## 11. Чек-лист перед вводом в эксплуатацию

- [ ] `SECRET_KEY` длинный случайный, ротация предусмотрена
- [ ] `ENVIRONMENT=production`, dev-вход и демо-сид выключены (fail-fast проходит)
- [ ] TLS включён, HSTS желателен; на хостингах с TLS-терминированием (Amvera) — `COOKIE_SECURE=false`, см. п. 10
- [ ] `YANDEX_REDIRECT_URI` = боевой домен, зарегистрирован в OAuth-консоли
- [ ] `BOOTSTRAP_ADMIN_YA_IDS` заполнен; после создания админов — очищен
- [ ] Telegram-токен проверен: бот отвечает `/help`
- [ ] Настроен cron-бэкап `/data` (проверено восстановление!)
- [ ] E2E-тесты зелёные на стенде: `docker compose exec caesar python scripts/e2e_test.py`
