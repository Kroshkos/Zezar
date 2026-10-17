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
import xml.etree.ElementTree as ET

import httpx

from app.core.security import decrypt_secret

log = logging.getLogger("caesar.caldav")

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


async def ensure_calendar(client: httpx.AsyncClient, email: str, apw: str,
                          cal_name: str = "Цезарь") -> str:
    """Возвращает URL WebDAV-папки для .ics-файлов, создаёт её если нет.

    Папка лежит внутри «календарной» области (/calendar/), чтобы события
    попадали именно в Яндекс.Календарь, а не в обычный диск.
    """
    base = caldav_base() + "/"
    cal_root = base + "calendar/"
    folder_url = cal_root + _q(cal_name) + "/"
    # уже существует?
    r = await client.request("PROPFIND", folder_url,
                             headers={"Depth": "0", "Authorization": _basic(email, apw)},
                             content=_propfind(["getctag"]))
    if r.status_code in (207, 200):
        return folder_url
    if r.status_code in (401, 403):
        raise CalDavError(f"WebDAV отклонил учётные данные: HTTP {r.status_code}")
    # создаём папку (405 = уже существует — гонка, ок)
    mk = await client.request("MKCOL", folder_url,
                              headers={"Authorization": _basic(email, apw)})
    if mk.status_code not in (201, 204, 405):
        # фолбэк: пишем прямо в /calendar/ (дефолтный календарь Яндекса)
        rr = await client.request("PROPFIND", cal_root,
                                  headers={"Depth": "0", "Authorization": _basic(email, apw)},
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


def _q(name: str) -> str:
    import urllib.parse
    return urllib.parse.quote(name)


def _propfind(props: list[str]) -> bytes:
    body = '<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop>' + \
        "".join(f"<d:{p}/>" for p in props) + "</d:prop></d:propfind>"
    return body.encode()


def _window(ent) -> tuple[str, str]:
    """Кортеж (DTSTART, DTEND) без префиксов TZID.

    Совещания: точное время из payload.start_at/end_at (или date+time_);
    остальные сущности: 09:00–18:00 дня срока (due_date).
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
            return s, e
    due = ent.due_date  # 'YYYY-MM-DD'
    dt = due.replace("-", "")
    return f"{dt}T090000", f"{dt}T180000"


def ics_for_entity(ent, responsible_name: str) -> str:
    """UID стабильный (cal_uid) — повторные записи обновляют событие."""
    tzid = "Europe/Moscow"
    s, e = _window(ent)
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
    apw = decrypt_secret(apw_enc)
    if not apw:
        return False
    url = cal_path.rstrip("/") + "/" + _q(ent.cal_uid) + ".ics"
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.put(url, content=ics_for_entity(ent, responsible_name).encode(),
                             headers={"Content-Type": "text/calendar; charset=utf-8",
                                      "Authorization": _basic(email, apw)})
    ok = r.status_code in (201, 204)
    if not ok:
        log.warning("CalDAV PUT %s -> %s %s", url, r.status_code, r.text[:200])
    return ok


async def delete_event(email: str, apw_enc: str, cal_path: str, cal_uid: str) -> bool:
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
    """
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
    return r.status_code in (200, 207), r.status_code
