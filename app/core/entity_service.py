"""
Сервис мутаций сущностей (ядро). Используется ВСЕМИ модулями-плагинами.

Гарантии на каждую запись:
  - инвариант ответственных (access.ensure_responsibles);
  - AuditLog + OutboxEvent в одной транзакции (Transactional Outbox) ->
    Telegram/Календарь обрабатываются фонами, HTTP не блокируется;
  - entity.sync_state=pending — маркер для diff-прохода календаря.
"""
from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import access
from app.core.events import log_event
from app.models.core import (
    AuditLog, DepartmentMember, Entity, EntityKind, EntityShare, EntityResponsible,
    OutboxEvent, Permission, SyncState, User,
)


_PERM_ORDER = {Permission.READ: 0, Permission.EDIT: 1, Permission.FULL: 2}


def _perm_lt_local(a: Permission, b: Permission) -> bool:
    return _PERM_ORDER[a] < _PERM_ORDER[b]


def _uid() -> str:
    return uuid.uuid4().hex[:12]


async def create_entity(
    session: AsyncSession, user: User, *, kind: EntityKind, module_key: str,
    owner_department_id: int, title: str, description: str | None = None,
    status_key: str | None = None, priority_key: str | None = None,
    due_date: str | None = None, tags: list | None = None,
    extra_json: dict | None = None, responsible_user_id: int | None = None,
    executor_user_ids: list[int] | None = None,
    share_with: list[tuple[int, Permission]] | None = None,
) -> Entity:
    """Создание сущности.

    share_with — межпространственный контекст (например, задача проекта,
    в котором участвуют несколько подразделений): [(dept_id, permission), ...].
    Для каждого участвующего подразделения создаётся свой ответственный/слот
    (инвариант ТЗ «несколько ответственных» реализуется через ensure_responsibles).

    executor_user_ids — исполнители (может быть несколько, как в прототипе).
    Хранятся денормализованно в extra_json['executors']=[{id,name}]; если
    исполнитель состоит в подразделении-владельце, он дополнительно получает
    слот ответственного в этом пространстве (доступ + уведомления).
    """
    # Валидация подразделения-владельца ДО записи: если dept_id отсутствует в
    # departments (например, фронт передал department_id=0 при «своём» выборе),
    # SQLite с включёнными FK откатывал всю транзакцию на первом же flush внутри
    # create_entity — нарушался инвариант «слоты ответственных создаются до
    # шерингов», и последующий INSERT task_payloads падал с
    # FOREIGN KEY constraint failed. Даём понятную 422 вместо 500.
    from app.models.core import Department
    if await session.get(Department, owner_department_id) is None:
        raise ValueError(f"Подразделение не найдено: id={owner_department_id}")
    ent = Entity(
        uid=_uid(), kind=kind, module_key=module_key,
        owner_department_id=owner_department_id, title=title,
        description=description, status_key=status_key, priority_key=priority_key,
        due_date=due_date, tags=tags or [],
        # created_by — автор карточки: используется планировщиком напоминаний
        # (notify.schedule_due_reminders), чтобы не слать автору «сам себе»
        extra_json={**(extra_json or {}), "created_by": user.id},
        sync_state=SyncState.PENDING, cal_uid=f"caesar-{uuid.uuid4().hex[:10]}@app",
    )
    # Исполнители (несколько, как в прототипе): денормализованно в extra_json —
    # без миграции схемы, имена фиксируются на момент назначения (историчность).
    exec_ids = [e for e in dict.fromkeys(executor_user_ids or []) if e]
    if exec_ids:
        users = (await session.scalars(select(User).where(User.id.in_(exec_ids)))).all()
        umap = {u.id: u.display_name for u in users}
        missing = [e for e in exec_ids if e not in umap]
        if missing:
            raise ValueError(f"Исполнители не найдены: {missing}")
        ent.extra_json = {**ent.extra_json,
                          "executors": [{"id": e, "name": umap[e]} for e in exec_ids]}
    # срок проставляем до flush, чтобы он попал и в INSERT, и в payload события
    # (иначе воркер читал due_date='не указан' из pending-объекта)
    if due_date:
        ent.due_date = due_date
    session.add(ent)
    await session.flush()
    # ВАЖНО (FK-инвариант): сначала создаём слоты ответственных для владельца и
    # всех подразделений из share_with, и только затем пишем EntityShare. Иначе
    # access.ensure_responsibles может удалить pending-пустые слоты чужих
    # пространств до commit — на боевой SQLite (включённые FK + откат транзакции)
    # это приводило к IntegrityError: FOREIGN KEY constraint failed при вставке
    # task_payloads сразу после create_entity.
    owner_slot_user_id = responsible_user_id
    external_resp_dept: int | None = None
    if responsible_user_id is not None:
        in_owner = await session.scalar(
            select(DepartmentMember.id).where(
                DepartmentMember.user_id == responsible_user_id,
                DepartmentMember.department_id == owner_department_id))
        if not in_owner:
            owner_slot_user_id = None
            # Подразделение внешнего исполнителя выбираем ДЕТЕРМИНИРОВАННО:
            # 1) если исполнитель входит в share_with — берём именно это
            #    подразделение (иначе у админа/координатора, состоящего в своём
            #    пространстве, служебный слот открывался бы в его родном
            #    департаменте и сущность просачивалась бы туда);
            # 2) иначе — первое по id подразделение исполнителя + служебный шэр.
            share_depts = {d for d, _ in (share_with or []) if d != owner_department_id}
            resp_depts = (await session.scalars(
                select(DepartmentMember.department_id).where(
                    DepartmentMember.user_id == responsible_user_id)
                .order_by(DepartmentMember.id))).all()
            external_resp_dept = next(
                (d for d in resp_depts if d in share_depts),
                resp_depts[0] if resp_depts else None)
    session.add(EntityResponsible(
        entity_id=ent.id, department_id=owner_department_id,
        user_id=owner_slot_user_id, is_primary=True))
    for dept_id, perm in (share_with or []):
        if dept_id != owner_department_id:
            session.add(EntityShare(
                entity_id=ent.id, target_department_id=dept_id,
                permission=perm, granted_by_user_id=user.id))
            # слот принимающей стороны (назначает head; user_id=NULL до назначения)
            has_slot = await session.scalar(
                select(EntityResponsible.id).where(
                    EntityResponsible.entity_id == ent.id,
                    EntityResponsible.department_id == dept_id))
            if not has_slot:
                session.add(EntityResponsible(
                    entity_id=ent.id, department_id=dept_id, user_id=None))
    await session.flush()
    if external_resp_dept is not None:
        # Внешний ответственный: доступ к сущности даётся ТОЛЬКО через явный
        # механизм шеринга (иначе нарушается изоляция пространств). Если
        # принимающая сторона уже указана в share_with — назначаем его в слот
        # этого подразделения; иначе открываем служебный шэр (READ) + слот.
        resp_perm = None
        for d, p in (share_with or []):
            if d == external_resp_dept:
                resp_perm = p
                break
        if resp_perm is not None:
            row = await session.scalar(
                select(EntityResponsible).where(
                    EntityResponsible.entity_id == ent.id,
                    EntityResponsible.department_id == external_resp_dept))
            if row is None:
                row = EntityResponsible(entity_id=ent.id,
                                        department_id=external_resp_dept)
                session.add(row)
                await session.flush()
            row.user_id = responsible_user_id
            if _perm_lt_local(resp_perm, Permission.EDIT):
                # ответственный принимающей стороны >= EDIT (см. access.resolve_permission)
                sh = await session.scalar(
                    select(EntityShare).where(
                        EntityShare.entity_id == ent.id,
                        EntityShare.target_department_id == external_resp_dept))
                if sh:
                    sh.permission = Permission.EDIT
        else:
            session.add(EntityShare(
                entity_id=ent.id, target_department_id=external_resp_dept,
                permission=Permission.READ, granted_by_user_id=user.id))
            session.add(EntityResponsible(
                entity_id=ent.id, department_id=external_resp_dept,
                user_id=responsible_user_id))
    await access.ensure_responsibles(session, ent)
    # Исполнители из подразделения-владельца получают слот ответственного в этом
    # пространстве (доступ + уведомления). Слот владельца занят «основным» —
    # освобождаем его только если явного responsible не задавали (тогда основным
    # становится первый исполнитель); иначе исполнители занимают свободные слоты
    # принимающих подразделений (уже расшаренных выше) либо игнорируются как
    # purely-информационные (полный список живёт в extra_json.executors).
    for e in exec_ids:
        if e == responsible_user_id:
            continue
        slot = await session.scalar(
            select(EntityResponsible).where(
                EntityResponsible.entity_id == ent.id,
                EntityResponsible.user_id.is_(None),
                EntityResponsible.department_id.in_(
                    select(DepartmentMember.department_id).where(
                        DepartmentMember.user_id == e))))
        if slot is not None:
            slot.user_id = e
    await _log_only(session, user, "create", ent,
                    {"title": title, "responsible_user_id": responsible_user_id,
                     "executors": exec_ids,
                     "shared_with": [d for d, _ in (share_with or [])]})
    # Адресные уведомления вместо широкого 'create'-события: ранее событие
    # create доставлялось ВСЕМ участникам (включая автора — спам), а адресат
    # мог его не получить (фильтр по enabled_event_keys). Теперь:
    #  - назначенный ответственный получает 'assigned';
    #  - каждый исполнитель получает 'executor_added';
    #  - остальные участники (слоты подразделений из share_with) — 'created'.
    # ВАЖНО: автор НЕ исключается, если он явно указан ответственным или
    # исполнителем («задача на себе» — штатная ситуация: пользователю нужно
    # уведомление о назначении в своей же карточке; молчаливый пропуск
    # «сам себе» лишал таких пользователей всех уведомлений). Анти-спам
    # фильтр воркера снимает широкие события от автора, но адресные
    # назначения сохраняются.
    targets_resp = [r for r in {responsible_user_id} | set(exec_ids) if r]
    if targets_resp:
        await _emit_assigned(session, user, ent, targets_resp)
        # мобильный контур: лента + push «на вас назначено» (см. app/core/mobile_notify)
        from app.core.mobile_notify import emit_mobile_event
        for t in targets_resp:
            await emit_mobile_event(
                session, user_id=t, type_="assigned",
                title=f"Вас назначили: {ent.title}", body=ent.description or None,
                entity_uid=ent.uid, module_key=ent.module_key,
                payload={"kind": ent.kind.value, "due_date": ent.due_date})
    others = [uid for uid in (await _recipients_ids(session, ent))
              if uid and uid != user.id and uid not in targets_resp]
    if others:
        from app.core.notify import link_for
        session.add(OutboxEvent(topic="telegram.notify", payload_json={
            "action": "create", "event_keys": ["created"],
            "entity_id": ent.id, "entity_uid": ent.uid,
            "kind": ent.kind.value, "module": ent.module_key, "title": ent.title,
            "actor": user.display_name, "actor_user_id": user.id,
            "details": {"note": f"Создана новая сущность ({ent.module_key})"},
            "link": link_for(ent.uid, ent.kind.value),
            "target_user_ids": others,
        }))
    return ent


