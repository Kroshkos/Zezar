"""Фоновый push-воркер Zezar-app: лента MobileEvent → Expo Push API.

Цикл каждые POLL_SECONDS:
 1. выбираем события с pushed_at IS NULL;
 2. по их получателям берём активные подписки PushSubscription;
 3. формируем сообщения (одно событие → каждый токен устройства пользователя);
 4. отправляем пакетами по PUSH_BATCH на Expo, разбираем квитанции
    (DeviceNotRegistered → деактивация подписки);
 5. отмечаем pushed_at только для событий, по которым хотя бы один пакет ушёл
    успешно — при сетевой ошибке событие останется в очереди и будет
    переотправлено следующим циклом (at-least-once; дедупликация на клиенте
    по data.event_id).

События пользователей БЕЗ активных подписок помечаем отправленными сразу —
лента (GET /api/mobile/events) всё равно их отдаёт, а бесконечно сканировать
очередь ради несуществующих устройств незачем.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging

from app.db.engine import SessionLocal
from app.core import mobile_notify as mn

logger = logging.getLogger("caesar.push_worker")

POLL_SECONDS = 10


async def push_worker(stop: asyncio.Event) -> None:
    logger.info("push worker started")
    while not stop.is_set():
        try:
            await _cycle()
        except Exception as e:  # noqa: BLE001
            logger.warning("push cycle failed: %s", e)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=POLL_SECONDS)
    logger.info("push worker stopped")


async def _cycle() -> None:
    async with SessionLocal() as session:
        events = list(await mn.pending_events(session, limit=200))
        if not events:
            return
        user_ids = {ev.user_id for ev in events}
        rows = await mn.tokens_for_users(session, user_ids)
        token_map = {tok: uid for tok, uid in rows}

        subscribed = {uid for _, uid in rows}
        unsubscribed = [ev.id for ev in events if ev.user_id not in subscribed]
        if unsubscribed:
            await mn.mark_sent(session, unsubscribed)

        live_events = [ev for ev in events if ev.user_id in subscribed]
        sent_event_ids: set[int] = set()
        if live_events:
            messages = mn._messages(live_events, token_map)
            ok = True
            receipts_all: list[tuple[dict, tuple[str, int]]] = []
            for i in range(0, len(messages), mn.PUSH_BATCH):
                batch = messages[i:i + mn.PUSH_BATCH]
                pairs = [(m["to"], token_map[m["to"]]) for m in batch]
                try:
                    rc = await mn.send_push(batch)
                    receipts_all.extend(zip(rc, pairs))
                except Exception as e:  # noqa: BLE001
                    logger.warning("push batch failed (%d msgs): %s", len(batch), e)
                    ok = False
            if receipts_all:
                await mn.deactivate_bad_tokens(
                    session, [r for r, _ in receipts_all], [p for _, p in receipts_all])
            if ok:
                sent_event_ids.update(ev.id for ev in live_events)

        await mn.mark_sent(session, sorted(sent_event_ids))
        await session.commit()
        if sent_event_ids or unsubscribed:
            logger.info("push: events done=%d (sent=%d, no-device=%d)",
                        len(sent_event_ids | set(unsubscribed)),
                        len(sent_event_ids), len(unsubscribed))
