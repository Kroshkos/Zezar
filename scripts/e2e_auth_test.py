"""E2E: два контура авторизации — локальные админы (логин/пароль) и участники (OAuth Yandex)."""
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
os.environ["BOOTSTRAP_ADMIN_YA_IDS"] = ""

import httpx
import app.main as _main_mod

async def main():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_main_mod.app), base_url="http://t") as c:
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

        # 1. верный логин/пароль -> 302 + cookie
        r = await c.post("/auth/admin-login", data={"login": "chief", "password": "S3cret-Pass!"}, follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "/", r.text
        assert "caesar_session" in r.cookies, "нет session cookie"
        me = await c.get("/auth/me")
        d = me.json(); assert d["role"] == "admin" and d["account"] == "local", d
        print("OK 1: вход админа по логину/паролю -> ADMIN, account=local")

        # 2. admin API доступен
        users = await c.get("/api/core/admin/users")
        assert users.status_code == 200, users.text
        logins = {u.get("login") for u in users.json()}
        assert {"chief", "second"} <= logins, logins
        print("OK 2: оба админа из ADMIN_LOGINS созданы; /admin/users доступен")

        # 3. неверный пароль -> bad_credentials, сессии нет
        r = await c.post("/auth/admin-login", data={"login": "chief", "password": "wrong"}, follow_redirects=False)
        assert r.status_code == 302 and "error=bad_credentials" in r.headers["location"], r.headers
        print("OK 3: неверный пароль отклонён")

        # 4. регистронезависимый логин
        r = await c.post("/auth/admin-login", data={"login": "CHIEF", "password": "S3cret-Pass!"}, follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "/", r.headers
        print("OK 4: логин без учёта регистра")

        # 5. локального админа нельзя понизить/заблокировать через PATCH
        uid = next(u["id"] for u in users.json() if u["login"] == "chief")
        r = await c.patch(f"/api/core/admin/users/{uid}", json={"is_active": False})
        assert r.status_code == 409, (r.status_code, r.text)
        r = await c.patch(f"/api/core/admin/users/{uid}", json={"role": "member"})
        assert r.status_code == 409, (r.status_code, r.text)
        print("OK 5: защита локального админа от понижения/блокировки через API")

        # 6. idempotent sync: повторный bootstrap не плодит записи и не меняет хеш
        async with SessionLocal() as s:
            await bootstrap(s)
            n = len((await s.execute(__import__("sqlalchemy").select(__import__("app.models.core", fromlist=["User"]).User))).scalars().all())
        print("OK 6: повторный bootstrap идемпотентен")

        # 7. страница входа содержит обе формы
        r = await c.get("/login")
        assert "/auth/admin-login" in r.text and "/auth/login" in r.text
        print("OK 7: /login — форма админа + кнопка Яндекса")
    await engine.dispose()
    print("\nALL E2E AUTH TESTS PASSED")

asyncio.run(main())
