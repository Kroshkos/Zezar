"""E2E: раздел «Карточки» модуля «Совещания» — подзадачи (parent_task_uid)
не должны печататься отдельными карточками; они выводятся на родительской
карточке строками «наименование · дедлайн · статус»."""
import asyncio, os, tempfile, json, re
_tmp = tempfile.mkdtemp(prefix="caesar_meetcards_")
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

            r = await c.get("/tasks/", headers=adm)
            m = re.search(r'<script id="taskData" type="application/json">(.*?)</script>', r.text, re.S)
            data = json.loads(m.group(1).replace("&#34;", '"'))
            dep = (data.get("departments") or [{}])[0].get("id") or 1

            async def mk(title, due, parent=None):
                body = {"title": title, "due_date": due, "department_id": dep,
                        "status_key": "in_progress"}
                if parent: body["parent_task_uid"] = parent
                r = await c.post("/api/tasks", headers=adm, json=body)
                assert r.status_code == 200, r.text
                return r.json()["uid"]

            root = await mk("МК ROOT", "2026-11-10")
            sA   = await mk("МК SUB-A", "2026-11-05", root)
            gW   = await mk("МК GRAND-W", "2026-11-06", sA)
            solo = await mk("МК SOLO", "2026-11-09")

            r = await c.get("/api/meetings/cards/api-list", headers=adm)
            assert r.status_code == 200, r.text[:300]
            cards = {x["uid"]: x for x in r.json()["cards"]}

            print("root in cards:", root in cards)
            print("subA in cards (should be False):", sA in cards)
            print("grandW in cards (should be False):", gW in cards)
            print("solo in cards (should be True):", solo in cards)
            rc = cards.get(root, {})
            print("root.subtasks field:", rc.get("subtasks"))
            print("root keys:", sorted(k for k in rc if 'sub' in k or 'task' in k))

asyncio.run(main())