async def set_executors(
    session: AsyncSession, user: User, ent: Entity, executor_user_ids: list[int],
    *, notify: bool = True, actor_user_id: int | None = None,
) -> None:
    """Обновление списка исполнителей сущности (может быть несколько).

    Синхронизирует денормализованный extra_json['executors'] и слоты
    ответственных внутри подразделений, которым сущность принадлежит/расшарена.

    notify=False — не ставить событие в outbox (используется при создании
    сущности: исполнитель уже уведомляется событием 'create', иначе дубль).
    actor_user_id — пользователь, ЧЬЁ назначение обрабатывается: уведомления о
    назначении получают только новые исполнители (не тот, кто сам себя назначил).
    """
    prev_extra = {e.get("id") for e in ((ent.extra_json or {}).get("executors") or [])
                  if isinstance(e, dict) and e.get("id")}
    ids = [e for e in dict.fromkeys(executor_user_ids or []) if e]
    users = (await session.scalars(select(User).where(User.id.in_(ids)))).all() if ids else []
    umap = {u.id: u.display_name for u in users}
    missing = [e for e in ids if e not in umap]
    if missing:
        raise ValueError(f"Исполнители не найдены: {missing}")
    ent.extra_json = {**(ent.extra_json or {}),
                      "executors": [{"id": e, "name": umap[e]} for e in ids]}
    await session.flush()  # executors видны воркеру из extra_json (слот может не создаться)
    # освобождаем неиспользуемые слоты, занимаемые бывшими исполнителями
    slots = (await session.scalars(
        select(EntityResponsible).where(
            EntityResponsible.entity_id == ent.id,
            EntityResponsible.is_primary == False))).all()  # noqa: E712
    for s in slots:
        if s.user_id and s.user_id not in ids and s.user_id != (
                await session.scalar(
                    select(EntityResponsible.user_id).where(
                        EntityResponsible.entity_id == ent.id,
                        EntityResponsible.is_primary == True))):  # noqa: E712
            s.user_id = None
    for e in ids:
        occupied = await session.scalar(
            select(EntityResponsible.id).where(
                EntityResponsible.entity_id == ent.id,
                EntityResponsible.user_id == e))
        if occupied:
            continue
        slot = await session.scalar(
            select(EntityResponsible).where(
                EntityResponsible.entity_id == ent.id,
                EntityResponsible.user_id.is_(None),
                EntityResponsible.department_id.in_(
                    select(DepartmentMember.department_id).where(
                        DepartmentMember.user_id == e))))
        if slot is not None:
            slot.user_id = e
    ent.sync_state = SyncState.PENDING
    if notify:
        # событие 'update' (event_key=updated) — базовые сценарии на него не
        # подписаны; отдельно ставим 'assigned'/'executor_added' НОВЫМ
        # исполнителям (кроме самого инициатора), чтобы они реально получили
        # уведомление о назначении.
        await _log_only(session, user, "update", ent, {"changed": {"executors": ids}})
        # Автор может быть исполнителем своей же сущности («назначение на
        # себя») — уведомление о назначении ему положено; из события
        # исключается только пользователь, ЧЬЁ назначение обрабатывается
        # (actor_user_id), чтобы не спамить самому себе при самомоназначении.
        new_ids = [e for e in ids if e not in prev_extra and e != actor_user_id]
        if new_ids:
            await _emit_assigned(session, user, ent, new_ids)
    else:
        await _log_and_emit(session, user, "update", ent,
                            {"changed": {"executors": ids}})


