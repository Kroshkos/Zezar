import asyncio, os, sys, tempfile
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_tmp, "test.db")
os.environ["DEV_LOGIN_ENABLED"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import httpx
from app.main import app


async def main():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t", follow_redirects=False) as c:
        async with app.router.lifespan_context(app):
            r = await c.get("/auth/dev-login?email=admin@caesar.local")
            cookie = {"Cookie": f"caesar_session={r.cookies['caesar_session']}"}
            r = await c.get("/api/core/departments", headers=cookie)
            depts = {d["code"]: d["id"] for d in r.json()}
            print("depts:", depts)
            # 8b из e2e: админ создаёт задачу в своём пространстве с share_with=[dep-strat]
            r = await c.post("/api/tasks", headers=cookie, json={
                "title": "КС: интеграция страт+ИТ", "status_key": "new",
                "priority_key": "high", "due_date": "2026-12-01",
                "share_with": [depts["dep-strat"]]})
            assert r.status_code == 200, r.text
            cross_uid = r.json()["uid"]
            print("cross_uid:", cross_uid)

            # глазами админа (is_global)
            r = await c.get("/api/tasks/api-list", headers=cookie)
            adm = [t for t in r.json() if t["uid"] == cross_uid]
            print("ADMIN view:", adm and (adm[0]["cross_dept"], adm[0].get("owner_department_id")))

            # глазами участника dep-strat
            r = await c.get("/auth/dev-login?email=member-strat@caesar.local")
            o_cookie = {"Cookie": f"caesar_session={r.cookies['caesar_session']}"}
            r = await c.get("/api/tasks/api-list", headers=o_cookie)
            lst2 = [t for t in r.json() if t["uid"] == cross_uid]
            print("STRAT view:", lst2 and (lst2[0]["cross_dept"], lst2[0]["can_edit"]))

            # прямые шары через API
            r = await c.get(f"/api/core/entities/{cross_uid}/shares", headers=cookie)
            print("shares:", r.status_code, r.text[:300])
            r = await c.get(f"/api/core/entities/{cross_uid}/responsibles", headers=cookie)
            print("resps:", r.status_code, r.text[:400])

            # диагностика на уровне сервиса (без HTTP): flush-последовательность
            from app.db.engine import SessionLocal
            from app.models.core import Entity, EntityShare, EntityResponsible
            from sqlalchemy import select
            async with SessionLocal() as s:
                ent = await s.scalar(select(Entity).where(Entity.uid == cross_uid))
                print("DB owner_dept_id:", ent.owner_department_id)
                sh = list(await s.scalars(select(EntityShare).where(EntityShare.entity_id == ent.id)))
                print("DB shares:", [(x.target_department_id, x.permission) for x in sh])
                rs = list(await s.scalars(select(EntityResponsible).where(EntityResponsible.entity_id == ent.id)))
                print("DB resps:", [(x.department_id, x.user_id, x.is_primary) for x in rs])

asyncio.run(main())
