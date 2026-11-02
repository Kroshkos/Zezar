"""Целевая проверка интеграции с Яндекс.Календарём (без сети).

Проверяет исправленные дефекты:
  1) YANDEX_CAL_ENABLED=false — события calendar.* НЕ обрабатываются и attempts
     НЕ наращиваются (после включения флага досинхронизируются);
  2) diff-проход: PENDING-сущность с привязанным календарём ставится в очередь,
     без привязки — молча ждёт; дубли не создаются; троттлинг по cal_synced_at;
  3) мусорный due_date: _window/ics_for_entity -> None, upsert_event -> CalDavError,
     воркер помечает ERROR без сетевых попыток;
  4) совещание end_at<=start_at -> окно +2 часа;
  5) кэш путей WebDAV инвалидируется при переименовании календаря.

Запуск: PYTHONPATH=/workspace python scripts/cal_sync_test.py
"""
import asyncio
import os
import tempfile
from datetime import timedelta

_tmp = tempfile.mkdtemp(prefix="caesar_cal_")
os.environ["DATA_DIR"] = _tmp
os.environ["DATABASE_PATH"] = os.path.join(_tmp, "test.db")
os.environ["SECRET_KEY"] = "cal-test-secret"
os.environ["DEV_LOGIN_ENABLED"] = "true"
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD"] = "Cal-Test-Pass1"
os.environ["WORKERS_ENABLED"] = "false"
os.environ["YANDEX_CAL_ENABLED"] = "true"
os.environ["SYNC_INTERVAL_SECONDS"] = "300"

from app.main import app  # noqa: E402
from app.db.engine import SessionLocal  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.security import encrypt_secret  # noqa: E402
from app.models.core import (Entity, EntityKind, OutboxEvent, Role as _Role,
                       SyncState, User)  # noqa: E402
import integrations.yandex_calendar as yc  # noqa: E402
import integrations.workers as w  # noqa: E402


_seq = iter(range(1000, 9999))


def _mk_ent(dept_id: int, **kw) -> Entity:
    base = dict(uid=f"t{next(_seq)}", cal_uid=f"caesar-t{next(_seq)}@app",
                kind=EntityKind.TASK, module_key="tasks", title="Тест",
                status_key="new", sync_state=SyncState.PENDING,
                owner_department_id=dept_id)
    base.update(kw)
    return Entity(**base)