async def _recipients_ids(session: AsyncSession, ent: Entity) -> list[int]:
    """Все пользователи-участники сущности (слоты ответственных + исполнители)."""
    ids = set(await session.scalars(
        select(EntityResponsible.user_id).where(
            EntityResponsible.entity_id == ent.id,
            EntityResponsible.user_id.is_not(None))))
    for e in ((ent.extra_json or {}).get("executors") or []):
        if isinstance(e, dict) and e.get("id"):
            ids.add(int(e["id"]))
    return sorted(i for i in ids if i)


async def notify_participants(
    session: AsyncSession, actor: User, ent: Entity, event_key: str, note: str,
) -> None:
    """Адресное уведомление ВСЕМ участникам сущности (включая автора, если он
    сам ответствен/исполнитель — «задача на себе»).

    Используется модулями для событий вне CRUD ядра (например, продвижение
    этапа маршрута): ранее такие изменения писались только в журнал и в
    Яндекс.Календарь, а в Telegram участники не получали ничего.
    """
    from app.core.notify import link_for
    targets = await _recipients_ids(session, ent)
    if not targets:
        return
    session.add(OutboxEvent(topic="telegram.notify", payload_json={
        "action": event_key, "event_keys": [event_key],
        "entity_id": ent.id, "entity_uid": ent.uid,
        "kind": ent.kind.value, "module": ent.module_key, "title": ent.title,
        # author_uid — автор события; воркер исключает его из адресатов только
        # если у него нет слота resp (анти-спам «сам себе» для широких
        # событий). Для участников с слотом уведомление сохраняется.
        "actor": actor.display_name, "actor_user_id": actor.id,
        "details": {"note": note},
        "link": link_for(ent.uid, ent.kind.value),
        "target_user_ids": targets,
    }))
    from app.core.mobile_notify import emit_mobile_event
    for t in targets:
        await emit_mobile_event(
            session, user_id=t, type_="entity_updated",
            title=f"{ent.title}: {note}", body=None,
            entity_uid=ent.uid, module_key=ent.module_key,
            payload={"kind": ent.kind.value, "event": event_key})


