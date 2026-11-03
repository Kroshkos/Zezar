"""
Яндекс.Календарь: CalDAV (WebDAV) синхронизация сущностей со сроком (due_date).

Логика «одним проходом» (diff):
  - entity.sync_state=pending  -> upsert события (создать/обновить по cal_uid);
  - entity.sync_state=deleted  -> удалить событие;
  - удаление/отключение пользователя -> его календарные события чистятся отдельно.
Каждый пользователь пишет в СВОЙ календарь «Цезарь» app-паролем (шифруется в БД).
Сетевые вызовы — только из фонового воркера, никогда из HTTP-запроса.
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET

import httpx

from app.core.security import decrypt_secret

log = logging.getLogger("caesar.caldav")


def cal_enabled() -> bool:
    """Флаг отключения интеграции (YANDEX_CAL_ENABLED=false).

    Используется воркерами (diff-проход и обработка calendar.* событий) и
    точкой сохранения настроек профиля — при выключенном флаге сетевые
    вызовы к Яндексу не выполняются вовсе.
    """
    try:
        from app.core.config import get_settings
        return bool(get_settings().yandex_cal_enabled)
    except Exception:  # noqa: BLE001
        return True


# Яндекс закрыл CalDAV-эндпоинт dav.yandex.ru (он возвращает 405 на PROPFIND/MKCOL).
# Рабочий протокол — WebDAV: https://webdav.yandex.ru (значение из настроек;
# там же живёт дефолтный календарь в папке /calendar/).
DEFAULT_CAL_BASE = "https://webdav.yandex.ru"
NS_DAV = {"d": "DAV:"}


class CalDavError(Exception):
    pass


def caldav_base() -> str:
    try:
        from app.core.config import get_settings
        base = (get_settings().yandex_cal_base or "").strip().rstrip("/")
    except Exception:  # noqa: BLE001
        base = ""
    return base or DEFAULT_CAL_BASE


def _auth(user_email: str, app_password: str) -> tuple[str, str]:
    return (user_email, app_password)


def normalize_login(login: str | None) -> str:
    """Логин для CalDAV Basic auth: trim + отсечение доменной части.

    Яндекс аутентифицирует по ЛОГИНУ аккаунта без @домена; при ручном вводе
    почта вместо логина — самая частая ошибка. Все точки обращения к WebDAV
    проходят через эту функцию, чтобы поведение было одинаковым и предсказуемым.
    """
    login = (login or "").strip()
    if "@" in login:
        login = login.split("@")[0].strip()
    return login


async def ensure_calendar(client: httpx.AsyncClient, email: str, apw: str,
                          cal_name: str = "Цезарь") -> str:
    """Возвращает URL WebDAV-папки для .ics-файлов, создаёт её если нет.

    Папка лежит внутри «календарной» области (/calendar/), чтобы события
    попадали именно в Яндекс.Календарь, а не в обычный диск.
    """
    login = normalize_login(email)   # CalDAV требует логин без @домена
    base = caldav_base() + "/"
    cal_root = base + "calendar/"
    folder_url = cal_root + _q(cal_name) + "/"
    # уже существует?
    r = await client.request("PROPFIND", folder_url,
                             headers={"Depth": "0", "Authorization": _basic(login, apw)},
                             content=_propfind(["getctag"]))
    if r.status_code in (207, 200):
        return folder_url
    if r.status_code in (401, 403):
        # Здесь уже используется нормализованный логин — повторная проверка
        # «без домена» не имеет смысла: остаётся только честная ошибка.
        raise CalDavError(f"WebDAV отклонил учётные данные для логина «{login}»: "
                          f"HTTP {r.status_code} (проверьте логин аккаунта Яндекса "
                          f"и app-пароль — сервис «Другие» на id.yandex.ru)")
    # создаём папку (405 = уже существует — гонка, ок)
    mk = await client.request("MKCOL", folder_url,
                              headers={"Authorization": _basic(login, apw)})
    if mk.status_code not in (201, 204, 405):
        # фолбэк: пишем прямо в /calendar/ (дефолтный календарь Яндекса)
        rr = await client.request("PROPFIND", cal_root,
                                  headers={"Depth": "0", "Authorization": _basic(login, apw)},
                                  content=_propfind(["getctag"]))
        if rr.status_code in (207, 200):
            log.warning("MKCOL %s -> %s, fallback to %s", folder_url, mk.status_code, cal_root)
            return cal_root
        raise CalDavError(f"Не удалось создать календарь '{cal_name}': MKCOL HTTP {mk.status_code}, "
                          f"PROPFIND {cal_root} HTTP {rr.status_code}")
    return folder_url


def _basic(email: str, apw: str) -> str:
    import base64
    return "Basic " + base64.b64encode(f"{email}:{apw}".encode()).decode()


def is_app_password(apw: str) -> bool:
    """App-пароль Яндекса: 12 символов без пробелов.

    Формат выдаваемых Яндексом паролей приложений — строго [0-9a-f]{12}, но при
    копировании из интерфейса id.yandex.ru иногда теряются символы или добавляются
    неразрывные пробелы/кириллические похожие буквы. Требовать ровно hex-алфавит
    рискованно: реальный рабочий пароль мог быть отвергнут локально ещё до сетевой
    проверки («ложное отклонение»). Поэтому проверка мягкая: длина и отсутствие
    пробелов — явный признак «ввели обычный пароль аккаунта»; всё остальное решает
    сам Яндекс (401/403).
    """
    apw = (apw or "").strip()
    return len(apw) == 12 and " " not in apw


def looks_like_account_password(apw: str) -> bool:
    """Похож ли ввод на обычный пароль аккаунта (не на пароль приложений).

    App-пароль Яндекса — одна строка ровно из 12 символов. При копировании из
    интерфейса id.yandex.ru иногда прилипают пробелы/NBSP — убираем ВСЕ пробельные
    символы перед сравнением длины, иначе «abc123 def456» ошибочно считался бы
    обычным паролем.
    """
    apw = "".join((apw or "").split())   # все whitespace incl. \u00a0
    if not apw:
        return True           # пустой ввод — точно не app-пароль
    return len(apw) != 12     # пароль приложений всегда ровно 12 символов


def _q(name: str) -> str:
    import urllib.parse
    return urllib.parse.quote(name)


def _propfind(props: list[str]) -> bytes:
    body = '<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop>' + \
        "".join(f"<d:{p}/>" for p in props) + "</d:prop></d:propfind>"
    return body.encode()


def _window(ent) -> tuple[str, str] | None:
    """Кортеж (DTSTART, DTEND) без префиксов TZID.

    Совещания: точное время из payload.start_at/end_at (или date+time_);
    остальные сущности: 09:00–18:00 дня срока (due_date).
    Возвращает None, если окно построить нельзя (мусорные даты) — событие
    в календарь не ставится.
    """
    p = getattr(ent, "_cal_payload", None)
    # kind может быть строкой (raw SQL) или Enum — сравниваем значение толерантно
    kind_val = ent.kind.value if hasattr(ent.kind, "value") else str(ent.kind or "")
    if kind_val == "meeting" and p is not None:
        start = getattr(p, "start_at", None) or ""
        end = getattr(p, "end_at", None) or ""

        def fmt_iso(v: str) -> str | None:
            """'YYYY-MM-DDTHH:MM[:SS]' -> 'YYYYMMDDTHHMM00';None если не распознано."""
            v = (v or "").strip()
            try:
                from datetime import datetime as _dtn
                dt = _dtn.fromisoformat(v)
                return dt.strftime("%Y%m%dT%H%M%S")
            except ValueError:
                return None
        s, e = fmt_iso(start), fmt_iso(end)
        if not s and ent.due_date:
            # fallback: payload.start_at не задан, но есть date+time_ из карточки демо
            s = fmt_iso(f"{ent.due_date}T{getattr(p, 'time_', '') or ''}")
        if s:
            if not e:  # конец = +2 часа (ограниченно) или 18:00 того же дня
                try:
                    from datetime import datetime as _dt, timedelta as _td
                    e = (_dt.strptime(s, "%Y%m%dT%H%M%S") + _td(hours=2)).strftime("%Y%m%dT%H%M%S")
                except ValueError:
                    e = s[:8] + "T180000"
            elif e <= s:
                # валидация: end_at раньше/равен start_at — невалидное окно;
                # берём стандартные +2 часа от начала
                log.warning("meeting %s: end_at<=start_at (%s..%s), window -> +2h",
                            getattr(ent, "uid", "?"), s, e)
                from datetime import datetime as _dt, timedelta as _td
                e = (_dt.strptime(s, "%Y%m%dT%H%M%S") + _td(hours=2)).strftime("%Y%m%dT%H%M%S")
            return s, e
    # 'YYYY-MM-DD' — валидируем перед подстановкой в ICS: мусорная дата
    # молча превращалась в невалидный DTSTART (Яндекс отклонял PUT с 400).
    due = ent.due_date
    from datetime import date as _date
    try:
        d = _date.fromisoformat(str(due)[:10])
    except (ValueError, TypeError):
        # НЕ бросаем исключение: у сущностей без payload (не совещания) из-за
        # него падал и весь upsert_event, и весь ics_for_entity. Возвращаем None —
        # вызывающий код (upsert_event) отклонит такую сущность с понятной ошибкой.
        log.warning("caldav window %s: некорректный due_date '%s' — событие не ставится",
                    getattr(ent, "uid", "?"), due)
        return None
    dt = d.strftime("%Y%m%d")
    return f"{dt}T090000", f"{dt}T180000"


def ics_for_entity(ent, responsible_name: str) -> str | None:
    """UID стабильный (cal_uid) — повторные записи обновляют событие.

    Возвращает None, если календарное окно не строится (некорректные даты)."""
    tzid = "Europe/Moscow"
    w = _window(ent)
    if w is None:
        return None
    s, e = w
    dtstamp = s[:8] + "T000000Z"
    summary = f"[{ent.module_key}] {ent.title}"
    desc = f"Статус: {ent.status_key or '—'}; ответственный: {responsible_name}"
    extra = []
    p = getattr(ent, "_cal_payload", None)
    if p is not None and getattr(p, "location", None):
        extra.append(f"LOCATION:{p.location}")
    if p is not None and getattr(p, "chair", None):
        desc += f"; председатель: {p.chair}"
    return "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Caesar//RU",
        "BEGIN:VEVENT",
        f"UID:{ent.cal_uid}",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART;TZID={tzid}:{s}",
        f"DTEND;TZID={tzid}:{e}",
        f"SUMMARY:{summary}",
        f"DESCRIPTION:{desc}",
        *extra,
        "END:VEVENT", "END:VCALENDAR",
    ])


async def upsert_event(email: str, apw_enc: str, cal_path: str, ent,
                       responsible_name: str) -> bool:
    if not cal_enabled():      # YANDEX_CAL_ENABLED=false — сетевых вызовов нет вовсе
        return False
    apw = decrypt_secret(apw_enc)
    if not apw:
        return False
    ics = ics_for_entity(ent, responsible_name)
    if ics is None:
        # некорректные даты (см. _window) — PUT с битым DTSTART Яндекс вернул бы 400;
        # считаем это ошибкой конфигурации карточки, а не сети: без ретраев
        raise CalDavError(f"некорректный due_date у сущности "
                          f"{getattr(ent, 'uid', '?')} — событие в календарь не ставится")
    url = cal_path.rstrip("/") + "/" + _q(ent.cal_uid) + ".ics"
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.put(url, content=ics.encode(),
                             headers={"Content-Type": "text/calendar; charset=utf-8",
                                      "Authorization": _basic(email, apw)})
    ok = r.status_code in (201, 204)
    if not ok:
        log.warning("CalDAV PUT %s -> %s %s", url, r.status_code, r.text[:200])
        # поднимаем исключение с причиной — воркер пишет её в ent.cal_error,
        # иначе на странице «Интеграции» виден только статус ERROR без объяснения
        raise CalDavError(f"PUT {cal_path} -> HTTP {r.status_code}: {r.text[:160]}")
    return True


async def delete_event(email: str, apw_enc: str, cal_path: str, cal_uid: str) -> bool:
    if not cal_enabled():      # YANDEX_CAL_ENABLED=false — сетевых вызовов нет вовсе
        return False
    apw = decrypt_secret(apw_enc)
    if not apw:
        return False
    url = cal_path.rstrip("/") + "/" + _q(cal_uid) + ".ics"
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.delete(url, headers={"Authorization": _basic(email, apw)})
    return r.status_code in (200, 204, 404)


async def verify_credentials(login: str, app_password: str) -> tuple[bool, int]:
    """Проверка app-пароля при сохранении настроек (PUT /api/profile/calendar).

    Возвращает (ok, http_code): код нужен, чтобы отличить неверный пароль (401/403)
    от сетевых/сервисных ошибок и дать пользователю точную подсказку.
    Примечание: CalDAV Яндекса аутентифицирует по ЛОГИНУ аккаунта, а не по email.
    При YANDEX_CAL_ENABLED=false сетевой запрос НЕ выполняется вовсе — интеграция
    отключена на уровне контура сохранения настроек тоже.
    """
    if not cal_enabled():
        return False, 0
    login = normalize_login(login)   # CalDAV Basic auth — логин без @домена
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.request("PROPFIND", caldav_base() + "/",
                                     headers={"Depth": "0", "Authorization": _basic(login, app_password),
                                              "Content-Type": 'application/xml'},
                                     content=_propfind(["getctag"]))
    except httpx.HTTPError as e:
        log.warning("verify_credentials network error: %s", e)
        return False, 0
    if r.status_code == 405:
        # Признак того, что запрос ушёл на закрытый CalDAV-эндпоинт (dav.yandex.ru):
        # проверяем корректный WebDAV-базовый адрес.
        log.error("WebDAV вернул 405 — проверьте YANDEX_CAL_BASE (нужен https://webdav.yandex.ru)")
    if r.status_code in (401, 403):
        # Диагностика типичных ошибок ручного ввода — иначе пользователь получает
        # безликое «отклонили учётные данные» и не понимает, что именно не так.
        # Логин уже нормализован (домен отсечён), поэтому проверяем только его
        # форму; про исходный ввод с доменом судит credential_hint по данным
        # роутера сохранения.
        base = caldav_base()
        if not login:
            log.warning("verify_credentials: пустой логин после нормализации — "
                        "CalDAV не может аутентифицировать запрос")
        elif len(login) < 3 or not re.fullmatch(r"[A-Za-z0-9._-]+", login):
            log.warning("verify_credentials: логин %r выглядит не как логин Яндекса", login)
        if looks_like_account_password(app_password):
            log.warning("verify_credentials %s: ввод НЕ похож на app-пароль "
                        "(длина != 12 или пробелы) — вероятно, введён обычный "
                        "пароль аккаунта", login)
        else:
            log.warning("verify_credentials %s: app-пароль корректной длины, "
                        "но Яндекс его отвергает (%s) — просрочен/отозван или "
                        "создан не для сервиса «Другие»", login, r.status_code)
        if base != DEFAULT_CAL_BASE:
            log.warning("verify_credentials: проверка идёт против нестандартной базы %r", base)
    return r.status_code in (200, 207), r.status_code


def credential_hint(login: str, app_password: str, code: int,
                    *, expect_email: bool = False) -> str | None:
    """Уточнение к сообщению об ошибке аутентификации (для пользователя).

    Вызывается из роутера сохранения настроек после неудачной verify_credentials.
    ВАЖНО: expect_email=True означает, что в login уже лежит ИСХОДНЫЙ ввод
    пользователя (до нормализации) — тогда признак «почта вместо логина» ещё
    видно. При вызове с нормализованным логином доменную часть проверять
    бессмысленно (@ там не останется), поэтому флаг по умолчанию False.
    """
    if code not in (401, 403):
        return None
    if looks_like_account_password(app_password):
        return ("Введённый пароль не похож на app-пароль Яндекса (пароль приложений "
                "— это одна строка ровно из 12 символов без пробелов). Скорее всего, "
                "скопирован обычный пароль от аккаунта или ввод обрезал/добавил "
                "пробелы. Создайте пароль на id.yandex.ru → «Пароли приложений» → "
                "сервис «Другие» и вставьте его целиком.")
    ls = (login or "").strip()
    if not ls:
        return "Поле «Логин CalDAV» пустое — укажите логин аккаунта Яндекса."
    if expect_email and "@" in ls:
        return ("Логин указан с доменной частью («почта»). Для CalDAV нужен логин "
                "аккаунта Яндекса без @домена: ivan@yandex.ru → ivan.")
    # Формальных признаков ошибки ввода нет: пароль похож на app-пароль, логин
    # без домена, но Яндекс всё равно 401/403. Типичные оставшиеся причины —
    # пароль создан для другого сервиса (не «Другие»), отозван или это старый
    # пароль; проверить это со стороны приложения невозможно.
    return ("Если логин и пароль точно верны — создайте НОВЫЙ пароль приложений "
            "для сервиса «Другие» на id.yandex.ru и сохраните его ещё раз: "
            "app-пароль выдаётся один раз и не восстанавливается, старые Яндекс "
            "может отзывать при смене пароля аккаунта.")
