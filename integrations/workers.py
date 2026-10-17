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

from sqlalchemy import select, text

from app.db.engine import SessionLocal
from app.core import notify as notify_core
from app.models.core import (
    DepartmentMember, Entity, EntityKind, EntityResponsible, Notification,
    OutboxEvent, Role, SyncState, User,
)
from app.core.config import get_settings as _gs
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
                elif ev.topic == "telegram.due":
                    await _handle_tg_due(session, ev.payload_json or {})
                elif ev.topic == "telegram.manual":
                    await _handle_tg_manual(session, ev.payload_json or {})
                elif ev.topic == "telegram.test":
                    await _handle_tg_test(session, ev.payload_json or {})
                elif ev.topic in ("calendar.sync", "calendar.remove"):
                    await _handle_calendar(session, ev.topic, ev.payload_json or {})
                ev.status = "done"
                ev.processed_at = datetime.now(timezone.utc)
            except Exception as e:  # noqa: BLE001
                ev.status = "error"
                ev.last_error = str(e)[:2000]
                log.warning("outbox event %s failed (attempt %s): %s", ev.id, ev.attempts, e)
        await session.commit()


async def _recipients_map(session, ent_id: int) -> dict[int, list[str]]:
    """user_id -> ['resp'|'exec'] — все участники сущности.

    Ответственные — из EntityResponsible; исполнители — из денормализованного
    extra_json['executors'] (см. entity_service.create_entity/set_executors).
    """
    out: dict[int, list[str]] = {}
    resp_rows = (await session.scalars(
        select(EntityResponsible).where(EntityResponsible.entity_id == ent_id))).all()
    for r in resp_rows:
        if r.user_id:
            out.setdefault(r.user_id, []).append("resp")
    ent = await session.get(Entity, ent_id)
    for e in ((ent.extra_json or {}).get("executors") or []) if ent else []:
        uid = e.get("id") if isinstance(e, dict) else None
        if uid:
            out.setdefault(int(uid), []).append("exec")
    return out


async def _deliver_by_scenarios(session, p: dict, roles_default: list[str]) -> None:
    """Доставка события участникам сущности с фильтром по их эффективным сценариям.

    roles_default — кого уведомлять по умолчанию ('resp','exec'). Для каждого
    получателя проверяется: хотя бы один включённый сценарий с event_key из
    p['event_keys'] подходит под kind/module сущности. Базовые сценарии всегда
    включены — отключить их пользователь не может.
    """
    if not _gs().telegram_notify_enabled:
        return
    ent_id = p.get("entity_id")
    ent = await session.get(Entity, ent_id) if ent_id else None
    if ent is None:
        return
    recipients = await _recipients_map(session, ent_id)
    if not recipients:
        return
    ctx = notify_core.build_ctx(p)
    event_keys = set(p.get("event_keys") or [p.get("action", "")])
    kinds = p.get("kind", "")
    module = p.get("module", "")
    scn_cache: dict[int, tuple[set, dict]] = {}
    for uid, roles in recipients.items():
        u = await session.get(User, uid)
        if not (u and u.is_active and u.telegram_chat_id and u.telegram_optout != "all"):
            continue
        if uid not in scn_cache:
            enabled = await notify_core.enabled_event_keys(session, u, kinds, module)
            # все видимые сценарии пользователя для рендера подходящего шаблона
            vis = {s.event_key: s for s in await notify_core.visible_scenarios(session, u)}
            scn_cache[uid] = (enabled, vis)
        enabled, vis = scn_cache[uid]
        keys_hit = event_keys & enabled
        if not keys_hit:
            continue
        scn = None
        for k in sorted(keys_hit):
            cand = vis.get(k)
            if cand and (cand.is_base or roles_default):
                scn = cand
                break
        text_ = notify_core.render_scenario_text(scn, ctx)
        await tg.send_message(int(u.telegram_chat_id), text_)


async def _handle_tg_notify(session, p: dict) -> None:
    await _deliver_by_scenarios(session, p, ["resp", "exec"])


async def _handle_tg_due(session, p: dict) -> None:
    await _deliver_by_scenarios(session, p, ["resp", "exec"])