async def update_entity(
    session: AsyncSession, user: User, ent: Entity, changes: dict[str, Any],
) -> Entity:
    """changes — только разрешённые поля ядра (payload обновляет сам модуль)."""
    allowed = {"title", "description", "status_key", "priority_key",
               "due_date", "tags", "extra_json"}
    old_status = ent.status_key
    changed: dict[str, Any] = {}
    for k, v in changes.items():
        if k in allowed and getattr(ent, k) != v:
            setattr(ent, k, v)
            changed[k] = v
    if changed:
        ent.sync_state = SyncState.PENDING
        action = "status" if "status_key" in changed else "update"
        # ВАЖНО: extra_json в changes перезаписывает поле целиком — синхронизируем
        # flush'ем до постановки события, иначе воркер прочитает устаревших
        # исполнителей из extra_json['executors'] и адресаты не получат уведомление.
        await session.flush()
        await _log_only(session, user, action, ent,
                        {"changed": changed, "old_status": old_status})
        from app.core.notify import ACTION_EVENT_KEYS, link_for
        targets = [i for i in await _recipients_ids(session, ent) if i != user.id]
        if targets:
            session.add(OutboxEvent(topic="telegram.notify", payload_json={
                "action": action,
                "event_keys": sorted(ACTION_EVENT_KEYS.get(action, {action})),
                "entity_id": ent.id, "entity_uid": ent.uid,
                "kind": ent.kind.value, "module": ent.module_key, "title": ent.title,
                "actor": user.display_name, "actor_user_id": user.id,
                "details": {"changed": changed, "old_status": old_status,
                            "due_date": ent.due_date},
                "link": link_for(ent.uid, ent.kind.value),
                "target_user_ids": targets,
            }))
            # мобильный контур: событие в ленту каждому участнику (кроме актора)
            from app.core.mobile_notify import emit_mobile_event
            _mtype = ("status_changed" if "status_key" in changed
                      else "due_soon" if "due_date" in changed else "entity_updated")
            _verb = ("статус изменён" if "status_key" in changed
                     else "срок изменён" if "due_date" in changed else "карточка обновлена")
            for t in targets:
                await emit_mobile_event(
                    session, user_id=t, type_=_mtype,
                    title=f"{ent.title}: {_verb}",
                    body=f"{user.display_name} → {ent.status_key or ''}".strip(),
                    entity_uid=ent.uid, module_key=ent.module_key,
                    payload={"changed": changed})
        # Календарь: любое изменение сущности (в т.ч. патч статуса/сроков из
        # модулей) требует re-sync события в календарях ответственных — sync_state
        # уже PENDING. Раньше calendar.sync ставился только при create/delete,
        # поэтому update зависал в PENDING навсегда (outbox-контракт E2E).
        session.add(OutboxEvent(topic="calendar.sync", payload_json={
            "entity_id": ent.id, "entity_uid": ent.uid, "reason": action,
        }))
    return ent


