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

CALDAV_BASE = "https://dav.yandex.ru/calendar"
NS_DAV = {"d": "DAV:"}


class CalDavError(Exception):
    pass


def _auth(user_email: str, app_password: str) -> tuple[str, str]:
    return (user_email, app_password)


async def ensure_calendar(client: httpx.AsyncClient, email: str, apw: str,
                          cal_name: str = "Цезарь") -> str:
    """Возвращает путь коллекции (calendars/<name>/), создаёт если нет."""
    base = f"{CALDAV_BASE}/"
    r = await client.request("PROPFIND", base, headers={"Depth": "0", "Authorization": _basic(email, apw)},
                             content=_propfind(["getctag"]))
    # ищем существующую коллекцию по имени
    href = None
    if r.status_code in (207, 200):
        root = ET.fromstring(r.text)
        for resp in root.findall(".//{DAV:}response"):
            for st in resp.findall(".//{DAV:}status"):
                if "200" in (st.text or "") or "OK" in (st.text or ""):
                    h = resp.find("{DAV:}href")
                    if h is not None and cal_name.lower() in h.text.lower():
                        href = h.text
                        break
    if href:
        return href
    mk = await client.request("MKCOL", base + httpx.QueryParams({"name": ""}).encode().decode() if False else base + _q(cal_name) + "/",
                              headers={"Authorization": _basic(email, apw)})
    if mk.status_code not in (201, 405):
        raise CalDavError(f"Не удалось создать календарь '{cal_name}': {mk.status_code}")
    return f"{base}{_q(cal_name)}/"


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


async def verify_credentials(email: str, app_password: str) -> bool:
    """Проверка app-пароля при сохранении настроек (PUT /api/profile/calendar)."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.request("PROPFIND", CALDAV_BASE + "/",
                                 headers={"Depth": "0", "Authorization": _basic(email, app_password),
                                          "Content-Type": 'application/xml'})
    return r.status_code in (200, 207)
