"""Интеграция: единая админ-панель /admin_dept/ (вкладки) для админа и руководителя."""
import os
import pathlib
import tempfile

tmp = tempfile.mkdtemp()
os.environ["DATA_DIR"] = tmp
os.environ["DATABASE_PATH"] = str(pathlib.Path(tmp) / "test.db")
os.environ["ENVIRONMENT"] = "development"
os.environ["SECRET_KEY"] = "x" * 40
os.environ["ADMIN_USERNAME"] = "rootadmin"
os.environ["ADMIN_PASSWORD"] = "RootPass123"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def login(c, login_, password):
    r = c.post("/auth/login", data={"login": login_, "password": password})
    assert r.status_code == 200, (r.status_code, r.text[:300])
    if "/change-password" in str(r.url) or "Смена пароля" in r.text and "Первый вход" in r.text:
        r = c.post("/auth/change-password", data={
            "current_password": password, "new_password": password + "x",
            "repeat_password": password + "x"})
    return r


def is_logged_in(c):
    return "Дашборд" in c.get("/").text or c.get("/admin_dept/", follow_redirects=True).status_code == 200


with TestClient(app) as c:
    # ---- админ -------------------------------------------------------------
    login(c, "rootadmin", "RootPass123")
    r = c.get("/admin_dept/")
    assert r.status_code == 200, r.status_code
    assert "Администрирование" in r.text
    assert 'pane-departments' in r.text, "у админа должна быть вкладка Подразделения"
    assert "Новый пользователь" in r.text or "openCreateUser" in r.text

    # создать подразделение через UI-API
    d1 = c.post("/api/core/departments", json={"name": "Отдел разработки", "code": "dev"})
    assert d1.status_code == 200, d1.text
    dept_id = d1.json()["id"]
    d2 = c.post("/api/core/departments", json={"name": "Отдел аналитики", "code": "ana"})
    assert d2.status_code == 200, d2.text

    r = c.get(f"/admin_dept/?department_id={dept_id}")
    assert r.status_code == 200
    assert "Отдел разработки" in r.text and "Отдел аналитики" in r.text  # переключатель
    assert 'data-tab="events"' in r.text

    # создать пользователя
    u = c.post("/api/core/admin/users", json={
        "display_name": "Петров Пётр", "login": "petrov",
        "initial_password": "Start12345", "role": "member",
        "department_id": dept_id})
    assert u.status_code == 200, u.text
    petrov_id = u.json()["id"]

    # список пользователей + поиск
    lst = c.get("/api/core/admin/users?q=петров".replace("петров", "petrov"))
    assert lst.status_code == 200 and len(lst.json()) >= 1
    assert lst.json()[0]["departments"], "должна быть карта подразделений"

    # журнал подразделения
    ev = c.get(f"/api/core/admin/dept-events?department_id={dept_id}")
    assert ev.status_code == 200, ev.text

    # PATCH подразделения админом — любые поля
    p = c.patch(f"/api/core/departments/{dept_id}", json={"description": "тест"}); assert p.status_code == 200, p.text

    # ---- руководитель ------------------------------------------------------
    # назначаем petrov руководителем dev
    m = c.post(f"/api/core/departments/{dept_id}/members",
               json={"user_id": petrov_id, "role_in_dept": "HEAD"})
    assert m.status_code == 200, m.text
    c.post("/auth/logout")

    # вход петрова + принудительная смена пароля
    login(c, "petrov", "Start12345")
    r = c.get("/")  # редирект на смену пароля
    r = c.post("/auth/change-password", data={
        "current_password": "Start12345", "new_password": "MyNewPass9",
        "repeat_password": "MyNewPass9"})
    assert r.status_code == 200, r.text[:300]

    page = c.get("/admin_dept/")
    assert page.status_code == 200, page.status_code
    assert 'pane-departments' not in page.text, "руководителю нельзя вкладку «Подразделения»"
    assert "Учётные записи" in page.text
    assert "openCreateUser()" not in page.text, "кнопка создания пользователя только у админа"

    # users scoped: видит только своих
    lst = c.get("/api/core/admin/users")
    assert lst.status_code == 200
    names = [x["login"] for x in lst.json()]
    assert "petrov" in names and "rootadmin" not in names, names

    # candidates доступны
    cand = c.get(f"/api/core/departments/{dept_id}/candidates?q=root")
    assert cand.status_code == 200, cand.text

    # events scoped
    ev = c.get(f"/api/core/admin/dept-events?department_id={dept_id}")
    assert ev.status_code == 200

    # полный журнал — запрещено
    forbidden = c.get("/api/core/admin/events")
    assert forbidden.status_code == 403, forbidden.text

    # PATCH чужого поля подразделения — запрещено; rename своего — можно
    bad = c.patch(f"/api/core/departments/{dept_id}", json={"is_active": False})
    assert bad.status_code == 403, bad.text
    ok = c.patch(f"/api/core/departments/{dept_id}", json={"name": "Разработка v2"})
    assert ok.status_code == 200, ok.text

    # создание подразделения руководителем — запрещено
    nd = c.post("/api/core/departments", json={"name": "Хочу своё", "code": "mine"})
    assert nd.status_code == 403, nd.text

    print("ALL ADMIN PANEL TESTS PASSED")
