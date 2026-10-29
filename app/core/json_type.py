"""Устойчивый к «грязным» данным JSON-тип для SQLite.

Проблема в проде: часть БД содержит легаси-строки вместо корректного JSON
(например ``cards = "[]"`` записанные как TEXT, пустая строка ``""`` или невалидный
JSON). SQLAlchemy при чтении столбца типа ``JSON`` вызывает ``json.loads(value)``
без всякой защиты и падает с ``JSONDecodeError`` на любом GET-запросе, который
читает такую запись (см. traceback: /api/meetings/cards/api-list, /docs/).

Решение: собственный тип ``SafeJSON``, наследующий ``sqlalchemy.JSON`` и
подменяющий ``result_processor`` — если десериализация не удалась, вместо падения
возвращается значение по умолчанию (``None`` → NULL, иначе ``default()``).

Тип полностью совместим с существующей схемой: физический тип колонки остаётся
``JSON`` (SQLite хранит его как TEXT), миграции не требуются.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Optional

from sqlalchemy import TypeDecorator
from sqlalchemy.engine import Dialect
from sqlalchemy.sql import sqltypes


def _safe_json_loads(value: Any, default_factory: Callable[[], Any]) -> Any:
    """Десериализует значение из БД, никогда не бросая исключение."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):  # уже декодировано драйвером/кэшем
        return value
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except Exception:
            return default_factory()
    if not isinstance(value, str):
        return default_factory()
    text = value.strip()
    if not text:
        return default_factory()
    try:
        return json.loads(text)
    except Exception:
        # Невалидный JSON в легаси-строке — отдаём безопасный дефолт вместо 500.
        return default_factory()


class SafeJSON(TypeDecorator):
    """JSON-тип, устойчивый к невалидным/пустым строкам в колонке.

    Физический тип остаётся стандартным ``sqlalchemy.JSON`` (миграции не нужны),
    но на чтении вместо ``json.loads`` без защиты используется безопасная
    десериализация: битая строка → дефолт (``list``/``dict``/``None``), а не 500.

    ВАЖНО: реализован как ``TypeDecorator``, а не наследник ``JSON``: диалект
    SQLite оборачивает result-процессоры типов-наследников своим
    ``_sqlite_JSON_datetime_processor`` (см. base.py:1099), который вызывает
    оригинальный json.loads в обход переопределения — унаследованный override
    не срабатывает.
    """

    impl = sqltypes.JSON
    cache_ok = True

    def __init__(self, default_factory: Optional[Callable[[], Any]] = None, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._default_factory = default_factory or (lambda: None)

    def result_processor(self, dialect: Dialect, coltype: object):  # type: ignore[override]
        default_factory = self._default_factory

        def process(value: Any) -> Any:
            return _safe_json_loads(value, default_factory)

        return process
