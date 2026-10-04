#!/usr/bin/env python3
"""Генерация записи для переменной окружения ADMIN_LOGINS.

Использование:
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
    print("Совет: если в ФИО/пароле есть ':' — используйте 'python -c' вывод как есть, "
          "разделитель полей внутри записи — только первые два ':'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
