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
