"""Смоук-тест мобильного API (app/mobile.py) на временной SQLite-БД.

Запуск:  ENVIRONMENT=development SECRET_KEY=test-secret-key-0123456789abcdef \
         DATABASE_PATH=/tmp/zezar_test.db ADMIN_USERNAME=admin ADMIN_PASSWORD=adminpass123 \
         WORKERS_ENABLED=false python scripts/smoke_mobile_api.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def main() -> int:
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import select, update

    import app.main as m
    from app.core.security import hash_password
    from app.db.base import Base
    from app.db.engine import SessionLocal, engine
    from app.models.core import ApiToken, Department, DepartmentMember, Entity, User

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # --- данные: подразделение + пользователь ---
    async with SessionLocal() as s:
        u = await s.scalar(select(User).where(User.login == "mobileuser"))
        if not u:
            u = User(login="mobileuser", display_name="Тест Тестович",
                     password_hash=hash_password("testpass123"),
                     must_change_password=False, role=u_role_default(),
                     is_active=True, initials="ТТ")
            s.add(u)
            d = Department(code="tst", name="Тестовое подразделение", is_active=True)
            s.add(d)
            await s.flush()
            s.add(DepartmentMember(user_id=u.id, department_id=d.id))
            ent = Entity(uid="smoke-task-1", kind="task", module_key="tasks",
                         owner_department_id=d.id, title="Стартовая задача",
                         status_key=None, priority_key="medium", tags=[],
                         extra_json={}, sync_state="none")
            s.add(ent)
        await s.commit()
        uid = u.id

    from asgi_lifespan import LifespanManager

    transport = ASGITransport(app=m.app)
    # Прогоняем lifespan приложения (создание таблиц, реестр модулей и т.д.)
    async with LifespanManager(m.app):
      async with AsyncClient(transport=transport, base_url="http://test") as c:
        # 1. token-login (неверный пароль -> 401)
        r = await c.post("/api/auth/token-login",
                         json={"login": "mobileuser", "password": "wrong"})
        assert r.status_code == 401, (r.status_code, r.text)
        print("OK  token-login rejects wrong password")

        # 2. token-login верно
        r = await c.post("/api/auth/token-login",
                         json={"login": "mobileuser", "password": "testpass123"})
        assert r.status_code == 200, (r.status_code, r.text)
        tok = r.json()["access_token"]
        H = {"Authorization": f"Bearer {tok}"}
        print("OK  token-login issues bearer token")

        # 3. /me
        r = await c.get("/api/auth/token/me", headers=H)
        assert r.status_code == 200 and r.json()["login"] == "mobileuser", r.text
        print("OK  /api/auth/token/me")

        # 4. bootstrap через мобильный роутер
        r = await c.get("/api/mobile/bootstrap", headers=H)
        assert r.status_code == 200, r.text
        b = r.json()
        assert b["me"]["login"] == "mobileuser" and b["departments"], r.text
        print(f"OK  bootstrap: depts={len(b['departments'])}, modules={len(b['modules'])}")

        # 5. entities list — видит стартовую задачу
        r = await c.get("/api/mobile/entities?module_key=tasks", headers=H)
        assert r.status_code == 200, r.text
        lst = r.json()
        assert any(e["uid"] == "smoke-task-1" for e in lst), r.text
        print(f"OK  entities list ({len(lst)})")

        # 6. без токена -> 401
        r = await c.get("/api/mobile/entities")
        assert r.status_code == 401, r.status_code
        print("OK  unauthenticated -> 401")

        # 7. создать задачу
        r = await c.post("/api/mobile/tasks", headers=H,
                         json={"title": "Задача из мобильного", "priority_key": "high"})
        assert r.status_code == 200, r.text
        new_uid = r.json()["uid"]
        print(f"OK  create task uid={new_uid[:8]}…")

        # 8. патч статуса
        r = await c.patch(f"/api/mobile/entities/{new_uid}", headers=H,
                          json={"status_key": "in_progress", "description": "обновлено из app"})
        assert r.status_code == 200, r.text
        print("OK  patch entity")

        # 8b. одна карточка по uid (экран деталей приложения)
        r = await c.get(f"/api/mobile/entities/{new_uid}", headers=H)
        assert r.status_code == 200, r.text
        one = r.json()
        assert one["uid"] == new_uid and one["status_key"] == "in_progress", r.text
        r = await c.get("/api/mobile/entities/no-such-uid", headers=H)
        assert r.status_code == 404, r.status_code
        print("OK  get single entity by uid (+404 for unknown)")

        # 9. существующие веб-API под Bearer (гибридная авторизация)
        r = await c.get("/api/tasks/api-list", headers=H)
        assert r.status_code == 200, (r.status_code, r.text[:200])
        assert any(t["uid"] == new_uid for t in r.json()), "новая задача не видна в web api-list"
        print(f"OK  legacy /api/tasks/api-list via Bearer ({len(r.json())} tasks)")

        r = await c.get("/api/core/departments", headers=H)
        assert r.status_code == 200, (r.status_code, r.text[:200])
        print("OK  legacy /api/core/departments via Bearer")

        # 10. отзыв токена -> снова 401
        r = await c.post("/api/auth/token-revoke", headers=H)
        assert r.status_code == 200, r.text
        r = await c.get("/api/mobile/bootstrap", headers=H)
        assert r.status_code == 401, r.status_code
        print("OK  token revoke invalidates bearer")

    await engine.dispose()
    print("\nALL SMOKE TESTS PASSED")
    return 0


def u_role_default():
    from app.models.core import Role
    return Role.MEMBER


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
