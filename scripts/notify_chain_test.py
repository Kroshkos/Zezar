"""Сквозной тест цепочки уведомлений (без реального Telegram).

Проверяет полный путь: создание задачи -> события в OutboxEvent (assigned +
created) -> обработка воркером _outbox_once с подменой tg.send_message на мок
-> статусы событий 'done', мок получил корректный текст со ссылкой системы.

Запуск: python scripts/notify_chain_test.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + os.path.join(
    tempfile.mkdtemp(), "notify_test.db")
os.environ["WORKERS_ENABLED"] = "false"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["DEV_LOGIN_ENABLED"] = "true"
os.environ["ENVIRONMENT"] = "development"
os.environ["TELEGRAM_NOTIFY_ENABLED"] = "true"
os.environ["PUBLIC_BASE_URL"] = "https://zezar-patfiz.waw0.amvera.tech"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def main() -> int:
    from sqlalchemy import select

    import integrations.telegram_service as tg
    from app.core import entity_service
    from app.core.notify import seed_base_scenarios
    from app.db.engine import SessionLocal
    from app.db.seed import bootstrap
    from app.models.core import EntityKind, OutboxEvent, Permission, User
    from integrations.workers import _outbox_once

    sent: list[tuple[int, str]] = []

    async def fake_send(chat_id: int, text: str) -> bool:
        sent.append((int(chat_id), text))
        return True

    tg.send_message = fake_send  # мок вместо реальной сети

    # --- инициализация БД (как в lifespan) ---
    import app.main as m
    m._import_all_models()
    from app.models.core import Base
    from app.db.engine import engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with SessionLocal() as s:
        await bootstrap(s)
        await seed_base_scenarios(s)

    async with SessionLocal() as s:
        users = list((await s.scalars(select(User))).all())
        by_login = {u.login: u for u in users}
        actor = by_login.get("ivanov") or next(u for u in users if u.is_active)
        target = next((u for u in users if u.id != actor.id and u.is_active), None)
        assert actor is not None, "нет пользователя-актора (включите SEED_DEMO_DATA)"
        assert target is not None, "нет второго пользователя"
        # привязываем «Telegram» фиктивными chat_id
        actor.telegram_chat_id = 111
        target.telegram_chat_id = 222
        from app.models.core import DepartmentMember
        dept_id = await s.scalar(
            select(DepartmentMember.department_id).where(
                DepartmentMember.user_id == actor.id))
        if dept_id is None:
            from app.models.core import Department, DepartmentMember as _DM
            d = Department(name="Тестовое подразделение", code="dep-test")
            s.add(d)
            await s.flush()
            dept_id = d.id
            s.add(_DM(user_id=actor.id, department_id=dept_id))
        if not await s.scalar(select(DepartmentMember.id).where(
                DepartmentMember.user_id == target.id)):
            s.add(DepartmentMember(user_id=target.id, department_id=dept_id))
        await s.commit()

        # 1) создание задачи с назначением ответственного + исполнителя
        ent = await entity_service.create_entity(
            s, actor, kind=EntityKind.TASK, module_key="tasks",
            owner_department_id=dept_id, title="Тестовая задача уведомлений",
            due_date="2026-10-20", responsible_user_id=target.id,
            executor_user_ids=[target.id])
        await s.commit()

        events = list((await s.scalars(
            select(OutboxEvent).where(OutboxEvent.topic == "telegram.notify"))).all())
        assert events, "после создания задачи нет ни одного telegram.notify-события"
        print(f"[1] create: событий в outbox: {len(events)}")

        # 2) смена статуса
        ent_db = await s.get(type(ent), ent.id)
        await entity_service.update_entity(s, actor, ent_db, {"status_key": "in_progress"})
        await s.commit()

        # 3) назначение дополнительного исполнителя через set_executors
        third = next((u for u in users if u.id not in (actor.id, target.id)), None)
        if third:
            third.telegram_chat_id = 333
            if not await s.scalar(select(DepartmentMember.id).where(
                    DepartmentMember.user_id == third.id)):
                s.add(DepartmentMember(user_id=third.id, department_id=dept_id))
            await s.commit()
            await entity_service.set_executors(s, actor, ent_db,
                                               [target.id, third.id],
                                               actor_user_id=actor.id)
            await s.commit()
            print("[3] set_executors: добавлен исполнитель, событие assigned поставлено")

        # 4) воркер обрабатывает всю очередь
        n_before = len(events)
        await _outbox_once()
        async with SessionLocal() as s2:
            rows = list((await s2.scalars(
                select(OutboxEvent).where(
                    OutboxEvent.topic.in_(["telegram.notify"])))).all())
        errors = [r for r in rows if r.status == "error"]
        dones = [r for r in rows if r.status == "done"]
        print(f"[4] worker: done={len(dones)} error={len(errors)} всего={len(rows)}")
        for r in errors:
            print(f"    ERROR event #{r.id}: {r.last_error}")

        assert sent, "мок send_message не вызывался — уведомления не доставляются!"
        base = "https://zezar-patfiz.waw0.amvera.tech"
        texts = "\n---\n".join(f"chat={c}:\n{t}" for c, t in sent)
        print(f"[5] доставлено сообщений: {len(sent)}")
        print(texts[:1200])
        assert any("Тестовая задача" in t for _, t in sent), "в тексте нет наименования сущности"
        assert any(base in t for _, t in sent), "в тексте нет ссылки на систему"
        chats = {c for c, _ in sent}
        assert 222 in chats, "назначенный ответственный/исполнитель не получил уведомление"
        assert actor.telegram_chat_id not in chats or True  # автор может получать status от себя? нет: исключаем
        assert all(actor.telegram_chat_id != c for c in chats), \
            "автор действия получил собственное уведомление (спам)"
        assert not errors, f"есть необработанные события с ошибкой: {[e.last_error for e in errors]}"

        # 5) напоминание о сроке
        from app.core.notify import schedule_due_reminders
        async with SessionLocal() as s3:
            n = await schedule_due_reminders(s3, days_ahead=10)
        print(f"[6] due reminders поставлено: {n}")
        await _outbox_once()
        async with SessionLocal() as s4:
            due_rows = list((await s4.scalars(select(OutboxEvent).where(
                OutboxEvent.topic == "telegram.due"))).all())
        assert due_rows and all(r.status == "done" for r in due_rows), \
            "напоминания о сроке не обработаны"
        assert any("Приближается срок" in t for _, t in sent), "нет текста напоминания"
        print("\n✅ ЦЕПОЧКА УВЕДОМЛЕНИЙ РАБОТАЕТ: создание → outbox → воркер → доставка")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
