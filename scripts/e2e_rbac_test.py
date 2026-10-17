"""E2E: RBAC-инварианты и инвалидация кэша прав.

Проверяет:
  1. Инвариант «хотя бы один HEAD»: понижение последнего руководителя в
     пространстве запрещено (HTTP 409); удаление последнего HEAD запрещено;
     после назначения второго руководителя операции разрешены.
  2. Унификация назначения ролей: set_department_role синхронизирует
     глобальную роль users.role с role_in_dept (повышение -> head,
     понижение последней руководимой области -> member).
  3. Инвалидация кэша прав: любая мутация прав инкрементирует acl_epoch в
     таблице acl_state; cookie, выпущенный под старой эпохой, помечается
     token_needs_refresh() как устаревший; cookie с новой эпохой — свежий.

Запуск: python scripts/e2e_rbac_test.py
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

tmp = tempfile.mkdtemp()
os.environ["DATA_DIR"] = tmp
os.environ["DATABASE_PATH"] = os.path.join(tmp, "test.db")
os.environ["SECRET_KEY"] = "x" * 40
os.environ["ADMIN_USERNAME"] = "chief"
os.environ["ADMIN_PASSWORD"] = "S3cret-Pass!"

import httpx  # noqa: E402
import app.main as _main_mod  # noqa: E402


def _tok_epoch(cookie_val: str) -> int:
    from app.core.security import read_session_token
    data = read_session_token(cookie_val)
    assert data is not None, "подписанный токен не прошёл HMAC-проверку"
    return int(data.get("acl_epoch", 0) or 0)


async def main():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_main_mod.app),
                                 base_url="http://t", follow_redirects=False) as c:
        from sqlalchemy import select

        from app.core.permcache import get_epoch, token_needs_refresh
        from app.db.base import Base
        from app.db.engine import SessionLocal, engine
        from app.db.seed import bootstrap
        from app.models.core import DepartmentMember, Role, User
        import app.models.core  # noqa: F401
        import app.models.module_payloads  # noqa: F401

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        from app.core.module_registry import registry as _reg
        _reg.autodiscover('modules')
        async with SessionLocal() as s:
            await _reg.sync_table(s)
            await bootstrap(s)

        # вход админа: cookie содержит acl_epoch
        r = await c.post("/auth/login", data={"login": "chief", "password": "S3cret-Pass!"})
        assert r.status_code == 302, r.text
        ck = r.cookies.get("caesar_session")
        assert ck, "нет session cookie"
        epoch0 = _tok_epoch(ck)
        print(f"OK 0: вход выполнен, cookie acl_epoch={epoch0}")

        # создаём подразделение + двух пользователей
        r = await c.post("/api/core/departments",
                         json={"code": "rbac-test", "name": "RBAC-тест"})
        assert r.status_code == 200, r.text
        dept_id = r.json()["id"]
        uids = {}
        for login in ("ann", "bob"):
            r = await c.post("/api/core/admin/users",
                             json={"login": login, "display_name": f"Тест {login}",
                                   "email": f"{login}@t.local",
                                   "role": "member",
                                   "initial_password": "Init-Pass1!"})
            assert r.status_code == 200, r.text
            uids[login] = r.json()["id"]
        print("OK 1: подразделение и пользователи созданы")

        # назначаем ann единственным HEAD
        r = await c.post(f"/api/core/departments/{dept_id}/members",
                         json={"user_id": uids["ann"], "role_in_dept": "HEAD"})
        assert r.status_code == 200, r.text
        # bump_epoch внутри запроса меняет только копию модуля permcache из sys.modules;
        # test-процесс импортирует его отдельно — синхронизируем mirror вручную
        from app.core.permcache import set_local_epoch
        async with SessionLocal() as s:
            set_local_epoch(await get_epoch(s))
        async with SessionLocal() as s:
            epoch1 = await get_epoch(s)
            assert epoch1 > epoch0, f"epoch не вырос после выдачи HEAD: {epoch0}->{epoch1}"
            ann = await s.get(User, uids["ann"])
            assert ann.role == Role.HEAD, f"глобальная роль не синхронизирована: {ann.role}"
        # старый cookie (эпоха epoch0) теперь «протухший», новый — нет
        stale = {"uid": uids["ann"], "acl_epoch": epoch0}
        fresh = {"uid": uids["ann"], "acl_epoch": epoch1}
        assert token_needs_refresh(stale, epoch1), "старый epoch не помечен устаревшим"
        assert not token_needs_refresh(fresh, epoch1), "актуальный epoch помечен устаревшим"
        print(f"OK 2: выдача HEAD -> bump_epoch ({epoch0}->{epoch1}), "
              "глобальная роль=head, токен со старым epoch признан устаревшим")

        # понижение последнего HEAD запрещено (409)
        r = await c.post(f"/api/core/departments/{dept_id}/members",
                         json={"user_id": uids["ann"], "role_in_dept": "MEMBER"})
        assert r.status_code == 409, f"ожидался 409, получен {r.status_code}: {r.text}"
        print("OK 3: понижение последнего HEAD запрещено (409)")

        # удаление последнего HEAD тоже запрещено (409)
        r = await c.delete(f"/api/core/departments/{dept_id}/members/{uids['ann']}")
        assert r.status_code == 409, f"ожидался 409, получен {r.status_code}: {r.text}"
        print("OK 4: удаление последнего HEAD запрещено (409)")

        # назначаем второго HEAD — теперь понижение/удаление ann разрешены
        r = await c.post(f"/api/core/departments/{dept_id}/members",
                         json={"user_id": uids["bob"], "role_in_dept": "HEAD"})
        assert r.status_code == 200, r.text
        async with SessionLocal() as s:
            set_local_epoch(await get_epoch(s))
        r = await c.post(f"/api/core/departments/{dept_id}/members",
                         json={"user_id": uids["ann"], "role_in_dept": "MEMBER"})
        assert r.status_code == 200, r.text
        async with SessionLocal() as s:
            set_local_epoch(await get_epoch(s))
        async with SessionLocal() as s:
            ann = await s.get(User, uids["ann"])
            assert ann.role == Role.MEMBER, \
                f"глобальная роль не снята после понижения последней HEAD-области: {ann.role}"
            bob = await s.get(User, uids["bob"])
            assert bob.role == Role.HEAD
        print("OK 5: второй HEAD назначен, понижение разрешено, "
              "глобальная роль синхронизирована вниз (head->member)")

        # удаление ann (уже не HEAD) проходит
        r = await c.delete(f"/api/core/departments/{dept_id}/members/{uids['ann']}")
        assert r.status_code == 200, r.text
        async with SessionLocal() as s:
            cnt = await s.scalar(select(DepartmentMember).where(
                DepartmentMember.user_id == uids["ann"],
                DepartmentMember.department_id == dept_id))
            assert cnt is None, "членство не удалено"
        print("OK 6: удаление НЕ-head участника разрешено")

    print("\nALL RBAC CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
