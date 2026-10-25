---
title: Архитектура (Шаг 1)
order: 80
date: 2026-10-26
tag: Architecture
---

# Цезарь — УСИП: Шаг 1 (структура, модели БД, интеграции)

Полный текст ответа по Шагу 1 — см. финальное сообщение в чате. Кратко:

## Структура
- `app/` — ядро: config, module_registry (плагины), access (RBAC+шеринг), db (async SQLite в /data), models, routers, templates, static (верстка из sipm_lite_v3.html без изменений).
- `modules/<name>/` — плагины: manifest.py (ModuleSpec) + router + шаблоны + payload-модели. Дашборд, Задачи, Проектная работа, Совещания, Администрирование подразделения.
- `integrations/` — yandex_calendar (WebDAV/CalDAV), telegram_bot, ics.
- `workers/` — outbox_worker, calendar_sync, scheduler (ARQ cron / BackgroundTasks в dev).

## Ключевое решение схемы
Полиморфный реестр `entities` (общие поля) + `*_payload` модулей (1:1). Шеринг
(`entity_shares`, permission READ/EDIT/FULL), ответственные (`entity_responsibles`,
UNIQUE entity×department — по одному от каждого участвующего подразделения, слот с
user_id=NULL до назначения), RBAC (Role + role_in_dept), справочники из демо
(status_sets/priority_levels/wf_templates), аудит + `outbox_events` для фоновых интеграций.

## Интеграции
Transactional Outbox: мутация + событие в одной транзакции → фоновые воркеры:
- Telegram: привязка chat_id через /start <код>; рассылка по области видимости ролей; ретраи.
- Яндекс.Календарь: app-пароль (шифрованный Fernet) per-user, WebDAV PUT/DELETE VEVENT с
  детерминированным UID `caesar-<entity.uid>@app`; точечный push по событию + cron diff-проход;
  область записи: Member — свои, Head — подразделение, Coordinator/Admin — всё.

Проверено smoke-тестом на реальном SQLite create_all (sherинг A→B, два ответственных,
RBAC, изоляция постороннего пользователя). Код: `app/models/core.py`,
`app/models/module_payloads.py`, `app/core/access.py`.
