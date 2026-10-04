#!/usr/bin/env python3
"""Диагностика доступа: почему вход не даёт ADMIN.

Запуск на хостинге (Amvera: веб-терминал проекта / VPS: docker exec):

    python scripts/check_access.py

Показывает:
  * значение BOOTSTRAP_ADMIN_YA_IDS из окружения (и видит ли его приложение);
  * содержимое таблиц users и invites в реальной БД;
  * совпадает ли чей-нибудь yandex_id с bootstrap-списком;
  * что сделает bootstrap при следующем перезапуске (dry-run, ничего не пишет).

Без аргументов — только диагностика. Флаг --apply дополнительно выполняет
bootstrap немедленно (идемпотентен, безопасно): повышает существующего
пользователя из BOOTSTRAP_ADMIN_YA_IDS до ADMIN без перезапуска сервиса.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def main(apply: bool) -> None:
    from sqlalchemy import select

    from app.core.config import get_settings
    from app.db.engine import SessionLocal
    from app.models.core import Invite, Role, User

    s = get_settings()
    print(f"DATABASE_PATH        : {s.database_path} "
          f"(файл существует: {os.path.exists(str(s.database_path))})")
    raw = os.getenv("BOOTSTRAP_ADMIN_YA_IDS", "<не задана>")
    print(f"ENV BOOTSTRAP_ADMIN_YA_IDS       : {raw!r}")
    print(f"Settings.bootstrap_admin_ya_ids  : {s.bootstrap_admin_ya_ids!r}")
    ids = [x.strip() for x in s.bootstrap_admin_ya_ids.split(",") if x.strip()]
    print(f"Разобранный список ya_id         : {ids or '— пусто —'}")
    if not ids:
        print("\n!! Bootstrap-список пуст: проверьте точное имя переменной в панели")
        print("   хостинга (BOOTSTRAP_ADMIN_YA_IDS) и что она попала в окружение рантайма.")

    async with SessionLocal() as session:
        users = (await session.execute(select(User))).scalars().all()
        print(f"\nusers ({len(users)}):")
        for u in users:
            mark = ""
            if u.yandex_id in ids:
                mark = "  <-- СОВПАДЕНИЕ с bootstrap"
                if u.role != Role.ADMIN:
                    mark += ", но роль НЕ admin!"
            print(f"  id={u.id:<3} yandex_id={u.yandex_id!r:<28} email={u.email!r:<28} "
                  f"role={u.role.value:<12} active={u.is_active}{mark}")

        invites = (await session.execute(select(Invite))).scalars().all()
        print(f"\ninvites ({len(invites)}):")
        for i in invites:
            print(f"  id={i.id:<3} yandex_id={i.yandex_id!r:<20} email={i.email!r:<28} "
                  f"role={i.role.value:<12} used_at={i.used_at} created_by={i.created_by_id}")

        # Частая причина «зашёл, но не админ»: пользователь заведён по email-invite,
        # а в users у него yandex_id, которого нет в bootstrap-списке.
        by_email_overlap = [
            u for u in users
            if any(i.email and u.email and i.email.lower() == u.email.lower()
                   for i in invites if i.used_at is not None)
        ]
        if by_email_overlap:
            print("\nПодсказка: эти пользователи вошли по email-приглашению — их реальный")
            print("yandex_id может отличаться от того, что указан в BOOTSTRAP_ADMIN_YA_IDS:")
            for u in by_email_overlap:
                print(f"  {u.email}: yandex_id={u.yandex_id!r} role={u.role.value}")

        if apply:
            from app.db.seed import bootstrap
            await bootstrap(session)
            print("\n--apply: bootstrap выполнен. Перепроверьте роли выше командой без флага.")
        else:
            print("\nДля немедленного применения bootstrap без перезапуска:"
                  " python scripts/check_access.py --apply")


if __name__ == "__main__":
    asyncio.run(main(apply="--apply" in sys.argv))
