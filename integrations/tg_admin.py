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
    "\nВсе функции доступны кнопками — нажмите «🛠 Меню администратора» или /start.\n"
    "\nОбщие: /help, /mute, /unmute, /unbind"
)


# ---------------- inline-клавиатуры (кнопки видны только админу/координатору) ----------------

def _btn(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def _rows(pairs: list[tuple[str, str]], per_row: int = 2) -> list:
    btns = [_btn(t, d) for t, d in pairs]
    return [btns[i:i + per_row] for i in range(0, len(btns), per_row)]


def main_keyboard(is_admin: bool) -> list | None:
    """Главное меню. Кнопки админ-функций — только если роль прошла _allow."""
    if not is_admin:
        return None
    return _rows([
        ("📊 Сводка", "adm:report"),
        ("⚠️ Просрочки", "adm:overdue"),
        ("📢 Рассылка", "adm:bcast"),
        ("🗂 Мои рассылки", "adm:pending"),
        ("🔎 Поиск", "adm:find"),
        ("📌 Карточка", "adm:task"),
        ("🖼 Медиа-библиотека", "adm:media"),
        ("ℹ️ Помощь", "adm:help"),
    ])


def back_keyboard() -> list:
    return [[_btn("« Меню", "adm:menu")]]


def cancel_keyboard() -> list:
    """Меню + подсказка: отмена рассылки — кнопкой из списка /pending."""
    return [[_btn("« Меню", "adm:menu")],
            [_btn("🚫 Отменить рассылку…", "adm:cancelpick")]]


def bcast_kb(step: str, rows: list) -> tuple[str, list]:
    """Единый формат шагов машины рассылки: текст + кнопки вместо ввода чисел."""
    return step + "\n\n" + "\n".join(
        " ".join(f"[{b['text']}]" for b in row) for row in rows), rows


def audience_keyboard() -> list:
    return _rows([(label, f"bc:aud:{key}") for key, label in AUDIENCE_BUTTONS]) \
        + [[_btn("✖️ Отмена", "bc:cancel")]]


def dept_keyboard(items: list[tuple[str, str]]) -> list:
    rows = _rows(items, per_row=1)
    rows.append([_btn("✖️ Отмена", "bc:cancel")])
    return rows


def confirm_keyboard() -> list:
    return [[_btn("✅ Отправить", "bc:send"), _btn("✖️ Отмена", "bc:cancel")],
            [_btn("✏️ Изменить текст", "bc:edit"), _btn("➕ Добавить медиа", "bc:addmedia")]]


def pending_keyboard(rows: list) -> list:
    kb = list(rows)
    kb.append([_btn("« Меню", "adm:menu")])
    return kb


def find_keyboard(items: list[tuple[str, str]]) -> list:
    kb = _rows(items, per_row=1)
    kb.append([_btn("🔎 Искать ещё", "adm:find"), _btn("« Меню", "adm:menu")])
    return kb


async def cmd_menu(session: AsyncSession, user: User) -> str:
    """Текст главного меню под клавиатуру main_keyboard()."""
    if not await _allow(session, user):
        return ("👋 <b>{name}</b>, бот ЦЕЗАРЬ присылает уведомления.\n\n"
                "Команды: /mute — отключить, /unmute — включить, "
                "/unbind — отвязать Telegram.").format(name=_esc(user.display_name))
    return ("👋 <b>{name}</b>, это меню администратора ЦЕЗАРЬ.\n"
            "Выберите действие кнопкой (команды текстом тоже работают — /help):").format(
        name=_esc(user.display_name))


async def cmd_find_kb(session: AsyncSession, user: User) -> tuple[str, list]:
    """Кнопка «🔎 Поиск»: список последних карточек — по нажатию открываются детали."""
    if not await _allow(session, user):
        return "⛔ Доступно только администраторам.", back_keyboard()
    ctx = await build_context(session, user)
    rows = (await session.scalars(
        select(Entity).where(entity_filter(ctx))
        .order_by(Entity.updated_at.desc()).limit(10))).all()
    if not rows:
        return "Карточек пока нет.", back_keyboard()
    items = [(f"{e.uid[:8]} · {(e.title or '')[:40]}", f"adm:task:{e.uid[:10]}") for e in rows]
    kb = find_keyboard(items)
    return ("<b>🔎 Последние карточки</b> — нажмите, чтобы увидеть детали, "
            "или «Искать ещё» и наберите текст запроса:"), kb


async def cmd_cancelpick(session: AsyncSession, user: User) -> tuple[str, list]:
    """Кнопка «🚫 Отменить рассылку…»: planned-рассылки отменяются одним тапом."""
    if not await _allow(session, user):
        return "⛔ Доступно только администраторам.", back_keyboard()
    rows = (await session.scalars(
        select(Notification).where(Notification.status == "scheduled")
        .order_by(Notification.created_at.desc()).limit(10))).all()
    if not rows:
        return "Запланированных рассылок нет.", back_keyboard()
    kb = _rows([(f"🚫 #{n.id} · {(n.title or '')[:36]}", f"adm:cancel:{n.id}")
                for n in rows], per_row=1)
    kb.append([_btn("« Меню", "adm:menu")])
    return "<b>🚫 Выберите рассылку для отмены:</b>", kb


# ---------------- обработчик нажатий кнопок ----------------

async def handle_callback(session: AsyncSession, cbq: dict) -> str | None:
    """Нажатие inline-кнопки. Возвращает НОВЫЙ текст сообщения (None — без изменений).

    Безопасность: права проверяются по автору callback_query (через его чат-id),
    а не по тому, кто видит сообщение, — подделать data нельзя, но даже с
    выдуманным data пользователь без роли получит отказ.
    """
    cq_chat = (cbq.get("message") or {}).get("chat", {})
    cid = cq_chat.get("id")
    from_id = (cbq.get("from") or {}).get("id")
    data = cbq.get("data") or ""
    if cid is None or from_id is None:
        return None
    user = await session.scalar(select(User).where(User.telegram_chat_id == str(cid)))
    if user is None:
        return "Диалог не привязан — отправьте свой логин ЦЕЗАРЬ."
    if int(from_id) != int(cid):
        return "⛔ Это сообщение не ваше."
    if not data.startswith("adm:") and not data.startswith("bc:"):
        return None
    if not await _allow(session, user):
        return "⛔ Административные кнопки доступны только администраторам и координаторам."

    # ---- главное меню ----
    if data == "adm:menu":
        return await cmd_menu(session, user)
    if data == "adm:report":
        return await cmd_report(session, user, "")
    if data == "adm:overdue":
        return await cmd_overdue(session, user, "")
    if data == "adm:help":
        return HELP_ADMIN
    if data == "adm:media":
        return ("🖼 <b>Медиа-библиотека</b>\nОтправьте фото или видео следующим "
                "сообщением — оно будет автоматически прикреплено к следующей рассылке.")
    if data == "adm:find":
        text, kb = await cmd_find_kb(session, user)
        return text + _kb_note(kb)
    if data == "adm:cancelpick":
        text, kb = await cmd_cancelpick(session, user)
        return text + _kb_note(kb)
    if data.startswith("adm:task:"):
        ref = data.split(":", 2)[2]
        text = await cmd_task(session, user, ref)
        return text + _kb_note(back_keyboard())
    if data.startswith("adm:cancel:"):
        try:
            nid = int(data.split(":", 2)[2])
        except ValueError:
            return "Некорректный id рассылки." + _kb_note(back_keyboard())
        text = await cmd_cancel(session, user, str(nid))
        return text + _kb_note(back_keyboard())
    if data == "adm:pending":
        text = await cmd_pending(session, user, "")
        return text + _kb_note(cancel_keyboard())
    if data == "adm:bcast":
        text, kb = await cmd_broadcast(session, user, "")
        return text + _kb_note(kb)

    # ---- шаги машины рассылки (bc:*) ----
    if data == "bc:cancel":
        st = _bcast.pop(int(cid), None)
        _media_lib.pop(int(cid), None)
        return "Рассылка отменена, ничего не отправлено." + _kb_note(main_keyboard(True))
    if data == "bc:back":
        st = _bcast.get(int(cid))
        if st is None:
            return "Черновик рассылки уже закрыт." + _kb_note(main_keyboard(True))
        st["step"] = "confirm_audience"
        st["expires"] = time.time() + 900
        text, kb = await _show_audience(st)
        return text + _kb_note(kb)
    if data == "bc:addmedia":
        st = _bcast.get(int(cid))
        if st is None:
            return "Черновик рассылки уже закрыт." + _kb_note(main_keyboard(True))
        st["expires"] = time.time() + 900
        return ("📎 Отправьте фото или видео следующим сообщением — добавлю к "
                f"вложениям (сейчас: {len(st['media'])}). После загрузки снова "
                "нажмите «Добавить медиа» или подтвердите рассылку.") + _kb_note(confirm_keyboard())
    if data == "bc:edit":
        st = _bcast.get(int(cid))
        if st is None:
            return "Черновик рассылки уже закрыт." + _kb_note(main_keyboard(True))
        st["step"] = "edit_text"
        st["expires"] = time.time() + 900
        return "✏️ Отправьте новый текст рассылки одним сообщением."
    if data == "bc:text":
        st = _bcast.get(int(cid))
        if st is None:
            return "Черновик рассылки устарел — начните заново: кнопка «📢 Рассылка»." \
                   + _kb_note(main_keyboard(True))
        st["step"] = "ask_text"
        st["expires"] = time.time() + 900
        return "✏️ Отправьте текст рассылки одним сообщением."
    if data == "bc:send":
        st = _bcast.get(int(cid))
        if st is None or st.get("step") != "confirm":
            return "Черновик рассылки устарел — начните заново: кнопка «📢 Рассылка»." \
                   + _kb_note(main_keyboard(True))
        text = await _do_send(session, user, st)
        return text + _kb_note(main_keyboard(True))
    if data.startswith("bc:aud:"):
        mode = data.split(":", 2)[2]
        st = _bcast.get(int(cid))
        if st is None:
            return "Черновик рассылки устарел — начните заново: кнопка «📢 Рассылка»." \
                   + _kb_note(main_keyboard(True))
        st["expires"] = time.time() + 900
        if mode in ("all", "heads"):
            st["audience"] = {"mode": mode}
            st["step"] = "confirm"
            text = await _confirm_text(session, st)
            return text + _kb_note(confirm_keyboard())
        if mode == "dept":
            depts = (await session.execute(
                select(Department.id, Department.name).order_by(Department.name)
            )).all()
            if not depts:
                st["step"] = "confirm_audience"
                return "Подразделения не заведены." + _kb_note(audience_keyboard())
            st["depts"] = {str(i + 1): did for i, (did, _) in enumerate(depts)}
            st["step"] = "ask_dept"
            items = [(_esc(name)[:36], f"bc:dept:{i + 1}") for i, (did, name) in enumerate(depts[:15])]
            return "<b>🏢 Выберите подразделение:</b>" + _kb_note(dept_keyboard(items))
        if mode == "users":
            st["step"] = "ask_users"
            return ("🎯 Через запятую укажите логины или фамилии получателей "
                    "(например: <code>ivanov i, petrov</code>).\n"
                    "0 — назад к выбору аудитории.")
        return None
    if data.startswith("bc:dept:"):
        st = _bcast.get(int(cid))
        if st is None or st.get("step") != "ask_dept":
            return "Шаг рассылки устарел — начните заново." + _kb_note(main_keyboard(True))
        did = st.get("depts", {}).get(data.split(":", 2)[2])
        if did is None:
            return "Такого подразделения нет." + _kb_note(back_keyboard())
        st["audience"] = {"mode": "department", "department_id": did, "include_head": True}
        st["step"] = "confirm"
        text = await _confirm_text(session, st)
        return text + _kb_note(confirm_keyboard())
    return None


def _kb_note(kb: list | None) -> str:
    """Сноска с подписями кнопок — текст сообщения остаёт читаемым после edit
    (Telegram хранит только последний reply_markup)."""
    if not kb:
        return ""
    labels = " · ".join(b["text"] for row in kb for b in row)
    return f"\n\n— {labels}"


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


async def _show_audience(st: dict) -> tuple[str, list]:
    """Шаг выбора аудитории: текст + inline-кнопки."""
    media_note = f"\nУже прикреплено: {len(st['media'])} 📎" if st["media"] else ""
    text = (f"<b>📢 Новая рассылка</b>\nТекст:\n<i>{_esc(st['body'])}</i>{media_note}\n\n"
            "Выберите аудиторию кнопкой:\n\n💡 Можно прикрепить фото/видео — просто "
            "отправьте медиа следующим сообщением (до подтверждения).")
    return bcast_kb(text, audience_keyboard())


async def cmd_broadcast(session: AsyncSession, user: User, args: str) -> tuple[str, list]:
    """Возвращает (текст, клавиатура)."""
    if not await _allow(session, user):
        return "⛔ Рассылки доступны только администраторам и координаторам.", back_keyboard()
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
                "Например: <code>/broadcast Завтра в 10:00 планёрка в переговорной</code>"
                + hint), [[_btn("📝 Написать текст", "bc:text"),
                           _btn("✖️ Отмена", "bc:cancel")]]
    if st["step"] == "audience":
        st["step"] = "confirm_audience"
        return await _show_audience(st)
    if st["step"] == "confirm_audience":
        return await _show_audience(st)
    if st["step"] in ("ask_dept", "ask_users"):
        return ("Рассылка уже оформляется. Дождитесь шага подтверждения или "
                "напишите 0 — отмена."), [[_btn("✖️ Отмена", "bc:cancel")]]
    if st["step"] == "edit_text":
        st["body"] = text
        st["title"] = text.split("\n", 1)[0][:120]
        st["step"] = "confirm_audience"
        return await _show_audience(st)
    if st["step"] == "confirm":
        t = await _confirm_text(session, st)
        return t, confirm_keyboard()
    return "Рассылка уже оформляется — выберите аудиторию кнопкой." \
           + "\n\n" + "\n".join(" ".join(f"[{b['text']}]" for b in row)
                                for row in audience_keyboard()), audience_keyboard()


