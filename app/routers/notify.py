"""API уведомлений: полное окно «Интеграции» + административные сценарии/рассылки.

Пользователь (любое окно «Мои интеграции» — теперь полноценная страница /integrations):
  GET    /api/core/notify/scenarios            мои эффективные сценарии
  POST   /api/core/notify/scenarios            создать пользовательский сценарий
  PATCH  /api/core/notify/scenarios/{id}       редактировать (только свои не-базовые)
  DELETE /api/core/notify/scenarios/{id}       удалить (только свои не-базовые)
  PUT    /api/core/notify/scenarios/{id}/enabled   вкл/выкл для себя (базовые — нельзя)
  POST   /api/core/notify/test                 тестовая отправка себе в Telegram

Администрирование (can_admin_system):
  базовые сценарии:     GET/POST/PATCH/DELETE /api/core/admin/notify/base-scenarios
  шаблоны рассылок:     GET/POST/PATCH/DELETE /api/core/admin/notify/templates
  рассылка:             GET/POST /api/core/admin/notify/broadcast
                        POST /api/core/admin/notify/broadcast/{id}/cancel
                        GET  /api/core/admin/notify/outbox   (очередь доставки)
  проверка доставки:    GET  /api/core/admin/notify/status   (настроен ли бот, счётчики)
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from app.core.timezone import MSK_TZ, now

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import notify
from integrations import telegram_service as tg
from app.core.access import build_context
from app.core.config import get_settings
from app.core.events import log_event
from app.core.security import get_current_user
from app.deps import session_dep
from app.models.core import (
    Department, DepartmentMember, EntityKind, Notification, NotifyScenario,
    NotifyTemplate, OutboxEvent, Role, User, UserNotifyPref,
)

router = APIRouter()

_EVENT_KEYS = {"assigned", "executor_added", "status_changed", "due_soon",
               "overdue", "created", "updated", "shared", "done", "custom"}
_VISIBILITIES = {"private", "department", "global"}


class ScenarioIn(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    description: str | None = None
    event_key: str = Field(min_length=2, max_length=50)
    entity_kinds: list[str] = []
    module_keys: list[str] = []
    template: str = ""
    visibility: str = "private"
    department_id: int | None = None


class EnabledIn(BaseModel):
    enabled: bool


class BaseScenarioIn(ScenarioIn):
    is_active: bool = True


class TemplateIn(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    title: str = Field(min_length=1, max_length=300)
    body: str = ""


class AudienceIn(BaseModel):
    mode: str                      # all | users | department | heads
    user_ids: list[int] = []
    department_id: int | None = None
    include_head: bool = True


class MediaIn(BaseModel):
    file_id: str = Field(min_length=5)   # Telegram file_id, загруженный ботом
    type: str = "photo"                  # photo | video
    caption: str | None = None


class BroadcastIn(BaseModel):
    title: str = Field(min_length=2, max_length=300)
    body: str = Field(min_length=1)
    send_at: datetime              # naive = локальное время сервера трактуем как UTC
    audience: AudienceIn
    template_id: int | None = None
    media: list[MediaIn] = []      # опциональные фото/видео вложения (file_id из TG)

    @field_validator("media")
    @classmethod
    def _check_media(cls, v: list[MediaIn]):
        if len(v) > 10:
            raise ValueError("Не более 10 вложений")
        for m in v:
            if m.type not in ("photo", "video"):
                raise ValueError(f"Недопустимый тип вложения: {m.type}")
        return v


def _validate_scenario_body(b: ScenarioIn):
    if b.event_key not in _EVENT_KEYS:
        raise HTTPException(422, f"Неизвестный тип события: {b.event_key}")
    valid_kinds = {k.value for k in EntityKind}
    bad = [k for k in b.entity_kinds if k not in valid_kinds]
    if bad:
        raise HTTPException(422, f"Неизвестные виды сущностей: {bad}")
    if b.visibility not in _VISIBILITIES:
        raise HTTPException(422, "visibility: private|department|global")
    unknown_ph = set(re.findall(r"\{(\w+)\}", b.template)) - {
        "title", "kind", "kind_label", "module", "action", "changed",
        "due_date", "actor", "link"}
    if unknown_ph and b.template:
        raise HTTPException(422, f"Неизвестные плейсхолдеры в шаблоне: {sorted(unknown_ph)}")


async def _require_admin(session, user):
    ctx = await build_context(session, user)
    if not ctx.can_admin_system:
        raise HTTPException(403, "Только администратор/координатор")
    return ctx


# ========================= пользовательская часть =========================

@router.get("/notify/scenarios")
async def my_scenarios(user: User = Depends(get_current_user),
                       session: AsyncSession = Depends(session_dep)):
    return {"scenarios": await notify.scenarios_for_user(session, user),
            "event_keys": sorted(_EVENT_KEYS),
            "entity_kinds": [k.value for k in EntityKind],
            "telegram_linked": bool(user.telegram_chat_id),
            "bot_username": get_settings().telegram_bot_username or ""}


@router.post("/notify/scenarios")
async def create_scenario(body: ScenarioIn, request=None,
                          user: User = Depends(get_current_user),
                          session: AsyncSession = Depends(session_dep)):
    _validate_scenario_body(body)
    dept_id = None
    if body.visibility == "department":
        from app.models.core import DepartmentMember as DM
        mine = list(await session.scalars(select(DM.department_id).where(DM.user_id == user.id)))
        dept_id = body.department_id if (body.department_id in mine) else (mine[0] if mine else None)
        if dept_id is None:
            raise HTTPException(422, "Вы не состоите ни в одном подразделении")
    scn = NotifyScenario(name=body.name, description=body.description,
                         event_key=body.event_key, entity_kinds=body.entity_kinds,
                         module_keys=body.module_keys, template=body.template,
                         is_base=False, created_by=user.id,
                         visibility=body.visibility, department_id=dept_id,
                         is_active=True)
    session.add(scn)
    await session.commit()
    return {"ok": True, "id": scn.id}


@router.patch("/notify/scenarios/{scn_id}")
async def edit_scenario(scn_id: int, body: ScenarioIn,
                        user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(session_dep)):
    scn = await session.get(NotifyScenario, scn_id)
    if not scn:
        raise HTTPException(404, "Сценарий не найден")
    if scn.is_base or scn.created_by != user.id:
        raise HTTPException(403, "Можно редактировать только собственные пользовательские сценарии")
    _validate_scenario_body(body)
    scn.name, scn.description = body.name, body.description
    scn.event_key, scn.entity_kinds, scn.module_keys = body.event_key, body.entity_kinds, body.module_keys
    scn.template, scn.visibility = body.template, body.visibility
    await session.commit()
    return {"ok": True}


@router.delete("/notify/scenarios/{scn_id}")
async def del_scenario(scn_id: int, user: User = Depends(get_current_user),
                       session: AsyncSession = Depends(session_dep)):
    scn = await session.get(NotifyScenario, scn_id)
    if not scn:
        raise HTTPException(404, "Сценарий не найден")
    if scn.is_base or scn.created_by != user.id:
        raise HTTPException(403, "Можно удалять только собственные пользовательские сценарии")
    await session.delete(scn)
    await session.commit()
    return {"ok": True}


@router.put("/notify/scenarios/{scn_id}/enabled")
async def toggle_scenario(scn_id: int, body: EnabledIn,
                          user: User = Depends(get_current_user),
                          session: AsyncSession = Depends(session_dep)):
    scn = await session.get(NotifyScenario, scn_id)
    if not scn:
        raise HTTPException(404, "Сценарий не найден")
    if scn.is_base:
        raise HTTPException(422, "Базовый сценарий администратора нельзя отключить")
    pref = await session.scalar(select(UserNotifyPref).where(
        UserNotifyPref.user_id == user.id, UserNotifyPref.scenario_id == scn_id))
    if pref is None:
        session.add(UserNotifyPref(user_id=user.id, scenario_id=scn_id, enabled=body.enabled))
    else:
        pref.enabled = body.enabled
    await session.commit()
    return {"ok": True, "enabled": body.enabled}


@router.post("/notify/test")
async def test_notify(user: User = Depends(get_current_user),
                      session: AsyncSession = Depends(session_dep)):
    """Проверка, что система реально отправляет: ставим telegram.manual-событие
    лично на пользователя (воркер доставит немедленно)."""
    if not user.telegram_chat_id:
        raise HTTPException(422, "Telegram не привязан — сначала привяжите аккаунт в этом окне")
    s = get_settings()
    if not s.telegram_bot_token:
        raise HTTPException(503, "Бот не настроен на сервере (TELEGRAM_BOT_TOKEN пуст)")
    session.add(OutboxEvent(topic="telegram.test", payload_json={"user_id": user.id}))
    await session.commit()
    return {"ok": True, "note": "Тестовое сообщение поставлено в очередь — придёт в течение ~5 секунд"}


# ========================= администрирование =========================

@router.get("/admin/notify/base-scenarios")
async def admin_base_scenarios(user: User = Depends(get_current_user),
                               session: AsyncSession = Depends(session_dep)):
    await _require_admin(session, user)
    rows = (await session.scalars(select(NotifyScenario)
            .where(NotifyScenario.is_base.is_(True)).order_by(NotifyScenario.name))).all()
    return {"scenarios": [{
        "id": r.id, "name": r.name, "description": r.description or "",
        "event_key": r.event_key, "entity_kinds": r.entity_kinds or [],
        "module_keys": r.module_keys or [], "template": r.template or "",
        "is_active": bool(r.is_active)} for r in rows],
        "event_keys": sorted(_EVENT_KEYS), "entity_kinds": [k.value for k in EntityKind]}


@router.post("/admin/notify/base-scenarios")
async def admin_create_base(body: BaseScenarioIn, user: User = Depends(get_current_user),
                            session: AsyncSession = Depends(session_dep)):
    await _require_admin(session, user)
    _validate_scenario_body(body)
    scn = NotifyScenario(name=body.name, description=body.description,
                         event_key=body.event_key, entity_kinds=body.entity_kinds,
                         module_keys=body.module_keys, template=body.template,
                         is_base=True, visibility="global", created_by=user.id,
                         is_active=body.is_active)
    session.add(scn)
    await log_event(session, "notify_base_scenario_create", category="admin", user=user,
                    object=f"scenario:{body.name}", message=f"Создан базовый сценарий «{body.name}»")
    await session.commit()
    return {"ok": True, "id": scn.id}


class BaseScenarioPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    event_key: str | None = None
    entity_kinds: list[str] | None = None
    module_keys: list[str] | None = None
    template: str | None = None
    visibility: str = "global"
    department_id: int | None = None
    is_active: bool | None = None


@router.patch("/admin/notify/base-scenarios/{scn_id}")
async def admin_edit_base(scn_id: int, body: BaseScenarioPatch,
                          user: User = Depends(get_current_user),
                          session: AsyncSession = Depends(session_dep)):
    await _require_admin(session, user)
    scn = await session.get(NotifyScenario, scn_id)
    if not scn or not scn.is_base:
        raise HTTPException(404, "Базовый сценарий не найден")
    merged = ScenarioIn(
        name=body.name if body.name is not None else scn.name,
        description=body.description if body.description is not None else scn.description,
        event_key=body.event_key if body.event_key is not None else scn.event_key,
        entity_kinds=body.entity_kinds if body.entity_kinds is not None else (scn.entity_kinds or []),
        module_keys=body.module_keys if body.module_keys is not None else (scn.module_keys or []),
        template=body.template if body.template is not None else (scn.template or ""),
        visibility="global", department_id=None)
    _validate_scenario_body(merged)
    scn.name, scn.description = merged.name, merged.description
    scn.event_key, scn.entity_kinds, scn.module_keys = merged.event_key, merged.entity_kinds, merged.module_keys
    scn.template = merged.template
    if body.is_active is not None:
        scn.is_active = body.is_active
    await log_event(session, "notify_base_scenario_edit", category="admin", user=user,
                    object=f"scenario:{scn.name}", message=f"Изменён базовый сценарий «{scn.name}»")
    await session.commit()
    return {"ok": True}


@router.delete("/admin/notify/base-scenarios/{scn_id}")
async def admin_del_base(scn_id: int, user: User = Depends(get_current_user),
                         session: AsyncSession = Depends(session_dep)):
    await _require_admin(session, user)
    scn = await session.get(NotifyScenario, scn_id)
    if not scn or not scn.is_base:
        raise HTTPException(404, "Базовый сценарий не найден")
    await session.delete(scn)
    await log_event(session, "notify_base_scenario_delete", category="admin", user=user,
                    object=f"scenario:{scn.name}", message=f"Удалён базовый сценарий «{scn.name}»")
    await session.commit()
    return {"ok": True}


# ---- шаблоны рассылок ----

@router.get("/admin/notify/templates")
async def list_templates(user: User = Depends(get_current_user),
                         session: AsyncSession = Depends(session_dep)):
    await _require_admin(session, user)
    rows = (await session.scalars(select(NotifyTemplate).order_by(NotifyTemplate.name))).all()
    return {"templates": [{"id": r.id, "name": r.name, "title": r.title,
                           "body": r.body or ""} for r in rows]}


@router.post("/admin/notify/templates")
async def create_template(body: TemplateIn, user: User = Depends(get_current_user),
                          session: AsyncSession = Depends(session_dep)):
    await _require_admin(session, user)
    t = NotifyTemplate(name=body.name, title=body.title, body=body.body, created_by=user.id)
    session.add(t)
    await session.commit()
    return {"ok": True, "id": t.id}


@router.patch("/admin/notify/templates/{tid}")
async def edit_template(tid: int, body: TemplateIn, user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(session_dep)):
    await _require_admin(session, user)
    t = await session.get(NotifyTemplate, tid)
    if not t:
        raise HTTPException(404, "Шаблон не найден")
    t.name, t.title, t.body = body.name, body.title, body.body
    await session.commit()
    return {"ok": True}


@router.delete("/admin/notify/templates/{tid}")
async def del_template(tid: int, user: User = Depends(get_current_user),
                       session: AsyncSession = Depends(session_dep)):
    await _require_admin(session, user)
    t = await session.get(NotifyTemplate, tid)
    if not t:
        raise HTTPException(404, "Шаблон не найден")
    await session.delete(t)
    await session.commit()
    return {"ok": True}


# ---- рассылка ----

async def _audience_users(session: AsyncSession, aud: AudienceIn) -> list[User]:
    if aud.mode == "all":
        cond = [User.is_active.is_(True)]
    elif aud.mode == "users":
        if not aud.user_ids:
            raise HTTPException(422, "Не выбраны пользователи")
        cond = [User.id.in_(aud.user_ids), User.is_active.is_(True)]
    elif aud.mode == "department":
        if not aud.department_id:
            raise HTTPException(422, "Не указано подразделение")
        q = select(DepartmentMember.user_id).where(
            DepartmentMember.department_id == aud.department_id)
        if not aud.include_head:
            q = q.where(DepartmentMember.role_in_dept != Role.HEAD)
        uid_set = list(await session.scalars(q))
        if not uid_set:
            raise HTTPException(422, "В подразделении нет участников")
        cond = [User.id.in_(uid_set), User.is_active.is_(True)]
    elif aud.mode == "heads":
        head_ids = list(await session.scalars(
            select(DepartmentMember.user_id).where(
                DepartmentMember.role_in_dept == Role.HEAD)))
        if not head_ids:
            raise HTTPException(422, "Руководители подразделений не назначены")
        cond = [User.id.in_(head_ids), User.is_active.is_(True)]
    else:
        raise HTTPException(422, "mode: all|users|department|heads")
    return list((await session.scalars(select(User).where(*cond))).all())


@router.get("/admin/notify/audience-preview")
async def audience_preview(department_id: int | None = None, mode: str = "all",
                           user: User = Depends(get_current_user),
                           session: AsyncSession = Depends(session_dep)):
    """Счётчик получателей для формы (не раскрывает списки без необходимости)."""
    await _require_admin(session, user)
    aud = AudienceIn(mode=mode, department_id=department_id)
    users = await _audience_users(session, aud)
    linked = sum(1 for u in users if u.telegram_chat_id)
    return {"total": len(users), "telegram_linked": linked}


@router.post("/admin/notify/upload-media")
async def upload_broadcast_media(file: UploadFile = File(...),
                                 caption: str | None = Form(None),
                                 user: User = Depends(get_current_user),
                                 session: AsyncSession = Depends(session_dep)):
    """Загрузка фото/видео для рассылки из веб-админки.

    Файл пересылается ботом в чат администратора (sendPhoto/sendVideo) — так
    получается постоянный file_id того же бота, которым затем доставляем
    вложения всем получателям (своё хранилище не нужно). Без привязанного
    Telegram у пользователя медиа для рассылок недоступно.
    """
    await _require_admin(session, user)
    if not user.telegram_chat_id:
        raise HTTPException(422, "У вас не привязан Telegram — пришлите файл боту или привяжите аккаунт")
    mime = file.content_type or ""
    if mime.startswith("image/"):
        mtype, tg_method = "photo", "sendPhoto"
    elif mime.startswith("video/"):
        mtype, tg_method = "video", "sendVideo"
    else:
        raise HTTPException(422, f"Поддерживаются только фото и видео (получено: {mime or 'unknown'})")
    data = await file.read()
    max_mb = 50 if mtype == "video" else 10
    if len(data) > max_mb * 1024 * 1024:
        raise HTTPException(422, f"Файл больше {max_mb} МБ")
    result = await tg.upload_media_to_chat(int(user.telegram_chat_id), data,
                                           file.filename or mtype, mime)
    if result is None:
        raise HTTPException(502, "Telegram не принял файл. Попробуйте другой формат.")
    msg = result.get("result") or {}
    if mtype == "photo":
        photos = msg.get("photo") or []
        fid = photos[-1]["file_id"] if photos else ""
        size = photos[-1].get("file_size", len(data)) if photos else len(data)
    else:
        vid = msg.get("video") or {}
        fid = vid.get("file_id", "")
        size = vid.get("file_size", len(data))
    if not fid:
        raise HTTPException(502, "Не удалось получить file_id ответа Telegram")
    await log_event(session, "notify_media_upload", category="admin", user=user,
                    object=f"media:{fid[:24]}",
                    message=f"Загружено вложение {mtype} «{file.filename}» ({size} Б)")
    await session.commit()
    return {"ok": True, "file_id": fid, "type": mtype, "caption": caption or "",
            "size": size, "name": file.filename}


@router.post("/admin/notify/broadcast")
async def create_broadcast(body: BroadcastIn, user: User = Depends(get_current_user),
                           session: AsyncSession = Depends(session_dep)):
    await _require_admin(session, user)
    users = await _audience_users(session, body.audience)
    if not users:
        raise HTTPException(422, "Нет получателей")
    send_at = body.send_at
    if send_at.tzinfo is not None:
        # aware (напр. из браузера с +03:00) -> наивно по Москве: колонки
        # DateTime платформы хранят локальное время MSK (app/core/timezone.py)
        send_at = send_at.astimezone(MSK_TZ).replace(tzinfo=None)
    title, text_body = body.title, body.body
    # применение шаблона рассылки (если выбран): подставляем плейсхолдеры {name} {login}
    tpl: NotifyTemplate | None = None
    if body.template_id:
        tpl = await session.get(NotifyTemplate, body.template_id)
        if tpl is None:
            raise HTTPException(422, "Шаблон рассылки не найден")
        title = title or tpl.title
        text_body = text_body or tpl.body
    n = Notification(title=title, body=text_body, send_at=send_at,
                     audience_json=body.audience.model_dump(),
                     status="scheduled", created_by=user.id,
                     recipients_count=len(users),
                     media_json=[m.model_dump() for m in body.media] or None)
    session.add(n)
    await session.flush()
    if send_at <= now():  # сравнение в наивном времени MSK (см. app/core/timezone.py)
        await notify.enqueue_manual(session, n)
        n.status = "sending"
    await log_event(session, "notify_broadcast", category="admin", user=user,
                    object=f"notification:{n.id}",
                    message=f"Рассылка «{n.title}» → {len(users)} получателей, "
                            f"старт {send_at.isoformat(timespec='minutes')}"
                            + (f", шаблон «{tpl.name}»" if tpl else ""))
    await session.commit()
    return {"ok": True, "id": n.id, "recipients": len(users),
            "immediate": n.status == "sending",
            "media": len(body.media),
            "send_at": send_at.isoformat(timespec="minutes")}


@router.get("/admin/notify/broadcast")
async def list_broadcasts(user: User = Depends(get_current_user),
                          session: AsyncSession = Depends(session_dep)):
    await _require_admin(session, user)
    rows = (await session.scalars(select(Notification)
            .order_by(Notification.created_at.desc()).limit(100))).all()
    return {"items": [{"id": r.id, "title": r.title, "body": (r.body or "")[:300],
                       "send_at": r.send_at.isoformat(timespec="minutes"),
                       "status": r.status, "audience": r.audience_json,
                       "media": len(r.media_json or []),
                       "recipients": r.recipients_count, "delivered": r.delivered_count,
                       "sent_at": r.sent_at.isoformat(timespec="seconds") if r.sent_at else None}
                      for r in rows]}


@router.post("/admin/notify/broadcast/{nid}/cancel")
async def cancel_broadcast(nid: int, user: User = Depends(get_current_user),
                           session: AsyncSession = Depends(session_dep)):
    await _require_admin(session, user)
    n = await session.get(Notification, nid)
    if not n:
        raise HTTPException(404, "Рассылка не найдена")
    if n.status != "scheduled":
        raise HTTPException(422, f"Нельзя отменить рассылку в статусе «{n.status}»")
    n.status = "canceled"
    await session.commit()
    return {"ok": True}


@router.get("/admin/notify/outbox")
async def outbox_view(limit: int = 50, user: User = Depends(get_current_user),
                      session: AsyncSession = Depends(session_dep)):
    """Диагностика доставки: последние события очередей telegram.*."""
    await _require_admin(session, user)
    rows = (await session.scalars(
        select(OutboxEvent).where(OutboxEvent.topic.like("telegram.%"))
        .order_by(OutboxEvent.id.desc()).limit(limit))).all()
    return {"items": [{"id": r.id, "at": r.at.isoformat(timespec="seconds"),
                       "topic": r.topic, "status": r.status, "attempts": r.attempts,
                       "error": (r.last_error or "")[:200],
                       "payload": {k: v for k, v in (r.payload_json or {}).items()
                                   if k in ("title", "action", "user_id", "notification_id",
                                            "chat_id", "kind", "link", "entity_uid",
                                            "entity_kind", "event_key")}}
                      for r in rows]}


@router.get("/admin/notify/status")
async def delivery_status(user: User = Depends(get_current_user),
                          session: AsyncSession = Depends(session_dep)):
    """Реально ли система отправляет: конфиг бота + счётчики очередей."""
    await _require_admin(session, user)
    s = get_settings()
    async def cnt(topic: str, done: bool | None = None):
        q = select(func.count(OutboxEvent.id)).where(OutboxEvent.topic.like(f"telegram.{topic}"))
        if done is True:
            q = q.where(OutboxEvent.processed_at.is_not(None), OutboxEvent.status == "done")
        elif done is False:
            q = q.where(OutboxEvent.processed_at.is_(None))
        return await session.scalar(q) or 0
    total_users = await session.scalar(
        select(func.count(User.id)).where(User.is_active.is_(True))) or 0
    linked = await session.scalar(
        select(func.count(User.id)).where(User.is_active.is_(True),
                                          User.telegram_chat_id.is_not(None))) or 0
    return {"bot_configured": bool(s.telegram_bot_token),
            "bot_username": s.telegram_bot_username or "",
            "notify_enabled": bool(s.telegram_notify_enabled),
            "public_base_url": s.public_base_url,
            "users_total": total_users, "users_telegram_linked": linked,
            "pending": await cnt("*", done=False),
            "sent_ok": await cnt("*", done=True),
            "errors": await session.scalar(
                select(func.count(OutboxEvent.id)).where(
                    OutboxEvent.topic.like("telegram.%"), OutboxEvent.status == "error")) or 0}
