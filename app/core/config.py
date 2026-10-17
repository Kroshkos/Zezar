"""
Конфигурация приложения (ядро). Все пути и ключи — из .env.
"""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Ядро
    app_name: str = "Цезарь — Управление стратегических инициатив и проектов"
    secret_key: str = "change-me"
    data_dir: Path = Path("/data")
    database_path: Path = Path("/data/caesar.db")

    # ---------- Администраторы: единственный контур входа — логин и пароль ----------
    # Авторизация через Яндекс ID полностью удалена из системы.
    # Оба значения задаются через переменные окружения ОТДЕЛЬНО:
    #   ADMIN_USERNAME  — логин администратора (по умолчанию 'admin')
    #   ADMIN_PASSWORD  — его пароль в открытом виде (система сама хеширует
    #                     его pbkdf2_sha256 при старте; хеш генерировать не нужно)
    #   ADMIN_FULL_NAME — ФИО для карточки пользователя (необязательно)
    # Резервирование (несколько админов): ADMIN_LOGINS="вася:хеш1:Вася П.;петя:хеш2",
    # где хеш — pbkdf2_sha256 (python scripts/gen_admin_password.py <логин>).
    # Простой путь — ADMIN_USERNAME/ADMIN_PASSWORD; ADMIN_LOGINS нужен, только если
    # админов несколько или хочется хранить хеши вместо открытого пароля.
    admin_username: str = "admin"
    admin_password: str = ""
    admin_full_name: str = ""
    admin_logins: str = ""

    @property
    def admin_login_entries(self) -> list[tuple[str, str, str]]:
        """Записи локальных администраторов [(login_lower, pw_hash, ФИО), ...].

        Источник 1 (основной): ADMIN_USERNAME + ADMIN_PASSWORD — пароль хешируется
        на лету (pbkdf2_sha256), поэтому в env светится только сам пароль, который
        вы и так знаете, а хеш нигде не хранится в явном виде.
        Источник 2 (резерв): ADMIN_LOGINS="логин:хеш[:ФИО];..." — для нескольких
        админов / хранения хешей. Одинаковые логины не дублируются (первый wins).
        """
        from app.core.security import hash_password

        out: list[tuple[str, str, str]] = []
        seen: set[str] = set()

        login = (self.admin_username or "").strip().lower()
        password = self.admin_password or ""
        if login and password.strip():
            name = (self.admin_full_name or "").strip() or f"Администратор {login}"
            out.append((login, hash_password(password), name))
            seen.add(login)

        for part in (self.admin_logins or "").split(";"):
            part = part.strip()
            if not part:
                continue
            fields = part.split(":", 2)
            if len(fields) < 2 or not fields[0].strip() or not fields[1].strip():
                continue
            lgn = fields[0].strip().lower()
            if lgn in seen:
                continue
            pwhash = fields[1].strip()
            name = (fields[2].strip() if len(fields) > 2 and fields[2].strip()
                    else f"Администратор {lgn}")
            out.append((lgn, pwhash, name))
            seen.add(lgn)
        return out

    # Telegram
    telegram_bot_token: str = ""
    telegram_bot_username: str = ""   # например CaesarNotifyBot — для инструкции в UI
    telegram_notify_enabled: bool = True
    # Публичный адрес системы для ссылок в уведомлениях (кнопка «Открыть» в Telegram)
    public_base_url: str = "https://zezar-patfiz.waw0.amvera.tech"
    # Напоминания «приближается срок»: за сколько дней и как часто проверять
    due_remind_days_ahead: int = 3
    due_reminder_interval_seconds: int = 3600

    # Яндекс.Календарь (WebDAV/CalDAV)
    yandex_cal_enabled: bool = True
    yandex_cal_base: str = "https://webdav.yandex.ru"
    yandex_cal_default_calendar: str = "default"

    # Фоновые задачи
    sync_interval_seconds: int = 300
    workers_enabled: bool = True          # фоновые воркеры в lifespan (в прод-контейнере можно вынести в ARQ)
    dev_login_enabled: bool = False       # вход по email без Яндекса (локальная разработка/тесты)

    # Продакшен-харденинг
    seed_demo_data: bool = False          # демо-пользователи/подразделения при пустой БД (только dev)
    cookie_secure: bool | None = None     # None => авто: secure в продакшене, иначе http-only без Secure
    trust_proxy_headers: bool = False     # доверять X-Forwarded-Proto (Amvera/nginx и т.п. TLS-терминаторы)

    @property
    def forwarded_allow_ips(self) -> str:
        """IP трафик-прокси для uvicorn --forwarded-allow-ips (из env FORWARDED_ALLOW_IPS)."""
        import os
        return os.getenv("FORWARDED_ALLOW_IPS", "*")

    @property
    def is_production(self) -> bool:
        import os
        return os.getenv("ENVIRONMENT", "development").lower() == "production"

    @property
    def session_cookie_secure(self) -> bool:
        """Явное значение из .env; если не задано — Secure только в продакшене.

        Хостинги с TLS-терминированием на балансировщике (Amvera и т.п.) отдают
        внутрь контейнера HTTP; там Secure-флаг ломает вход по cookie, поэтому
        его можно явно выключить: COOKIE_SECURE=false TRUST_PROXY_HEADERS=true.
        """
        if self.cookie_secure is not None:
            return self.cookie_secure
        return self.is_production

    def require_production_settings(self) -> None:
        """Fail-fast при запуске с дефолтными/небезопасными значениями (ENVIRONMENT=production)."""
        import sys

        errors = []
        # Защита данных: БД обязана лежать в постоянном хранилище, а не в ephemeral-папке.
        # Стандартный путь — /data (дефолт): на VPS это docker volume, на Amvera — папка
        # постоянного хранилища Data, монтируемая в run.persistenceMount (по умолчанию /data;
        # см. https://docs.amvera.ru/applications/storage.html).
        if self.is_production and str(self.database_path).startswith("/tmp"):
            errors.append(
                "DATABASE_PATH указывает в /tmp — данные будут потеряны при перезапуске! "
                "Используйте стандартный путь /data (DATABASE_PATH=/data/caesar.db), "
                "убедившись, что он примонтирован как постоянное хранилище "
                "(docker volume /data на VPS; persistenceMount: /data на Amvera)."
            )
        if self.secret_key in ("", "change-me"):
            errors.append("SECRET_KEY не задан (используется значение по умолчанию 'change-me')")
        if len(self.secret_key) < 32:
            errors.append("SECRET_KEY слишком короткий (минимум 32 символа)")
        if self.dev_login_enabled:
            errors.append("DEV_LOGIN_ENABLED=true в продакшене — закрытая регистрация обходится входом по email")
        if self.seed_demo_data:
            errors.append("SEED_DEMO_DATA=true в продакшене — будут созданы демо-пользователи с детерминированными id")
        # Единственный контур входа — логин/пароль. Администратор обязан быть задан,
        # иначе система остаётся без управляющего контура (именно так возникало
        # «зашёл, но админки нет»). Авторизация через Яндекс ID удалена полностью.
        if not self.admin_login_entries:
            errors.append(
                "Не задан администратор: заполните ADMIN_USERNAME + ADMIN_PASSWORD "
                "(логин и пароль администратора; ADMIN_USERNAME по умолчанию 'admin'). "
                "Вход в систему только по логину/паролю."
            )

        # Предупреждение (не блокирует запуск): /data — стандартный путь БД. Убедитесь,
        # что он примонтирован как постоянное хранилище: docker volume /data на VPS;
        # на Amvera — папка Data из run.persistenceMount (по умолчанию /data), см. доки
        # https://docs.amvera.ru/applications/storage.html.
        dpath = str(self.database_path)
        if self.is_production and dpath.startswith("/data"):
            print("[ЦЕЗАРЬ] DATABASE_PATH=" + dpath + " (стандартный путь) — проверьте, что /data"
                  " примонтирован как постоянное хранилище (docker volume на VPS; на Amvera —"
                  " persistenceMount: /data в amvera.yaml). Без этого база ephemeral и сотрётся"
                  " при пересборке.", file=sys.stderr)

        if errors:
            msg = "\n".join("  - " + e for e in errors)
            print(f"[ЦЕЗАРЬ] КРИТИЧЕСКИЕ ОШИБКИ КОНФИГУРАЦИИ (ENVIRONMENT=production):\n{msg}\n"
                  f"Приложение не запущено. Заполните .env или запускайте с ENVIRONMENT=development.", file=sys.stderr)
            raise SystemExit(1)

    @property
    def database_url(self) -> str:
        # гарантируем существование папки /data (volume контейнера)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return f"sqlite+aiosqlite:///{self.database_path}"

    class Config:
        env_file = ".env"
        extra = "ignore"


@lru_cache
def get_settings() -> Settings:
    return Settings()
