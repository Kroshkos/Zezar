"""Telegram-команды администратора/координатора: полноценный помощник.

Помимо уведомлений бот умеет (только для ролей admin/coordinator):
  /broadcast [текст]   — рассылка: выбор аудитории кнопками, опциональные
                         фото/видео (прислать медиа в ответ на запрос),
                         подтверждение -> Notification + outbox telegram.manual;
  /report              — сводка: KPI по всем модулям, просрочки, очередь TG;
  /overdue             — список просроченных задач/проектов с ответственными;
  /pending             — запланированные и отправленные рассылки (+ id для /cancel);
  /find <запрос>       — поиск карточек по названию (видимые согласно RBAC);
  /task <id|префикс>   — карточка задачи/сущности: статус, срок, ответственные;
  /cancel <id>         — отмена запланированной рассылки;
  /media               — загрузить фото/видео в библиотеку бота, не начиная рассылку.

Медиа: файл, присланный админом, скачивается Bot API (getFile) и тут же
переотправляется ботом каждому адресату (sendPhoto/sendVideoDocument) — file_id
получателя привязан к боту, поэтому пересылка работает без повторной загрузки.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import build_context, entity_filter
from app.core.config import get_settings
from app.models.core import (
    Department, DepartmentMember, Entity, EntityKind, EntityResponsible,
    Notification, OutboxEvent, Role, TgAdminLog, User,
)
from integrations import telegram_service as tg

log = logging.getLogger("caesar.tg_admin")

# ---------------- состояния диалога (в памяти воркера) ----------------

_bcast: dict[int, dict] = {}     # chat_id -> черновик рассылки
_media_lib: dict[int, list] = {}  # chat_id -> [{file_id,type,size}] (для /broadcast)
_THROTTLE: dict[int, float] = {}


def _throttled(chat_id: int, key: str, seconds: float = 2.0) -> bool:
    k = (chat_id, key)
    now = time.time()
    if now - _THROTTLE.get(k, 0) < seconds:
        return True
    _THROTTLE[k] = now
    return False


async def _allow(session: AsyncSession, user: User) -> bool:
    ctx = await build_context(session, user)
    return bool(ctx.can_admin_system)


async def _audit(session: AsyncSession, user: User, action: str, details: str) -> None:
    session.add(TgAdminLog(user_id=user.id, action=action, details=details[:2000]))
    await session.commit()


def _esc(s) -> str:
    return tg._esc(s)


# ---------------- вспомогательные формatters ----------------

TERMINAL_FALLBACK = {"done", "closed", "canceled"}


async def _terminal_keys(session, kind: str) -> set[str]:
    """Завершающие статусы набора модуля (fallback — базовая тройка)."""
    from app.core.notify import terminal_status_keys
    try:
        keys = await terminal_status_keys(session, kind)
        return keys or TERMINAL_FALLBACK
    except Exception:  # noqa: BLE001
        return TERMINAL_FALLBACK


def _is_overdue(due: str | None, today_iso: str) -> bool:
    return bool(due) and due < today_iso


async def overdue_entities(session: AsyncSession, user: User, limit: int = 30):
    """Просроченные незавершённые задачи/проекты в видимой области пользователя."""
    ctx = await build_context(session, user)
    today = date.today().isoformat()
    term_task = await _terminal_keys(session, "task")
    term_proj = await _terminal_keys(session, "project")
    rows = (await session.scalars(
        select(Entity).where(
            Entity.kind.in_([EntityKind.TASK, EntityKind.PROJECT]),
            Entity.due_date.is_not(None),
            entity_filter(ctx))
        .order_by(Entity.due_date))).all()
    out = []
    for e in rows:
        term = term_task if str(getattr(e.kind, "value", e.kind)) == "task" else term_proj
        if (e.status_key or "") in term or not _is_overdue(e.due_date, today):
            continue
        out.append(e)
        if len(out) >= limit:
            break
    # имена ответственных
    ids = [e.id for e in out]
    resp_names: dict[int, list[str]] = {}
    if ids:
        q = (select(EntityResponsible.entity_id, User.display_name)
             .join(User, User.id == EntityResponsible.user_id)
             .where(EntityResponsible.entity_id.in_(ids)))
        for eid, name in (await session.execute(q)).all():
            resp_names.setdefault(eid, []).append(name)
    return out, resp_names


async def _entity_line(session, e: Entity, resp_names: dict[int, list[str]]) -> str:
    days = ""
    try:
        d = date.fromisoformat(e.due_date)
        delta = (d - date.today()).days
        days = f" · просрочено на {-delta} дн." if delta < 0 else (f" · через {delta} дн." if delta else " · сегодня")
    except (ValueError, TypeError):
        pass
    resp = ", ".join(resp_names.get(e.id, [])) or "не назначен"
    return (f"<b>{_esc(e.uid[:8])}</b> {_esc(e.title)}\n"
            f"    {_esc(e.module_key)} · {_esc(e.status_key or '—')}{days} · 👤 {_esc(resp)}")


# ---------------- команды ----------------

HELP_ADMIN = (
    "<b>🛠 Команды администратора</b>\n"
    "/report — сводка по системе\n"
    "/overdue — просроченные задачи и проекты\n"
    "/broadcast [текст] — рассылка уведомлений (можно прикрепить фото/видео)\n"
    "/pending — мои рассылки\n"
    "/cancel &lt;id&gt; — отменить запланированную рассылку\n"
    "/find &lt;запрос&gt; — поиск карточек\n"
    "/task &lt;id&gt; — детали карточки\n"
    "/media — загрузить фото/видео в библиотеку\n"
    "\nОбщие: /help, /mute, /unmute, /unbind"
)


async def cmd_help(session, user, args: str) -> str:
    base = ("Бот присылает уведомления ЦЕЗАРЬ. Команды: /mute — отключить, "
            "/unmute — включить, /unbind — отвязать Telegram.")
    if await _allow(session, user):
        return HELP_ADMIN + "\n\n" + base
    return base


async def cmd_report(session: AsyncSession, user: User, args: str) -> str:
    if not await _allow(session, user):
        return "⛔ Команда доступна только администраторам и координаторам."
    ctx = await build_context(session, user)
    cond = entity_filter(ctx)
    total = await session.scalar(select(func.count(Entity.id)).where(cond))
    by_kind = (await session.execute(
        select(Entity.kind, func.count(Entity.id)).where(cond).group_by(Entity.kind)
    )).all()
    active = await session.scalar(
        select(func.count(Entity.id)).where(cond,
                                            Entity.status_key.notin_(["done", "closed", "canceled"])))
    users_total = await session.scalar(
        select(func.count(User.id)).where(User.is_active.is_(True)))
    users_bound = await session.scalar(
        select(func.count(User.id)).where(User.is_active.is_(True),
                                          User.telegram_chat_id.is_not(None)))
    od, resp_names = await overdue_entities(session, user, limit=100)
    queued = await session.scalar(
        select(func.count(OutboxEvent.id)).where(
            OutboxEvent.processed_at.is_(None), OutboxEvent.status != "error"))
    errors = await session.scalar(
        select(func.count(OutboxEvent.id)).where(OutboxEvent.status == "error"))
    sched = await session.scalar(
        select(func.count(Notification.id)).where(Notification.status == "scheduled"))

    kinds_txt = " · ".join(f"{str(k.value if hasattr(k, 'value') else k)}: {c}"
                           for k, c in sorted(by_kind, key=lambda r: -r[1]))
    week_start = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)
    created_week = await session.scalar(
        select(func.count(Entity.id)).where(cond, Entity.created_at >= week_start))
    lines = [
        "<b>📊 Сводка ЦЕЗАРЬ</b>",
        f"Карточек всего: <b>{total or 0}</b> (активных: {active or 0}, создано за 7 дн.: {created_week or 0})",
        kinds_txt or "",
        f"⚠️ Просрочено: <b>{len(od)}</b>" + (f" (показаны {min(len(od), 5)} из {len(od)})" if len(od) > 5 else ""),
        f"👥 Пользователи: {users_bound or 0}/{users_total or 0} привязано к Telegram",
        f"📨 Очередь доставки: {queued or 0} в ожидании · {errors or 0} с ошибками",
        f"📢 Рассылок запланировано: {sched or 0}",
    ]
    if od:
        lines.append("\n<b>Ближайшие просрочки:</b>")
        for e in od[:5]:
            lines.append(await _entity_line(session, e, resp_names))
    return "\n".join(l for l in lines if l)


async def cmd_overdue(session: AsyncSession, user: User, args: str) -> str:
    if not await _allow(session, user):
        return "⛔ Команда доступна только администраторам и координаторам."
    od, resp_names = await overdue_entities(session, user, limit=20)
    if not od:
        return "✅ Просроченных задач и проектов нет."
    lines = [f"<b>⚠️ Просрочено ({len(od)}):</b>"]
    for e in od:
        lines.append(await _entity_line(session, e, resp_names))
    return "\n".join(lines)


async def cmd_pending(session: AsyncSession, user: User, args: str) -> str:
    if not await _allow(session, user):
        return "⛔ Команда доступна только администраторам и координаторам."
    rows = (await session.scalars(
        select(Notification).order_by(Notification.created_at.desc()).limit(15))).all()
    if not rows:
        return "Рассылок ещё не было."
    labels = {"scheduled": "🕒 запланирована", "sending": "📡 отправляется",
              "sent": "✅ отправлена", "canceled": "🚫 отменена"}
    lines = ["<b>📢 Рассылки</b> (<code>/cancel id</code> — отменить):"]
    for n in rows:
        media = f" · 📎{len(n.media_json)}" if n.media_json else ""
        lines.append(
            f"<b>#{n.id}</b> {_esc(n.title)} — {labels.get(n.status, n.status)}"
            f" · {n.delivered_count}/{n.recipients_count} доставлено{media}")
    return "\n".join(lines)


async def cmd_find(session: AsyncSession, user: User, args: str) -> str:
    if not await _allow(session, user):
        return "⛔ Команда доступна только администраторам и координаторам."
    q = (args or "").strip()
    if len(q) < 2:
        return "Использование: /find &lt;фрагмент названия&gt;"
    ctx = await build_context(session, user)
    like = f"%{q.lower()}%"
    rows = (await session.scalars(
        select(Entity).where(entity_filter(ctx),
                             or_(func.lower(Entity.title).like(like),
                                 func.lower(func.coalesce(Entity.description, "")).like(like)))
        .order_by(Entity.updated_at.desc()).limit(12))).all()
    if not rows:
        return f"По запросу «{_esc(q)}» ничего не найдено."
    lines = [f"<b>🔎 Найдено: {len(rows)}</b>"]
    for e in rows:
        lines.append(f"<b>{_esc(e.uid[:8])}</b> {_esc(e.title)} · {_esc(e.module_key)} · {_esc(e.status_key or '—')}")
    lines.append("\nДетали: /task &lt;первые 8 символов id&gt;")
    return "\n".join(lines)


async def cmd_task(session: AsyncSession, user: User, args: str) -> str:
    if not await _allow(session, user):
        return "⛔ Команда доступна только администраторам и координаторам."
    ref = (args or "").strip()
    if not ref:
        return "Использование: /task &lt;uid или первые символы&gt;"
    ctx = await build_context(session, user)
    ent = await session.scalar(
        select(Entity).where(entity_filter(ctx), Entity.uid.like(f"{ref}%")))
    if ent is None:
        return f"Карточка «{_esc(ref)}» не найдена (или недоступна вам)."
    resp_rows = (await session.scalars(
        select(EntityResponsible).where(EntityResponsible.entity_id == ent.id))).all()
    names = []
    for r in resp_rows:
        if r.user_id:
            u = await session.get(User, r.user_id)
            if u:
                names.append(u.display_name)
    base = (get_settings().public_base_url or "").rstrip("/")
    from app.core.notify import KIND_PAGE
    page = KIND_PAGE.get(str(getattr(ent.kind, "value", ent.kind)), "/")
    return (f"<b>📌 {_esc(ent.title)}</b>\n"
            f"ID: <code>{_esc(ent.uid)}</code>\n"
            f"Модуль: {_esc(ent.module_key)} · Статус: {_esc(ent.status_key or '—')}\n"
            f"Приоритет: {_esc(ent.priority_key or '—')} · Срок: {_esc(ent.due_date or '—')}\n"
            f"Ответственные: {_esc(', '.join(names) or 'нет')}\n"
            f"{'' if not ent.description else _esc((ent.description or '')[:300]) + chr(10)}"
            f"🔗 {base}{page}?entity={_esc(ent.uid)}")


async def cmd_cancel(session: AsyncSession, user: User, args: str) -> str:
    if not await _allow(session, user):
        return "⛔ Команда доступна только администраторам и координаторам."
    try:
        nid = int((args or "").strip())
    except ValueError:
        return "Использование: /cancel &lt;id рассылки&gt; (список — /pending)"
    n = await session.get(Notification, nid)
    if n is None:
        return f"Рассылка #{nid} не найдена."
    if n.status != "scheduled":
        return f"Нельзя отменить рассылку #{nid} в статусе «{_esc(n.status)}»."
    n.status = "canceled"
    await session.commit()
    await _audit(session, user, "tg_broadcast_cancel", f"#{nid} «{n.title}»")
    return f"🚫 Рассылка #{nid} «{_esc(n.title)}» отменена."


# ---------------- /broadcast: машина состояний ----------------

AUDIENCE_BUTTONS = [
    ("all", "👥 Всем"),
    ("heads", "🧑‍💼 Руководителям"),
    ("dept", "🏢 Подразделению…"),
    ("users", "🎯 По именам…"),
]


def _bcast_expire() -> None:
    now = time.time()
    for cid in [c for c, st in _bcast.items() if st.get("expires", 0) < now]:
        _bcast.pop(cid, None)
        _media_lib.pop(cid, None)


async def cmd_broadcast(session: AsyncSession, user: User, args: str) -> str:
    if not await _allow(session, user):
        return "⛔ Рассылки доступны только администраторам и координаторам."
    _bcast_expire()
    cid = int(user.telegram_chat_id)
    text = (args or "").strip()
    st = _bcast.get(cid)
    if st is None:
        st = {"step": "audience", "title": "", "body": "", "media": [],
              "audience": None, "expires": time.time() + 900}
        _bcast[cid] = st
    # подхватываем медиа, загруженные ранее через /media
    attach_pending_media(st, cid)
    if text:
        st["body"] = text
        st["title"] = text.split("\n", 1)[0][:120]
    hint = ("\n\n💡 Можно прикрепить фото/видео — просто отправьте медиа следующим "
            "сообщением (до подтверждения).")
    if not st["body"]:
        return ("Формат: /broadcast &lt;текст сообщения&gt;\n"
                "Например: <code>/broadcast Завтра в 10:00 планёрка в переговорной</code>" + hint)
    if st["step"] == "audience":
        st["step"] = "confirm_audience"
        opts = "\n".join(f"  <b>{num}.</b> {label}" for num, (_, label) in enumerate(AUDIENCE_BUTTONS, 1))
        media_note = f"\nУже прикреплено: {len(st['media'])} 📎" if st["media"] else ""
        return (f"<b>📢 Новая рассылка</b>\nТекст:\n<i>{_esc(st['body'])}</i>{media_note}\n\n"
                f"Выберите аудиторию числом:{hint}\n{opts}")
    return "Рассылка уже оформляется — отправьте число выбора аудитории."


async def handle_broadcast_input(session: AsyncSession, user: User, text: str) -> str | None:
    """Второй+ шаги машины /broadcast. Возвращает ответ или None (не наша ветка)."""
    cid = int(user.telegram_chat_id)
    st = _bcast.get(cid)
    if st is None:
        return None
    t = text.strip()

    if st["step"] == "confirm_audience":
        if t == "1":
            st["audience"] = {"mode": "all"}
            st["step"] = "confirm"
            return await _confirm_text(session, st)
        if t == "2":
            st["audience"] = {"mode": "heads"}
            st["step"] = "confirm"
            return await _confirm_text(session, st)
        if t == "3":
            st["step"] = "ask_dept"
            depts = (await session.execute(
                select(Department.id, Department.name).order_by(Department.name)
            )).all()
            if not depts:
                _bcast.pop(cid, None)
                return "Подразделения не заведены."
            st["depts"] = {str(i + 1): did for i, (did, _) in enumerate(depts)}
            lines = "\n".join(f"  <b>{i + 1}.</b> {_esc(name)}" for i, (did, name) in enumerate(depts))
            return f"Выберите подразделением числом:\n{lines}"
        if t == "4":
            st["step"] = "ask_users"
            return ("Через запятую укажите логины или фамилии получателей "
                    "(например: <code>ivanov i, petrov</code>).")
        if t in ("0", "отмена", "cancel"):
            _bcast.pop(cid, None)
            return "Рассылка отменена."
        return "Отправьте число 1–4 (или 0 — отмена)."

    if st["step"] == "ask_dept":
        did = st.get("depts", {}).get(t)
        if did is None:
            return "Такого подразделения нет. Число из списка или 0 — отмена."
        st["audience"] = {"mode": "department", "department_id": did, "include_head": True}
        st["step"] = "confirm"
        return await _confirm_text(session, st)

    if st["step"] == "ask_users":
        parts = [p.strip().lower() for p in t.replace(";", ",").split(",") if p.strip()]
        if not parts:
            return "Не понял список. Логины/фамилии через запятую или 0 — отмена."
        found_ids, unknown = [], []
        for p in parts:
            u = await session.scalar(
                select(User).where(or_(func.lower(User.login) == p,
                                       func.lower(User.display_name).like(f"%{p}%")))
                .limit(1)) if len(p) <= 64 else None
            if u and u.is_active:
                found_ids.append(u.id)
            else:
                unknown.append(p)
        if not found_ids:
            return f"Никого не нашёл по: {_esc(', '.join(unknown))}. Повторите список или 0 — отмена."
        st["audience"] = {"mode": "users", "user_ids": sorted(set(found_ids))}
        st["unknown"] = unknown
        st["step"] = "confirm"
        return await _confirm_text(session, st)

    if st["step"] == "confirm":
        low = t.lower()
        if low in ("да", "ok", "yes", "✅", "отправить"):
            return await _do_send(session, user, st)
        if low in ("нет", "отмена", "cancel", "0", "✖️"):
            _bcast.pop(cid, None)
            _media_lib.pop(cid, None)
            return "Рассылка отменена, ничего не отправлено."
        if low in ("текст", "редактировать"):
            st["step"] = "edit_text"
            return "Отправьте новый текст рассылки одним сообщением."
        if low.startswith("+") or low.startswith("добавить"):
            # добавить ещё медиа: библиотека остаётся, шаг не меняется
            return ("Отправьте фото/видео — добавлю к вложениям "
                    f"(сейчас: {len(st['media'])}). Для подтверждения напишите «да».")
        return "Напишите «да» — отправить, «текст» — изменить, «+» — ещё медиа, «нет» — отмена."

    if st["step"] == "edit_text":
        st["body"] = t
        st["title"] = t.split("\n", 1)[0][:120]
        st["step"] = "confirm"
        return await _confirm_text(session, st)
    return None


async def _confirm_text(session: AsyncSession, st: dict) -> str:
    aud = st.get("audience") or {}
    mode_labels = {"all": "всем активным", "heads": "руководителям подразделений",
                   "department": f"подразделению #{aud.get('department_id')}",
                   "users": f"адресно ({len(aud.get('user_ids', []))} чел.)"}
    cnt = await _audience_count(session, aud)
    media_note = (f"\nВложения: {_esc(', '.join(m['type'] for m in st['media']))}"
                  if st.get("media") else "\nВложения: нет")
    unk = (" ⚠️ не найдены: " + _esc(", ".join(st.get("unknown", [])))
           if st.get("unknown") else "")
    return ("<b>📢 Подтвердите рассылку</b>\n\n"
            f"<i>{_esc(st['body'])}</i>\n\n"
            f"Аудитория: {mode_labels.get(aud.get('mode'), aud.get('mode'))} — ~{cnt} получ.{unk}"
            f"{media_note}\n\n"
            "«да» — отправить сейчас · «текст» — изменить · «+» — добавить медиа · «нет» — отмена")


async def _audience_count(session, aud: dict) -> int:
    mode = aud.get("mode", "all")
    if mode == "all":
        return await session.scalar(select(func.count(User.id)).where(
            User.is_active.is_(True),
            or_(User.telegram_optout.is_(None), User.telegram_optout != "all"))) or 0
    if mode == "heads":
        return await session.scalar(
            select(func.count(func.distinct(DepartmentMember.user_id)))
            .where(DepartmentMember.role_in_dept == Role.HEAD)) or 0
    if mode == "users":
        return len(aud.get("user_ids", []))
    if mode == "department":
        return await session.scalar(
            select(func.count(DepartmentMember.user_id))
            .where(DepartmentMember.department_id == aud.get("department_id"))) or 0
    return 0


async def _do_send(session: AsyncSession, user: User, st: dict) -> str:
    cid = int(user.telegram_chat_id)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    n = Notification(title=st["title"] or "Сообщение", body=st["body"],
                     send_at=now, audience_json=st["audience"],
                     status="sending", created_by=user.id,
                     recipients_count=await _audience_count(session, st["audience"]),
                     media_json=st.get("media") or None)
    session.add(n)
    await session.flush()
    session.add(OutboxEvent(topic="telegram.manual", payload_json={"notification_id": n.id}))
    await session.commit()
    await _audit(session, user, "tg_broadcast",
                 f"#{n.id} «{n.title}» mode={st['audience'].get('mode')} media={len(st.get('media') or [])}")
    _bcast.pop(cid, None)
    _media_lib.pop(cid, None)
    media_note = f", 📎 вложений: {len(st['media'])}" if st.get("media") else ""
    return (f"✅ Рассылка <b>#{n.id}</b> принята в отправку (~{n.recipients_count} "
            f"получателей{media_note}). Статус — /pending")


# ---------------- медиа ----------------

async def download_and_rehost_file(file_unique_id_or_path: str, caption: str = "") -> dict | None:
    """Скачать файл из чата бота (getFile) — вернуть описание вложения.

    Храним file_id бота: он повторно используется в sendPhoto/sendVideo для
    любых чатов (один bot — один file_id namespace), отдельное хранилище не нужно.
    """
    s = get_settings()
    if not s.telegram_bot_token:
        return None
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            tg.BOT_API.format(token=s.telegram_bot_token, method="getFile"),
            json={"file_id": file_unique_id_or_path})
        info = (r.json().get("result") or {})
        path = info.get("file_path")
        if not path:
            return None
        size = int(info.get("file_size") or 0)
    return {"file_id": file_unique_id_or_path, "path": path, "size": size,
            "caption": caption[:300]}


async def handle_media(session: AsyncSession, user: User, msg: dict) -> str | None:
    """Медиа от привязанного администратора: в библиотеку текущего /broadcast
    или standalone-загрузка (/media)."""
    cid = int(user.telegram_chat_id)
    st = _bcast.get(cid)
    photo = msg.get("photo") or []
    video = msg.get("video")
    document = msg.get("document")
    caption = msg.get("caption") or ""
    if photo:
        fid = photo[-1]["file_id"]      # максимальное разрешение
        mtype = "photo"
    elif video:
        fid = video["file_id"]
        mtype = "video"
    elif document:
        mime = (document.get("mime_type") or "")
        if not mime.startswith(("image/", "video/")):
            return "Поддерживаются только фото и видео (или документ image/video)."
        fid = document["file_id"]
        mtype = "photo" if mime.startswith("image/") else "video"
    else:
        return None
    info = await download_and_rehost_file(fid, caption)
    if info is None:
        return "⚠️ Не удалось получить файл от Telegram. Попробуйте ещё раз."
    info["type"] = mtype
    if st is not None:
        st["media"].append(info)
        st["expires"] = time.time() + 900
        note = "\n" + await _confirm_text(session, st) if st.get("audience") else ""
        return (f"📎 Добавлено {mtype} ({_fmt_size(info['size'])}), всего вложений: "
                f"{len(st['media'])}.{note}")
    lib = _media_lib.setdefault(cid, [])
    lib.append(info)
    return (f"📎 Файл загружен в библиотеку ({_fmt_size(info['size'])}) — "
            f"{mtype}. Он будет прикреплён к следующей /broadcast автоматически.\n"
            f"В библиотеке: {len(lib)}.")


def attach_pending_media(st: dict, cid: int) -> None:
    lib = _media_lib.pop(cid, None)
    if lib and not st["media"]:
        st["media"] = list(lib)


def _fmt_size(b: int) -> str:
    if b >= 1048576:
        return f"{b / 1048576:.1f} МБ"
    if b >= 1024:
        return f"{b / 1024:.0f} КБ"
    return f"{b} Б"
