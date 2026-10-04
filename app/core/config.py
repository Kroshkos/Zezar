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

    # OAuth 2.0 Яндекс ID
    yandex_client_id: str = ""
    yandex_client_secret: str = ""
    yandex_redirect_uri: str = "http://localhost:8000/auth/callback"
    # csv Yandex sub'ей для первичного администратора. Пусто по умолчанию —
    # иначе случайный/дефолтный id мог бы получить ADMIN (см. _bootstrap_ids).
    bootstrap_admin_ya_ids: str = ""

    @property
    def bootstrap_ids(self) -> list[str]:
        """BOOTSTRAP_ADMIN_YA_IDS как чистый список строк.

        Терпимость к типичным ошибкам заполнения на хостинге: разделители
        ';'/'/', переносы строк и пробелы, регистр букв в id. Сравнение с
        ya_id из userinfo Яндекса идёт по lower()."""
        import re
        raw = self.bootstrap_admin_ya_ids or ""
        parts = [p.strip() for p in re.split(r"[,;/\s]+", raw) if p.strip()]
        return [p.lower() for p in parts]

    # Telegram
    telegram_bot_token: str = ""
    telegram_notify_enabled: bool = True

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
        if not self.yandex_client_id or not self.yandex_client_secret:
            errors.append("YANDEX_CLIENT_ID / YANDEX_CLIENT_SECRET не заданы — OAuth-вход невозможен")

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
