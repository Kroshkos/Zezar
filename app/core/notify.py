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

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.core import (
    Entity, EntityKind, NotifyScenario, Notification, OutboxEvent,
    User, UserNotifyPref,
)

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
]


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def scenario_is_base(scn: NotifyScenario) -> bool:
    return bool(scn.is_base)


async def seed_base_scenarios(session: AsyncSession) -> None:
    """Идемпотентно создаёт базовые сценарии (если админ их удалил все — не воссоздаём:
    сид вызывается только при отсутствии хоть одного is_base-сценария)."""
    exists = await session.scalar(
        select(NotifyScenario.id).where(NotifyScenario.is_base.is_(True)).limit(1))
    if exists:
        return
    for key, name, descr, kinds, mods in BASE_SCENARIOS:
        session.add(NotifyScenario(
            name=name, description=descr, event_key=key,
            entity_kinds=kinds, module_keys=mods,
            template=DEFAULT_TEMPLATES[key],
            is_base=True, visibility="global", is_active=True))
    await session.commit()
    log.info("notify: созданы базовые сценарии уведомлений (%d)", len(BASE_SCENARIOS))


# ---------------- effective scenarios per user ----------------

async def _prefs_map(session: AsyncSession, user_id: int) -> dict[int, bool]:
    rows = (await session.execute(
        select(UserNotifyPref.scenario_id, UserNotifyPref.enabled)
        .where(UserNotifyPref.user_id == user_id))).all()
    return {r[0]: r[1] for r in rows}


async def visible_scenarios(session: AsyncSession, user: User) -> list[NotifyScenario]:
    """Все сценарии, видимые пользователю: базовые + global + его private +
    department-сценарии его подразделений."""
    from app.models.core import DepartmentMember, Role
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
    for scn in await visible_scenarios(session, user):
        if not scn.is_active:
            continue
        if not scn.is_base and prefs.get(scn.id, True) is False:
            continue
        if scn.entity_kinds and kind not in scn.entity_kinds:
            continue
        if scn.module_keys and module_key not in scn.module_keys:
            continue
        out.add(scn.event_key)
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

def link_for(uid: str) -> str:
    from app.core.config import get_settings
    base = (get_settings().public_base_url or "").rstrip("/")
    return f"{base}/?entity={uid}"


async def emit_entity_event(session: AsyncSession, *, action: str, ent: Entity,
                            actor_name: str, details: dict) -> None:
    """Пишет telegram.notify-событие в outbox (фильтрация по сценариям — в воркере)."""
    session.add(OutboxEvent(topic="telegram.notify", payload_json={
        "action": action, "event_keys": sorted(ACTION_EVENT_KEYS.get(action, {action})),
        "entity_id": ent.id, "entity_uid": ent.uid,
        "kind": ent.kind.value, "module": ent.module_key, "title": ent.title,
        "actor": actor_name, "details": details, "link": link_for(ent.uid),
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
    cnt = 0
    for ent in rows:
        dup = await session.scalar(
            select(OutboxEvent.id).where(
                OutboxEvent.topic == "telegram.due",
                OutboxEvent.at > horizon,
                OutboxEvent.payload_json["entity_uid"] == ent.uid).limit(1))
        if dup:
            continue
        session.add(OutboxEvent(topic="telegram.due", payload_json={
            "action": "due_soon", "event_keys": ["due_soon"],
            "entity_id": ent.id, "entity_uid": ent.uid, "kind": ent.kind.value,
            "module": ent.module_key, "title": ent.title, "actor": "планировщик",
            "details": {"due_date": ent.due_date}, "link": link_for(ent.uid)}))
        cnt += 1
    await session.commit()
    return cnt