async def _handle_tg_manual(session, p: dict) -> None:
    nid = p.get("notification_id")
    n = await session.get(Notification, nid) if nid else None
    if n is None or n.status == "canceled":
        return
    aud = n.audience_json or {}
    mode = aud.get("mode", "all")
    ids: list[int] = []
    if mode == "all":
        ids = list((await session.scalars(
            select(User.id).where(User.is_active.is_(True)))).all())
    elif mode == "users":
        ids = [int(x) for x in aud.get("user_ids", [])]
    elif mode == "department":
        q = select(DepartmentMember.user_id).where(
            DepartmentMember.department_id == aud.get("department_id"))
        if not aud.get("include_head", True):
            q = q.where(DepartmentMember.role_in_dept != Role.HEAD)
        ids = list((await session.scalars(q)).all())
    elif mode == "heads":
        ids = list((await session.scalars(
            select(DepartmentMember.user_id).where(
                DepartmentMember.role_in_dept == Role.HEAD))).all())
    users = list((await session.scalars(select(User).where(
        User.id.in_(ids), User.is_active.is_(True)))).all()) if ids else []
    sent = 0
    base = (_gs().public_base_url or "").rstrip("/")
    for u in users:
        body = (n.body or "").replace("{name}", u.display_name).replace("{login}", u.login or "")
        text_ = f"<b>📢 {tg._esc(n.title)}</b>\n\n{tg._esc(body)}\n\n🔗 {base}"
        if u.telegram_chat_id and u.telegram_optout != "all":
            if await tg.send_message(int(u.telegram_chat_id), text_):
                sent += 1
        else:
            # Telegram не привязан — сохраняем внутри系统的 уведомление в журнале,
            # чтобы пользователь увидел его при входе (banner на /dashboard)
            log.info("manual notify #%s: пользователь %s не привязан к Telegram", n.id, u.login)
    n.delivered_count = sent
    n.status = "sent"
    n.sent_at = datetime.now(timezone.utc)


async def _handle_tg_test(session, p: dict) -> None:
    u = await session.get(User, p.get("user_id"))
    if u and u.telegram_chat_id:
        base = (_gs().public_base_url or "").rstrip("/")
        ok = await tg.send_message(int(u.telegram_chat_id),
                                   f"✅ Проверка связи с ЦЕЗАРЬ: система отправляет вам "
                                   f"уведомления корректно.\n🔗 {base}")
        if not ok:
            raise RuntimeError("sendMessage вернул ошибку Bot API")


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
    """Long-polling getUpdates: регистрация в боте (логин→пароль) + команды.

    handle_update возвращает (текст_ответа, [message_id для удаления из чата]).
    Ответ отправляется первым, затем входящие сообщения пользователя удаляются —
    так пароль и логин не остаются в истории диалога.
    """
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
                    reply, delete_ids = await tg.handle_update(session, upd)
                    chat = (upd.get("message") or upd.get("edited_message") or {}).get("chat", {})
                    chat_id = chat.get("id")
                    if chat_id is None:
                        continue
                    if reply:
                        await tg.send_message(chat_id, reply)
                    for mid in delete_ids:
                        await tg.delete_message(chat_id, mid)
        except Exception as e:  # noqa: BLE001
            log.warning("tg poller: %s", e)
            await asyncio.sleep(5)


async def notifier_worker(stop: asyncio.Event) -> None:
    """Плановые задачи уведомлений:
      1) напоминания «приближается срок» (due_soon) для задач/проектов;
      2) запуск рассылок администратора, у которых наступило send_at.
    """
    from app.core import notify as _nc
    interval = _gs().due_reminder_interval_seconds or 3600
    timer = 0.0
    while not stop.is_set():
        try:
            async with SessionLocal() as session:
                # 1) due reminders — раз в interval
                if timer >= interval:
                    timer = 0.0
                    n = await _nc.schedule_due_reminders(
                        session, days_ahead=_gs().due_remind_days_ahead or 3)
                    if n:
                        log.info("notifier: поставлено напоминаний о сроке: %s", n)
                # 2) scheduled broadcasts
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                due = (await session.scalars(
                    select(Notification).where(
                        Notification.status == "scheduled",
                        Notification.send_at <= now))).all()
                for nn in due:
                    await _nc.enqueue_manual(session, nn)
                    nn.status = "sending"
                if due:
                    await session.commit()
                    log.info("notifier: запущено рассылок: %s", len(due))
        except Exception as e:  # noqa: BLE001
            log.warning("notifier tick: %s", e)
        timer += 5
        try:
            await asyncio.wait_for(stop.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass
