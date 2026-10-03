"""
Фоновые воркеры: обработка Outbox (Telegram) и diff-проход календаря.
Запускаются как asyncio-задачи lifespan'а приложения (в прод — отдельным процессом).

Инварианты:
  - сеть НИКОГДА не блокирует HTTP-запрос (только эти фоны);
  - события outbox помечаются processed_at; ошибка -> attempts++, retry в следующем цикле;
  - календарь: pending+due_date -> upsert; deleted -> delete; sync_state=synced после успеха.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.db.engine import SessionLocal
from app.models.core import Entity, EntityKind, EntityResponsible, OutboxEvent, SyncState, User
from integrations import telegram_service as tg
from integrations import yandex_calendar as yc

log = logging.getLogger("caesar.workers")


async def outbox_worker(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await _outbox_once()
        except Exception as e:  # noqa: BLE001
            log.exception("outbox tick failed: %s", e)
        try:
            await asyncio.wait_for(stop.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass


async def _outbox_once() -> None:
    async with SessionLocal() as session:
        rows = (await session.scalars(
            select(OutboxEvent).where(OutboxEvent.processed_at.is_(None),
                                      OutboxEvent.attempts < 8)
            .order_by(OutboxEvent.id).limit(50))).all()
        for ev in rows:
            ev.attempts += 1
            try:
                if ev.topic == "telegram.notify":
                    await _handle_tg_notify(session, ev.payload_json or {})
                elif ev.topic in ("calendar.sync", "calendar.remove"):
                    await _handle_calendar(session, ev.topic, ev.payload_json or {})
                ev.status = "done"
                ev.processed_at = datetime.now(timezone.utc)
            except Exception as e:  # noqa: BLE001
                ev.status = "error"
                ev.last_error = str(e)[:2000]
                log.warning("outbox event %s failed (attempt %s): %s", ev.id, ev.attempts, e)
        await session.commit()


async def _handle_tg_notify(session, p: dict) -> None:
    ent_id = p.get("entity_id")
    ent = await session.get(Entity, ent_id) if ent_id else None
    if ent is None:
        return
    text = tg.format_entity_event(p)
    # уведомляем всех ответственных по всем подразделениям + автора
    resp_rows = (await session.scalars(
        select(EntityResponsible).where(EntityResponsible.entity_id == ent_id))).all()
    for r in resp_rows:
        if r.user_id:
            await tg.notify_user(session, r.user_id, text)


async def _handle_calendar(session, topic: str, p: dict) -> None:
    ent = await session.get(Entity, p.get("entity_id")) if p.get("entity_id") else None
    if ent is None:
        return
    if topic == "calendar.remove":
        # удаляем событие из календарей всех ответственных (им именно оно и показывалось)
        resp_rows = (await session.scalars(
            select(EntityResponsible).where(EntityResponsible.entity_id == ent.id,
                                            EntityResponsible.user_id != None))).all()  # noqa: E711
        for r in resp_rows:
            u = await session.get(User, r.user_id)
            if not u or not u.yandex_cal_app_password_enc or not _cal_login(u):
                continue
            try:
                client_path = await _cal_path(u)
                if client_path:
                    await yc.delete_event(_cal_login(u), u.yandex_cal_app_password_enc,
                                          client_path, ent.cal_uid)
            except Exception as e:  # noqa: BLE001
                log.warning("cal delete failed user=%s: %s", u.id, e)
        ent.sync_state = SyncState.DELETED
        return
    if not ent.due_date:
        ent.sync_state = SyncState.SYNCED
        return
    # нет ни одного привязанного календаря — держим PENDING (не ложный SYNCED),
    # cron-проход досинхронизирует после привязки app-пароля
    bound = [r for r in await session.scalars(
        select(EntityResponsible).where(EntityResponsible.entity_id == ent.id,
                                        EntityResponsible.user_id != None))  # noqa: E711
             if (await session.get(User, r.user_id)) is not None
             and (await session.get(User, r.user_id)).yandex_cal_app_password_enc]
    if not bound:
        ent.sync_state = SyncState.PENDING
        return
    # пишем событие в календари ответственных (по одному событию на человека — см. ТЗ)
    # прицепляем payload модуля -> хук календаря возьмёт точное время/место
    ent._cal_payload = await _attach_payload(session, ent)
    resp_rows = (await session.scalars(
        select(EntityResponsible).where(EntityResponsible.entity_id == ent.id,
                                        EntityResponsible.user_id != None))).all()  # noqa: E711
    any_fail = False
    for r in resp_rows:
        u = await session.get(User, r.user_id)
        if not u or not u.yandex_cal_app_password_enc or not _cal_login(u):
            continue  # пользователь не привязал календарь — ждём, не считаем ошибкой
        path = await _cal_path(u)
        if not path:
            any_fail = True
            continue
        ok = await yc.upsert_event(_cal_login(u), u.yandex_cal_app_password_enc, path, ent,
                                   u.display_name)
        if ok:
            ent.sync_state = SyncState.SYNCED
            ent.cal_synced_at = datetime.now(timezone.utc)
        else:
            any_fail = True
    if any_fail and ent.sync_state != SyncState.SYNCED:
        ent.sync_state = SyncState.ERROR
        raise RuntimeError(f"calendar upsert failed for entity {ent.uid}")


async def _attach_payload(session, ent: Entity):
    """Payload-таблица модуля для хука календаря (точное время совещаний и т.п.)."""
    from app.models.module_payloads import (DocPayload, GrantPayload, MeetingPayload,
                                            PriorityPayload, ProjectPayload, ScholarPayload,
                                            SoftPayload, TaskPayload)
    mapping = {
        EntityKind.TASK: TaskPayload,
        EntityKind.MEETING: MeetingPayload,
        EntityKind.PROJECT: ProjectPayload,
        EntityKind.DOC: DocPayload,
        EntityKind.SOFT: SoftPayload,
        EntityKind.GRANT: GrantPayload,
        EntityKind.SCHOL: ScholarPayload,
        EntityKind.PRIORITY: PriorityPayload,
    }
    model = mapping.get(ent.kind)
    if model is None:
        return None
    return await session.get(model, ent.id)


_cal_path_cache: dict[int, str] = {}



def _cal_login(u: User) -> str | None:
    """Логин аккаунта Яндекса для CalDAV Basic auth.

    Яндекс принимает в качестве имени пользователя логин аккаунта (поле `login`
    из OAuth userinfo). Email тоже работает, но только если он основной и совпадает
    с логином; при @yandex.ru/-доменах это одно и то же значение до @.
    """
    if u.yandex_login:
        return u.yandex_login
    if u.email:
        return u.email.split("@")[0]
    return None

async def _cal_path(u: User) -> str | None:
    """Путь CalDAV-коллекции «Цезарь» пользователя (создаётся при первом обращении)."""
    if u.id in _cal_path_cache:
        return _cal_path_cache[u.id]
    from app.core.security import decrypt_secret
    apw = decrypt_secret(u.yandex_cal_app_password_enc)
    if not apw:
        return None
    try:
        async with __import__("httpx").AsyncClient(timeout=20) as client:
            path = await yc.ensure_calendar(client, _cal_login(u) or "", apw,
                                            u.yandex_cal_name or "Цезарь")
        _cal_path_cache[u.id] = path
        return path
    except Exception as e:  # noqa: BLE001
        log.warning("ensure_calendar user=%s: %s", u.id, e)
        return None


async def tg_poller(stop: asyncio.Event) -> None:
    """Long-polling getUpdates (для webhook замените роутером /api/tg/webhook)."""
    from app.core.config import get_settings
    offset = 0
    while not stop.is_set():
        s = get_settings()
        if not s.telegram_bot_token:
            await asyncio.sleep(5)
            continue
        try:
            import httpx
            async with httpx.AsyncClient(timeout=35) as client:
                r = await client.get(
                    f"https://api.telegram.org/bot{s.telegram_bot_token}/getUpdates",
                    params={"offset": offset, "timeout": 30})
                updates = r.json().get("result", [])
            async with SessionLocal() as session:
                for upd in updates:
                    offset = max(offset, upd["update_id"] + 1)
                    reply = await tg.handle_update(session, upd)
                    if reply:
                        chat = upd["message"]["chat"]["id"]
                        await tg.send_message(chat, reply)
        except Exception as e:  # noqa: BLE001
            log.warning("tg poller: %s", e)
            await asyncio.sleep(5)