async def handle_broadcast_input(session: AsyncSession, user: User, text: str) -> tuple[str | None, list | None]:
    """Второй+ шаги машины /broadcast (текстовый ввод — совместимость с кнопками).

    Возвращает (ответ, клавиатура) или (None, None), если это не наша ветка."""
    cid = int(user.telegram_chat_id)
    st = _bcast.get(cid)
    if st is None:
        return None, None
    t = text.strip()

    # шаг «написать текст» (кнопка 📝 Написать текст)
    if st["step"] == "ask_text":
        st["body"] = t
        st["title"] = t.split("\n", 1)[0][:120]
        st["step"] = "confirm_audience"
        st["expires"] = time.time() + 900
        return await _show_audience(st)

    if st["step"] == "confirm_audience":
        if t == "1":
            st["audience"] = {"mode": "all"}
            st["step"] = "confirm"
            return bcast_kb(await _confirm_text(session, st), confirm_keyboard())
        if t == "2":
            st["audience"] = {"mode": "heads"}
            st["step"] = "confirm"
            return bcast_kb(await _confirm_text(session, st), confirm_keyboard())
        if t == "3":
            st["step"] = "ask_dept"
            depts = (await session.execute(
                select(Department.id, Department.name).order_by(Department.name)
            )).all()
            if not depts:
                st["step"] = "confirm_audience"
                return bcast_kb("Подразделения не заведены.", audience_keyboard())
            st["depts"] = {str(i + 1): did for i, (did, _) in enumerate(depts)}
            items = [(_esc(name)[:36], f"bc:dept:{i + 1}") for i, (did, name) in enumerate(depts[:15])]
            return bcast_kb("<b>🏢 Выберите подразделение:</b>", dept_keyboard(items))
        if t == "4":
            st["step"] = "ask_users"
            return ("🎯 Через запятую укажите логины или фамилии получателей "
                    "(например: <code>ivanov i, petrov</code>).\n"
                    "0 — назад к выбору аудитории."), [[_btn("✖️ Отмена", "bc:cancel")]]
        if t in ("0", "отмена", "cancel"):
            _bcast.pop(cid, None)
            return "Рассылка отменена.", main_keyboard(True)
        return "Выберите аудиторию кнопкой (или числом 1–4, 0 — отмена).", \
            audience_keyboard()

    if st["step"] == "ask_dept":
        did = st.get("depts", {}).get(t)
        if did is None:
            return "Такого подразделения нет. Кнопка из списка или 0 — отмена.", \
                [[_btn("✖️ Отмена", "bc:cancel")]]
        st["audience"] = {"mode": "department", "department_id": did, "include_head": True}
        st["step"] = "confirm"
        return bcast_kb(await _confirm_text(session, st), confirm_keyboard())

    if st["step"] == "ask_users":
        if t in ("0", "отмена", "cancel"):
            st["step"] = "confirm_audience"
            return await _show_audience(st)
        parts = [p.strip().lower() for p in t.replace(";", ",").split(",") if p.strip()]
        if not parts:
            return "Не понял список. Логины/фамилии через запятую или 0 — отмена.", \
                [[_btn("✖️ Отмена", "bc:cancel")]]
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
            return (f"Никого не нашёл по: {_esc(', '.join(unknown))}. Повторите список "
                    "или 0 — отмена."), [[_btn("✖️ Отмена", "bc:cancel")]]
        st["audience"] = {"mode": "users", "user_ids": sorted(set(found_ids))}
        st["unknown"] = unknown
        st["step"] = "confirm"
        return bcast_kb(await _confirm_text(session, st), confirm_keyboard())

    if st["step"] == "confirm":
        low = t.lower()
        if low in ("да", "ok", "yes", "✅", "отправить"):
            text_ = await _do_send(session, user, st)
            return text_ + _kb_note(main_keyboard(True)), main_keyboard(True)
        if low in ("нет", "отмена", "cancel", "0", "✖️"):
            _bcast.pop(cid, None)
            _media_lib.pop(cid, None)
            return "Рассылка отменена, ничего не отправлено.", main_keyboard(True)
        if low in ("текст", "редактировать"):
            st["step"] = "edit_text"
            return "Отправьте новый текст рассылки одним сообщением.", None
        if low.startswith("+") or low.startswith("добавить"):
            # добавить ещё медиа: библиотека остаётся, шаг не меняется
            return ("Отправьте фото/видео — добавлю к вложениям "
                    f"(сейчас: {len(st['media'])}). Для подтверждения напишите «да»."
                    + _kb_note(confirm_keyboard()), confirm_keyboard())
        return ("Напишите «да» — отправить, «текст» — изменить, «+» — ещё медиа, "
                "«нет» — отмена.") + _kb_note(confirm_keyboard()), confirm_keyboard()

    if st["step"] == "edit_text":
        st["body"] = t
        st["title"] = t.split("\n", 1)[0][:120]
        st["step"] = "confirm"
        return bcast_kb(await _confirm_text(session, st), confirm_keyboard())
    return None, None


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
            "Нажмите «✅ Отправить» (или текстом: «да» — отправить, «текст» — изменить, "
            "«+» — добавить медиа, «нет» — отмена)")


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


