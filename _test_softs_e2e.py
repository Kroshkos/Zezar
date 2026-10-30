import asyncio, os, tempfile
_tmp = tempfile.mkdtemp(prefix="caesar_softs_")
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

            # создать тип ПО в справочнике (глобально)
            r = await c.post("/api/core/refdata/type", headers=adm,
                             json={"kind": "soft_type", "label": "Веб-сервис", "department_id": None})
            assert r.status_code == 200, r.text
            print("OK type created:", r.json())

            # страница softs рендерится и содержит софт-типы, people, me
            r = await c.get("/softs/", headers=adm)
            assert r.status_code == 200, r.text[:500]
            assert '"soft_types"' in r.text and '"me"' in r.text and '"people"' in r.text
            import json, re
            m = re.search(r'<script id="softData" type="application/json">(.*?)</script>', r.text, re.S)
            data = json.loads(m.group(1).replace("&#34;", '"'))
            labels = [t["label"] for t in data["soft_types"]]
            assert "Веб-сервис" in labels, labels
            assert data["me"], "me missing"
            assert len(data["people"]) >= 7, data["people"]
            print("OK page: types=", labels, "people=", len(data["people"]), "me=", data["me"])

            # создать ПО с ответственным (petrova) и исполнителями из ДРУГОГО подразделения (kuznetsov — dep-it)
            people = {p["name"]: p["id"] for p in data["people"]}
            petrova = next(p["id"] for p in data["people"] if "Петрова" in p["name"])
            kuzn = next(p["id"] for p in data["people"] if "Кузнецов" in p["name"])
            sidorov = next(p["id"] for p in data["people"] if "Сидоров" in p["name"])
            r = await c.post("/api/softs", headers=adm, json={
                "title": "Тестовый веб-сервис е2е", "status_key": "dev",
                "soft_type": "Веб-сервис", "env": "stage",
                "responsible_user_id": petrova,
                "executor_user_ids": [kuzn, sidorov],
            })
            assert r.status_code == 200, r.text
            uid = r.json()["uid"]
            print("OK created:", uid)

            r = await c.get("/api/softs/api-list", headers=adm)
            item = next(s for s in r.json() if s["uid"] == uid)
            assert item["responsible_user_id"] == petrova, item
            assert {e["id"] for e in item["executors"]} == {kuzn, sidorov}, item["executors"]
            assert item["soft_type"] == "Веб-сервис"
            print("OK list: resp=", item["responsibles"], "execs=", item["executor_names"])

            # невалидный тип -> 422
            r = await c.post("/api/softs", headers=adm, json={"title": "Другой", "soft_type": "Из головы"})
            assert r.status_code == 422, r.text
            print("OK invalid type rejected:", r.json()["detail"])

            # PATCH: смена ответственного и типа
            r = await c.patch(f"/api/softs/{uid}", headers=adm, json={
                "responsible_user_id": sidorov, "soft_type": "Веб-сервис",
                "executor_user_ids": [kuzn]})
            assert r.status_code == 200, r.text
            r = await c.get("/api/softs/api-list", headers=adm)
            item = next(s for s in r.json() if s["uid"] == uid)
            assert item["responsible_user_id"] == sidorov, item
            assert {e["id"] for e in item["executors"]} == {kuzn}, item["executors"]
            print("OK patch: resp=", item["responsibles"], "execs=", item["executor_names"])

            # PATCH невалидным типом -> 422
            r = await c.patch(f"/api/softs/{uid}", headers=adm, json={"soft_type": "Нет такого"})
            assert r.status_code == 422, r.text
            print("OK patch invalid type rejected")

            # обычный пользователь (kuznetsov, dep-it) может выбрать только своих коллег
            r = await c.get("/auth/dev-login?login=kuznetsov")
            mb = {"Cookie": f"caesar_session={r.cookies['caesar_session']}"}
            r = await c.get("/softs/", headers=mb)
            m = re.search(r'<script id="softData" type="application/json">(.*?)</script>', r.text, re.S)
            d2 = json.loads(m.group(1))
            names = [p["name"] for p in d2["people"]]
            assert all("Петрова" not in n for n in names), names  # изоляция workspace
            print("OK member people scoped:", names)
            r = await c.post("/api/softs", headers=mb, json={
                "title": "Задача от участника", "responsible_user_id": petrova})
            assert r.status_code in (400, 403, 422), (r.status_code, r.text)
            print("OK member cannot assign foreign responsible:", r.json().get("detail"))

    print("\nALL SOFTS E2E CHECKS PASSED")

asyncio.run(main())
