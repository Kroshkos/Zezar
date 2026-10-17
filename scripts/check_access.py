#!/usr/bin/env python3
"""Диагностика доступа: почему вход по логину/паролю не даёт ADMIN.

Запуск на хостинге (Amvera: веб-терминал проекта / VPS: docker exec):

    python scripts/check_access.py

Показывает:
  * значения ADMIN_USERNAME / ADMIN_PASSWORD (и видит ли их приложение);
  * содержимое таблицы users в реальной БД;
  * совпадает ли чей-нибудь login со списком env-администраторов;
  * что сделает bootstrap при следующем перезапуске (dry-run, ничего не пишет).

Без аргументов — только диагностика. Флаг --apply дополнительно выполняет
bootstrap немедленно (идемпотентен, безопасно): создаёт/чинит env-администратора
без перезапуска сервиса.
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
    from app.db.seed import is_env_admin_login
    from app.models.core import Role, User

    s = get_settings()
    print(f"DATABASE_PATH        : {s.database_path} "
          f"(файл существует: {os.path.exists(str(s.database_path))})")
    print(f"ENV ADMIN_USERNAME             : {os.getenv('ADMIN_USERNAME', '<не задана>')!r}")
    print(f"ENV ADMIN_PASSWORD             : {'задан' if os.getenv('ADMIN_PASSWORD') else '<не задана>'}")
    print(f"ENV ADMIN_LOGINS               : {'задан' if os.getenv('ADMIN_LOGINS') else '<не задана>'}")
    entries = s.admin_login_entries
    logins = [e[0] for e in entries]
    print(f"Разобранный список env-админов : {logins or '— пусто —'}")
    if not entries:
        print("\n!! Список пуст: задайте ADMIN_USERNAME + ADMIN_PASSWORD в панели")
        print("   хостинга и убедитесь, что переменные попали в окружение рантайма.")

    # На чистой/первой БД таблицы ещё не созданы (это делает lifespan приложения:
    # create_all + bootstrap). Чиним схему прямо здесь — иначе диагностика падала
    # с «no such table: users» до того, как успеет что-либо показать.
    from app.db.base import Base
    from app.main import _import_all_models

    _import_all_models()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with SessionLocal() as session:
        users = (await session.execute(select(User).order_by(User.id))).scalars().all()
        print(f"\nusers ({len(users)}):")
        for u in users:
            mark = ""
            if is_env_admin_login(u.login or ""):
                mark = "  <-- СОВПАДЕНИЕ с env-админом"
                if u.role != Role.ADMIN:
                    mark += ", но роль НЕ admin!"
            print(f"  id={u.id:<3} login={(u.login or '')!r:<24} email={(u.email or '')!r:<28} "
                  f"role={u.role.value:<12} active={u.is_active} "
                  f"pwd={'есть' if u.password_hash else 'НЕТ'}{mark}")

        env_in_db = {e[0] for e in entries}
        missing = env_in_db - {(u.login or "").lower() for u in users}
        if missing:
            print(f"\nПодсказка: env-админы {sorted(missing)} ещё не созданы в БД — "
                  "bootstrap создаст их при старте (или выполните --apply сейчас).")

        if apply:
            from app.db.seed import bootstrap
            await bootstrap(session)
            print("\n--apply: bootstrap выполнен. Перепроверьте роли выше командой без флага.")
        else:
            print("\nДля немедленного применения bootstrap без перезапуска:"
                  " python scripts/check_access.py --apply")


if __name__ == "__main__":
    asyncio.run(main(apply="--apply" in sys.argv))
