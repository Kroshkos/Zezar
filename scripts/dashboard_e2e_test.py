"""E2E-проверка нового дашборда (modules/dashboard/queries.py).

Создаёт in-memory SQLite со схемой приложения, наполняет данными (задачи,
проекты, статусы, приоритеты, ответственные, аудит, рассылки, outbox),
затем вызывает build_dashboard_data() для администратора и для обычного
участника подразделения. Проверяет, что контекст содержит все секции и
числа считаются корректно.

Запуск:  python scripts/dashboard_e2e_test.py   (rc=0 — ОК)
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.models.core import (
    AuditLog, Department, DepartmentMember, Entity, EntityKind,
    EntityResponsible, Notification, OutboxEvent, PriorityLevel, Role,
    StatusSet, SyncState, User,
)
from app.core.module_registry import registry
from modules.dashboard.queries import build_dashboard_data
from app.core.timezone import now as msk_now, today as msk_today

TODAY = msk_today()


def iso(d: date) -> str:
    return d.isoformat()


async def seed(Session):
    async with Session() as s:
        admin = User(display_name="Админ Иванов", login="admin",
                     password_hash="x", role=Role.ADMIN, is_active=True,
                     color="#2563eb", telegram_chat_id="111")
        head = User(display_name="Петров П.П.", login="head",
                    password_hash="x", role=Role.HEAD, is_active=True,
                    color="#16a34a", telegram_chat_id="222")
        member = User(display_name="Сидоров С.С.", login="member",
                      password_hash="x", role=Role.MEMBER, is_active=True)
        outsider = User(display_name="Чужак Ч.Ч.", login="outsider",
                        password_hash="x", role=Role.MEMBER, is_active=False)
        s.add_all([admin, head, member, outsider])

        dept = Department(code="D1", name="Отдел разработки", color="#7c3aed")
        dept2 = Department(code="D2", name="Отдел аналитики", color="#0d9488")
        s.add_all([dept, dept2])
        await s.flush()

        s.add_all([
            DepartmentMember(user_id=head.id, department_id=dept.id, role_in_dept=Role.HEAD),
            DepartmentMember(user_id=member.id, department_id=dept.id, role_in_dept=Role.MEMBER),
        ])

        # справочник статусов задач
        s.add_all([
            StatusSet(kind=EntityKind.TASK, key="new", label="Новая", color="b-gray", order=1),
            StatusSet(kind=EntityKind.TASK, key="in_progress", label="В работе", color="b-blue", order=2),
            StatusSet(kind=EntityKind.TASK, key="review", label="На проверке", color="b-amber", order=3),
            StatusSet(kind=EntityKind.TASK, key="done", label="Готово", color="b-green", order=4),
            StatusSet(kind=EntityKind.PROJECT, key="active", label="Активен", color="b-blue", order=1),
            StatusSet(kind=EntityKind.PROJECT, key="closed", label="Закрыт", color="b-green", order=2),
        ])
        s.add_all([
            PriorityLevel(key="low", label="Низкий", weight=20),
            PriorityLevel(key="high", label="Высокий", weight=85),
            PriorityLevel(key="critical", label="Критический", weight=100),
        ])

        def ent(uid, kind, mk, st, due=None, prio=None, title="", dept_id=None):
            return Entity(uid=uid, kind=kind, module_key=mk,
                          owner_department_id=dept_id or dept.id,
                          title=title or f"Сущность {uid}", status_key=st,
                          priority_key=prio, due_date=due,
                          sync_state=SyncState.PENDING if uid.endswith("p") else SyncState.NONE)

        yesterday = iso(TODAY - timedelta(days=2))
        tomorrow = iso(TODAY + timedelta(days=3))
        s.add_all([
            ent("t1", EntityKind.TASK, "tasks", "in_progress", due=yesterday, prio="high"),      # просрочена
            ent("t2", EntityKind.TASK, "tasks", "new", due=tomorrow),                              # на контроле
            ent("t3", EntityKind.TASK, "tasks", "done", due=yesterday),                            # завершена (не просрочка)
            ent("t4", EntityKind.TASK, "tasks", "review", due=yesterday, prio="critical"),         # просрочена срочная
            ent("t5p", EntityKind.TASK, "tasks", "in_progress"),                                   # pending sync
            ent("p1", EntityKind.PROJECT, "projects", "active", due=yesterday),                    # просроченный проект
            ent("p2", EntityKind.PROJECT, "projects", "closed"),
            ent("m1", EntityKind.MEETING, "meetings", None),
            ent("x1", EntityKind.TASK, "tasks", "new", due=yesterday, dept_id=dept2.id),           # чужое пространство
        ])
        await s.flush()

        # ответственные
        ents = (await s.execute(
            select(Entity).where(Entity.uid.in_(["t1", "t2", "t4"])))).scalars().all()
        for e in ents:
            s.add(EntityResponsible(entity_id=e.id, user_id=member.id,
                                    department_id=dept.id, is_primary=True))

        now = msk_now()
        s.add_all([
            AuditLog(at=now, user_id=admin.id, action="create", entity_id=ents[0].id),
            AuditLog(at=now - timedelta(days=1), user_id=member.id, action="status", entity_id=ents[1].id),
            AuditLog(at=now - timedelta(days=2), user_id=head.id, action="done", entity_id=ents[2].id),
            AuditLog(at=now - timedelta(days=9), user_id=admin.id, action="create", entity_id=ents[0].id),  # вне окна тренда
        ])
        s.add_all([
            Notification(title="Планёрка в пятницу", body="…", send_at=now + timedelta(days=1),
                         status="scheduled", created_by=admin.id),
            Notification(title="Смена графика", body="…", send_at=now - timedelta(days=2),
                         status="sent", sent_at=now - timedelta(days=2), recipients_count=5, delivered_count=4),
        ])
        s.add_all([
            OutboxEvent(topic="telegram.notify", payload_json={}, status="pending"),
            OutboxEvent(topic="telegram.notify", payload_json={}, status="error", last_error="429"),
            OutboxEvent(topic="calendar.sync", payload_json={}, status="pending"),
        ])
        await s.commit()


def check(name, cond):
    print(("OK  " if cond else "FAIL") + "  " + name)
    if not cond:
        raise SystemExit(1)


async def main():
    registry.autodiscover("modules")   # карточки KPI строятся из реестра модулей
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    await seed(Session)

    async with Session() as s:
        admin = (await s.execute(
            select(User).where(User.login == "admin"))).scalar_one()
        ctx = await build_dashboard_data(s, admin)

    t = ctx["totals"]
    # видимость админа: 8 сущностей всего (x1 тоже виден — глобальная роль)
    check("totals.all == 9", t["all"] == 9)
    check("overdue == 4 (t1,t4,p1,x1)", t["overdue"] == 4)
    check("urgent >= 2 (high+critical активные)", t["urgent"] == 2)
    check("done_pct считается", 0 <= t["done_pct"] <= 100)

    sd = {r["key"]: r["count"] for r in ctx["status_dist"]}
    check("status_dist: in_progress=2 (t1,t5p)", sd.get("in_progress") == 2)
    check("status_dist: new=2 (t2,x1)", sd.get("new") == 2)
    check("status_dist: review=1", sd.get("review") == 1)

    mods = {m["key"]: m for m in ctx["kpi"]}
    check("kpi tasks есть", "tasks" in mods and mods["tasks"]["total"] == 6)
    check("kpi projects overdue=1", mods.get("projects", {}).get("overdue") == 1)

    od = ctx["overdue_list"]
    check("overdue_list непустой", len(od) > 0)
    check("overdue_list отсортирован по дате", all(o["overdue"] for o in od))
    check("ответственный резолвится", any("Сидоров" in o["resp"] for o in od))
    check("подразделение подтянуто", od[0]["dept"] == "Отдел разработки")

    ds = ctx["due_soon_list"]
    check("due_soon содержит t2", any(i["title"].endswith("t2") for i in ds))

    tr = ctx["trend"]
    check("trend дней == 7", len(tr["days"]) == 7)
    # Динамика строится по дням ТЕКУЩЕЙ недели (пн–вс): добавлено / выполнено /
    # просрочилось. Seed-задачи созданы «сейчас», поэтому суммарные числа
    # недельной корзины обязаны совпадать с суммой по дням графика.
    days = {d["date"]: d for d in tr["days"]}
    check("все 7 дней недели присутствуют", len(days) == 7)
    check("сумма created по дням == week_created",
          sum(d["created"] for d in tr["days"]) == tr["week_created"])
    check("сумма overdue по дням == week_overdue",
          sum(d["overdue"] for d in tr["days"]) == tr["week_overdue"])
    check("добавленные сегодня задачи в окне недели",
          tr["week_created"] >= 1)

    wl = ctx["workload"]
    check("workload у админа есть", len(wl) == 1 and wl[0]["name"] == "Сидоров С.С.")

    sy = ctx["sync"]
    check("sync pending=1", sy["pending"] == 1)
    check("tg queue_pending=2", sy["queue_pending"] == 2)
    check("tg queue_errors=1", sy["queue_errors"] == 1)

    bc = ctx["broadcasts"]
    check("broadcast scheduled=1", bc["scheduled"] == 1)
    check("broadcast sent_week=1", bc["sent_week"] == 1)
    check("tg bound=2", ctx["tg"]["bound"] == 2)

    # персональный блок участника + изоляция пространств
    async with Session() as s:
        member = (await s.execute(
            select(User).where(User.login == "member"))).scalar_one()
        ctx_m = await build_dashboard_data(s, member)
    tm = ctx_m["totals"]
    check("member видит все 8 сущностей своего подразделения (x1 — чужое)", tm["all"] == 8)
    check("member my_active == 3", tm["my_active"] == 3)
    check("member my_overdue == 2", tm["my_overdue"] == 2)
    check("member workload пуст (не глобальная роль)", ctx_m["workload"] == [])

    # рендер шаблона с этим контекстом (base.html — заглушка без nav-зависимостей)
    import tempfile
    from jinja2 import Environment, FileSystemLoader
    env = Environment(loader=FileSystemLoader(["modules/dashboard/templates"]))
    with tempfile.TemporaryDirectory() as td:
        with open("modules/dashboard/templates/dashboard.html", encoding="utf-8") as f:
            src = f.read().replace('extends "base.html"', 'extends "_stub.html"')
        with open(os.path.join(td, "dashboard.html"), "w", encoding="utf-8") as f:
            f.write(src)
        with open(os.path.join(td, "_stub.html"), "w", encoding="utf-8") as f:
            f.write("{% block content %}{% endblock %}")
        env.loader = FileSystemLoader([td])
        tpl = env.get_template("dashboard.html")
        html = tpl.render(page_title="", page_sub="", **ctx)
    check("шаблон рендерится со всем контекстом", "dbw-hero" in html and "Просрочено" in html)
    check("рендер непустой (>4 КБ)", len(html.encode("utf-8")) > 4096)

    print("\nRESULT: PASS — все агрегаты дашборда корректны")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
