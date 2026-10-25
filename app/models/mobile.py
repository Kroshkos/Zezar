"""Модели мобильного контура: подписки push-уведомлений и лента событий.

PushSubscription — токен Expo Push Token сервиса (Expo / FCM / APNs) одного
устройства пользователя. Один пользователь может иметь несколько устройств.

MobileEvent — «лента» событий для мобильного приложения: ставится транзакционно
в тех же местах, где и outbox (entity_service). Приложение читает её long-poll-ем
через GET /api/mobile/events?since_id=N — это даёт мгновенное обновление списков
без фоновых зависимостей; поверх ленты работает push-доставка (push_worker).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Integer, JSON, String, Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, utcnow


class PushSubscription(TimestampMixin, Base):
    """Подписка устройства на push через Expo Push API."""
    __tablename__ = "push_subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # Expo Push Token (строитcя из installationId) — уникален на устройстве
    expo_token: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    platform: Mapped[str] = mapped_column(String(10), default="ios")  # ios|android|web
    device_label: Mapped[Optional[str]] = mapped_column(String(120))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # счётчик неудач подряд: при >=3 подписку деактивируем (DeviceNotRegistered и пр.)
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class MobileEvent(TimestampMixin, Base):
    """Событие для мобильного клиента (лента + источник push-сообщения)."""
    __tablename__ = "mobile_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True)
    type: Mapped[str] = mapped_column(String(40), index=True)
    # assigned | executor_added | status_changed | comment | due_soon | overdue | entity_updated
    title: Mapped[str] = mapped_column(String(500))         # заголовок push/ленты
    body: Mapped[Optional[str]] = mapped_column(Text)
    entity_uid: Mapped[Optional[str]] = mapped_column(String(36), index=True)
    module_key: Mapped[Optional[str]] = mapped_column(String(50))
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    pushed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
