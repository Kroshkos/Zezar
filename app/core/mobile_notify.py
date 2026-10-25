"""Сервис мобильного уведомления: лента MobileEvent + доставка Expo Push.

Дизайн:
- emit_mobile_event() вызывается ТРАНЗАКЦИОННО из entity_service (вместе с
  записью outbox 'telegram.notify') — событие не потеряется, если push-сервис
  недоступен: лента переживёт любой простой воркера.
- push_worker (integrations/workers) периодически выбирает события с
  pushed_at IS NULL и отправляет на https://exp.host/--/api/v2/push/send
  пакетам по 100 (лимит Expo). Fallback-канал Telegram НЕ дублируется здесь —
  он уже покрыт штатным outbox-воркером.
- Ключ Expo push (опционально) берётся из env EXPO_PUSH_ACCESS_TOKEN; без него
  Expo принимает запросы в rate-limited режиме — для демо достаточно.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.models.core import User
from app.models.mobile import MobileEvent, PushSubscription

logger = logging.getLogger("caesar.mobile_notify")

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
PUSH_BATCH = 100


async def emit_mobile_event(
    session: AsyncSession, *, user_id: int, type_: str, title: str,
    body: Optional[str] = None, entity_uid: Optional[str] = None,
    module_key: Optional[str] = None, payload: Optional[dict] = None,
) -> MobileEvent:
    """Поставить событие в ленту пользователя (без commit — в рамках транзакции вызывающего)."""
    ev = MobileEvent(
        user_id=user_id, type=type_, title=title[:500], body=body,
        entity_uid=entity_uid, module_key=module_key, payload_json=payload or {},
    )
    session.add(ev)
    await session.flush()
    return ev


async def recipients_for_change(session: AsyncSession, ent, actor_id: Optional[int],
                                changes: dict) -> list[tuple[int, str, str]]:
    """Кому и что отправить при изменении карточки.

    Возвращает [(user_id, type, title)] — ответственные и исполнители, кроме
    самого актора (себе push не нужен). Тип зависит от изменения: статус —
    status_changed, срок — due_soon, иначе entity_updated.
    """
    from app.models.core import EntityResponsible

    if "status_key" in changes:
        etype, verb = "status_changed", "статус изменён"
    elif "due_date" in changes:
        etype, verb = "due_soon", "срок изменён"
    else:
        etype, verb = "entity_updated", "карточка обновлена"

    uids: set[int] = set()
    for r in ent.responsibles or []:
        if r.user_id:
            uids.add(r.user_id)
    exec_names = (ent.extra_json or {}).get("executors") or []
    # исполнители хранились именами в демо-формате — резолвим по display_name
    if exec_names:
        rows = (await session.scalars(
            select(User.id).where(User.display_name.in_(exec_names)))).all()
        uids.update(rows)
    uids.discard(actor_id)

    out = []
    for uid_ in uids:
        out.append((uid_, etype, f"{ent.title}: {verb}"))
    return out


async def pending_events(session: AsyncSession, limit: int = 200):
    """События, ещё не отправленные на push."""
    return (await session.scalars(
        select(MobileEvent).where(MobileEvent.pushed_at.is_(None))
        .order_by(MobileEvent.id).limit(limit))).all()


async def tokens_for_users(session: AsyncSession, user_ids: set[int]):
    rows = (await session.execute(
        select(PushSubscription.expo_token, PushSubscription.user_id)
        .where(PushSubscription.is_active == True,       # noqa: E712
               PushSubscription.user_id.in_(user_ids or [-1])))).all()
    return rows  # [(token, user_id)]


def _messages(events, token_map: dict[str, int]) -> list[dict]:
    """Собрать пул сообщений Expo: одно событие → N токенов устройства пользователя."""
    msgs: list[dict] = []
    by_user: dict[int, list] = {}
    for tok, uid_ in token_map.items():
        by_user.setdefault(uid_, []).append(tok)
    for ev in events:
        data = {"event_id": ev.id, "type": ev.type, "entity_uid": ev.entity_uid}
        for tok in by_user.get(ev.user_id, []):
            msgs.append({
                "to": tok, "title": ev.title, "body": ev.body or "",
                "data": data, "badge": None,
                "sound": "default",
            })
    return msgs


async def send_push(messages: list[dict]) -> list[dict]:
    """Отправить пакет на Expo Push API. Возвращает список ответов-приёмов."""
    if not messages:
        return []
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Host": "exp.host",
    }
    token = os.getenv("EXPO_PUSH_ACCESS_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(EXPO_PUSH_URL, headers=headers,
                                 content=json.dumps(messages))
        resp.raise_for_status()
        return resp.json().get("data", [])


async def mark_sent(session: AsyncSession, event_ids: list[int]) -> None:
    if not event_ids:
        return
    await session.execute(
        update(MobileEvent).where(MobileEvent.id.in_(event_ids))
        .values(pushed_at=utcnow()))


async def deactivate_bad_tokens(session: AsyncSession, receipts: list[dict],
                                sent_pairs: list[tuple[str, int]]) -> None:
    """Разобрать квитанции Expo: DeviceNotRegistered → деактивировать подписку."""
    dead: set[str] = set()
    for rcpt, (tok, _) in zip(receipts, sent_pairs):
        st = (rcpt or {}).get("status")
        detail = (rcpt or {}).get("details") or {}
        if st == "error" and (detail.get("error") == "DeviceNotRegistered"):
            dead.add(tok)
    if dead:
        await session.execute(
            update(PushSubscription)
            .where(PushSubscription.expo_token.in_(dead))
            .values(is_active=False))
        logger.info("Деактивировано подписок: %d", len(dead))