async def delete_entity(session: AsyncSession, user: User, ent: Entity) -> None:
    await _log_and_emit(session, user, "delete", ent, {"title": ent.title})
    # Явное удаление зависимых строк ПЕРЕД удалением сущности. ORM-коллекция
    # shares — viewonly (защита от orphan-откатов pending-объектов), поэтому
    # каскад по relationship не срабатывает; на уровне БД включён PRAGMA
    # foreign_keys=ON и часть payload-таблиц ссылается без ondelete=CASCADE.
    from sqlalchemy import delete as _del
    from app.models.module_payloads import (
        DocPayload, GrantPayload, MeetingPayload, PriorityPayload,
        ProjectPayload, ScholarPayload, SoftPayload, TaskPayload)
    for payload_model in (TaskPayload, MeetingPayload, ProjectPayload, DocPayload,
                          SoftPayload, GrantPayload, ScholarPayload, PriorityPayload):
        await session.execute(_del(payload_model).where(payload_model.entity_id == ent.id))
    await session.execute(_del(EntityShare).where(EntityShare.entity_id == ent.id))
    # произвольные связи (EntityLink) — с обеих сторон, иначе FK-ссылка
    # останется на удаляемую сущность
    from app.models.core import EntityLink as _EL
    await session.execute(_del(_EL).where(
        (_EL.entity_a_id == ent.id) | (_EL.entity_b_id == ent.id)))
    await session.delete(ent)