async def handle_media(session: AsyncSession, user: User, msg: dict) -> tuple[str | None, list | None]:
    """Медиа от привязанного администратора: в библиотеку текущего /broadcast
    или standalone-загрузка (/media). Возвращает (ответ, клавиатура)."""
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
            return "Поддерживаются только фото и видео (или документ image/video).", None
        fid = document["file_id"]
        mtype = "photo" if mime.startswith("image/") else "video"
    else:
        return None, None
    info = await download_and_rehost_file(fid, caption)
    if info is None:
        return "⚠️ Не удалось получить файл от Telegram. Попробуйте ещё раз.", None
    info["type"] = mtype
    if st is not None:
        st["media"].append(info)
        st["expires"] = time.time() + 900
        if st.get("audience"):
            st["step"] = "confirm"
            text, kb = bcast_kb(await _confirm_text(session, st), confirm_keyboard())
            note = (f"📎 Добавлено {mtype} ({_fmt_size(info['size'])}), всего вложений: "
                    f"{len(st['media'])}.\n\n{text}")
            return note, kb
        a_text, a_kb = await _show_audience(st)
        note = (f"📎 Добавлено {mtype} ({_fmt_size(info['size'])}), всего вложений: "
                f"{len(st['media'])}.\n\n{a_text}")
        return note, a_kb
    lib = _media_lib.setdefault(cid, [])
    lib.append(info)
    return (f"📎 Файл загружен в библиотеку ({_fmt_size(info['size'])}) — "
            f"{mtype}. Он будет прикреплён к следующей рассылке автоматически.\n"
            f"В библиотеке: {len(lib)}."), [[_btn("📢 Начать рассылку", "adm:bcast"),
                                             _btn("« Меню", "adm:menu")]]


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
