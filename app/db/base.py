"""Базовый DeclarativeBase для ядра и всех модулей."""
from datetime import datetime, timezone

from sqlalchemy import DateTime, MetaData
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Единое имя метаданных: все модули регистрируют таблицы в одном Base.metadata,
# что позволяет ядру создать схему одной командой create_all.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utcnow() -> datetime:
    """Текущий момент по московскому времени (см. app/core/timezone.py).

    Название сохранено для совместимости; колонки DateTime платформы и все
    отображаемые метки («время создания/изменения», журнал, уведомления)
    хранятся в наивном времени MSK, чтобы сервер в другой стране не сдвигал
    сутки относительно пользователей.
    """
    from app.core.timezone import now  # локальный импорт — циклических слоёв нет
    return now(tz=True)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