async def assign_responsible(
    session: AsyncSession, user: User, ent: Entity,
    department_id: int, user_id: Optional[int],
) -> None:
    """Назначение ответственного от конкретного подразделения (слот создаётся шерингом).

    Инварианты изоляции рабочих пространств:
      - назначать можно только в подразделение-владельца либо в подразделение,
        которому сущность реально расшарена (EntityShare);
      - назначаемый пользователь обязан состоять в этом подразделении.
    Иначе назначение открывало бы «лазейку» доступа к чужому пространству.
    """
    if user_id is not None:
        member = await session.scalar(
            select(DepartmentMember.id).where(
                DepartmentMember.user_id == user_id,
                DepartmentMember.department_id == department_id))
        if not member:
            raise PermissionError(
                "Ответственный должен состоять в выбранном подразделении")
        participates = department_id == ent.owner_department_id or await session.scalar(
            select(EntityShare.id).where(
                EntityShare.entity_id == ent.id,
                EntityShare.target_department_id == department_id))
        if not participates:
            raise PermissionError(
                "Нельзя назначить ответственного из подразделения, "
                "которому сущность не принадлежит и не расшарена")
    row = await session.scalar(
        select(EntityResponsible).where(
            EntityResponsible.entity_id == ent.id,
            EntityResponsible.department_id == department_id))
    if row is None:
        row = EntityResponsible(
            entity_id=ent.id, department_id=department_id, user_id=user_id,
            is_primary=(department_id == ent.owner_department_id))
        session.add(row)
    else:
        row.user_id = user_id
    # событие 'reassign' (event_key assigned): уведомление — назначенному
    # пользователю, а не всем участникам сущности; автор не уведомляется.
    await session.flush()  # слот с новым user_id виден воркеру из БД
    await _log_only(session, user, "reassign", ent,
                    {"department_id": department_id, "user_id": user_id})
    if user_id is not None and user_id != user.id:
        await _emit_assigned(session, user, ent, [user_id])
        from app.core.mobile_notify import emit_mobile_event
        await emit_mobile_event(
            session, user_id=user_id, type_="assigned",
            title=f"Вас назначили ответственным: {ent.title}",
            body=ent.description or None, entity_uid=ent.uid,
            module_key=ent.module_key,
            payload={"kind": ent.kind.value, "due_date": ent.due_date})


async def share_entity(
    session: AsyncSession, user: User, ent: Entity,
    target_department_id: int, permission: Permission,
) -> None:
    existing = await session.scalar(
        select(EntityShare).where(
            EntityShare.entity_id == ent.id,
            EntityShare.target_department_id == target_department_id))
    if existing:
        existing.permission = permission
    else:
        session.add(EntityShare(
            entity_id=ent.id, target_department_id=target_department_id,
            permission=permission, granted_by_user_id=user.id))
    await access.ensure_responsibles(session, ent)  # слот ответственного принимающей стороны
    await _log_and_emit(session, user, "share", ent,
                        {"target_department_id": target_department_id,
                         "permission": permission.value})


