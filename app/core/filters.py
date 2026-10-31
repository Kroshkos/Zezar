"""Общие клиентские фильтры списков сущностей («дедлайн», «подразделение»).

История: раньше в каждом модуле (Задачи, Проекты, Документы, ПО) была своя
галочка «только просроченные» с жёсткой проверкой ``status_key !== 'done'``,
которая не учитывала завершающие статусы справочника подразделения. Теперь
набор фильтров единый и определяется здесь:

  df_today     — дедлайн сегодня;
  df_week      — дедлайн в пределах текущей недели (пн–вс);
  df_month     — дедлайн в пределах текущего месяца;
  df_overdue   — просрочено: дедлайн прошёл и задача ещё не завершена.

Все проверки — на ISO-датах (YYYY-MM-DD), лексикографическое сравнение
корректно. Даты передаются извне (см. dashboard.build_dashboard_data): это
позволяет e2e-тестам подменять «сегодня» без правки данных в БД.

Функции чистые и работают на dicts (строки таблиц/карточек модулей) — JS-версия
в static/js/core.js (window.filtMatch / window.applyDeadlineFilter) повторяет
эту же логику один-в-один; при изменении здесь меняйте и там.
"""
from __future__ import annotations

from datetime import date, timedelta


def month_bounds(today: date) -> tuple[str, str]:
    """(первое, последнее) число текущего месяца в ISO."""
    first = today.replace(day=1)
    nxt = first + timedelta(days=32)
    last = nxt.replace(day=1) - timedelta(days=1)
    return first.isoformat(), last.isoformat()


def week_bounds(today: date) -> tuple[str, str]:
    """(понедельник, воскресенье) текущей недели в ISO."""
    monday = today - timedelta(days=today.weekday())
    return monday.isoformat(), (monday + timedelta(days=6)).isoformat()


def match_deadline(due: str | None, flags: set[str], *, today_iso: str,
                   week_iso: tuple[str, str], month_iso: tuple[str, str],
                   done_keys: set[str], status_key: str | None) -> bool:
    """True, если запись проходит выбранные фильтры дедлайна.

    Флаги внутри одной группы складываются по ИЛИ (так привычнее пользователю:
    «сегодня + эта неделя» = всё до конца недели). Если флагов нет — True.
    Запись без дедлайна отсекается любым включённым фильтром дедлайна.
    """
    if not flags:
        return True
    if not due:
        return False
    ok = False
    if "today" in flags and due == today_iso:
        ok = True
    if "week" in flags and week_iso[0] <= due <= week_iso[1]:
        ok = True
    if "month" in flags and month_iso[0] <= due <= month_iso[1]:
        ok = True
    if "overdue" in flags and due < today_iso and (status_key or "") not in done_keys:
        ok = True
    return ok


def apply_deadline_filter(items: list[dict], flags: set[str], *, today: date,
                          done_keys: set[str]) -> list[dict]:
    """Серверная фильтрация списка dicts-строк (ключи due_date/status_key)."""
    if not flags:
        return items
    kw = dict(today_iso=today.isoformat(), week_iso=week_bounds(today),
              month_iso=month_bounds(today), done_keys=done_keys)
    return [it for it in items
            if match_deadline(it.get("due_date"), flags, status_key=it.get("status_key"), **kw)]


async def page_filters(request, session, kind: str, dept_ids: list[int] | None,
                       is_global: bool) -> dict:
    """Контекст фильтров страницы сущностей (Задачи/Проекты/Документы/ПО).

    Разбирает query-параметры GET-страницы и возвращает dict для шаблона:

      deadline_flags — множество включённых флагов дедлайнов
                       (подмножество {today, week, month, overdue});
      department_id  — выбранный фильтр подразделения (int|None);
      departments    — список {id,name} для <select>: админ/координатор — все
                       активные, остальные — только свои пространства;
      today          — ISO «сегодня» (пробрасывается в JS, чтобы границы
                       недели/месяца в браузере совпадали с серверными);
      filter_qs      — строка уже выбранных фильтров для сохранения состояния
                       при переходах по ссылкам (пагинация/профили).

    Неизвестные/некорректные значения молча игнорируются (безопасно).
    """
    flags: set[str] = set()
    for name in ("df_today", "df_week", "df_month", "df_overdue"):
        v = request.query_params.get(name, "")
        if v not in ("", "0", "false"):
            flags.add(name[3:])          # df_week -> week
    dept: int | None = None
    raw = request.query_params.get("dept", "")
    try:
        d = int(raw)
        if d > 0:
            dept = d
    except (TypeError, ValueError):
        dept = None

    from sqlalchemy import select as _select

    from app.core.notify import terminal_status_keys
    from app.models.core import Department

    if is_global:
        rows = (await session.scalars(
            _select(Department).where(Department.is_active == True)  # noqa: E712
            .order_by(Department.name))).all()
    else:
        rows = (await session.scalars(
            _select(Department)
            .where(Department.id.in_(dept_ids or [-1]))
            .order_by(Department.name))).all()
    departments = [{"id": d.id, "name": d.name} for d in rows]
    # выбор вне доступных подразделений игнорируем (безопасно по умолчанию)
    if dept is not None and not any(d["id"] == dept for d in departments):
        dept = None

    done_keys = await terminal_status_keys(session, kind)
    qs_parts = []
    if dept:
        qs_parts.append(f"dept={dept}")
    for f in sorted(flags):
        qs_parts.append(f"df_{f}=1")

    return {
        "deadline_flags": flags,
        "department_id": dept,
        "departments": departments,
        "today": date.today().isoformat(),
        "week_range": week_bounds(date.today()),
        "month_range": month_bounds(date.today()),
        "done_keys": sorted(done_keys),
        "filter_qs": ("&" + "&".join(qs_parts)) if qs_parts else "",
    }


def apply_page_filters(items: list[dict], fctx: dict) -> list[dict]:
    """Сервером применяем фильтры к списку строк страницы до рендера.

    Подразделение — точное совпадение owner (department_id); дедлайны — по
    общим правилам match_deadline. JS на странице повторяет фильтрацию для
    интерактивной смены фильтров без перезагрузки (см. core.js filtMatch).
    """
    out = items
    dept = fctx.get("department_id")
    if dept:
        out = [it for it in out if it.get("department_id") == dept]
    flags = fctx.get("deadline_flags") or set()
    if flags:
        out = apply_deadline_filter(out, flags, today=date.fromisoformat(fctx["today"]),
                                    done_keys=set(fctx.get("done_keys") or []))
    return out
