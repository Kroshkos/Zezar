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
os.environ["ADMIN_USERNAME"] = "ivanov"
os.environ["ADMIN_PASSWORD"] = "E2E-Admin-Pass1"
os.environ["WORKERS_ENABLED"] = "false"   # сеть не трогаем, outbox проверим напрямую

import httpx  # noqa: E402

from app.main import app  # noqa: E402
from app.db.engine import SessionLocal  # noqa: E402
from app.models.core import Entity, OutboxEvent, User  # noqa: E402
from integrations.workers import _outbox_once  # noqa: E402

EMAIL_TO_LOGIN = {'admin@caesar.local': 'ivanov', 'coord@caesar.local': 'petrova', 'head-it@caesar.local': 'sidorov', 'member-it@caesar.local': 'kuznetsov', 'member-strat@caesar.local': 'smirnova', 'head-hr@caesar.local': 'morozova', 'member-hr@caesar.local': 'gavrilov'}


async def _create_task_as(email: str, title: str) -> tuple[str, dict]:
    """Создать задачу от имени пользователя (через реальный HTTP-стек)."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                 follow_redirects=False) as c:
        r = await c.get(f"/auth/dev-login?login={EMAIL_TO_LOGIN[email]}")
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
            r = await c.get("/auth/dev-login?login=ivanov")
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
            #     нескольких подразделений (share_with) — по одному ответственному от каждого.
            #     Владелец — dep-it, принимающая сторона — dep-strat (домашнее пространство
            #     админа): семантически корректный кросс-шеринг между ДВУМЯ разными ws.
            r = await c.post("/api/tasks", headers=cookie, json={
                "title": "КС: интеграция страт+ИТ", "status_key": "new",
                "priority_key": "high", "due_date": "2026-12-01",
                "department_id": depts["dep-it"],
                "share_with": [depts["dep-strat"]]})
            assert r.status_code == 200, r.text
            cross_uid = r.json()["uid"]

            # 9. RBAC: member видит расшаренное только на чтение
            r = await c.get("/auth/dev-login?login=kuznetsov")
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
            r = await c.get("/api/core/admin/users", headers=a_cookie)
            assert r.status_code == 200, r.text
            users_by_email = {u["email"]: u["id"] for u in r.json() if u.get("email")}
            r = await c.post("/api/tasks", headers=a_cookie, json={
                "title": "Задача админа в dep-it без шеринга", "status_key": "new",
                "priority_key": "medium",
                "department_id": admin_depts["dep-it"],
                "responsible_user_id": users_by_email["head-it@caesar.local"]})
            assert r.status_code == 200, r.text
            leak_uid = r.json()["uid"]
            own_uid, _ = await _create_task_as("member-strat@caesar.local",
                                               "Своя задача участника страт.")
            r = await c.get("/auth/dev-login?login=smirnova")
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
            r = await c.get("/auth/dev-login?login=morozova")
            h_cookie = {"Cookie": f"caesar_session={r.cookies['caesar_session']}"}
            r = await c.get("/api/tasks/api-list", headers=h_cookie)
            lst3 = r.json()
            assert all(t["uid"] not in (uid, leak_uid, cross_uid) for t in lst3), \
                "задача просочилась подразделению, не участвующему в шеринге"

            # 11. модуль «Совещания»: CRUD + карточная страница + payload-календарь
            r = await c.get("/meetings", headers=cookie)
            assert r.status_code == 200 and "Совещания" in r.text and "mcards" in r.text, r.status_code
            # совещание создаётся ЯВНО в dep-it (домашнее пространство cookie-юзера) —
            # иначе _pick_department детерминированно возьмёт первое членство и для
            # админа это dep-strat, где member-it (проверка ниже) ничего не видит
            r = await c.post("/api/meetings", headers=cookie, json={
                "title": "КС по дорожной карте ИС «Цезарь»", "status_key": "planned",
                "due_date": "2026-10-20", "time": "14:00", "num": "17",
                "chair": "Иванов И.И.", "location": "к. 412",
                "department_id": depts["dep-it"]})
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
            # admin/координатору страница требует department_id (управляет чужим ws)
            r = await c.get(f"/admin_dept/?department_id={depts['dep-it']}", headers=cookie)
            assert r.status_code == 200 and "Участники" in r.text \
                and "Модули рабочего пространства" in r.text, r.status_code
            assert "admin_dept" in r.text  # пункт навигации/контекст страницы
            # RBAC состава: member НЕ может читать/менять чужое пространство;
            # head — только СВОЁ пространство и только роль MEMBER (HEAD-назначение — admin);
            # admin — может назначать роль HEAD
            # Кандидат в HEAD: участник ЧУЖОГО для morozova пространства (member-it),
            # а не сама head-hr — иначе «назначение HEAD» превращалось в no-op
            # (morozova уже была HEAD в dep-hr) и право head не проверялось.
            new_head_id = users_by_email["member-it@caesar.local"]
            r = await c.get("/api/core/departments/%d/members" % depts["dep-it"],
                            headers=o_cookie)
            assert r.status_code == 403, "member прочитал состав чужого пространства!"
            # member-strat пытается МЕНЯТЬ чужое пространство dep-it -> 403
            r = await c.post("/api/core/departments/%d/members" % depts["dep-it"],
                             headers=o_cookie,
                             json={"user_id": new_head_id, "role_in_dept": "member"})
            assert r.status_code == 403, "member получил право менять чужое пространство!"
            # head-hr пытается управлять ЧУЖИМ пространством dep-it -> 403
            r = await c.post("/api/core/departments/%d/members" % depts["dep-it"],
                             headers=h_cookie,
                             json={"user_id": new_head_id, "role_in_dept": "member"})
            assert r.status_code == 403, "head получил право менять чужое пространство!"
            # head в своём пространстве не может назначать роль выше MEMBER -> 403
            r = await c.get("/api/core/departments", headers=h_cookie)
            hr_depts = {d["code"]: d["id"] for d in r.json()} if r.status_code == 200 else {}
            if "dep-hr" in hr_depts:
                r = await c.post("/api/core/departments/%d/members" % hr_depts["dep-hr"],
                                 headers=h_cookie,
                                 json={"user_id": new_head_id, "role_in_dept": "head"})
                assert r.status_code == 403, "head назначил HEAD-роль вне своих прав!"
            r = await c.post("/api/core/departments/%d/members" % depts["dep-it"],
                             headers=cookie,
                             json={"user_id": new_head_id, "role_in_dept": "head"})
            assert r.status_code == 200, f"admin не смог назначить HEAD: {r.status_code} {r.text}"
            r = await c.get("/api/core/departments/%d/members" % depts["dep-it"],
                            headers=cookie)
            assert any(m["user_id"] == new_head_id and m["role_in_dept"] == "head"
                       for m in r.json()), r.json()
            # toggle_module теперь через тело запроса (PUT .../modules/{key} {is_enabled})
            r = await c.put(f"/api/core/departments/{depts['dep-it']}/modules/nonexistent",
                            headers=cookie, json={"is_enabled": True})
            assert r.status_code == 404, r.status_code  # не зарегистрирован в реестре

            # 13. healthz / реестр
            r = await c.get("/healthz")
            hm = r.json()["modules"]
            assert "tasks" in hm and "dashboard" in hm and "meetings" in hm, hm
            assert "admin_dept" in hm, f"модуль администрирования не авто-обнаружен: {hm}"

            # 14. все модули прототипа зарегистрированы динамическим реестром
            for mk in ("projects", "docs", "softs", "grants", "schols", "priority"):
                assert mk in hm, f"модуль {mk} не в реестре: {hm}"

            # 15. «Документооборот»: HTML + payload (doc_type/number) + изоляция пространств
            r = await c.get("/docs", headers=cookie)
            assert r.status_code == 200 and "Новый документ" in r.text, r.status_code
            r = await c.post("/api/docs", headers=cookie, json={
                "title": "Приказ о создании рабочей группы", "status_key": "draft",
                "due_date": "2026-10-15", "doc_type": "Приказ", "number": "123-ОД",
                "department_id": depts["dep-it"],
                "share_with": [depts["dep-strat"]]})
            assert r.status_code == 200, r.text
            d_uid = r.json()["uid"]
            r = await c.get("/api/docs/api-list", headers=m_cookie)   # member-it видит своё ws
            dl = [d for d in r.json() if d["uid"] == d_uid]
            assert dl and dl[0]["number"] == "123-ОД" and dl[0]["doc_type"] == "Приказ", dl
            r = await c.get("/api/docs/api-list", headers=o_cookie)   # strat — видит по шерингу
            ds = [d for d in r.json() if d["uid"] == d_uid]
            assert ds and ds[0]["cross_dept"], \
                "расшаренный документ не виден принимающему пространству"
            r = await c.patch(f"/api/docs/{d_uid}", headers=o_cookie,
                              json={"number": "хакр"})               # EDIT-шеринг: запись ок
            assert r.status_code == 200, r.text
            # сторонний участник dep-hr: в сценарии 12 head-hr был повышен админом
            # до HEAD в dep-it (легален — видит сущности своего пространства), поэтому
            # берём ЧИСТОГО member-hr, который не участвует ни во владельце, ни в шеринге
            r = await c.get("/auth/dev-login?login=gavrilov")
            x_cookie = {"Cookie": f"caesar_session={r.cookies['caesar_session']}"}
            r = await c.get("/api/docs/api-list", headers=x_cookie)
            assert all(d["uid"] != d_uid for d in r.json()), \
                "документ просочился подразделению вне шеринга"

            # 16. остальные модули: страница рендерится, CRUD работает через ядро (RBAC/шэры)
            for mk, api_path, title in (
                    ("projects", "/api/projects", "Проект"),
                    ("softs", "/api/softs", "Программ"),
                    ("grants", "/api/grants", "Грант"),
                    ("schols", "/api/schols", "Стипенди"),
                    ("priority", "/api/priority", "Приоритет")):
                r = await c.get(f"/{mk}", headers=cookie)
                assert r.status_code == 200, f"{mk}: страница {r.status_code}"
                r = await c.post(api_path, headers=cookie, json={
                    "title": f"E2E {mk} сущность", "status_key": "new",
                    "department_id": depts["dep-it"]})
                assert r.status_code == 200, f"{mk}: create {r.status_code} {r.text[:200]}"
                e_uid = r.json()["uid"]
                r = await c.get(f"{api_path}/api-list", headers=m_cookie)
                assert any(x["uid"] == e_uid for x in r.json()), f"{mk}: не видно участнику ws"

    print("E2E OK: auth(RBAC+закрытый вход), страницы(Jinja), CRUD задач, outbox-воркер, "
          "шеринг READ/изоляция, совещания(payload+ICS-время), docs/softs/grants/schols/"
          "priority/projects (реестр+payload+изоляция)")


async def notify_chain_test() -> None:
    """Сквозная проверка доставки уведомлений (без реальной сети):
      1) базовые сценарии засеяны и не отключаются пользователем;
      2) планировщик создаёт события due_soon и overdue, НЕ для завершающих статусов;
      3) воркер обрабатывает telegram.overdue (не зависает в очереди);
      4) sendMessage реально вызывается для привязанного пользователя (заглушка tg).
    """
    import datetime as _dt

    from sqlalchemy import select as _select

    from app.core import notify as ncore
    from app.models.core import (EntityResponsible, NotifyScenario, User,
                                 UserNotifyPref)
    import integrations.telegram_service as tg_mod

    async with app.router.lifespan_context(app):
        # 1. посев базовых сценариев
        async with SessionLocal() as s:
            await ncore.seed_base_scenarios(s)
            base = (await s.scalars(_select(NotifyScenario).where(
                NotifyScenario.is_base.is_(True)))).all()
            assert {b.event_key for b in base} >= {"assigned", "executor_added",
                                                   "status_changed", "due_soon"}, \
                "базовые сценарии не засеяны"

            # пользователь с «привязанным Telegram»
            u = await s.get(User, 1)
            u.telegram_chat_id = 111222333
            u.telegram_optout = None
            ent = await s.scalar(_select(Entity).where(Entity.due_date.is_not(None)))
            assert ent, "нет сущности со сроком"
            ent.status_key = "new"
            y = (_dt.date.today() - _dt.timedelta(days=5)).isoformat()
            ent.due_date = y
            # просроченная, но «завершённая» — не должна напоминать
            done_ent = await s.scalar(_select(Entity).where(
                Entity.id != ent.id, Entity.kind == ent.kind))
            if done_ent:
                done_ent.status_key = "done"
                done_ent.due_date = y
            resp = await s.scalar(_select(EntityResponsible).where(
                EntityResponsible.entity_id == ent.id))
            if resp is None:
                s.add(EntityResponsible(entity_id=ent.id, user_id=u.id,
                                        department_id=ent.owner_department_id,
                                        is_primary=True))
            elif resp.user_id is None:
                resp.user_id = u.id
            # попытка пользователя отключить базовый сценарий — игнорируется ядром
            s.add(UserNotifyPref(user_id=u.id, scenario_id=base[0].id, enabled=False))
            await s.commit()
            uid_e = ent.uid

        # 2. планировщик: due + overdue события
        async with SessionLocal() as s:
            n = await ncore.schedule_due_reminders(s, days_ahead=30, window_days=1)
        async with SessionLocal() as s:
            od = (await s.scalars(_select(OutboxEvent).where(
                OutboxEvent.topic == "telegram.overdue"))).all()
            assert any(e.payload_json["entity_uid"] == uid_e for e in od), \
                "просроченная незавершённая сущность без напоминания (фильтр overdue)"
            d = (await s.scalars(_select(OutboxEvent).where(
                OutboxEvent.topic == "telegram.due"))).all()
            assert n > 0 and d, "напоминания о сроке не поставлены"

        # 3+4. воркер c заглушкой Telegram: overdue обработан, sendMessage вызван
        sent: list[tuple[int, str]] = []

        async def fake_send(chat_id: int, text: str) -> bool:
            sent.append((chat_id, text))
            return True
        orig_send = tg_mod.send_message
        tg_mod.send_message = fake_send
        try:
            await _outbox_once()
        finally:
            tg_mod.send_message = orig_send
        async with SessionLocal() as s:
            pend_od = (await s.scalars(_select(OutboxEvent).where(
                OutboxEvent.topic == "telegram.overdue",
                OutboxEvent.processed_at.is_(None)))).all()
            assert not pend_od, "telegram.overdue не обрабатывается воркером"
        assert sent, "система НЕ вызывает Telegram sendMessage для привязанного пользователя"
        assert any("Срок истёк" in t for _, t in sent), \
            f"текст overdue не дошёл: {sent[:2]}"
        # доставка адресована привязанному получателю
        assert all(cid == 111222333 for cid, _ in sent)
    print("NOTIFY E2E OK: seed базовых сценариев, due/overdue-планировщик, "
          "обработка telegram.overdue воркером, фактический вызов sendMessage")


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
