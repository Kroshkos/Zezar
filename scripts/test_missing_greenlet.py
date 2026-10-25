"""Репро/регрессия MissingGreenlet на /api/mobile/events (long-poll + rollback).

Сценарий из прод-трейса 25.10.26 18:41:
  1) get_current_user_from_token возвращает ORM-объект User из сессии запроса;
  2) list_events в цикле ожидания long-poll делает session.rollback() —
     откат снимает snapshot и инвалидирует ВСЕ объекты identity map;
  3) следующее построение условия `MobileEvent.user_id == user.id` обращается
     к истёкшему атрибуту -> ленивая перезагрузка вне greenlet-контекста ->
     sqlalchemy.exc.MissingGreenlet -> 500.

Запуск:  python scripts/test_missing_greenlet.py   (rc=0 — фикс работает)
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
from app.models.core import ApiToken, User
from app.models.mobile import MobileEvent

TOK = "tok123"
TOK_HASH = hashlib.sha256(TOK.encode()).hexdigest()


async def seed(Session) -> int:
    async with Session() as s:
        u = User(email="t@t.t", display_name="T", login="t",
                 password_hash="x", role="admin", is_active=True)
        s.add(u)
        await s.flush()
        s.add(ApiToken(user_id=u.id, token_hash=TOK_HASH, name="dev", is_active=True))
        for i in range(3):
            s.add(MobileEvent(user_id=u.id, type="entity_updated", title=f"e{i}"))
        uid = u.id
        await s.commit()
    return uid


async def old_auth_dep(session: AsyncSession) -> User:
    """Прежняя версия get_current_user_from_token (до фикса)."""
    api_token = (await session.execute(
        select(ApiToken).where(ApiToken.token_hash == TOK_HASH,
                               ApiToken.is_active == True))).scalar_one()  # noqa: E712
    from datetime import datetime, timezone
    api_token.last_used_at = datetime.now(timezone.utc)  # dirty-state в живой транзакции
    return (await session.execute(
        select(User).where(User.id == api_token.user_id))).scalar_one()


async def events_endpoint(Session, auth_dep, first_query: bool):
    """Копия цикла long-poll из list_events.

    first_query=True — точная копия прод-кода (первый запрос ДО обращения к
    user.id держит snapshot открытым, и rollback инвалидирует объект User).
    """
    async with Session() as session:
        user = await auth_dep(session)
        q = lambda: (select(MobileEvent)
                     .where(MobileEvent.user_id == user.id, MobileEvent.id > 0)
                     .order_by(MobileEvent.id.desc()).limit(50))
        if first_query:
            _ = (await session.scalars(q())).all()   # открывает snapshot
        await session.rollback()                     # как в while-цикле long-poll
        rows = (await session.scalars(q())).all()    # здесь падало на user.id
        return len(rows)


async def main() -> int:
    url = "sqlite+aiosqlite:///" + tempfile.mktemp(suffix=".db")
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    uid = await seed(Session)

    rc = 0
    # 1) старый код + точный прод-сценарий обязан воспроизводить баг
    try:
        await events_endpoint(Session, old_auth_dep, first_query=True)
        print("[repro] OLD code did NOT raise — воспроизведение не удалось")
        rc = 1
    except Exception as e:
        if "MissingGreenlet" in type(e).__name__ or "greenlet_spawn" in str(e):
            print(f"[repro] OLD code raises {type(e).__name__}: баг подтверждён ✔")
        else:
            print(f"[repro] OLD code raised unexpected {type(e).__name__}: {e}")
            rc = 1

    # 2) реальный фикс: get_current_user_from_token (после правки) + тот же endpoint
    from app.routers.token_auth import get_current_user_from_token
    from fastapi.security.http import HTTPAuthorizationCredentials

    async def fixed_auth_dep(session: AsyncSession) -> User:
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=TOK)
        return await get_current_user_from_token(credentials=creds, session=session)

    try:
        n = await events_endpoint(Session, fixed_auth_dep, first_query=True)
        assert n == 3, f"expected 3 events, got {n}"
        print(f"[fix] FIXED auth dep OK через точную копию прод-сценария, events={n} ✔")
    except Exception as e:
        print(f"[fix] FAILED: {type(e).__name__}: {e}")
        rc = 1

    # 3) last_used_at реально обновился в БД
    async with Session() as s2:
        t = (await s2.execute(select(ApiToken))).scalar_one()
        assert t.last_used_at is not None, "last_used_at не обновился"
        print("[integration] last_used_at updated in DB: OK ✔")

    # 4) другие мобильные эндпоинты не сломаны: user доступен после commit
    async with Session() as s3:
        user = await fixed_auth_dep(s3)
        assert user.id == uid and user.display_name == "T"
        # эндпоинты пишут в ту же сессию — коммитить можно без ошибок
        ev = MobileEvent(user_id=user.id, type="test", title="t")
        s3.add(ev)
        await s3.commit()
        print("[integration] write-path after auth-dep commit: OK ✔")

    await engine.dispose()
    print("RESULT:", "PASS" if rc == 0 else "FAIL")
    return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
