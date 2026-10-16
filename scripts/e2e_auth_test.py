"""E2E: единый контур авторизации — логин/пароль + принудительная смена пароля + журнал событий."""
import asyncio, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

tmp = tempfile.mkdtemp()
os.environ["DATA_DIR"] = tmp
os.environ["DATABASE_PATH"] = os.path.join(tmp, "test.db")
os.environ["SECRET_KEY"] = "x" * 40
from app.core.security import hash_password
# Основной способ: логин и пароль задаются ОТДЕЛЬНЫМИ env-переменными,
# система сама хеширует пароль при старте.
os.environ["ADMIN_USERNAME"] = "chief"
os.environ["ADMIN_PASSWORD"] = "S3cret-Pass!"
os.environ["ADMIN_FULL_NAME"] = "Коршунов К. К."
# Резервный способ (несколько админов / хеши в env):
os.environ["ADMIN_LOGINS"] = f"second:{hash_password('another-pass')}:Второй Админ"

import httpx
import app.main as _main_mod


async def main():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_main_mod.app),
                                 base_url="http://t", follow_redirects=False) as c:
        # lifespan вручную
        from app.db.engine import engine, SessionLocal
        from app.db.base import Base
        import app.models.core, app.models.module_payloads
        from app.core.module_registry import registry as _reg
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        _reg.autodiscover('modules')
        from app.db.seed import bootstrap
        async with SessionLocal() as s:
            await _reg.sync_table(s)
            await bootstrap(s)

        # 1. верный логин/пароль -> 302 на / + cookie
        r = await c.post("/auth/login", data={"login": "chief", "password": "S3cret-Pass!"})
        assert r.status_code == 302 and r.headers["location"] == "/", r.text
        assert "caesar_session" in r.cookies, "нет session cookie"
        me = await c.get("/auth/me")
        d = me.json()
        assert d["role"] == "admin" and d["login"] == "chief", d
        print("OK 1: вход админа по логину/паролю -> ADMIN")

        # 2. admin API доступен, оба env-админа созданы
        users = await c.get("/api/core/admin/users")
        assert users.status_code == 200, users.text
        logins = {u.get("login") for u in users.json()}
        assert {"chief", "second"} <= logins, logins
        print("OK 2: оба админа из env созданы; /api/core/admin/users доступен")

        # 3. неверный пароль -> bad_credentials
        r = await c.post("/auth/login", data={"login": "chief", "password": "wrong"})
        assert r.status_code == 302 and "error=bad_credentials" in r.headers["location"], r.headers
        print("OK 3: неверный пароль отклонён")

        # 4. регистронезависимый логин
        r = await c.post("/auth/login", data={"login": "CHIEF", "password": "S3cret-Pass!"})
        assert r.status_code == 302 and r.headers["location"] == "/", r.headers
        print("OK 4: логин без учёта регистра")

        # 5. локального админа нельзя понизить/заблокировать через PATCH
        uid = next(u["id"] for u in users.json() if u["login"] == "chief")
        r = await c.patch(f"/api/core/admin/users/{uid}", json={"is_active": False})
        assert r.status_code == 409, (r.status_code, r.text)
        r = await c.patch(f"/api/core/admin/users/{uid}", json={"role": "member"})
        assert r.status_code == 409, (r.status_code, r.text)
        print("OK 5: защита env-админа от понижения/блокировки через API")

        # 6. идемпотентность: повторный bootstrap не плодит записи
        import sqlalchemy as sa
        async with SessionLocal() as s:
            await bootstrap(s)
            n = len((await s.execute(sa.select(app.models.core.User))).scalars().all())
        assert n == 2, n
        print("OK 6: повторный bootstrap идемпотентен")

        # 7. создание пользователя админом + принудительная смена пароля при первом входе
        r = await c.post("/api/core/admin/users", json={
            "display_name": "Петров П.П.", "login": "petrov",
            "initial_password": "Start-Pass1", "role": "member"})
        assert r.status_code == 200, r.text
        new_uid = r.json()["id"]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_main_mod.app),
                                     base_url="http://t", follow_redirects=False) as c2:
            r = await c2.post("/auth/login", data={"login": "petrov", "password": "Start-Pass1"})
            assert r.status_code == 302 and "change-password?forced=1" in r.headers["location"], r.headers
            # прямой заход на / всё равно редиректит на смену пароля
            # (cookie из предыдущего ответа может не перенестись — берём явно)
            c2.cookies.set("caesar_session", r.cookies.get("caesar_session", ""))
            r = await c2.get("/")
            assert r.status_code == 302 and "change-password" in r.headers["location"], r.headers
            # слишком короткий пароль отклоняется
            r = await c2.post("/auth/change-password", data={
                "current_password": "Start-Pass1", "new_password": "short", "repeat_password": "short"})
            assert "error=too_short" in r.headers["location"], r.headers
            # нормальная смена
            r = await c2.post("/auth/change-password", data={
                "current_password": "Start-Pass1", "new_password": "New-Pass-2026",
                "repeat_password": "New-Pass-2026"})
            assert r.status_code == 302 and r.headers["location"] == "/", r.headers
            r = await c2.get("/")
            assert r.status_code == 200 or r.status_code == 302, r.status_code
            # старый пароль больше не работает (новая клиентка — без сессии,
            # иначе /auth/login у незалогиненного редиректит на /login)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_main_mod.app),
                                         base_url="http://t", follow_redirects=False) as c2b:
                r = await c2b.post("/auth/login", data={"login": "petrov", "password": "Start-Pass1"})
                assert "error=bad_credentials" in r.headers["location"], r.headers
        print("OK 7: пользователь создан, первый вход -> принудительная смена пароля, стартый пароль отключён")

        # 8. сброс пароля админом -> снова must_change_password
        r = await c.post(f"/api/core/admin/users/{new_uid}/reset-password",
                         json={"new_password": "Reset-Pass-9"})
        assert r.status_code == 200, r.text
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_main_mod.app),
                                     base_url="http://t", follow_redirects=False) as c3:
            r = await c3.post("/auth/login", data={"login": "petrov", "password": "Reset-Pass-9"})
            assert "change-password?forced=1" in r.headers["location"], r.headers
        print("OK 8: сброс пароля администратором -> принудительная смена при входе")

        # 9. журнал событий: всё ключевое зарегистрировано
        r = await c.get("/api/core/admin/events?limit=500")
        assert r.status_code == 200, r.text
        ev = r.json()
        actions = {e["action"] for e in ev["items"]}
        for a in ("login", "login_failed", "create_user", "password_changed",
                  "password_change_required", "reset_password"):
            assert a in actions, (a, sorted(actions))
        stats = await c.get("/api/core/admin/events/stats")
        assert stats.status_code == 200 and stats.json(), stats.text
        print(f"OK 9: журнал событий — {ev['total']} записей, действия: {sorted(actions)[:8]}...")

        # 10. страница входа — только форма логин/пароль (без Яндекса)
        r = await c.get("/login")
        assert "/auth/login" in r.text and "yandex" not in r.text.lower()
        print("OK 10: /login — единая форма логин+пароль, Яндекс отсутствует")

        # 11. HTML-страницы админки
        for path in ("/admin", "/admin/events", "/change-password"):
            r = await c.get(path)
            assert r.status_code == 200, (path, r.status_code)
        print("OK 11: /admin, /admin/events, /change-password доступны")
    await engine.dispose()
    print("\nALL E2E AUTH CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
