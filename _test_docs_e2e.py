import asyncio, os, tempfile
_tmp = tempfile.mkdtemp(prefix="caesar_docs_")
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

            # создать вид документа в справочнике (глобально)
            r = await c.post("/api/core/refdata/type", headers=adm,
                             json={"kind": "doc_type", "label": "Договор", "department_id": None})
            assert r.status_code == 200, r.text
            print("OK type created:", r.json())

            # страница docs рендерится и содержит doc_types, people, me
            r = await c.get("/docs/", headers=adm)
            assert r.status_code == 200, r.text[:500]
            assert '"doc_types"' in r.text and '"me"' in r.text and '"people"' in r.text
            import json, re
            m = re.search(r'<script id="docData" type="application/json">(.*?)</script>', r.text, re.S)
            data = json.loads(m.group(1).replace("&#34;", '"'))
            labels = [t["label"] for t in data["doc_types"]]
            assert "Договор" in labels, labels
            assert data["me"], "me missing"
            assert len(data["people"]) >= 7, data["people"]
            print("OK page: types=", labels, "people=", len(data["people"]), "me=", data["me"])

            petrova = next(p["id"] for p in data["people"] if "Петрова" in p["name"])
            kuzn = next(p["id"] for p in data["people"] if "Кузнецов" in p["name"])
            sidorov = next(p["id"] for p in data["people"] if "Сидоров" in p["name"])

            # создать документ с ответственным (petrova) и исполнителями из ДРУГОГО подразделения
            r = await c.post("/api/docs", headers=adm, json={
                "title": "Тестовый договор е2е", "status_key": "draft",
                "doc_type": "Договор", "due_date": "2026-11-20",
                "responsible_user_id": petrova,
                "executor_user_ids": [kuzn, sidorov],
            })
            assert r.status_code == 200, r.text
            uid = r.json()["uid"]
            print("OK created:", uid)

            r = await c.get("/api/docs/api-list", headers=adm)
            item = next(s for s in r.json() if s["uid"] == uid)
            assert item["responsible_user_id"] == petrova, item
            assert {e["id"] for e in item["executors"]} == {kuzn, sidorov}, item["executors"]
            assert item["doc_type"] == "Договор"
            assert item["due_date"] == "2026-11-20"
            print("OK list: resp=", item["responsibles"], "execs=", item["executor_names"])

            # невалидный вид -> 422
            r = await c.post("/api/docs", headers=adm, json={"title": "Другой документ", "doc_type": "Из головы"})
            assert r.status_code == 422, r.text
            print("OK invalid doc_type rejected:", r.json()["detail"])

            # PATCH: смена ответственного и вида
            r = await c.patch(f"/api/docs/{uid}", headers=adm, json={
                "responsible_user_id": sidorov, "doc_type": "Договор",
                "executor_user_ids": [kuzn]})
            assert r.status_code == 200, r.text
            r = await c.get("/api/docs/api-list", headers=adm)
            item = next(s for s in r.json() if s["uid"] == uid)
            assert item["responsible_user_id"] == sidorov, item
            assert {e["id"] for e in item["executors"]} == {kuzn}, item["executors"]
            print("OK patch: resp=", item["responsibles"], "execs=", item["executor_names"])

            # PATCH невалидным видом -> 422
            r = await c.patch(f"/api/docs/{uid}", headers=adm, json={"doc_type": "Нет такого"})
            assert r.status_code == 422, r.text
            print("OK patch invalid doc_type rejected")

            # обычный пользователь (kuznetsov, dep-it) — только свои коллеги; чужой ответственный запрещён
            r = await c.get("/auth/dev-login?login=kuznetsov")
            mb = {"Cookie": f"caesar_session={r.cookies['caesar_session']}"}
            r = await c.get("/docs/", headers=mb)
            m = re.search(r'<script id="docData" type="application/json">(.*?)</script>', r.text, re.S)
            d2 = json.loads(m.group(1))
            names = [p["name"] for p in d2["people"]]
            assert all("Петрова" not in n for n in names), names  # изоляция workspace
            print("OK member people scoped:", names)
            r = await c.post("/api/docs", headers=mb, json={
                "title": "Документ от участника", "responsible_user_id": petrova})
            assert r.status_code in (400, 403, 422), (r.status_code, r.text)
            print("OK member cannot assign foreign responsible:", r.json().get("detail"))

            # участник создаёт документ со своим выбором ответственного/исполнителей из своих коллег
            inner = d2["people"][0]["id"]
            r = await c.post("/api/docs", headers=mb, json={
                "title": "Акт от участника", "doc_type": "Договор",
                "responsible_user_id": inner, "executor_user_ids": [inner]})
            assert r.status_code == 200, r.text
            print("OK member create with responsible:", r.json()["uid"])

    print("\nALL DOCS E2E CHECKS PASSED")

asyncio.run(main())
