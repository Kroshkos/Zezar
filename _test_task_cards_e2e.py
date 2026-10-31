"""E2E: карточки печати задач — подзадачи не печатаются отдельно,
а выводятся в содержании родительской задачи (наименование · дедлайн · статус).

Проверяет реальный HTTP-контур /api/tasks/cards/api-list на тестовой БД
(как в _test_softs_e2e.py).
"""
import asyncio, os, tempfile
_tmp = tempfile.mkdtemp(prefix="caesar_taskcards_")
os.environ["DATA_DIR"] = _tmp
os.environ["DATABASE_PATH"] = os.path.join(_tmp, "test.db")
os.environ["SECRET_KEY"] = "e2e-secret"
os.environ["DEV_LOGIN_ENABLED"] = "true"
os.environ["ADMIN_USERNAME"] = "ivanov"
os.environ["ADMIN_PASSWORD"] = "E2E-Admin-Pass1"
os.environ["WORKERS_ENABLED"] = "false"
os.environ["SEED_DEMO_DATA"] = "true"

import httpx
from app.main import app


async def main():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t", follow_redirects=False) as c:
        async with app.router.lifespan_context(app):
            r = await c.get("/auth/dev-login?login=ivanov")
            assert r.status_code == 302, r.text
            adm = {"Cookie": f"caesar_session={r.cookies['caesar_session']}"}

            # страница задач: берём пространство и людей из контекста страницы
            r = await c.get("/tasks/", headers=adm)
            assert r.status_code == 200, r.text[:300]
            import json, re
            m = re.search(r'<script id="taskData" type="application/json">(.*?)</script>', r.text, re.S)
            assert m, "no taskData on page"
            data = json.loads(m.group(1).replace("&#34;", '"'))
            dep = (data.get("departments") or [{}])[0].get("id") or 1
            people = data.get("people") or []

            async def mk(title, due, parent=None):
                body = {"title": title, "due_date": due, "department_id": dep,
                        "status_key": "in_progress",
                        "executor_user_ids": [people[0]["id"]] if people else []}
                if parent: body["parent_task_uid"] = parent
                r = await c.post("/api/tasks", headers=adm, json=body)
                assert r.status_code == 200, r.text
                return r.json()["uid"]

            root = await mk("ЭО ROOT-карточка", "2026-11-10")
            sA   = await mk("ЭО SUB-A", "2026-11-05", root)
            sB   = await mk("ЭО SUB-B", "2026-11-07", root)
            gW   = await mk("ЭО GRAND-W", "2026-11-06", sA)   # потомок 2 уровня
            solo = await mk("ЭО SOLO", "2026-11-09")

            r = await c.get("/api/tasks/cards/api-list", headers=adm)
            assert r.status_code == 200, r.text[:300]
            cards = {x["uid"]: x for x in r.json()["cards"]}

            for uid, name in ((root, "родитель"), (solo, "обычная")):
                assert uid in cards, f"{name} должен печататься отдельной карточкой"
            for uid, name in ((sA, "SUB-A"), (sB, "SUB-B"), (gW, "GRAND-W")):
                assert uid not in cards, \
                    f"{name}: подзадача НЕ должна печататься отдельной карточкой"

            subs = cards[root]["subtasks"]
            got = [(x["title"], x["due_date"], x["status_label"]) for x in subs]
            assert ("ЭО SUB-A", "2026-11-05") in [(t, d) for t, d, _ in got], got
            assert any(t == "ЭО SUB-B" and d == "2026-11-07" for t, d, _ in got), got
            assert all(lbl for _, _, lbl in got), f"статус выполнения пуст: {got}"
            assert any(t.startswith("\u21b3") and "GRAND-W" in t for t, _, _ in got), \
                f"внучка не в составе родителя: {subs}"
            assert not cards[solo]["subtasks"], "у обычной задачи подзадач быть не должно"

            # PATCH: перевод обычной задачи в подзадачи -> её карточка исчезает,
            # она появляется в составе нового родителя
            r = await c.patch("/api/tasks/" + solo, headers=adm,
                              json={"parent_task_uid": root})
            assert r.status_code == 200, r.text
            r = await c.get("/api/tasks/cards/api-list", headers=adm)
            cards = {x["uid"]: x for x in r.json()["cards"]}
            assert solo not in cards, "после PATCH подзадача всё ещё печатается отдельно!"
            assert any(x["title"] == "ЭО SOLO" for x in cards[root]["subtasks"]), \
                "после PATCH задача не появилась в составе родителя"

            print("OK: отдельные карточки:", sorted(x["title"] for x in cards.values()))
            print("OK: содержание родителя:",
                  [(x["title"], x["due_date"], x["status_label"]) for x in cards[root]["subtasks"]])
            print("ALL CHECKS PASSED")

asyncio.run(main())