async def main() -> None:
    async with app.router.lifespan_context(app):
        # ---------- 1. флаг отключения: attempts не растёт ----------
        get_settings().yandex_cal_enabled = False
        assert not yc.cal_enabled(), "cal_enabled должен читать флаг"
        async with SessionLocal() as s:
            s.add(OutboxEvent(topic="calendar.sync",
                              payload_json={"entity_id": 999999, "reason": "test"}))
            await s.commit()
        await w._outbox_once()
        await w._outbox_once()
        async with SessionLocal() as s:
            from sqlalchemy import select
            ev = (await s.scalars(select(OutboxEvent).where(
                OutboxEvent.topic == "calendar.sync"))).one()
            assert ev.attempts == 0, f"attempts растёт при выключенном флаге: {ev.attempts}"
            assert ev.processed_at is None
        get_settings().yandex_cal_enabled = True
        print("OK 1: YANDEX_CAL_ENABLED respected, attempts не наращивается")

        # ---------- 2. diff-проход + _handle_calendar ----------
        w._cal_path_cache.clear()
        from sqlalchemy import select
        from app.models.core import Department, EntityResponsible
        async with SessionLocal() as s:
            dept = Department(code="cal-dept", name="Тестовое пространство")
            u = User(login="cal-user", display_name="Тест", role=_Role.MEMBER,
                     password_hash="x", yandex_login="caluser",
                     yandex_cal_app_password_enc=encrypt_secret("apw-fake"),
                     yandex_cal_name="Цезарь")
            s.add_all([dept, u])
            await s.flush()
            e_no = _mk_ent(dept.id, title="Без календаря", due_date="2026-11-20")  # нет привязки
            e_ok = _mk_ent(dept.id, title="С календарём", due_date="2026-11-21")
            s.add_all([e_no, e_ok])
            await s.flush()
            s.add(EntityResponsible(entity_id=e_ok.id, user_id=u.id,
                                    department_id=dept.id, is_primary=True))
            await s.commit()
            uid, ok_id, no_id, dept_id = u.id, e_ok.id, e_no.id, dept.id

        async def events_for(eid, pending_only=False):
            async with SessionLocal() as s:
                evs = (await s.scalars(select(OutboxEvent).where(
                    OutboxEvent.topic == "calendar.sync"))).all()
                out = [ev for ev in evs if (ev.payload_json or {}).get("entity_id") == eid]
                if pending_only:
                    out = [ev for ev in out if ev.processed_at is None]
                return out

        # 2.1 diff: привязанная PENDING-сущность -> событие; непривязанная — молчит
        await w._cal_diff_once()
        assert len(await events_for(ok_id)) == 1, "diff не поставил событие в очередь"
        assert not await events_for(no_id), "diff поставил событие для непривязанной сущности"
        # 2.2 повторный проход — без дубля (уже есть необработанное calendar.sync)
        await w._cal_diff_once()
        assert len(await events_for(ok_id, True)) == 1, "diff дублирует события"
        # 2.3 троттлинг: свежая cal_synced_at (< цикла) — не переочерёживаем
        async with SessionLocal() as s:
            ent = await s.get(Entity, ok_id)
            ent.cal_synced_at = w.msk_now()
            ent.sync_state = SyncState.ERROR
            for ev in (await s.scalars(select(OutboxEvent).where(
                    OutboxEvent.topic == "calendar.sync"))).all():
                ev.processed_at = w.msk_now()
            await s.commit()
        await w._cal_diff_once()
        assert not await events_for(ok_id, True), "троттлинг по cal_synced_at не работает"
        # 2.4 «старая» попытка (> цикла) — догоняющий ретрай
        async with SessionLocal() as s:
            ent = await s.get(Entity, ok_id)
            ent.cal_synced_at = w.msk_now() - timedelta(seconds=400)
            await s.commit()
        await w._cal_diff_once()
        assert len(await events_for(ok_id, True)) == 1, "догоняющий diff после интервала не работает"
        print("OK 2: diff-проход — очередь, без дублей, троттлинг, догоняющий ретрай")

        # 2.5 _handle_calendar: мусорный due_date -> ERROR+cal_error БЕЗ сетевых вызовов
        orig_upsert, orig_delete = yc.upsert_event, yc.delete_event
        net_calls = []

        async def spy(*a, **k):
            net_calls.append("net")
            return False
        yc.upsert_event = yc.delete_event = spy
        try:
            async with SessionLocal() as s:
                ent = await s.get(Entity, ok_id)
                ent.due_date = "не дата"
                ent.sync_state = SyncState.PENDING
                s.add(OutboxEvent(topic="calendar.sync",
                                  payload_json={"entity_id": ok_id, "reason": "test"}))
                await s.commit()
            await w._outbox_once()
            assert not net_calls, f"сетевой вызов при мусорном due_date: {net_calls}"
            async with SessionLocal() as s:
                ent = await s.get(Entity, ok_id)
                assert ent.sync_state == SyncState.ERROR and ent.cal_error, \
                    (ent.sync_state, ent.cal_error)
            # 2.6 нормальная дата + привязка: upsert вызван, sync_state=ERROR (spy->False),
            #     cal_synced_at проставлен (база троттлинга)
            async with SessionLocal() as s:
                ent = await s.get(Entity, ok_id)
                ent.due_date = "2026-11-21"
                ent.sync_state = SyncState.PENDING
                ent.cal_synced_at = None
                s.add(OutboxEvent(topic="calendar.sync",
                                  payload_json={"entity_id": ok_id, "reason": "test"}))
                await s.commit()
            await w._outbox_once()
            assert net_calls, "upsert не вызван для валидной сущности с привязкой"
            async with SessionLocal() as s:
                ent = await s.get(Entity, ok_id)
                assert ent.sync_state == SyncState.ERROR, ent.sync_state
                assert ent.cal_synced_at is not None, "cal_synced_at (база троттлинга) не проставлен"
            # 2.7 успех: sync_state=SYNCED
            async def ok_upsert(*a, **k):
                return True
            yc.upsert_event = ok_upsert
            async with SessionLocal() as s:
                ent = await s.get(Entity, ok_id)
                ent.sync_state = SyncState.PENDING
                s.add(OutboxEvent(topic="calendar.sync",
                                  payload_json={"entity_id": ok_id, "reason": "test"}))
                await s.commit()
            await w._outbox_once()
            async with SessionLocal() as s:
                ent = await s.get(Entity, ok_id)
                assert ent.sync_state == SyncState.SYNCED, ent.sync_state
        finally:
            yc.upsert_event, yc.delete_event = orig_upsert, orig_delete
        print("OK 2b: _handle_calendar — отклонение битой даты до сети, ERROR/SYNCED/cal_synced_at")

        # ---------- 3. валидация дат ----------
        bad = _mk_ent(dept_id, title="Bad date", due_date="не дата")
        assert yc._window(bad) is None, "_window должен возвращать None для мусора"
        assert yc.ics_for_entity(bad, "Тест") is None, "ics_for_entity -> None"
        orig_ups = yc.upsert_event
        try:
            await yc.upsert_event("login", encrypt_secret("x"), "http://localhost/", bad, "Тест")
            raise AssertionError("upsert_event обязан бросить CalDavError")
        except yc.CalDavError:
            pass
        finally:
            yc.upsert_event = orig_ups
        # пустой due_date тоже не должен бросать ValueError из _window
        empty = _mk_ent(dept_id, title="Empty", due_date="")
        assert yc._window(empty) is None
        print("OK 3: мусорные даты — None/CalDavError вместо падения воркера")

        # ---------- 4. совещание end_at <= start_at ----------
        class P:  # payload-заглушка
            def __init__(self, s_, e_): self.start_at, self.end_at = s_, e_
        m = _mk_ent(dept_id, title="Meeting", kind=EntityKind.MEETING, module_key="meetings",
                    due_date="2026-11-25")
        m._cal_payload = P("2026-11-25T15:00", "2026-11-25T15:00")
        s_, e_ = yc._window(m)
        assert s_ == "20261125T150000" and e_ == "20261125T170000", (s_, e_)
        m._cal_payload = P("2026-11-25T15:00", "2026-11-25T13:00")
        s_, e_ = yc._window(m)
        assert e_ == "20261125T170000", (s_, e_)
        ics = yc.ics_for_entity(m, "Тест")
        assert ics and "DTSTART;TZID=Europe/Moscow:20261125T150000" in ics
        print("OK 4: невалидное окно совещания корректируется на +2ч")

        # ---------- 5. кэш путей инвалидируется при переименовании ----------
        async with SessionLocal() as s:
            uu = await s.get(User, uid)
            name1 = uu.yandex_cal_name
        w._cal_path_cache[uid] = (name1, "/calendar/old/")
        # путь для того же имени — из кэша (без сети: заглушим httpx-вызов проверкой hit'а)
        async with SessionLocal() as s:
            uu = await s.get(User, uid)
            p_cached = await w._cal_path(uu)
            assert p_cached == "/calendar/old/", p_cached
            uu.yandex_cal_name = "Новое имя"
            await s.commit()
        called = {}

        async def fake_ensure(client, login, apw, cal_name):
            called["name"] = cal_name
            return f"/calendar/{cal_name}/"
        orig = yc.ensure_calendar
        yc.ensure_calendar = fake_ensure
        try:
            async with SessionLocal() as s:
                uu = await s.get(User, uid)
                p_new = await w._cal_path(uu)
            assert called.get("name") == "Новое имя", "переименованный календарь не пересоздан"
            assert p_new == "/calendar/Новое имя/", p_new
            assert w._cal_path_cache[uid] == ("Новое имя", p_new)
        finally:
            yc.ensure_calendar = orig
        print("OK 5: кэш WebDAV-путей keyed by имя календаря, сбрасывается при rename")

    print("\nCAL SYNC TESTS OK")


asyncio.run(main())
