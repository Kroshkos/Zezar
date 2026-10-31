# -*- coding: utf-8 -*-
"""Единая точка определения «текущего времени» платформы.

Сервер может находиться в другой стране (UTC или локальный часовой пояс
провайдера), поэтому вся бизнес-логика (дедлайны, фильтры, дашборд,
уведомления) должна работать по московскому времени, независимо от
системных часов хоста.

Использование:
    from app.core.timezone import now, today

    d = today()          # date по Москве
    dt = now()           # naive datetime по Москве (для БД/сравнений)
    aware = now(tz=True) # aware datetime с фиксированным смещением +03:00
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

# Московское время: стабильное смещение UTC+3 (MSK, без перехода на летнее
# время с 2014 года). Фиксированное смещение не требует базы tzdata внутри
# образа контейнера и работает одинаково на любом хосте.
MSK_TZ = timezone(timedelta(hours=3), name="MSK")


def now(tz: bool = False):
    """Текущие дата-время по Москве.

    По умолчанию возвращает naive datetime (удобно для сравнения со
    значениями из БД). При tz=True — aware datetime с смещением +03:00.
    """
    dt = datetime.now(MSK_TZ)
    return dt if tz else dt.replace(tzinfo=None)


def today() -> date:
    """Сегодняшняя дата по Москве."""
    return now().date()
