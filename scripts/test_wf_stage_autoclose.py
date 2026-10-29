"""Интеграционный тест замыкания «Задача → этап маршрута».

Сценарий пользователя: у проекта есть маршрут (этапы) с автозадачами; при
переводе задачи в done этап должен закрываться, а следующий — активироваться.

Запуск: python scripts/test_wf_stage_autoclose.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB = tempfile.mktemp(suffix=".db")
os.environ["DATABASE_PATH"] = DB


async def main() -> None:
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.db.base import Base
    import app.models.core  # noqa
    import app.models.module_payloads  # noqa
    import app.models.mobile  # noqa
    from app.models.core import Department, Entity, EntityKind, User
    from app.models.module_payloads import ProjectPayload, TaskPayload
    from app.core import entity_service, wf_service

    engine = create_async_engine(f"sqlite+aiosqlite:///{DB}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as s:
        u = User(email="t@t.t", login="t", password_hash="x", display_name="Тест", is_active=True)
        d = Department(code="DEP1", name="Деп", is_active=True)
        s.add_all([u, d])
        await s.commit()
        await s.refresh(u)
        await s.refresh(d)

        # 1. Проект с маршрутом из трёх этапов + автозадачами
        proj = await entity_service.create_entity(
            s, u, kind=EntityKind.PROJECT, module_key="projects",
            owner_department_id=d.id, title="Проект А")
        stages = wf_service.wf_build_stages(
            [{"name": "Подготовка"}, {"name": "Согласование"}, {"name": "Сдача"}])
        await wf_service.wf_create_stage_tasks(s, u, proj, stages, d.id)
        s.add(ProjectPayload(entity_id=proj.id, stages=stages))
        await s.commit()

        task_uids = [st["taskId"] for st in stages if st.get("taskId")]
        assert len(task_uids) == 3, f"автозадачи не созданы: {stages}"

        # привяжем project_entity_uid (как это делает роутер projects)
        for tu in task_uids:
            t = await s.scalar(select(Entity).where(Entity.uid == tu))
            tp = await s.scalar(select(TaskPayload).where(TaskPayload.entity_id == t.id))
            if tp is None:
                s.add(TaskPayload(entity_id=t.id, project_entity_uid=proj.uid))
            else:
                tp.project_entity_uid = proj.uid
        await s.commit()

        # 2. Отмечаем первую автозадачу выполненной через update_entity
        task = await s.scalar(select(Entity).where(Entity.uid == task_uids[0]))
        await entity_service.update_entity(s, u, task, {"status_key": "done"})
        await s.commit()

        # 3. Проверяем состояние этапов
        pl = await s.scalar(select(ProjectPayload).where(ProjectPayload.entity_id == proj.id))
        st = {x["name"]: x for x in pl.stages}
        print("ЭТАПЫ:", [(x["name"], x["state"]) for x in pl.stages])
        assert st["Подготовка"]["state"] == "done", "первый этап не закрыт"
        assert st["Согласование"]["state"] == "active", "второй этап не активирован"
        assert st["Сдача"]["state"] == "pending", "третий этап не должен был дёрнуться"

        # 4. Завершаем вторую задачу — третий этап активируется
        task2 = await s.scalar(select(Entity).where(Entity.uid == task_uids[1]))
        await entity_service.update_entity(s, u, task2, {"status_key": "done"})
        await s.commit()
        pl = await s.scalar(select(ProjectPayload).where(ProjectPayload.entity_id == proj.id))
        st = {x["name"]: x for x in pl.stages}
        assert st["Согласование"]["state"] == "done"
        assert st["Сдача"]["state"] == "active", "третий этап не активирован"

        # 5. Обычная задача без этапа — не должна ломаться
        plain = await entity_service.create_entity(
            s, u, kind=EntityKind.TASK, module_key="tasks",
            owner_department_id=d.id, title="Обычная задача")
        await entity_service.update_entity(s, u, plain, {"status_key": "done"})
        await s.commit()

        # 6. Легаси-путь: задача без project_entity_uid, но с описанием «— этап: X»
        legacy_proj = await entity_service.create_entity(
            s, u, kind=EntityKind.PROJECT, module_key="projects",
            owner_department_id=d.id, title="Проект Б (легаси)")
        lraw = wf_service.wf_build_stages([{"name": "Черновик"}, {"name": "Финал"}])
        lraw[0]["taskId"] = ""  # taskId пустой — имитация легаси
        s.add(ProjectPayload(entity_id=legacy_proj.id, stages=lraw))
        ltask = await entity_service.create_entity(
            s, u, kind=EntityKind.TASK, module_key="tasks",
            owner_department_id=d.id,
            title="Проект Б (легаси) — этап: Черновик",
            description="Автозадача этапа «Черновик». Завершение задачи закрывает этап.")
        await s.commit()
        await entity_service.update_entity(s, u, ltask, {"status_key": "done"})
        await s.commit()
        lpl = await s.scalar(select(ProjectPayload).where(
            ProjectPayload.entity_id == legacy_proj.id))
        lst = {x["name"]: x for x in lpl.stages}
        print("ЛЕГАСИ ЭТАПЫ:", [(x["name"], x["state"]) for x in lpl.stages])
        assert lst["Черновик"]["state"] == "done", "легаси-этап не закрыт по описанию"
        assert lst["Финал"]["state"] == "active", "легаси: следующий этап не активирован"

    print("OK: все проверки wf-stage-autoclose пройдены")
    await engine.dispose()
    os.unlink(DB)


if __name__ == "__main__":
    asyncio.run(main())
