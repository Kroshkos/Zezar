"""E2E: справочники (refdata) — админ/руководитель, преемственность шаблонов, активация/деактивация.
Запуск:  python scripts/e2e_refdata_test.py
Временная БД + dev-вход; все данные хранятся в SQLite через REST API (localStorage не используется)."""
import asyncio
import os
import tempfile

# --- конфигурация ДО импорта приложения ---
_tmp = tempfile.mkdtemp(prefix="caesar_ref_e2e_")
os.environ["DATA_DIR"] = _tmp
os.environ["DATABASE_PATH"] = os.path.join(_tmp, "test.db")
os.environ["SECRET_KEY"] = "e2e-secret"
os.environ["DEV_LOGIN_ENABLED"] = "true"
os.environ["ADMIN_USERNAME"] = "ivanov"
os.environ["ADMIN_PASSWORD"] = "E2E-Admin-Pass1"
os.environ["WORKERS_ENABLED"] = "false"

import httpx  # noqa: E402

from app.main import app  # noqa: E402


async def main() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                 follow_redirects=False) as c:
        async with app.router.lifespan_context(app):
            async def ck(login: str) -> dict:
                r = await c.get(f"/auth/dev-login?login={login}")
                assert r.status_code == 302, r.text
                return {"Cookie": f"caesar_session={r.cookies['caesar_session']}"}

            adm = await ck("ivanov")     # администратор
            hd = await ck("sidorov")     # руководитель ИТ
            hr = await ck("morozova")    # руководитель HR
            mb = await ck("kuznetsov")   # рядовой сотрудник ИТ

            # подразделения руководителей (контекст берём из /sections без department_id)
            r = await c.get("/api/core/refdata/sections", headers=hd)
            assert r.status_code == 200, r.text
            it_dept = r.json()["department_id"]
            r = await c.get("/api/core/refdata/sections", headers=hr)
            assert r.status_code == 200, r.text
            hr_dept = r.json()["department_id"]
            assert it_dept and hr_dept and it_dept != hr_dept
            print(f"OK 0: depts resolved IT={it_dept} HR={hr_dept}")

            # 1. Админ создаёт ГЛОБАЛЬНЫЙ статус проекта (department_id=null)
            r = await c.post("/api/core/refdata/status", headers=adm, json={
                "set": "project", "label": "На ревизии глоб", "color": "b-amber",
                "order": 5, "department_id": None})
            assert r.status_code == 200, r.text
            gid = r.json()["id"]
            gstat_key = r.json()["key"]
            print("OK 1: admin created global project status id=", gid, "key=", gstat_key)

            # 2. Руководитель видит глобальный шаблон в эффективном наборе своего подразд.
            r = await c.get(f"/api/core/refdata/effective?kind=project&department_id={it_dept}",
                            headers=hd)
            assert r.status_code == 200, r.text
            keys = [i["key"] for i in r.json()["statuses"]]
            assert gstat_key in keys, keys
            print("OK 2: effective statuses include global:", keys)

            # 3. В «доступных для активации» у руководителя он помечен неприменённым
            r = await c.get(f"/api/core/refdata/view?ref=status&set=project&department_id={it_dept}",
                            headers=hd)
            assert r.status_code == 200, r.text
            avail = {a["id"]: a for a in r.json()["available_templates"]}
            assert gid in avail and not avail[gid]["activated"], avail
            print("OK 3: template listed as available for activation")

            # 4. Руководитель активирует шаблон (создаётся копия)
            r = await c.post("/api/core/refdata/activate", headers=hd, json={
                "global_id": gid, "department_id": it_dept})
            assert r.status_code == 200, r.text
            copy_id = r.json()["copy_id"]
            r = await c.get(f"/api/core/refdata/effective?kind=project&department_id={it_dept}",
                            headers=hd)
            assert gstat_key in [i["key"] for i in r.json()["statuses"]]
            print("OK 4: head activated template copy id=", copy_id)

            # 5. Деактивация копии выключает шаблон для подразделения
            r = await c.post(f"/api/core/refdata/deactivate/status/{copy_id}"
                             f"?department_id={it_dept}", headers=hd)
            assert r.status_code == 200, r.text
            assert r.json()["is_active"] is False
            r = await c.get(f"/api/core/refdata/effective?kind=project&department_id={it_dept}",
                            headers=hd)
            assert gstat_key not in [i["key"] for i in r.json()["statuses"]], \
                r.json()["statuses"]
            print("OK 5: deactivated template hidden from effective set")

            # 6. Повторная активация восстанавливает ту же запись (преемственность)
            r = await c.post("/api/core/refdata/activate", headers=hd, json={
                "global_id": gid, "department_id": it_dept})
            assert r.status_code == 200, r.text
            assert r.json()["copy_id"] == copy_id, "must reuse same row"
            r = await c.get(f"/api/core/refdata/effective?kind=project&department_id={it_dept}",
                            headers=hd)
            assert gstat_key in [i["key"] for i in r.json()["statuses"]]
            print("OK 6: reactivation restored the SAME copy row (continuity)")

            # 7. Руководитель создаёт собственный локальный статус
            r = await c.post("/api/core/refdata/status", headers=hd, json={
                "set": "project", "label": "ИТ внутренний", "color": "b-blue",
                "order": 9, "department_id": None})
            assert r.status_code == 200, r.text
            lid = r.json()["id"]
            lkey = r.json()["key"]
            print("OK 7: head created local status id=", lid)

            # 8. Другое подразделение НЕ видит локальный статус, но видит глобальный
            r = await c.get(f"/api/core/refdata/effective?kind=project&department_id={hr_dept}",
                            headers=hr)
            keys_hr = [i["key"] for i in r.json()["statuses"]]
            assert lkey not in keys_hr, keys_hr
            assert gstat_key in keys_hr, keys_hr
            print("OK 8: HR sees global but not IT-local:", keys_hr)

            # 9. Рядовой сотрудник не может управлять справочниками
            r = await c.post("/api/core/refdata/status", headers=mb, json={
                "set": "task", "label": "Хак", "department_id": None})
            assert r.status_code in (401, 403), r.status_code
            print("OK 9: member forbidden:", r.status_code)

            # 10. Руководитель НЕ может править сам глобальный шаблон админа
            r = await c.patch(f"/api/core/refdata/status/{gid}", headers=hd,
                              json={"label": "Взлом", "department_id": None})
            assert r.status_code in (401, 403), r.status_code
            r = await c.delete(f"/api/core/refdata/status/{gid}?department_id={it_dept}",
                               headers=hd)
            assert r.status_code in (401, 403), r.status_code
            print("OK 10: head cannot edit/delete global template directly")

            # 11. Типы документов / ПО / стипендий (глобально админом)
            type_ids = {}
            for kind in ("doc_type", "soft_type", "schol_type"):
                r = await c.post("/api/core/refdata/type", headers=adm, json={
                    "kind": kind, "label": f"{kind} тест", "department_id": None})
                assert r.status_code == 200, (kind, r.text)
                type_ids[kind] = r.json()["id"]
            print("OK 11: doc/soft/schol types created:", type_ids)

            # 12. Приоритеты: глобальный виден подразделению с включённым модулем tasks
            r = await c.post("/api/core/refdata/prio", headers=adm, json={
                "label": "Критический е2е", "color": "b-red", "weight": 100,
                "department_id": None})
            assert r.status_code == 200, r.text
            pgid = r.json()["id"]
            pkey = r.json()["key"]
            r = await c.get(f"/api/core/refdata/effective?kind=task&department_id={it_dept}",
                            headers=hd)
            assert pkey in [i["key"] for i in r.json()["priorities"]], r.json()["priorities"]
            print("OK 12: global priority visible to dept")

            # 13. Маршруты (wf) и группы задач (для совещаний)
            r = await c.post("/api/core/refdata/wf", headers=adm, json={
                "name": "Быстрый маршрут е2е", "steps": ["Черновик", "Согласование", "Подписан"],
                "department_id": None})
            assert r.status_code == 200, r.text
            wfid = r.json()["id"]
            r = await c.post("/api/core/refdata/groups", headers=adm, json={
                "name": "ИТ-повестка е2е", "criteria": {"statuses": ["new"]},
                "department_id": None})
            assert r.status_code == 200, r.text
            grpid = r.json()["id"]
            r = await c.get(f"/api/core/refdata/effective?kind=task&department_id={it_dept}",
                            headers=hd)
            eff = r.json()
            assert any(t["id"] == wfid for t in eff["templates"]), eff["templates"]
            assert any(g["id"] == grpid for g in eff["groups"]), eff["groups"]
            print("OK 13: wf route + task group created & effective")

            # 14. Информативность: нельзя удалить занятый элемент (используется сущностью)
            r = await c.post("/api/tasks", headers=adm, json={
                "title": "Задача под приоритетом", "status_key": "new", "priority_key": pkey})
            assert r.status_code == 200, r.text
            r = await c.delete(f"/api/core/refdata/prio/{pgid}", headers=adm)
            assert r.status_code == 400, (r.status_code, r.text)
            print("OK 14: used priority deletion blocked:", r.json()["detail"])

            # 15. Удаление неиспользуемого глобального статуса админом — исчезает у всех
            r = await c.delete(f"/api/core/refdata/status/{gid}", headers=adm)
            assert r.status_code == 200, r.text
            r = await c.get(f"/api/core/refdata/effective?kind=project&department_id={hr_dept}",
                            headers=hr)
            assert gstat_key not in [i["key"] for i in r.json()["statuses"]]
            print("OK 15: admin deleted global status, gone everywhere")

            # 16. Данные переживают новый запрос (хранение в БД, а не в клиенте)
            r = await c.get(f"/api/core/refdata/effective?kind=project&department_id={it_dept}",
                            headers=hd)
            assert lkey in [i["key"] for i in r.json()["statuses"]]
            print("OK 16: local status persisted in DB across requests")

    print("\nALL REFDATA E2E CHECKS PASSED")


asyncio.run(main())