async def unshare_entity(session: AsyncSession, user: User, ent: Entity,
                         target_department_id: int) -> None:
    row = await session.scalar(
        select(EntityShare).where(
            EntityShare.entity_id == ent.id,
            EntityShare.target_department_id == target_department_id))
    if row:
        await session.delete(row)
        await session.flush()
        await access.ensure_responsibles(session, ent)
        await _log_and_emit(session, user, "unshare", ent,
                            {"target_department_id": target_department_id})


async def _log_only(session: AsyncSession, user: User, action: str,
                    ent: Entity, details: dict) -> None:
    """Аудит + общий журнал событий БЕЗ telegram-события (когда уведомление
    ставится адресно через _emit_assigned, чтобы не дублировать)."""
    session.add(AuditLog(user_id=user.id, action=action, entity_id=ent.id,
                         details_json={"kind": ent.kind.value, "uid": ent.uid, **details}))
    await log_event(session, f"entity_{action}", category="entity", user=user,
                    object=f"{ent.kind.value}:{ent.title}"[:200], level="info",
                    message=f"{action}: {ent.kind.value} «{ent.title}» "
                            f"(uid {ent.uid}, модуль {ent.module_key})",
                    details={"entity_uid": ent.uid, "kind": ent.kind.value, **details})


async def _emit_assigned(session: AsyncSession, actor: User, ent: Entity,
                         target_user_ids: list[int]) -> None:
    """Адресное событие 'reassign' (event_key assigned/executor_added) —
    получателем будет каждый из target_user_ids (кроме самого актёра)."""
    from app.core.notify import ACTION_EVENT_KEYS, link_for
    targets = [t for t in dict.fromkeys(target_user_ids or []) if t and t != actor.id]
    if not targets:
        return
    session.add(OutboxEvent(topic="telegram.notify", payload_json={
        "action": "reassign",
        "event_keys": sorted(ACTION_EVENT_KEYS.get("reassign", {"assigned"})),
        "entity_id": ent.id, "entity_uid": ent.uid,
        "kind": ent.kind.value, "module": ent.module_key, "title": ent.title,
        "actor": actor.display_name, "actor_user_id": actor.id,
        "details": {"due_date": ent.due_date},
        "link": link_for(ent.uid, ent.kind.value),
        "target_user_ids": targets,
    }))


async def _log_and_emit(session: AsyncSession, user: User, action: str,
                        ent: Entity, details: dict) -> None:
    session.add(AuditLog(user_id=user.id, action=action, entity_id=ent.id,
                         details_json={"kind": ent.kind.value, "uid": ent.uid, **details}))
    # Дублирование в общий журнал событий системы (EventLog): виден в админке
    # вместе с действиями пользователей/админа, а не только по конкретной сущности.
    await log_event(session, f"entity_{action}", category="entity", user=user,
                    object=f"{ent.kind.value}:{ent.title}"[:200], level="info",
                    message=f"{action}: {ent.kind.value} «{ent.title}» "
                            f"(uid {ent.uid}, модуль {ent.module_key})",
                    details={"entity_uid": ent.uid, "kind": ent.kind.value, **details})
    # события интеграций (воркеры заберут после commit'а)
    from app.core.notify import ACTION_EVENT_KEYS, link_for
    session.add(OutboxEvent(topic="telegram.notify", payload_json={
        "action": action,
        "event_keys": sorted(ACTION_EVENT_KEYS.get(action, {action})),
        "entity_id": ent.id, "entity_uid": ent.uid,
        "kind": ent.kind.value, "module": ent.module_key, "title": ent.title,
        "actor": user.display_name, "actor_user_id": user.id, "details": details,
        "link": link_for(ent.uid, ent.kind.value),
    }))
    if action != "delete":
        session.add(OutboxEvent(topic="calendar.sync", payload_json={
            "entity_id": ent.id, "entity_uid": ent.uid, "reason": action,
        }))
    else:
        session.add(OutboxEvent(topic="calendar.remove", payload_json={
            "entity_id": ent.id, "cal_uid": ent.cal_uid,
        }))
    await session.flush()
