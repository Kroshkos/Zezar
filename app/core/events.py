"""Журнал событий системы (EventLog).

Единая точка записи: сюда пишется ВСЁ, что происходит — вход/выход, неудачные
попытки входа, смена пароля, действия администратора с пользователями и
подразделениями, CRUD бизнес-сущностей всех модулей (через entity_service),
системные события старта.

Дизайн:
  * log_event(session, ...) — ORM-запись в текущей транзакции вызывающего
    (событие фиксируется атомарно с действием; если транзакция откатится —
    откатится и запись журнала);
  * write_event(...) — автономная запись в собственной сессии: для мест вне
    HTTP-транзакций (lifespan/стартап, диагностика). Никогда не бросает
    исключений наружу — журнал не должен ломать основной поток;
  * read_events / event_stats — чтение для страницы и API журнала.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import Request
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.core import EventLog, User

log = logging.getLogger("caesar.events")


def client_ip(request: Optional[Request]) -> Optional[str]:
    """IP клиента с учётом reverse-proxy хостингов (X-Forwarded-For: первый адрес)."""
    if request is None:
        return None
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()[:64]
    return request.client.host if request.client else None


async def log_event(session: AsyncSession, action: str, *, category: str = "system",
                    user: Optional[User] = None, actor_name: Optional[str] = None,
                    user_id: Optional[int] = None,
                    object: Optional[str] = None, ip: Optional[str] = None,
                    level: str = "info", message: Optional[str] = None,
                    details: Optional[dict] = None) -> EventLog:
    """Записать событие в журнал (в транзакции вызывающего, commit — на его стороне).

    Поля актора читаются лениво: если объект ``user`` мог быть expired/detached
    (например, после invalidate_session/commit в другом сервисе), передавайте
    явно ``user_id=...`` и ``actor_name=...`` скалярными значениями — иначе
    lazy-load вне сессии бросит MissingGreenlet.
    """
    ev = EventLog(
        action=action[:60], category=category[:30],
        user_id=user_id if user_id is not None else (user.id if user else None),
        user_name=(actor_name or (user.display_name if user else None)),
        object=(object or "")[:200] or None,
        ip=ip, level=level, message=message, details_json=details or {},
    )
    session.add(ev)
    return ev


async def write_event(action: str, **kw) -> None:
    """Автономная запись события (собственная сессия). Исключения глотаем:
    журнал не должен ронять стартовые процедуры или запросы."""
    from app.db.engine import SessionLocal
    try:
        async with SessionLocal() as s:
            await log_event(s, action, **kw)
            await s.commit()
    except Exception as e:  # noqa: BLE001
        log.warning("event_log: не удалось записать '%s': %s", action, e)


async def read_events(session: AsyncSession, *, limit: int = 200, offset: int = 0,
                      category: Optional[str] = None, action: Optional[str] = None,
                      user_id: Optional[int] = None, q: Optional[str] = None):
    """Чтение журнала (новые сверху) + общее число подходящих записей."""
    stmt = select(EventLog)
    if category:
        stmt = stmt.where(EventLog.category == category)
    if action:
        stmt = stmt.where(EventLog.action == action)
    if user_id:
        stmt = stmt.where(EventLog.user_id == user_id)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(EventLog.message.like(like),
                               EventLog.object.like(like),
                               EventLog.user_name.like(like),
                               EventLog.action.like(like)))
    total = await session.scalar(
        select(func.count()).select_from(stmt.subquery())) or 0
    rows = (await session.scalars(
        stmt.order_by(EventLog.at.desc(), EventLog.id.desc())
        .limit(max(1, min(limit, 1000))).offset(max(0, offset)))).all()
    items = [{
        "id": r.id,
        "at": r.at.isoformat(timespec="seconds") + "Z" if r.at else "",
        "user": r.user_name or ("—" if not r.user_id else f"#{r.user_id}"),
        "action": r.action, "category": r.category, "object": r.object,
        "ip": r.ip, "level": r.level, "message": r.message,
        "details": r.details_json or {},
    } for r in rows]
    return items, total


async def event_stats(session: AsyncSession) -> dict:
    """Сводка по журналу: всего записей, по категориям, по уровням, last event."""
    total = await session.scalar(select(func.count(EventLog.id))) or 0
    by_cat = dict((await session.execute(
        select(EventLog.category, func.count(EventLog.id))
        .group_by(EventLog.category))).all())
    by_level = dict((await session.execute(
        select(EventLog.level, func.count(EventLog.id))
        .group_by(EventLog.level))).all())
    last_at = await session.scalar(select(func.max(EventLog.at)))
    return {"total": total, "by_category": by_cat, "by_level": by_level,
            "last_at": last_at.isoformat() if last_at else None}
