"""Агрегаты дашборда: сводка по всем модулям реестра в рамках доступа пользователя.

Возвращает dict-контекст для шаблона dashboard.html:
  kpi            — карточки по модулям (кол-во, просрочено, % готовности);
  status_dist    — распределение активных задач по статусам (справочник);
  trend          — динамика «создано/завершено» за TREND_DAYS дней;
  overdue_list   — ближайшие просрочки (top-N) со ссылками на карточки;
  due_soon_list  — что сдаётся в ближайшие 7 дней;
  activity       — лента последних изменений (audit_log);
  workload       — топ ответственных по активным задачам (глобальные роли);
  sync           — состояние синхронизации с Яндекс.Календарём и очередью TG;
  broadcasts     — сводка рассылок администратора;
  totals         — общие числа для шапки KPI.

История: раньше здесь был хрупкий код распаковки строк из session.scalars
(ValueError: too many values to unpack). Все запросы ниже используют
session.execute + именованные поля строк или явные индексы.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import access
from app.core.notify import terminal_status_keys
from app.models.core import (
    AuditLog, Department, Entity, EntityKind, EntityResponsible, Notification,
    OutboxEvent, PriorityLevel, StatusSet, SyncState, User,
)

# Палитра badge-классов справочника статусов -> hex для inline-стилей
BADGE_HEX = {
    "b-red": "#dc2626", "b-orange": "#ea580c", "b-amber": "#d97706",
    "b-yellow": "#ca8a04", "b-green": "#16a34a", "b-emerald": "#059669",
    "b-teal": "#0d9488", "b-blue": "#2563eb", "b-sky": "#0284c7",
    "b-indigo": "#4f46e5", "b-purple": "#7c3aed", "b-pink": "#db2777",
    "b-gray": "#6b7280",
}

TOP_N = 6           # строк в списках «Просрочено» / «На контроле»
TREND_DAYS = 7      # окно недельного тренда
ACTIVITY_LIMIT = 8  # строк ленты активности


def _kind_val(kind) -> str:
    return str(getattr(kind, "value", kind))


def _hex(color: str | None) -> str:
    return BADGE_HEX.get(color or "", "#6b7280")


def _fmt_dt(at: datetime | None) -> str:
    return at.strftime("%d.%m %H:%M") if at else ""


async def build_dashboard_data(session: AsyncSession, user: User) -> dict:
    ctx = await access.build_context(session, user)
    filt = access.entity_filter(ctx)
    today = date.today()
    today_iso = today.isoformat()
    horizon_iso = (today + timedelta(days=7)).isoformat()

    # --- завершающие статусы (для «активные» и просрочек) ---
    term_by_kind: dict[str, set[str]] = {}
    for kind in ("task", "project", "meeting", "doc"):
        term_by_kind[kind] = await terminal_status_keys(session, kind)

    # --- подписи и порядок статусов задач из справочника ---
    label_rows = (await session.execute(
        select(StatusSet.key, StatusSet.label, StatusSet.color, StatusSet.order)
        .where(StatusSet.kind == EntityKind.TASK)
        .order_by(StatusSet.order))).all()
    label_map: dict[str, tuple] = {}
    for key, label, color, order in label_rows:
        label_map.setdefault(key, (label, color, order))

    # ================= 1. Разрез видимых сущностей: module x status x due ====
    rows = (await session.execute(
        select(Entity.module_key, Entity.status_key, Entity.kind, Entity.due_date,
               func.count(Entity.id).label("cnt"))
        .where(filt)
        .group_by(Entity.module_key, Entity.status_key, Entity.kind,
                  Entity.due_date))).all()

    by_module: dict[str, dict] = {}
    task_status_count: dict[str, int] = {}
    total_all = active_all = overdue_all = done_all = 0
    for mk, sk, kind, due, cnt in rows:
        kv = _kind_val(kind)
        st = sk or ""
        is_term = st in term_by_kind.get(kv, set())
        is_overdue = (not is_term and due and due < today_iso
                      and kv in ("task", "project"))
        m = by_module.setdefault(mk, {"total": 0, "active": 0, "overdue": 0,
                                      "done": 0})
        m["total"] += cnt
        if is_term:
            m["done"] += cnt
        else:
            m["active"] += cnt
        if is_overdue:
            m["overdue"] += cnt
        total_all += cnt
        done_all += cnt if is_term else 0
        active_all += 0 if is_term else cnt
        overdue_all += cnt if is_overdue else 0
        if kv == "task" and not is_term:
            task_status_count[st] = task_status_count.get(st, 0) + cnt

    # --- распределение активных задач по статусам ---
    status_dist, used = [], set()
    for key, (label, color, _o) in sorted(label_map.items(), key=lambda x: x[1][2]):
        c = task_status_count.get(key, 0)
        if c:
            status_dist.append({"key": key, "label": label, "count": c,
                                "color": _hex(color)})
            used.add(key)
    for key, c in task_status_count.items():   # «сироты» удалённых статусов
        if key not in used and c:
            status_dist.append({"key": key, "label": key or "—", "count": c,
                                "color": _hex("b-gray")})

    # ================= 2. Карточки KPI по модулям реестра =====================
    from app.core.module_registry import registry
    icons: dict[str, str] = {}
    kpi = []
    for spec in registry.all():
        icons[spec.key] = spec.icon
        if spec.key == "dashboard":
            continue
        d = by_module.get(spec.key)
        if d is None:
            # модуль включает сущности, но данных пока нет — пустая карточка
            if spec.entity_kinds:
                d = {"total": 0, "active": 0, "overdue": 0, "done": 0}
            else:
                continue
        pct = round(100 * d["done"] / d["total"]) if d["total"] else 0
        kpi.append({"key": spec.key, "title": spec.title, "icon": spec.icon,
                    "url": f"/{spec.key}/", "total": d["total"],
                    "active": d["active"], "done": d["done"],
                    "overdue": d["overdue"], "pct": pct})
    kpi.sort(key=lambda x: -x["total"])

    # ================= 3. Недельный тренд (audit_log) =========================
    since_dt = datetime.combine(today - timedelta(days=TREND_DAYS - 1),
                                 datetime.min.time())

    def _trend_q(actions):
        return (select(func.date(AuditLog.at), func.count())
                .join(Entity, Entity.id == AuditLog.entity_id)
                .where(filt, AuditLog.action.in_(actions),
                       AuditLog.at >= since_dt)
                .group_by(func.date(AuditLog.at)))
    cr = {str(d): c for d, c in (await session.execute(_trend_q(("create",)))).all()}
    dr = {str(d): c for d, c in (await session.execute(
        _trend_q(("done", "status")))).all()}
    trend_days, maxv = [], 1
    for i in range(TREND_DAYS):
        day = (today - timedelta(days=TREND_DAYS - 1 - i)).isoformat()
        c_, d_ = cr.get(day, 0), dr.get(day, 0)
        maxv = max(maxv, c_, d_)
        trend_days.append({"date": day, "created": c_, "done": d_,
                           "label": f"{day[8:10]}.{day[5:7]}"})
    week_done = sum(dr.values())
    comp_rate = (min(100, round(100 * week_done / active_all)) if active_all
                 else (100 if week_done else 0))

    # ================= 4. Списки: просрочки и «на контроле» ===================
    def _mk_item(r, resp_names: dict[int, str], resp_ids: list[int]) -> dict:
        st = r.status_key or ""
        info = label_map.get(st)
        is_od = bool(r.due_date and r.due_date < today_iso)
        days = None
        if r.due_date:
            try:
                days = (date.fromisoformat(r.due_date) - today).days
            except ValueError:
                days = None
        return {"uid": r.uid, "title": r.title, "due": r.due_date, "days": days,
                "overdue": is_od, "module": r.module_key,
                "icon": icons.get(r.module_key, "📌"),
                "dept": r.dept_name, "dept_color": r.dept_color or "#64748b",
                "resp": ", ".join(resp_names.get(i, "") for i in resp_ids
                                  if resp_names.get(i)),
                "status_label": (info[0] if info else (st or "без статуса")),
                "status_color": _hex(info[1]) if info else _hex("b-gray"),
                "prio": r.priority_key or ""}

    async def _list_items(q, limit):
        """Строки запроса -> карточки; ответственные подгружаются одним IN-запросом."""
        items_raw = list((await session.execute(q)).all())
        ids = [r.id for r in items_raw]
        resp_names: dict[int, str] = {}
        per_entity: dict[int, list[int]] = {r.id: [] for r in items_raw}
        if ids:
            rrows = (await session.execute(
                select(EntityResponsible.entity_id, User.id, User.display_name)
                .join(User, User.id == EntityResponsible.user_id)
                .where(EntityResponsible.entity_id.in_(ids),
                       EntityResponsible.user_id.is_not(None)))).all()
            for eid, uid_, name in rrows:
                if eid in per_entity and uid_ not in per_entity[eid]:
                    per_entity[eid].append(uid_)
                    resp_names[uid_] = name
        out = []
        for r in items_raw:
            kv = _kind_val(r.kind)
            if (r.status_key or "") in term_by_kind.get(kv, set()):
                continue
            out.append(_mk_item(r, resp_names, per_entity.get(r.id, [])))
            if len(out) >= limit:
                break
        return out

    base_cols = (select(
        Entity.id, Entity.uid, Entity.title, Entity.due_date, Entity.status_key,
        Entity.priority_key, Entity.kind, Entity.module_key,
        Department.name.label("dept_name"), Department.color.label("dept_color"))
        .join(Department, Department.id == Entity.owner_department_id))

    od_q = (base_cols.where(
        filt, Entity.kind.in_([EntityKind.TASK, EntityKind.PROJECT]),
        Entity.due_date.is_not(None), Entity.due_date < today_iso)
        .order_by(Entity.due_date.asc()).limit(TOP_N * 3))
    overdue_list = await _list_items(od_q, TOP_N)

    ds_q = (base_cols.where(
        filt, Entity.kind.in_([EntityKind.TASK, EntityKind.PROJECT]),
        Entity.due_date.is_not(None), Entity.due_date >= today_iso,
        Entity.due_date <= horizon_iso)
        .order_by(Entity.due_date.asc()).limit(TOP_N * 2))
    due_soon_list = await _list_items(ds_q, TOP_N)

    # ================= 5. Срочные приоритеты в работе =========================
    prio_rows = (await session.execute(
        select(PriorityLevel.key, PriorityLevel.weight)
        .where(or_(PriorityLevel.department_id == None,        # noqa: E711
                   PriorityLevel.department_id.in_(ctx.department_ids or [-1]))))
    ).all()
    high_keys = {k for k, w in prio_rows if (w or 0) >= 80} | {"high", "critical"}
    urgent = 0
    if high_keys:
        uq = (select(func.count(Entity.id))
              .where(filt,
                     Entity.kind.in_([EntityKind.TASK, EntityKind.PROJECT]),
                     Entity.priority_key.in_(sorted(high_keys)),
                     # статус не завершающий для своего вида
                     and_(or_(Entity.kind != EntityKind.TASK,
                             Entity.status_key.is_(None),
                             Entity.status_key.notin_(sorted(term_by_kind["task"]))),
                          or_(Entity.kind != EntityKind.PROJECT,
                              Entity.status_key.is_(None),
                              Entity.status_key.notin_(sorted(term_by_kind["project"]))))))
        urgent = await session.scalar(uq) or 0

    # ================= 6. Лента активности =====================================
    act_q = (select(AuditLog.at, AuditLog.action, User.display_name,
                    Entity.uid, Entity.title, Entity.module_key)
             .join(Entity, Entity.id == AuditLog.entity_id)
             .outerjoin(User, User.id == AuditLog.user_id)
             .where(filt, Entity.id.is_not(None))
             .order_by(AuditLog.id.desc()).limit(ACTIVITY_LIMIT))
    ACTION_RU = {"create": "создал(а)", "update": "обновил(а)",
                 "status": "сменил(а) статус", "reassign": "переназначил(а)",
                 "share": "расшарил(а)", "delete": "удалил(а)",
                 "done": "завершил(а)"}
    activity = []
    for at, action, uname, uid, title, mk in (await session.execute(act_q)).all():
        activity.append({"at": _fmt_dt(at), "action": ACTION_RU.get(action, action),
                         "user": uname or "—", "uid": uid, "title": title,
                         "module": mk, "icon": icons.get(mk, "•")})

    # ================= 7. Загрузка ответственных (глобальные роли) ============
    workload = []
    if ctx.is_global:
        wl_q = (select(User.display_name, User.color,
                       func.count(EntityResponsible.id).label("cnt"))
                .join(EntityResponsible, EntityResponsible.user_id == User.id)
                .join(Entity, Entity.id == EntityResponsible.entity_id)
                .where(filt, Entity.kind == EntityKind.TASK,
                       or_(Entity.status_key.is_(None),
                           Entity.status_key.notin_(sorted(term_by_kind["task"]))))
                .group_by(User.id, User.display_name, User.color)
                .order_by(func.count(EntityResponsible.id).desc()).limit(5))
        rows_wl = (await session.execute(wl_q)).all()
        maxw = max((r.cnt for r in rows_wl), default=1) or 1
        workload = [{"name": r.display_name, "count": r.cnt,
                     "color": r.color or "#2563eb",
                     "pct": round(100 * r.cnt / maxw)} for r in rows_wl]

    # ================= 8. Синхронизация и доставка уведомлений ================
    pending_sync = await session.scalar(
        select(func.count(Entity.id)).where(
            filt, Entity.sync_state.in_([SyncState.PENDING, SyncState.ERROR]))) or 0
    sync_errors = await session.scalar(
        select(func.count(Entity.id)).where(
            filt, Entity.sync_state == SyncState.ERROR)) or 0

    queue_pending = await session.scalar(
        select(func.count(OutboxEvent.id)).where(
            OutboxEvent.topic.like("telegram.%"),
            OutboxEvent.processed_at.is_(None))) or 0
    queue_errors = await session.scalar(
        select(func.count(OutboxEvent.id)).where(
            OutboxEvent.topic.like("telegram.%"),
            OutboxEvent.status == "error")) or 0

    now_utc = datetime.utcnow()
    broadcasts_scheduled = await session.scalar(
        select(func.count(Notification.id)).where(
            Notification.status == "scheduled")) or 0
    broadcasts_sent_week = await session.scalar(
        select(func.count(Notification.id)).where(
            Notification.status == "sent",
            Notification.sent_at >= now_utc - timedelta(days=7))) or 0

    tg_bound = await session.scalar(
        select(func.count(User.id)).where(User.telegram_chat_id.is_not(None))) or 0
    users_active = await session.scalar(
        select(func.count(User.id)).where(User.is_active.is_(True))) or 0

    # ================= 9. Персональный блок ===================================
    my_q = (select(func.count(func.distinct(Entity.id)))
            .select_from(Entity)
            .join(EntityResponsible, EntityResponsible.entity_id == Entity.id)
            .where(filt, Entity.kind == EntityKind.TASK,
                   EntityResponsible.user_id == user.id,
                   or_(Entity.status_key.is_(None),
                       Entity.status_key.notin_(sorted(term_by_kind["task"])))))
    my_active = await session.scalar(my_q) or 0
    all_term = sorted(term_by_kind["task"] | term_by_kind["project"])
    my_od_q = (select(func.count(func.distinct(Entity.id)))
               .select_from(Entity)
               .join(EntityResponsible, EntityResponsible.entity_id == Entity.id)
               .where(filt, Entity.kind.in_([EntityKind.TASK, EntityKind.PROJECT]),
                      EntityResponsible.user_id == user.id,
                      Entity.due_date.is_not(None), Entity.due_date < today_iso,
                      or_(Entity.status_key.is_(None),
                          Entity.status_key.notin_(all_term))))
    my_overdue = await session.scalar(my_od_q) or 0

    last_bc = (await session.execute(
        select(Notification.title, Notification.sent_at, Notification.status)
        .where(Notification.status.in_(["sent", "sending", "scheduled"]))
        .order_by(Notification.created_at.desc()).limit(3))).all()
    broadcasts_items = [{"title": r.title, "at": _fmt_dt(r.sent_at),
                         "status": r.status} for r in last_bc]

    return {
        "kpi": kpi,
        "status_dist": status_dist,
        "trend": {"days": trend_days, "max": maxv,
                  "week_created": sum(cr.values()), "week_done": week_done,
                  "comp_rate": comp_rate},
        "overdue_list": overdue_list,
        "due_soon_list": due_soon_list,
        "activity": activity,
        "workload": workload,
        "sync": {"pending": pending_sync, "errors": sync_errors,
                 "queue_pending": queue_pending, "queue_errors": queue_errors},
        "broadcasts": {"scheduled": broadcasts_scheduled,
                       "sent_week": broadcasts_sent_week, "items": broadcasts_items},
        "tg": {"bound": tg_bound, "users_active": users_active},
        "totals": {"all": total_all, "active": active_all, "overdue": overdue_all,
                   "urgent": urgent, "my_active": my_active,
                   "my_overdue": my_overdue,
                   "done_pct": round(100 * done_all / total_all) if total_all else 0},
    }
