"""Проверка диагностики ошибок аутентификации Яндекс.Календаря (без сети)."""
import os, tempfile
_tmp = tempfile.mkdtemp(prefix="caesar_auth_")
os.environ.setdefault("DATA_DIR", _tmp); os.environ["DATABASE_PATH"] = os.path.join(_tmp, "t.db")
os.environ["SECRET_KEY"] = "auth-test-secret"
from integrations import yandex_calendar as yc

# normalize_login
assert yc.normalize_login("ivan@yandex.ru") == "ivan"
assert yc.normalize_login("  Ivan.Petrov@mail.ru ") == "Ivan.Petrov"
assert yc.normalize_login("ivan") == "ivan"
assert yc.normalize_login(None) == ""
assert yc.normalize_login("") == ""

# app-password heuristics
assert yc.looks_like_account_password("") is True          # пусто
assert yc.looks_like_account_password("Qwerty123!") is True  # обычный пароль (длина != 12)
assert yc.looks_like_account_password("abc123def456") is False
assert yc.looks_like_account_password("abc123 def456\u00a0") is False  # пробелы/NBSP убраны → 12

# credential_hint
h = yc.credential_hint("ivan", "Qwerty123!", 401)
assert h and "не похож на app-пароль" in h, h
h = yc.credential_hint("", "abc123def456", 401)
assert h and "пустое" in h, h
h = yc.credential_hint("ivan@yandex.ru", "abc123def456", 401, expect_email=True)
assert h and "с доменной частью" in h, h
h = yc.credential_hint("ivan", "abc123def456", 403)
assert h and "НОВЫЙ пароль приложений" in h, h
assert yc.credential_hint("ivan", "abc123def456", 500) is None  # не auth-ошибка — без хинта

# verify_credentials respects disabled flag without network
os.environ["YANDEX_CAL_ENABLED"] = "false"
from app.core.config import get_settings
get_settings.cache_clear()
assert yc.cal_enabled() is False
import asyncio
ok, code = asyncio.run(yc.verify_credentials("ivan", "abc123def456"))
assert ok is False and code == 0

print("AUTH DIAGNOSTICS OK")

# --- Контракт роутера: ошибки привязки календаря — HTTP 200 + {ok:false, detail}.
# Раньше возвращались 400; фронтенд (integrations.js calForm) текст не показывал,
# и в консоли оставалось только «Failed to load resource: 400».
os.environ["YANDEX_CAL_ENABLED"] = "true"
os.environ["DEV_LOGIN_ENABLED"] = "true"
os.environ["ADMIN_USERNAME"] = "caladmin"
os.environ["ADMIN_PASSWORD"] = "Cal-Admin-Pass1"
get_settings.cache_clear()
assert yc.cal_enabled() is True

import httpx
from app.main import app as fastapi_app
from app.db.engine import SessionLocal
from app.models.core import User


async def _router_contract():
    # lifespan создаёт схему и дефолтного администратора
    async with fastapi_app.router.lifespan_context(fastapi_app):
        transport = httpx.ASGITransport(app=fastapi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                     follow_redirects=False) as c:
            r = await c.get("/auth/dev-login?login=caladmin")
            assert r.status_code in (200, 302, 303), (r.status_code, r.text)
            ck = {"Cookie": f"caesar_session={r.cookies['caesar_session']}"}
            async with SessionLocal() as s:
                u = (await s.execute(
                    __import__("sqlalchemy").select(User).limit(1))).scalar_one()
                uid, login = u.id, u.login

            # 1) пустой app-пароль -> 200 ok:false (было 400)
            r = await c.put("/api/profile/calendar", headers=ck,
                            json={"cal_login": "ivan", "app_password": "   "})
            assert r.status_code == 200, (r.status_code, r.text)
            j = r.json(); assert j.get("ok") is False and "пустое" in j["detail"], j

            async def fake_409(login_, apw):
                yc._set_last_error("calendars are out of sync, retry later")
                return False, 409

            # логин нормализуется и сохраняется в сессию ДО сетевой проверки —
            # фиксируем факт через spy на verify_credentials (commit при отказе
            # не делается, в БД значения не будет)
            seen_login = []

            async def spy_409(login_, apw):
                seen_login.append(login_)
                return await fake_409(login_, apw)
            orig = yc.verify_credentials
            yc.verify_credentials = spy_409
            try:
                r = await c.put("/api/profile/calendar", headers=ck,
                                json={"cal_login": "ivan@yandex.ru",
                                      "app_password": "abc123def456"})
                assert r.status_code == 200, (r.status_code, r.text)
                j = r.json()
                assert j.get("ok") is False and "HTTP 409" in j["detail"], j
                assert "out of sync" in j["detail"], j          # текст Яндекса донесён
                assert "попробуйте позже" not in j["detail"], j  # без ложного шаблона
                assert seen_login == ["ivan"], seen_login       # домен отсечён
            finally:
                yc.verify_credentials = orig

            # 3) 401 -> подсказка credential_hint про app-пароль/сервис «Другие»
            async def fake_401(login_, apw):
                return False, 401
            yc.verify_credentials = fake_401
            try:
                r = await c.put("/api/profile/calendar", headers=ck,
                                json={"cal_login": "ivan", "app_password": "Qwerty123!"})
                assert r.status_code == 200, (r.status_code, r.text)
                j = r.json()
                assert j.get("ok") is False and "не похож на app-пароль" in j["detail"], j
            finally:
                yc.verify_credentials = orig

            # 4) успех -> ok:true, пароль без пробелов (NBSP при копировании вычищен)
            async def fake_ok(login_, apw):
                assert apw == "abc123def456", apw   # пробелы вычищены до проверки
                return True, 207
            yc.verify_credentials = fake_ok
            try:
                r = await c.put("/api/profile/calendar", headers=ck,
                                json={"cal_login": "ivan",
                                      "app_password": "abc\u00a0123 def456"})
                assert r.status_code == 200, (r.status_code, r.text)
                j = r.json()
                assert j.get("ok") is True and j.get("calendar"), j
            finally:
                yc.verify_credentials = orig

import asyncio
asyncio.run(_router_contract())
print("ROUTER CONTRACT OK (200+ok:false instead of bare 400)")
