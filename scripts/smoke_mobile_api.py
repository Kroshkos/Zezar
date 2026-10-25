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
            d2 = Department(code="oth", name="Чужое подразделение", is_active=True)
            s.add(d2)
            await s.flush()
            s.add(Entity(uid="foreign-card-1", kind="task", module_key="tasks",
                         owner_department_id=d2.id, title="Чужая карточка",
                         status_key=None, priority_key="medium", tags=[],
                         extra_json={}, sync_state="none"))
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

        # 9b. push-подписка + лента событий (v1.3)
        r = await c.post("/api/mobile/push/register", headers=H,
                         json={"expo_token": "ExponentPushToken[smoketest000000]",
                               "platform": "ios", "device_label": "Smoke iPhone"})
        assert r.status_code == 200, r.text
        print("OK  push/register")

        r = await c.get("/api/mobile/events?since_id=0&wait=0", headers=H)
        assert r.status_code == 200, r.text
        feed = r.json()
        assert "events" in feed and "unread" in feed, r.text
        print(f"OK  events feed (unread={feed['unread']}, last_id={feed['last_id']})")

        # патч чужой карточки другим пользователем → событие в ленте владельца;
        # но проще: создали/патчили сами — себе не шлём. Проверяем read no-op и unregister.
        r = await c.post("/api/mobile/events/read", headers=H, json={"all": True})
        assert r.status_code == 200, r.text
        r = await c.post("/api/mobile/push/unregister", headers=H,
                         json={"expo_token": "ExponentPushToken[smoketest000000]"})
        assert r.status_code == 200, r.text
        print("OK  events/read + push/unregister")

        # 9c. QR-шлюз «приложение или сайт» (v1.5, app/routers/entity_open.py)
        from app.core.security import SESSION_COOKIE, create_session_token
        cookie_h = {"Cookie": f"{SESSION_COOKIE}={create_session_token({'uid': uid})}"}

        r = await c.get("/open/e/smoke-task-1")            # гость (без куки)
        assert r.status_code == 200 and "text/html" in r.headers.get("content-type", ""), r.status_code
        page = r.text
        assert "zezar://entity/smoke-task-1" in page, "нет deep link в шлюзе"
        assert "/login?next=" in page, "гость не редиректится на логин с next"
        print("OK  /open/e/{uid} gateway for guest (deep link + login?next)")

        r = await c.get("/open/e/no-such-uid-xyz")         # гость: ответ идентичен
        assert r.status_code == 200 and "/login?next=" in r.text, r.status_code
        assert "smoke-task" not in r.text, "ответ гостю различает существующие uid"
        print("OK  /open/e unknown uid as guest -> same login page (no leak)")

        # авторизованный владелец (session-cookie как в браузере) -> свой модуль
        r = await c.get(f"/open/e/{new_uid}", headers=cookie_h)
        assert r.status_code == 200, r.status_code
        page = r.text.replace("&amp;", "&")
        assert "/tasks?focus=" + new_uid in page, r.text[:400]
        assert f"zezar://entity/{new_uid}?t=" in page, "нет гостевого токена в deep link"
        import re as _re
        m = _re.search(rf"zezar://entity/{new_uid}\?t=([\w.-]+)", page)
        assert m, "не извлёкся гостевой токен"
        guest_tok = m.group(1)
        print("OK  /open/e authorized owner -> module deep link + guest token")

        # обмен гостевого токена на Bearer access_token (вход в приложение без пароля)
        r = await c.post("/api/mobile/guest-login", json={"token": guest_tok})
        assert r.status_code == 200, r.text[:200]
        gl = r.json()
        assert gl.get("access_token") and gl.get("guest") is True, gl
        GH = {"Authorization": f"Bearer {gl['access_token']}"}
        r = await c.get(f"/api/mobile/entities/{new_uid}", headers=GH)
        assert r.status_code == 200, r.text[:200]  # права того же пользователя
        print("OK  guest-login -> bearer token opens the card")

        # одноразовость: повторный обмен того же токена -> 401
        r = await c.post("/api/mobile/guest-login", json={"token": guest_tok})
        assert r.status_code == 401, (r.status_code, r.text[:200])
        # подделка токена -> 401
        r = await c.post("/api/mobile/guest-login", json={"token": "7.9999999999.deadbeef.cafe"})
        assert r.status_code == 401, r.status_code
        print("OK  guest token single-use + forged token rejected")

        # прямой deep-эндпоинт для сканеров, открывающих zezar:// мигом:
        # авторизованный кукой -> 302 в приложение с токеном
        r = await c.get(f"/open/e/{new_uid}/deep", headers=cookie_h, follow_redirects=False)
        assert r.status_code == 302, r.status_code
        loc = r.headers.get("location", "")
        assert loc.startswith(f"zezar://entity/{new_uid}?t="), loc
        # гость -> информативная страница (идентичный ответ для любых uid)
        r = await c.get(f"/open/e/no-such-uid-xyz/deep", follow_redirects=False)
        assert r.status_code == 200 and "отсканируйте" in r.text, (r.status_code, r.text[:200])
        # чужая карточка -> 404
        r = await c.get("/open/e/foreign-card-1/deep", headers=cookie_h, follow_redirects=False)
        assert r.status_code == 404, r.status_code
        print("OK  /open/e/{uid}/deep redirect + auth checks")

        # чужая карточка без доступа -> 404 (шлюз не канал разведки)
        r = await c.get("/open/e/foreign-card-1", headers=cookie_h)
        assert r.status_code == 404, (r.status_code, r.text[:200])
        print("OK  /open/e foreign entity -> 404")

        # malformed uid -> 404 без похода в БД
        r = await c.get("/open/e/bad~uid!@", headers=cookie_h)
        assert r.status_code == 404, r.status_code
        print("OK  /open/e malformed uid -> 404")

        r = await c.get(f"/api/core/entities/{new_uid}/share-link")
        assert r.status_code == 200, r.text
        share = r.json()
        assert share["share_url"].endswith(f"/open/e/{new_uid}"), share
        assert share["qr_data"] == share["share_url"], share
        assert share["deep_link"] == f"zezar://entity/{new_uid}", share
        print(f"OK  share-link -> {share['share_url']}")

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
