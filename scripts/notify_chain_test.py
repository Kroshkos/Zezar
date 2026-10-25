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
    from sqlalchemy import func, select

    import integrations.telegram_service as tg
    from app.core import entity_service
    from app.core.notify import seed_base_scenarios
    from app.db.engine import SessionLocal
    from app.db.seed import bootstrap
    from app.models.core import (Entity, EntityKind, EntityResponsible,
                                 OutboxEvent, Permission, User)
    from integrations.workers import _outbox_once

    sent: list[tuple[int, str]] = []

    async def fake_send(chat_id: int, text: str) -> bool:
        sent.append((int(chat_id), text))
        return True

    tg.send_message = fake_send  # мок вместо реальной сети
    tg.send_media = fake_send    # медиа тоже в мок (иначе реальная сеть/токен)

    async def _mkdue(session, day_offset: int, title: str, owner, dept_id: int):
        """Создать задачу с due_date = today+day_offset и ответственным owner.

        Дедлайн считается от фактической даты прогона (в демо-сиде сроки
        захардкожены на конкретные даты и «протухают» — напоминания о сроке
        перестают попадать в окно [today, today+N]).
        """
        from datetime import datetime, timedelta, timezone
        from app.core import entity_service as es
        due = (datetime.now(timezone.utc).date() + timedelta(days=day_offset)).isoformat()
        return await es.create_entity(
            session, owner, kind=EntityKind.TASK, module_key="tasks",
            owner_department_id=dept_id, title=title, due_date=due,
            responsible_user_id=owner.id)

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

    # Чистим outbox ДО создания тестовых событий. Демо-сид сам ставит в
    # очередь telegram.notify-события (см. app/db/seed.py); нижняя «пред-чистка»
    # не может отличить их от наших — она помечает done ВСЕ необработанные
    # события, включая только что созданные тестом, и мок никогда не вызывается.
    from app.models.core import OutboxEvent as _OE
    async with SessionLocal() as s:
        from sqlalchemy import delete as _del
        await s.execute(_del(_OE))
        await s.commit()

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

        # 4) воркер обрабатывает всю очередь.
        #    Пред-чистка: из демо-сида в outbox могли остаться непривязанные к
        #    Telegram пользователи и карточки без ответственных — их события
        #    штатно завершаются «в никуда», но для чистоты проверок [4]/[5]
        #    отвязываем чужие chat_id и помечаем чужие события обработанными,
        #    чтобы в очереди остались только события нашего теста.
        #    ВАЖНО: фильтруем по entity_uid наших сущностей (ent + задачи из
        #    сида не участвуют). Раньше здесь стоял flat-список id участников,
        #    который НЕ учитывал адресные события с target_user_ids (assigned /
        #    status_changed ставятся именно по target'ам) — пред-чистка помечала
        #    done события самого теста до прогона воркера, и мок никогда не
        #    вызывался («уведомления не доставляются»).
        foreign = [u for u in users if u.id not in (actor.id, target.id)]
        for u in foreign:
            u.telegram_chat_id = None
        if third is not None:
            third.telegram_chat_id = 333  # восстановим привязку нашего третьего
        ours_uids = {ent.uid}
        test_uids = {actor.id, target.id} | ({third.id} if third else set())

        def _touches_test(p: dict) -> bool:
            if p.get("entity_uid") in ours_uids:
                return True
            involved = {p.get("actor_user_id")} | set(p.get("target_user_ids") or [])
            return bool(involved & test_uids)

        pre_ids = [r.id for r in (await s.scalars(
            select(OutboxEvent).where(
                OutboxEvent.topic.in_(["telegram.notify", "telegram.due",
                                        "telegram.overdue"]),
                OutboxEvent.processed_at.is_(None)))).all()
            if not _touches_test(r.payload_json or {})]
        if pre_ids:
            from datetime import datetime as _dt, timezone as _tz
            from sqlalchemy import update as _upd
            await s.execute(_upd(OutboxEvent).where(
                OutboxEvent.id.in_(pre_ids)).values(
                status="done", processed_at=_dt.now(_tz.utc)))
        await s.commit()

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
        assert all(actor.telegram_chat_id != c for c in chats), \
            "автор действия получил собственное уведомление (спам)"
        assert not errors, f"есть необработанные события с ошибкой: {[e.last_error for e in errors]}"

        # 5) напоминание о сроке
        from app.core.notify import schedule_due_reminders
        # В демо-сиде сроки захардкожены на конкретные даты и «протухают» —
        # в окно [today, today+days_ahead] не попадает ни одна сущность.
        # Создаём свою задачу с дедлайном завтра (как _mkdue выше).
        await _mkdue(s, 1, "Задача с завтрашним сроком", target, dept_id)
        # Вторая задача: автор = ответственный = target. Единственный участник
        # отсеивается воркером как автор действия (анти-спам «сам себе») —
        # широкое событие должно завершиться штатно (done), а не error-ретраями.
        await _mkdue(s, 2, "Задача без отдельных получателей", target, dept_id)
        # Третья задача: устаревшая карточка из демо-сида (дедлайн в прошлом)
        # без ответственных вообще — планировщик ставит по ней overdue-событие.
        # Это тоже штатная ситуация «в никуда»: событие обязано завершиться
        # done (без адресных target_user_ids ошибка не поднимается).
        stale_list = (await s.scalars(select(Entity).where(
            Entity.kind.in_([EntityKind.TASK, EntityKind.PROJECT]),
            Entity.due_date.is_not(None),
            Entity.due_date < "2026-10-26"))).all()
        stale = next(iter(stale_list), None)
        if stale is not None:
            stale_resps = list((await s.scalars(select(EntityResponsible).where(
                EntityResponsible.entity_id == stale.id))).all())
            for r in stale_resps:
                await s.delete(r)
            ex = dict(stale.extra_json or {})
            ex["executors"] = []
            stale.extra_json = ex
        # NB: entity_responsibles.user_id имеет FK (ondelete=RESTRICT) на users.id,
        # «висячую» ссылку нельзя смоделировать удалением пользователя — делаем
        # слот NULL (слот без ответственного) и добавляем в extra_json['executors']
        # запись с несуществующим id: денормализованные списки исполнителей
        # чистятся только при редактировании карточки, на проде такие ссылки
        # реально встречаются. Единственный получатель не резолвится в живого
        # пользователя -> событий доставки нет (eligible=0) — штатное «в никуда»,
        # событие обязано завершиться done, а не error-ретраями.
        ghost = await _mkdue(s, 3, "Задача с битым ответственным", target, dept_id)
        gslots = list((await s.scalars(select(EntityResponsible).where(
            EntityResponsible.entity_id == ghost.id))).all())
        for gs_ in gslots:
            gs_.user_id = None  # слот без ответственного
        gx = dict(ghost.extra_json or {})
        gx["executors"] = [{"id": 999999, "name": "Удалённый сотрудник"}]
        ghost.extra_json = gx
        from sqlalchemy.orm.attributes import flag_modified as _flag_modified
        _flag_modified(ghost, "extra_json")  # JSON-мутация без new объекта
        await s.commit()
        async with SessionLocal() as s3:
            n = await schedule_due_reminders(s3, days_ahead=10)
            await s3.commit()
        print(f"[6] due reminders поставлено: {n}")
        assert n > 0, "schedule_due_reminders не поставил ни одного напоминания"
        # Один предсказуемый проход воркера: все due/overdue-события ставятся
        # ПЛАНИРОВЩИКОМ (после пред-чистки очереди в шаге [4]), поэтому один
        # прогон обрабатывает всю очередь целиком. Повторные проходы в цикле
        # опасны: если бы планировщик из-за бага дедупликации ставил события
        # заново, цикл бесконечно поднимал бы attempts до 8 и статус error —
        # тест падал бы с запутанным «error» вместо явного дубликата.
        await _outbox_once()
        async with SessionLocal() as s4:
            due_rows = list((await s4.scalars(select(OutboxEvent).where(
                OutboxEvent.topic == "telegram.due"))).all())
        # Воркер завершает недоставляемые напоминания без ошибки (см.
        # _deliver_by_scenarios), поэтому все telegram.due должны быть done.
        assert due_rows and all(r.status == "done" for r in due_rows), \
            f"напоминания о сроке не обработаны: {[(r.id, r.status, r.last_error) for r in due_rows if r.status != 'done']}"
        assert any("Приближается срок" in t for _, t in sent), "нет текста напоминания"
        # напоминание по нашей задаче с завтрашним сроком должен получить её
        # ответственный (target) — проверка сквозной доставки due-событий
        due_msgs = [(c, t) for c, t in sent if "Задача с завтрашним сроком" in t]
        assert due_msgs, "напоминание не доставлено ни в один чат"
        assert target.telegram_chat_id in {c for c, _ in due_msgs}, \
            "ответственный не получил напоминание о сроке своей задачи"
        print("\n✅ ЦЕПОЧКА УВЕДОМЛЕНИЙ РАБОТАЕТ: создание → outbox → воркер → доставка")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
