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
    bootstrap_admin_ya_ids: str = ""     # csv, первый вход при пустой БД

    # Telegram
    telegram_bot_token: str = ""
    telegram_notify_enabled: bool = True

    # Яндекс.Календарь (WebDAV/CalDAV)
    yandex_cal_enabled: bool = True
    yandex_cal_base: str = "https://webdav.yandex.ru"
    yandex_cal_default_calendar: str = "default"

    # Фоновые задачи
    sync_interval_seconds: int = 300

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
