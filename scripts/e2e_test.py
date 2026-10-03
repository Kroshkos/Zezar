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

            # 10. изоляция: посторонний участник не видит чужие задачи (созданные НЕ им);
            #     расшаренную задачу он видеть ДОЛЖЕН (межпространственный контекст)
            own_uid, _ = await _create_task_as("member-strat@caesar.local",
                                               "Своя задача участника страт.")
            r = await c.get("/auth/dev-login?email=member-strat@caesar.local")
            o_cookie = {"Cookie": f"caesar_session={r.cookies['caesar_session']}"}
            r = await c.get("/api/tasks/api-list", headers=o_cookie)
            lst = r.json()
            assert all(t["uid"] != uid for t in lst), \
                "задача админа без share_with просочилась в чужое пространство"
            assert any(t["uid"] == own_uid for t in lst), "участник не видит собственную задачу"

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

            # 10c. обычная задача админа (без share_with) по-прежнему изолирована
            assert all(t["uid"] != uid for t in lst2), \
                "задача админа без share_with просочилась в чужое пространство"

            # 11. healthz / реестр
            r = await c.get("/healthz")
            assert "tasks" in r.json()["modules"] and "dashboard" in r.json()["modules"]

    print("E2E OK: auth(RBAC+закрытый вход), страницы(Jinja), CRUD задач, outbox-воркер, "
          "шеринг READ/изоляция")


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
