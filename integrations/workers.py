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
                elif ev.topic in ("telegram.due", "telegram.overdue"):
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

    p['target_user_ids'] — адресная доставка (назначение ответственного/
    исполнителя, смена статуса): уведомление получают ТОЛЬКО эти пользователи,
    остальные участники не задействуются.

    Если ни один адресат реально не получил сообщение (нет chat_id / все
    отписались / нет подходящего включённого сценария) — бросается RuntimeError:
    событие остаётся в очереди со статусом error и видно во вкладке «Очередь
    доставки» админ-панели (иначе уведомления терялись молча).
    """
    if not _gs().telegram_notify_enabled:
        return
    ent_id = p.get("entity_id")
    ent = await session.get(Entity, ent_id) if ent_id else None
    if ent is None:
        return
    recipients = await _recipients_map(session, ent_id)
    targets = p.get("target_user_ids")
    if targets:
        # адресное событие: цели НЕ обязаны уже быть в карте получателей
        # (слот исполнителя мог ещё не создаться к моменту enqueue)
        recipients = {int(t): ["resp", "exec"] for t in targets if t}
    # автор действия не должен получать уведомление о собственном действии
    # (спам «сам себе» при смене статуса/редактировании своей карточки).
    # ВАЖНО: для широких событий (не адресных) исключаем автора только если у
    # него есть слот ответственного (роль 'resp'). Плоский список
    # extra_json['executors'] может содержать автора как purely-информационного
    # исполнителя — такие назначения делаются осознанно (см. set_executors),
    # и уведомление «вам назначено исполнение» автору-исполнителю не является
    # спамом «сам себе».
    actor_uid = p.get("actor_user_id")
    # Явное адресное назначение (event_key assigned/executor_added): уведомление
    # положено адресату даже если он сам является автором события («назначение
    # на себя» в своей карточке) — это не спам «сам себе», а системный факт.
    is_assignment = bool(targets) and set(p.get("event_keys") or []) & {"assigned", "executor_added"}
    # Системные события планировщика (напоминания о сроке/просрочке): автор
    # карточки НЕ исключается. Это не чьё-то действие, а сигнал «срок
    # истекает/истёк» — его обязан получать ответственный, даже если он же
    # автор (штатный случай «задача на себе»). Иначе единственный участник
    # отсеивался анти-спам фильтром и напоминание молча не доставлялось
    # («получателей: 1, отфильтровано по сценариям: 1»).
    is_system_scheduled = p.get("topic") in ("telegram.due", "telegram.overdue") \
        or p.get("actor") == "планировщик" or p.get("action") in ("due_soon", "overdue")
    if actor_uid and not is_system_scheduled and not is_assignment:
        if targets:
            # Для адресных событий фильтрация обычно сделана на этапе enqueue
            # (entity_service исключает актёра из target_user_ids), но payload
            # мог быть поставлен другим кодом (бот, админ-панель) без этой
            # проверки — исключаем автора действия здесь всегда, иначе автор
            # получает уведомление «сам себе» (спам).
            recipients.pop(int(actor_uid), None)
        elif "resp" in recipients.get(int(actor_uid), []):
            recipients.pop(int(actor_uid), None)
    if not recipients:
        # Получателей нет вообще (сущность без ответственных/исполнителей или
        # все исключены как авторы действия). Для адресных событий (есть
        # target_user_ids) это аномалия — поднимаем ошибку, чтобы событие
        # осталось видным во вкладке «Очередь доставки» админ-панели.
        # Для широких событий (напр. напоминание о сроке задачи, где
        # единственный участник — её автор) — штатная ситуация «в никуда»:
        # иначе планировщик при каждом проходе ставит их заново и очередь
        # забивается 8-кратными ретраями.
        if targets:
            raise RuntimeError(
                f"нет получателей уведомления ({p.get('action')}) у сущности "
                f"{p.get('entity_uid')} — никто не назначен")
        log.info("tg notify (%s): нет получателей у %s — событие пропущено",
                 p.get("action"), p.get("entity_uid"))
        return
    ctx = notify_core.build_ctx(p)
    event_keys = set(p.get("event_keys") or [p.get("action", "")])
    # kind/module нормализуем к строке: часть постановщиков пишет в payload
    # EntityKind(str, Enum), а не .value; JSON-колонка после перезапуска может
    # вернуть enum-совместимое значение — сравнения с entity_kinds тогда
    # молча проваливаются и события отбрасываются фильтром («отфильтровано по
    # сценариям», уведомления исчезают).
    kinds = getattr(p.get("kind", ""), "value", None) or (ent.kind.value if ent else "")
    module = getattr(p.get("module", ""), "value", None) or (ent.module_key if ent else "")
    scn_cache: dict[int, tuple[set, dict]] = {}
    delivered = 0
    eligible = 0          # получатели, которым сообщение положено по сценариям
    skipped_no_chat: list[str] = []
    skipped_muted: list[str] = []
    skipped_filter = 0
    for uid, roles in recipients.items():
        u = await session.get(User, uid)
        if not (u and u.is_active):
            continue
        if not u.telegram_chat_id:
            skipped_no_chat.append(u.login or str(uid))
            continue
        if u.telegram_optout == "all":
            # пользователь отключил уведомления (/mute) — это НЕ «нет chat_id»
            # и НЕ «отфильтрован по сценариям»: учитываем отдельно, иначе в
            # журнале появляется ложная формулировка про сценарии
            skipped_muted.append(u.login or str(uid))
            continue
        if uid not in scn_cache:
            enabled = await notify_core.enabled_event_keys(session, u, kinds, module)
            # все видимые сценарии пользователя для рендера подходящего шаблона
            vis = {s.event_key: s for s in await notify_core.visible_scenarios(session, u)}
            scn_cache[uid] = (enabled, vis)
        enabled, vis = scn_cache[uid]
        keys_hit = event_keys & enabled
        if not keys_hit:
            skipped_filter += 1
            log.info("tg notify (%s): %s не получил уведомление — ни один включённый "
                     "сценарий не подходит (событие: %s; сущность: kind=%s module=%s)",
                     p.get("action"), u.login or uid, sorted(event_keys), kinds, module)
            continue
        eligible += 1
        scn = None
        for k in sorted(keys_hit):
            cand = vis.get(k)
            if cand and (cand.is_base or roles_default):
                scn = cand
                break
        text_ = notify_core.render_scenario_text(scn, ctx)
        ok = await tg.send_message(int(u.telegram_chat_id), text_)
        if ok:
            delivered += 1
    if delivered == 0 and eligible > 0:
        # Реальная попытка отправки была и не удалась — ошибка, будет ретрай.
        raise RuntimeError(
            f"уведомление не доставлено ни одному адресату из {eligible}: "
            f"ошибка Telegram API (send_message вернул False)")
    if delivered == 0:
        # Никому не положено (все отфильтрованы по сценариям / отписались через
        # /mute) или нет chat_id — это штатная ситуация «обработано в никуда»,
        # а не ошибка: иначе планировщик reminder-событий при каждом проходе
        # ставит их заново и очередь забивается 8-кратными ретраями.
        # В журнале явно разделяем причины, чтобы «отписался» не выглядел как
        # «отфильтровано по сценариям».
        parts = [f"получателей: {len(recipients)}"]
        if skipped_no_chat:
            parts.append(f"без chat_id: {len(skipped_no_chat)} ({', '.join(skipped_no_chat)})")
        if skipped_muted:
            parts.append(f"отписались (/mute): {len(skipped_muted)} "
                         f"({', '.join(skipped_muted)})")
        if skipped_filter:
            parts.append(f"отфильтровано по сценариям: {skipped_filter}")
        log.info("tg notify (%s): не доставлено — %s — событие завершено без ошибки",
                 p.get("action"), ", ".join(parts))
        return


async def _handle_tg_notify(session, p: dict) -> None:
    payload = dict(p or {})
    if "event_keys" not in payload:
        # обратная совместимость: события, поставленные до introduction
        # event_keys (например, legacy OutboxEvent из entity_service)
        from app.core.notify import ACTION_EVENT_KEYS
        action = payload.get("action", "")
        payload["event_keys"] = sorted(ACTION_EVENT_KEYS.get(action, {action}))
    await _deliver_by_scenarios(session, payload, ["resp", "exec"])


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
    media = n.media_json or []
    for u in users:
        body = (n.body or "").replace("{name}", u.display_name).replace("{login}", u.login or "")
        text_ = f"<b>📢 {tg._esc(n.title)}</b>\n\n{tg._esc(body)}\n\n🔗 {base}"
        if u.telegram_chat_id and u.telegram_optout != "all":
            # сначала медиа (фото/видео), затем текст — как в мессенджерах;
            # сбой вложения не отменяет доставку текста
            for m in media:
                try:
                    await tg.send_media(int(u.telegram_chat_id), m)
                except Exception as e:  # noqa: BLE001
                    log.warning("broadcast #%s: media send failed user=%s: %s", n.id, u.login, e)
            if await tg.send_message(int(u.telegram_chat_id), text_):
                sent += 1
        else:
            # Telegram не привязан — сохраняем внутреннее уведомление в журнале,
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
                    params={"offset": offset, "timeout": 30,
                            "allowed_updates": ["message", "edited_message",
                                                "callback_query"]})
                updates = r.json().get("result", [])
            async with SessionLocal() as session:
                for upd in updates:
                    offset = max(offset, upd["update_id"] + 1)
                    reply, delete_ids, keyboard = await tg.handle_update(session, upd)
                    cbq = upd.get("callback_query")
                    if cbq:
                        # handle_callback может вернуть (текст, клавиатура) — когда
                        # вместе с текстом нужно сменить закреплённую reply-клавиатуру
                        if isinstance(reply, tuple):
                            reply, keyboard = reply
                        else:
                            keyboard = None
                        # ответ на нажатие кнопки: редактируем сообщение бота или
                        # отправляем новое; часики на кнопке закрываем всегда
                        chat_id = (cbq.get("message") or {}).get("chat", {}).get("id")
                        if chat_id is None:
                            continue
                        msg_id = cbq["message"]["message_id"]
                        if reply is not None:
                            edited = await tg.edit_message_text(chat_id, msg_id, reply,
                                                                keyboard)
                            if not edited and keyboard is None:
                                # «повторное нажатие» — текст не изменился, Telegram
                                # вернёт error; молча пропускаем (часики закроем ниже)
                                pass
                            elif not edited and keyboard is not None:
                                await tg.send_message(chat_id, reply, keyboard)
                        await tg.answer_callback(cbq["id"])
                        continue
                    chat = (upd.get("message") or upd.get("edited_message") or {}).get("chat", {})
                    chat_id = chat.get("id")
                    if chat_id is None:
                        continue
                    if reply:
                        # inline-клавиатура (dict или строки с callback_data) — под
                        # сообщение; обычная reply-клавиатура закрепляется над полем
                        # ввода отдельным «сервисным» сообщением, чтобы не дублировать
                        # кнопки в истории чата.
                        kb_inline = isinstance(keyboard, dict) or (
                            isinstance(keyboard, list) and tg._is_inline(keyboard))
                        await tg.send_message(chat_id, reply,
                                              keyboard if kb_inline else None)
                        if keyboard is not None and not kb_inline:
                            await tg.set_chat_reply_keyboard(chat_id, keyboard)
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
