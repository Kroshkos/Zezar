"""
E2E-проверка ядра + модуля «Задачи» через httpx ASGITransport (без поднятия порта).
Запуск:  python scripts/e2e_test.py
Использует временную БД и dev-вход (dev_login_enabled=True задаётся в env до импорта app).
"""
import asyncio
import os
import tempfile

# --- конфигурация ДО импорта приложения ---
_tmp = tempfile.mkdtemp(prefix="caesar_e2e_")
os.environ["DATA_DIR"] = _tmp
os.environ["DATABASE_PATH"] = os.path.join(_tmp, "test.db")
os.environ["SECRET_KEY"] = "e2e-secret"
os.environ["DEV_LOGIN_ENABLED"] = "true"
os.environ["WORKERS_ENABLED"] = "false"   # сеть не трогаем, outbox проверим напрямую

import httpx  # noqa: E402

from app.main import app  # noqa: E402
from app.db.engine import SessionLocal  # noqa: E402
from app.models.core import Entity, OutboxEvent, User  # noqa: E402
from integrations.workers import _outbox_once  # noqa: E402


async def _create_task_as(email: str, title: str) -> tuple[str, dict]:
    """Создать задачу от имени пользователя (через реальный HTTP-стек)."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                 follow_redirects=False) as c:
        r = await c.get(f"/auth/dev-login?email={email}")
        ck = {"Cookie": f"caesar_session={r.cookies['caesar_session']}"}
        r = await c.post("/api/tasks", headers=ck, json={
            "title": title, "status_key": "new", "priority_key": "medium"})
        assert r.status_code == 200, r.text
        return r.json()["uid"], ck


async def main() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                 follow_redirects=False) as c:
        async with app.router.lifespan_context(app):
            # 1. dev-вход админа
            r = await c.get("/auth/dev-login?email=admin@caesar.local")
            assert r.status_code == 302, r.text
            cookie = {"Cookie": f"caesar_session={r.cookies['caesar_session']}"}

            # 2. страница задач рендерится (Jinja + base.html)
            r = await c.get("/tasks", headers=cookie)
            assert r.status_code == 200 and "Задачи" in r.text, r.status_code
            assert "ttb" in r.text

            # 3. дашборд
            r = await c.get("/", headers=cookie)
            assert r.status_code == 200, r.text

            # 4. REST: создать задачу
            r = await c.post("/api/tasks", headers=cookie, json={
                "title": "Подготовить дорожную карту ИС «Цезарь»",
                "status_key": "new", "priority_key": "high",
                "due_date": "2026-11-13"})
            assert r.status_code == 200, r.text
            uid = r.json()["uid"]

            # 5. список содержит новую задачу
            r = await c.get("/api/tasks/api-list", headers=cookie)
            assert any(t["uid"] == uid for t in r.json())

            # 6. патч статуса -> AuditLog/Outbox
            r = await c.patch(f"/api/tasks/{uid}", headers=cookie,
                              json={"status_key": "in_work"})
            assert r.status_code == 200, r.text

            # 7. outbox-воркер: события telegram/calendar обработаны (без сети — tg просто
            #    пропустит непривязанных, calendar поставит SYNCED т.к. due есть, но пароля нет)
            before = len(await _pending_events())
            await _outbox_once()          # без network-заглушек ошибки ловятся retry-логикой
            after = await _pending_events()
            assert before > 0, "outbox пуст — Transactional Outbox не работает"
            done = await _done_events()
            assert len(done) > 0, "ни одно событие не помечено done — воркер не обрабатывает очередь"

            # 8. шеринг другому подразделению + слот ответственного
            r = await c.get("/api/core/departments", headers=cookie)
            depts_all = {d["code"]: d["id"] for d in r.json()}
            depts = depts_all
            r = await c.post(f"/api/core/entities/{uid}/shares", headers=cookie, json={
                "target_department_id": depts["dep-it"], "permission": "read"})
            assert r.status_code == 200, r.text

            # 8b. межпространственная задача проекта: создаётся СРАЗУ с контекстом
            #     нескольких подразделений (share_with) — по одному ответственному от каждого
            r = await c.post("/api/tasks", headers=cookie, json={
                "title": "КС: интеграция страт+ИТ", "status_key": "new",
                "priority_key": "high", "due_date": "2026-12-01",
                "share_with": [depts["dep-strat"]]})
            assert r.status_code == 200, r.text
            cross_uid = r.json()["uid"]

            # 9. RBAC: member видит расшаренное только на чтение
            r = await c.get("/auth/dev-login?email=member-it@caesar.local")
            m_cookie = {"Cookie": f"caesar_session={r.cookies['caesar_session']}"}
            r = await c.get("/api/tasks/api-list", headers=m_cookie)
            shared = [t for t in r.json() if t["uid"] == uid]
            assert shared and shared[0]["perm"] == "read" and not shared[0]["can_edit"], shared
            r = await c.patch(f"/api/tasks/{uid}", headers=m_cookie, json={"title": "хакр" * 3})
            assert r.status_code == 403, "READ-шеринг дал право на запись!"

            # 10. изоляция: посторонний участник НЕ видит задачи ЧУЖИХ пространств.
            #     member-strat (dep-strat) не должен видеть задачу, созданную в dep-it.
            #     Задача админа создаётся в dep-it с внешним ответственным = head-it —
            #     это легитимный сценарий «админ поручает задачу подразделению», при
            #     котором служебный слот открывается В департаменте исполнителя (dep-it),
            #     а не в родном пространстве админа (dep-strat). Иначе любой выбор
            #     пространства админом автоматически расшаривал бы сущность обратно
            #     в его домашнее подразделение (утечка между пространствами).
            _, a_cookie = await _create_task_as("admin@caesar.local", "Личная задача админа")
            r = await c.get("/api/core/departments", headers=a_cookie)
            admin_depts = {d["code"]: d["id"] for d in r.json()}
            r = await c.get("/api/core/users", headers=a_cookie)
            users_by_email = {u["email"]: u["id"] for u in r.json()}
            r = await c.post("/api/tasks", headers=a_cookie, json={
                "title": "Задача админа в dep-it без шеринга", "status_key": "new",
                "priority_key": "medium",
                "department_id": admin_depts["dep-it"],
                "responsible_user_id": users_by_email["head-it@caesar.local"]})
            assert r.status_code == 200, r.text
            leak_uid = r.json()["uid"]
            own_uid, _ = await _create_task_as("member-strat@caesar.local",
                                               "Своя задача участника страт.")
            r = await c.get("/auth/dev-login?email=member-strat@caesar.local")
            o_cookie = {"Cookie": f"caesar_session={r.cookies['caesar_session']}"}
            r = await c.get("/api/tasks/api-list", headers=o_cookie)
            lst = r.json()
            assert all(t["uid"] != leak_uid for t in lst), \
                "задача админа без share_with просочилась в чужое пространство"
            assert any(t["uid"] == uid for t in lst), \
                "участник не видит READ-расшаренную в его пространство задачу"
            assert any(t["uid"] == own_uid for t in lst), \
                "участник не видит собственную задачу"

            # 10b. межпространственная задача (из 8b) видна принимающему подразделению
            #      на запись, и у неё есть слот ответственного от dep-strat
            r = await c.get("/api/tasks/api-list", headers=o_cookie)
            lst2 = r.json()
            cross = [t for t in lst2 if t["uid"] == cross_uid]
            assert cross and cross[0]["can_edit"], \
                "межпространственная задача недоступна принимающему подразделению"
            r = await c.get(f"/api/core/entities/{cross_uid}/responsibles", headers=o_cookie)
            assert r.status_code == 200, r.text
            slots = r.json()
            strat_slot = [s for s in slots if s.get("department_code") == "dep-strat"]
            assert strat_slot, f"нет слота ответственного принимающего подразделения: {slots}"

            # 10c. обычная задача админа (без share_with) по-прежнему изолирована;
            #      межпространственная задача видна ТОЛЬКО участникам шеринга:
            #      у dep-strat она есть, у стороннего подразделения — нет
            assert all(t["uid"] != leak_uid for t in lst2), \
                "задача админа без share_with просочилась в чужое пространство"
            assert any(t["uid"] == cross_uid and t["cross_dept"] for t in lst2), \
                "у межпространственной задачи не проставлен признак cross_dept"
            r = await c.get("/auth/dev-login?email=head-hr@caesar.local")
            h_cookie = {"Cookie": f"caesar_session={r.cookies['caesar_session']}"}
            r = await c.get("/api/tasks/api-list", headers=h_cookie)
            lst3 = r.json()
            assert all(t["uid"] not in (uid, leak_uid, cross_uid) for t in lst3), \
                "задача просочилась подразделению, не участвующему в шеринге"

            # 11. модуль «Совещания»: CRUD + карточная страница + payload-календарь
            r = await c.get("/meetings", headers=cookie)
            assert r.status_code == 200 and "Совещания" in r.text and "mcards" in r.text, r.status_code
            r = await c.post("/api/meetings", headers=cookie, json={
                "title": "КС по дорожной карте ИС «Цезарь»", "status_key": "planned",
                "due_date": "2026-10-20", "time": "14:00", "num": "17",
                "chair": "Иванов И.И.", "location": "к. 412"})
            assert r.status_code == 200, r.text
            m_uid = r.json()["uid"]
            r = await c.get("/api/meetings/api-list", headers=cookie)
            ml = [m for m in r.json() if m["uid"] == m_uid]
            assert ml and ml[0]["num"] == "17" and ml[0]["time"] == "14:00", ml
            # точное время из payload попадает в ICS (хук календаря модуля)
            from sqlalchemy import select as _sel
            from app.db.engine import SessionLocal as _SL
            from app.models.core import Entity as _E
            from app.models.module_payloads import MeetingPayload as _MP
            from integrations.yandex_calendar import ics_for_entity
            async with _SL() as _s:
                ent_obj = await _s.scalar(_sel(_E).where(_E.uid == m_uid))
                ent_obj._cal_payload = await _s.get(_MP, ent_obj.id)
                ics = ics_for_entity(ent_obj, "Админ")
            assert "DTSTART;TZID=Europe/Moscow:20261020T140000" in ics, ics
            assert "LOCATION:к. 412" in ics
            # патч статуса -> outbox-события (telegram+calendar) идут через ядро
            r = await c.patch(f"/api/meetings/{m_uid}", headers=cookie,
                              json={"status_key": "held"})
            assert r.status_code == 200, r.text
            # member видит совещание своего подразделения
            r = await c.get("/api/meetings/api-list", headers=m_cookie)
            assert any(x["uid"] == m_uid for x in r.json()), "совещание не видно участнику пространства"
            # удаление — событие calendar.remove уходит в outbox
            r = await c.delete(f"/api/meetings/{m_uid}", headers=cookie)
            assert r.status_code == 200, r.text

            # 12. модуль «Администрирование подразделения»: страница + права
            r = await c.get("/admin_dept", headers=cookie)
            assert r.status_code == 200 and "Участники" in r.text \
                and "Модули рабочего пространства" in r.text, r.status_code
            assert "admin_dept" in r.text  # пункт навигации/контекст страницы
            # member не может управлять составом чужого пространства (HEAD-only для не-global)
            r = await c.post("/api/core/departments/%d/members" % depts["dep-it"],
                             headers=o_cookie, json={"user_id": 999, "role_in_dept": "MEMBER"})
            assert r.status_code == 403, "member получил право управлять участниками!"
            # toggle_module теперь через тело запроса (PUT .../modules/{key} {is_enabled})
            r = await c.put(f"/api/core/departments/{depts['dep-it']}/modules/nonexistent",
                            headers=cookie, json={"is_enabled": True})
            assert r.status_code == 404, r.status_code  # не зарегистрирован в реестре

            # 13. healthz / реестр
            r = await c.get("/healthz")
            hm = r.json()["modules"]
            assert "tasks" in hm and "dashboard" in hm and "meetings" in hm, hm
            assert "admin_dept" in hm, f"модуль администрирования не авто-обнаружен: {hm}"

    print("E2E OK: auth(RBAC+закрытый вход), страницы(Jinja), CRUD задач, outbox-воркер, "
          "шеринг READ/изоляция, совещания(payload+ICS-время)")


async def _pending_events():
    from sqlalchemy import select
    async with SessionLocal() as s:
        return (await s.scalars(select(OutboxEvent).where(
            OutboxEvent.processed_at.is_(None)))).all()


async def _done_events():
    from sqlalchemy import select
    async with SessionLocal() as s:
        return (await s.scalars(select(OutboxEvent).where(
            OutboxEvent.processed_at.is_not(None)))).all()


if __name__ == "__main__":
    asyncio.run(main())
