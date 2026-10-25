"""Ядро уведомлений: сценарии Telegram + адресная рассылка (outbox).

Два потока событий пишутся в OutboxEvent:
  topic="telegram.notify" — автоматические уведомления по сценариям
      (назначение ответственного/исполнителя, смена статуса, создание);
  topic="telegram.due"    — плановые напоминания «приближается срок»
      (создаёт воркер notifier_worker, интервал из settings);
  topic="telegram.manual" — ручные рассылки администратора (Notification).

Воркеры (integrations/workers.py) фильтруют события по ЭФФЕКТИВНЫМ сценариям
пользователя (NotifyScenario + UserNotifyPref): базовые сценарии администратора
применимы всем и НЕ могут быть отключены пользователем; пользовательские
сценарии настраиваются в полном окне «Интеграции».
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.core import (
    Entity, EntityKind, NotifyScenario, Notification, OutboxEvent,
    StatusSet, User, UserNotifyPref,
)

# Ключи «завершающих» статусов по видам сущностей (см. seed: TASK_STATUSES /
# PROJECT_STATUSES и т.д.). Использовать == "done" для всех kinds нельзя:
# у совещаний финальный статус — "held", у документов — "signed"/"archived".
TERMINAL_STATUS_KEYS: dict[str, set[str]] = {
    "task": {"done", "canceled"},
    "project": {"done", "canceled"},
    "meeting": {"held", "canceled"},
    "doc": {"signed", "archived", "canceled"},
}


def terminal_keys_for(kind: str) -> set[str]:
    """Ключи завершающих статусов вида: детерминированный набор + любые
    status_sets этого kind с цветом b-green (пользовательские справочники)."""
    keys = set(TERMINAL_STATUS_KEYS.get(kind, set()))
    keys.add("done")  # историческая совместимость
    return keys


async def terminal_status_keys(session, kind: str) -> set[str]:
    """Эффективные завершающие статусы вида с учётом пользовательских
    справочников подразделений (color=b-green ⇒ финальный успешный статус)."""
    keys = terminal_keys_for(kind)
    rows = (await session.execute(
        select(StatusSet.key).where(StatusSet.kind == kind,
                                    StatusSet.color == "b-green"))).all()
    return keys | {r[0] for r in rows}

log = logging.getLogger("caesar.notify")

# Действия ядра -> event_key сценариев
ACTION_EVENT_KEYS: dict[str, set[str]] = {
    "reassign": {"assigned"},
    "create":   {"created", "executor_added", "assigned"},
    "update":   {"updated"},
    "status":   {"status_changed"},
    "share":    {"shared"},
    "done":     {"status_changed", "done"},
}

ENTITY_KIND_LABELS = {
    "task": "Задача", "project": "Проект", "doc": "Документ", "soft": "ПО",
    "grant": "Грант", "scholarship": "Стипендия", "priority": "Приоритет",
    "meeting": "Совещание",
}

DEFAULT_TEMPLATES = {
    "assigned": "<b>{kind_label}</b>: <b>{title}</b>\nВас назначили ответственным.\n{changed}\nСрок: {due_date}\n{actor}\n🔗 {link}",
    "executor_added": "<b>{kind_label}</b>: <b>{title}</b>\nВы добавлены в исполнители.\n{changed}\nСрок: {due_date}\n{actor}\n🔗 {link}",
    "status_changed": "<b>{kind_label}</b>: <b>{title}</b>\nИзменился статус: {changed}\nСрок: {due_date}\n{actor}\n🔗 {link}",
    "created": "<b>{kind_label}</b>: <b>{title}</b>\nСоздана новая сущность ({module}).\nСрок: {due_date}\n{actor}\n🔗 {link}",
    "updated": "<b>{kind_label}</b>: <b>{title}</b>\nОбновление: {changed}\nСрок: {due_date}\n{actor}\n🔗 {link}",
    "shared": "<b>{kind_label}</b>: <b>{title}</b>\nСущность расшарена вашему подразделению.\nСрок: {due_date}\n{actor}\n🔗 {link}",
    "done": "<b>{kind_label}</b>: <b>{title}</b>\nСущность выполнена ✅\n{actor}\n🔗 {link}",
    "due_soon": "<b>⏰ Приближается срок</b>\n<b>{kind_label}</b>: <b>{title}</b>\nСрок: {due_date}\n🔗 {link}",
    "overdue": "<b>🔥 Срок истёк</b>\n<b>{kind_label}</b>: <b>{title}</b>\nСрок был: {due_date}\n🔗 {link}",
    "custom": "<b>{kind_label}</b>: <b>{title}</b>\n{action}\n{changed}\nСрок: {due_date}\n{actor}\n🔗 {link}",
}

BASE_SCENARIOS = [
    # (event_key, название, описание, entity_kinds, модули)
    ("assigned", "Назначение ответственным",
     "Уведомление приходит пользователю, назначенному ответственным за сущность. Не отключается.",
     [], []),
    ("executor_added", "Назначение исполнителем",
     "Уведомление всем исполнителям при добавлении в задачу/проект/совещание. Не отключается.",
     [], []),
    ("status_changed", "Смена статуса",
     "Ответственные и исполнители получают уведомление об изменении статуса. Не отключается.",
     [], []),
    ("due_soon", "Приближение срока (за 3 дня)",
     "Плановое напоминание ответственным и исполнителям проектов и задач. Не отключается.",
     ["task", "project"], []),
    ("overdue", "Просроченный срок",
     "Уведомление ответственных и исполнителей о том, что срок задачи/проекта истёк, "
     "а статус не завершающий. Повторяется раз в сутки, пока задача не закрыта. Не отключается.",
     ["task", "project"], []),
]


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def scenario_is_base(scn: NotifyScenario) -> bool:
    return bool(scn.is_base)


async def seed_base_scenarios(session: AsyncSession) -> None:
    """Идемпотентно создаёт базовые сценарии.

    Исторически сид выполнялся только при полном отсутствии is_base-сценариев,
    из-за чего новые базовые ключи (например, 'overdue'), добавленные в
    BASE_SCENARIOS позже первого запуска, никогда не появлялись на существующих
    БД — уведомления об истёкшем сроке молча не доставлялись (фильтр по
    включённым сценариям отбрасывал неизвестный event_key). Теперь недостающие
    базовые ключи добираются выборочно; если админ удалил все базовые — не
    воссоздаём пачку целиком (уважение к ручной чистке), но и новые ключи
    тогда тоже не добавляются.
    """
    have = set((await session.scalars(
        select(NotifyScenario.event_key).where(
            NotifyScenario.is_base.is_(True)))).all())
    if not have:
        return
    added = 0
    for key, name, descr, kinds, mods in BASE_SCENARIOS:
        if key in have:
            continue
        session.add(NotifyScenario(
            name=name, description=descr, event_key=key,
            entity_kinds=kinds, module_keys=mods,
            template=DEFAULT_TEMPLATES[key],
            is_base=True, visibility="global", is_active=True))
        added += 1
    if added:
        await session.commit()
        log.info("notify: добавлены недостающие базовые сценарии: +%d", added)


# ---------------- effective scenarios per user ----------------

async def _prefs_map(session: AsyncSession, user_id: int) -> dict[int, bool]:
    rows = (await session.execute(
        select(UserNotifyPref.scenario_id, UserNotifyPref.enabled)
        .where(UserNotifyPref.user_id == user_id))).all()
    return {r[0]: r[1] for r in rows}


def scenario_applies(scn: NotifyScenario, kind: str, module_key: str) -> bool:
    """Фильтр сценария по виду/модулю сущности. Пустые списки = «все».

    Нормализация входов обязательна: часть кода ставит в payload `kind` как
    EntityKind(str, Enum), а не строку (`ent.kind.value` опущено) — без `.value`
    сравнение `kind not in scn.entity_kinds` молча давало True для ВСЕХ
    сценариев с ограничением по видам, и такие события отбрасывались фильтром
    воркера («отфильтровано по сценариям» — уведомления исчезали).
    """
    kind = getattr(kind, "value", kind) or ""
    module_key = getattr(module_key, "value", module_key) or ""
    if scn.entity_kinds and kind not in list(scn.entity_kinds):
        return False
    if scn.module_keys and module_key not in list(scn.module_keys):
        return False
    return True


async def _scn_rows(session: AsyncSession, user: User):
    """Сырые строки видимых сценариев (без ORM-объектов) — один запрос."""
    from app.models.core import DepartmentMember
    dept_ids = list((await session.scalars(
        select(DepartmentMember.department_id).where(
            DepartmentMember.user_id == user.id))).all())
    conds = [NotifyScenario.is_base.is_(True), NotifyScenario.visibility == "global",
             NotifyScenario.created_by == user.id]
    if dept_ids:
        conds.append(NotifyScenario.department_id.in_(dept_ids))
    q = (select(NotifyScenario.id, NotifyScenario.event_key,
                NotifyScenario.entity_kinds, NotifyScenario.module_keys,
                NotifyScenario.is_base, NotifyScenario.is_active)
         .where(or_(*conds)))
    return (await session.execute(q)).all()


async def visible_scenarios(session: AsyncSession, user: User) -> list[NotifyScenario]:
    """Все сценарии, видимые пользователю: базовые + global + его private +
    department-сценарии его подразделений."""
    from app.models.core import DepartmentMember
    dept_ids = list((await session.scalars(
        select(DepartmentMember.department_id).where(
            DepartmentMember.user_id == user.id))).all())
    conds = [NotifyScenario.is_base.is_(True), NotifyScenario.visibility == "global",
             NotifyScenario.created_by == user.id]
    if dept_ids:
        conds.append(NotifyScenario.department_id.in_(dept_ids))
    rows = (await session.scalars(
        select(NotifyScenario).where(or_(*conds)).order_by(NotifyScenario.name))).all()
    return list(rows)


async def enabled_event_keys(session: AsyncSession, user: User,
                             kind: str, module_key: str) -> set[str]:
    """Множество event_key сценариев, включённых для данного пользователя под
    конкретную сущность (kind/module). Базовые — всегда включены."""
    prefs = await _prefs_map(session, user.id)
    out: set[str] = set()
    for r in await _scn_rows(session, user):
        if not r.is_active:
            continue
        if not r.is_base and prefs.get(r.id, True) is False:
            continue
        scn = NotifyScenario(id=r.id, event_key=r.event_key,
                             entity_kinds=r.entity_kinds, module_keys=r.module_keys)
        if not scenario_applies(scn, kind, module_key):
            continue
        out.add(r.event_key)
    return out


async def scenarios_for_user(session: AsyncSession, user: User) -> list[dict]:
    """Карточка сценариев для UI окна «Интеграции» (с эффективным флагом)."""
    prefs = await _prefs_map(session, user.id)
    out = []
    for scn in await visible_scenarios(session, user):
        eff = True if scn.is_base else (scn.is_active and prefs.get(scn.id, True))
        out.append({
            "id": scn.id, "name": scn.name, "description": scn.description or "",
            "event_key": scn.event_key, "entity_kinds": scn.entity_kinds or [],
            "module_keys": scn.module_keys or [], "template": scn.template or "",
            "is_base": bool(scn.is_base), "visibility": scn.visibility,
            "editable": (not scn.is_base and scn.created_by == user.id),
            "enabled": bool(eff),
            "switchable": not scn.is_base,
        })
    return out


# ---------------- rendering ----------------

def render_scenario_text(scn: NotifyScenario | None, ctx: dict) -> str:
    tpl = (scn.template if scn and scn.template else DEFAULT_TEMPLATES.get(
        (scn.event_key if scn else "custom"), DEFAULT_TEMPLATES["custom"]))
    try:
        return tpl.format(**ctx)
    except Exception:  # noqa: BLE001  — неизвестный плейсхолдер в шаблоне
        return f"<b>{ctx['kind_label']}</b>: <b>{ctx['title']}</b>\n{ctx['action']}\n🔗 {ctx['link']}"


def build_ctx(p: dict) -> dict:
    """Контекст рендера из payload события."""
    kind = p.get("kind", "")
    changed = ""
    d = p.get("details") or {}
    ch = d.get("changed")
    if isinstance(ch, dict) and ch:
        parts = []
        for k, v in ch.items():
            old = ch[k].get("old") if isinstance(v, dict) and "old" in ch[k] else None
            new = v.get("new") if isinstance(v, dict) else v
            if old is not None:
                parts.append(f"{k}: {old} → {new}")
            elif new is not None:
                parts.append(f"{k}: {new}")
        changed = "; ".join(parts)
    elif d.get("note"):
        changed = str(d["note"])
    return {
        "title": p.get("title", ""),
        "kind": kind,
        "kind_label": ENTITY_KIND_LABELS.get(kind, kind or "Сущность"),
        "module": p.get("module", ""),
        "action": p.get("action", ""),
        "changed": changed or "—",
        "due_date": d.get("due_date") or p.get("due_date") or "не указан",
        "actor": p.get("actor", "система"),
        "link": p.get("link", ""),
    }


# ---------------- enqueue helpers (вызываются из HTTP-транзакций) ----------------

KIND_PAGE = {
    "task": "/tasks", "project": "/projects", "doc": "/docs", "soft": "/softs",
    "grant": "/grants", "scholarship": "/schols", "priority": "/priority",
    "meeting": "/meetings",
}


def link_for(uid: str, kind: str = "") -> str:
    """Ссылка на сущность в системе (открывает профиль по клику из Telegram)."""
    from app.core.config import get_settings
    base = (get_settings().public_base_url or "").rstrip("/")
    page = KIND_PAGE.get(kind or "", "/")
    return f"{base}{page}?entity={uid}"


async def emit_entity_event(session: AsyncSession, *, action: str, ent: Entity,
                            actor_name: str, details: dict) -> None:
    """Пишет telegram.notify-событие в outbox (фильтрация по сценариям — в воркере)."""
    session.add(OutboxEvent(topic="telegram.notify", payload_json={
        "action": action, "event_keys": sorted(ACTION_EVENT_KEYS.get(action, {action})),
        "entity_id": ent.id, "entity_uid": ent.uid,
        "kind": ent.kind.value, "module": ent.module_key, "title": ent.title,
        "actor": actor_name, "details": details, "link": link_for(ent.uid, ent.kind.value),
    }))


async def enqueue_manual(session: AsyncSession, n: Notification) -> None:
    session.add(OutboxEvent(topic="telegram.manual", payload_json={"notification_id": n.id}))


async def schedule_due_reminders(session: AsyncSession, *, days_ahead: int = 3,
                                 window_days: int = 1) -> int:
    """Воркер-проход: задачи/проекты с due_date в пределах [today, today+days_ahead],
    о которых ещё не напоминали в этом окне. Возвращает число поставленных событий."""
    today = datetime.now(timezone.utc).date()
    lo = today.isoformat()
    hi = (today + timedelta(days=days_ahead)).isoformat()
    rows = (await session.scalars(
        select(Entity).where(Entity.kind.in_([EntityKind.TASK, EntityKind.PROJECT]),
                             Entity.due_date.is_not(None),
                             Entity.due_date >= lo, Entity.due_date <= hi))).all()
    horizon = utcnow_naive() - timedelta(days=window_days)
    # Дедуп по payload_json['entity_uid']: generic sqlalchemy.JSON на SQLite не
    # поддерживает оператор ->> (сравнение молча даёт NULL, дубликаты никогда
    # не находятся и напоминания ставятся заново при каждом проходе). На PG
    # используем json_extract_path_text, на прочих диалектах — множество uid
    # свежих событий, загруженное на стороне Python.
    is_pg = bool(session.bind and session.bind.dialect.name == "postgresql")
    cnt = 0
    recent_uids: set[tuple[str, str]] | None = None
    if not is_pg:
        seen_rows = (await session.execute(
            select(OutboxEvent.topic, OutboxEvent.payload_json).where(
                OutboxEvent.topic.in_(["telegram.due", "telegram.overdue"]),
                OutboxEvent.at > horizon))).all()
        recent_uids = {(r[0], (r[1] or {}).get("entity_uid")) for r in seen_rows}

    async def _already_sent(topic: str, uid: str) -> bool:
        if recent_uids is not None:
            return (topic, uid) in recent_uids
        dup = await session.scalar(
            select(OutboxEvent.id).where(
                OutboxEvent.topic == topic,
                OutboxEvent.at > horizon,
                func.json_extract_path_text(
                    OutboxEvent.payload_json, "entity_uid") == uid).limit(1))
        return bool(dup)

    for ent in rows:
        if await _already_sent("telegram.due", ent.uid):
            continue
        session.add(OutboxEvent(topic="telegram.due", payload_json={
            "action": "due_soon", "event_keys": ["due_soon"],
            "entity_id": ent.id, "entity_uid": ent.uid, "kind": ent.kind.value,
            "module": ent.module_key, "title": ent.title, "actor": "планировщик",
            # автор карточки: воркер исключает его из получателей напоминания
            # (спам «сам себе»: resp/исполнитель часто = создатель задачи)
            "actor_user_id": (ent.extra_json or {}).get("created_by"),
            "details": {"due_date": ent.due_date}, "link": link_for(ent.uid, ent.kind.value)}))
        cnt += 1
    # просроченные (overdue): срок < сегодня, статус не «завершающий» — раз в window_days.
    # ВАЖНО: сравнение только с "done" ошибочно помечало бы просроченными снятые
    # задачи/остановленные проекты и не учитывало пользовательские статусы.
    terminal_task = await terminal_status_keys(session, "task")
    terminal_project = await terminal_status_keys(session, "project")
    overdue_rows = (await session.scalars(
        select(Entity).where(Entity.kind.in_([EntityKind.TASK, EntityKind.PROJECT]),
                             Entity.due_date.is_not(None),
                             Entity.due_date < lo))).all()
    overdue_rows = [e for e in overdue_rows
                    if (e.status_key or "") not in
                    (terminal_task if str(getattr(e.kind, "value", e.kind)) == "task"
                     else terminal_project)]
    ohorizon = utcnow_naive() - timedelta(days=window_days)
    for ent in overdue_rows:
        if await _already_sent("telegram.overdue", ent.uid):
            continue
        session.add(OutboxEvent(topic="telegram.overdue", payload_json={
            "action": "overdue", "event_keys": ["overdue"],
            "entity_id": ent.id, "entity_uid": ent.uid, "kind": ent.kind.value,
            "module": ent.module_key, "title": ent.title, "actor": "планировщик",
            "actor_user_id": (ent.extra_json or {}).get("created_by"),
            "details": {"due_date": ent.due_date}, "link": link_for(ent.uid, ent.kind.value)}))
        cnt += 1
    await session.commit()
    return cnt
