#!/usr/bin/env python3
"""Генерация хеша пароля для резервной переменной окружения ADMIN_LOGINS.

Основной способ задать администратора — проще и НЕ требует этого скрипта:
    ADMIN_USERNAME=<логин>
    ADMIN_PASSWORD=<пароль в открытом виде>   # система сама захеширует при старте
    ADMIN_FULL_NAME="<ФИО>"                   # необязательно

Скрипт нужен только если вы хотите хранить в env не пароль, а его хеш
(например, несколько админов):
    python scripts/gen_admin_password.py <логин> [ФИО]

Ввод пароля — интерактивно (getpass, не светится в истории shell) либо через
аргумент-переменную CAESAR_ADMIN_PASSWORD. Вывод — готовая строка вида
    логин:pbkdf2_sha256$240000$salt$hash:ФИО
Её нужно положить в ADMIN_LOGINS (несколько админов — через ';').
Смена пароля = перегенерировать хеш, обновить ADMIN_LOGINS и перезапустить сервис.
"""
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.security import hash_password  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    login = sys.argv[1].strip().lower()
    name = sys.argv[2].strip() if len(sys.argv) > 2 else f"Администратор {login}"
    password = os.environ.get("CAESAR_ADMIN_PASSWORD") or getpass.getpass("Пароль: ")
    password2 = os.environ.get("CAESAR_ADMIN_PASSWORD") or getpass.getpass("Пароль ещё раз: ")
    if password != password2:
        print("Пароли не совпадают", file=sys.stderr)
        return 2
    if len(password) < 8:
        print("Слишком короткий пароль (минимум 8 символов)", file=sys.stderr)
        return 2
    print(f"\nADMIN_LOGINS={login}:{hash_password(password)}:{name}\n")
    print("Примечание: если у вас один админ — достаточно ADMIN_USERNAME/ADMIN_PASSWORD, "
          "хеш не нужен. Разделитель полей внутри записи ADMIN_LOGINS — только первые два ':'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
