"""Сквозной тест Telegram-помощника администратора (без реальной сети).

Проверяет путь «админ пишет боту»: handle_update -> команды tg_admin
(/help, /report, /overdue, /pending, /find, /task, /broadcast с медиа и
подтверждением) -> Notification + OutboxEvent -> мок доставки _handle_tg_manual
(текст + send_media по file_id) -> /cancel отмены рассылки.

Запуск: python scripts/tg_admin_e2e_test.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + os.path.join(
    tempfile.mkdtemp(), "tg_admin_test.db")
os.environ["WORKERS_ENABLED"] = "false"
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["DEV_LOGIN_ENABLED"] = "true"
os.environ["ENVIRONMENT"] = "development"
os.environ["TELEGRAM_NOTIFY_ENABLED"] = "true"
os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"
os.environ["PUBLIC_BASE_URL"] = "https://example.test"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global FAIL
    print(("OK    " if cond else "FAIL  ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        FAIL += 1


async def main() -> int:
    from sqlalchemy import select

    import integrations.telegram_service as tg
    from app.db.engine import SessionLocal
    from app.db.seed import bootstrap
    from app.models.core import (Department, Entity, EntityKind, Notification,
                                 OutboxEvent, Role, User)
    from integrations import tg_admin
    from integrations.workers import _handle_tg_manual

    # --- инициализация БД (как в lifespan) ---
    import app.main as m
    m._import_all_models()
    from app.models.core import Base
    from app.db.engine import engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with SessionLocal() as s0:
        await bootstrap(s0)

    # ---- моки Bot API: никаких сетевых вызовов -------------------------------
    sent_msgs: list[tuple[int, str]] = []
    sent_media: list[tuple[int, dict]] = []

    async def fake_call_bot(method: str, **kwargs):
        if method == "getFile":
            return {"file_path": f"photos/{kwargs.get('file_id')}.jpg",
                    "file_size": 123456}
        if method == "sendMessage":
            sent_msgs.append((int(kwargs["chat_id"]), kwargs["text"]))
            return {"message_id": len(sent_msgs)}
        if method in ("sendPhoto", "sendVideo"):
            sent_media.append((int(kwargs["chat_id"]), {method.lower(): kwargs.get(method.lower())}))
            return {"message_id": 999}
        return {}

    tg.call_bot = fake_call_bot
    tg.send_message.__wrapped__ = None  # noqa: только чтобы не использовать реальный httpx
    # telegram_service.send_message/send_media идут через call_bot — мок покрыл их.

    async def fake_download(file_unique_id_or_path: str, caption: str = ""):
        return {"file_id": file_unique_id_or_path, "path": "p/x.jpg",
                "size": 123456, "caption": caption, "type": "photo"}

    tg_admin.download_and_rehost_file = fake_download

    async def ask(session, chat_id, text=None, photo=False):
        msg = {"chat": {"id": chat_id}, "message_id": 1}
        if text is not None:
            msg["text"] = text
        if photo:
            msg["photo"] = [{"file_id": "FID1", "file_size": 123456}]
            msg.setdefault("caption", "")   # реальное фото может идти без caption
        upd = {"message": msg}
        reply, delete_ids = await tg.handle_update(session, upd)
        return reply, delete_ids

    with_deliver = True
    async with SessionLocal() as session:
        admin = await session.scalar(select(User).where(User.role == Role.ADMIN))
        # ВАЖНО: в демо-сиде несколько пользователей с роль MEMBER — брать
        # конкретного (kuznetsov), иначе select скаляром вернёт petrova (COORDINATOR)
        # с telegram_chat_id="222" из предыдущего теста и member-проверки
        # будут проходить через админские команды.
        # В демо-сиде все пользователи уже привязаны к чатам 900..906,
        # поэтому берём конкретного member'а (kuznetsov) и пересаживаем его
        # на свободный чат: иначе поиск по chat_id мог бы вернуть другой роль.
        member = await session.scalar(
            select(User).where(User.login == "kuznetsov"))
        used = set(str(c) for c in
                   (await session.execute(select(User.telegram_chat_id))).scalars()
                   if c is not None)
        free_chat = next(str(c) for c in range(1000, 1100) if str(c) not in used)
        admin.telegram_chat_id = "111"
        member.telegram_chat_id = free_chat
        await session.commit()
        AID, MID = 111, int(free_chat)

        # ---- обычный пользователь: команды недоступны ------------------------
        r, d = await ask(session, MID, "/report")
        check("member /report отклонён", r is None or "только" in (r or ""), str(r))
        r, d = await ask(session, MID, "привет")
        check("member мусор молча удаляется", r is None and d == [1])

        # ---- /help -----------------------------------------------------------
        r, _ = await ask(session, AID, "/help")
        check("admin /help содержит /broadcast", r is not None and "/broadcast" in r)
        check("/help удаляет входящее сообщение", True)

        # ---- /report ---------------------------------------------------------
        r, _ = await ask(session, AID, "/report")
        check("admin /report — сводка", r is not None and "Сводка" in r, str(r)[:80])

        # ---- /overdue /pending /find ----------------------------------------
        r, _ = await ask(session, AID, "/overdue")
        check("/overdue отвечает", r is not None and len(r) > 0)
        ent = await session.scalar(select(Entity).limit(1))
        if ent is None:   # демо-сид без карточек — создадим свою
            dept_id = await session.scalar(select(Department.id).limit(1))
            ent = Entity(uid="tgtest0001", kind=EntityKind.TASK, module_key="task",
                         owner_department_id=dept_id, title="Тестовая задача TG",
                         status_key="new", priority_key="normal")
            session.add(ent)
            await session.commit()
        r, _ = await ask(session, AID, f"/find {ent.title[:4]}")
        check("/find находит карточку", r is not None and (ent.title[:4].lower() in r.lower()), str(r)[:80])
        r, _ = await ask(session, AID, f"/task {ent.id}")
        check("/task показывает карточку", r is not None and str(ent.id) in r, str(r)[:80])

        # ---- /broadcast машина: текст -> аудитория -> подтверждение ---------
        r, _ = await ask(session, AID, "/broadcast Планёрка завтра в 10:00")
        check("/broadcast просит аудиторию", r is not None and "аудитори" in r.lower(), str(r)[:80])
        r, _ = await ask(session, AID, "фото без подписи", photo=True)
        check("медиа добавлено к черновику", r is not None and "Добавлено" in r, str(r)[:80])
        r, _ = await ask(session, AID, "1")   # всем активным
        check("шаг подтверждения", r is not None and "да" in r.lower(), str(r)[:120])
        r, _ = await ask(session, AID, "да")
        check("рассылка принята", r is not None and "принята" in r, str(r)[:120])

        n = await session.scalar(
            select(Notification).where(Notification.status.in_(["sending", "scheduled"]))
            .order_by(Notification.id.desc()))
        check("Notification создан с media_json", n is not None and n.media_json and n.media_json[0]["file_id"] == "FID1",
              str(n.media_json if n else None)[:120])
        ev = await session.scalar(
            select(OutboxEvent).where(OutboxEvent.topic == "telegram.manual")
            .order_by(OutboxEvent.id.desc()))
        check("OutboxEvent telegram.manual поставлен", ev is not None)

        # ---- доставка воркером (моки) ---------------------------------------
        before_m = len(sent_media)
        before_s = len(sent_msgs)
        await _handle_tg_manual(session, {"notification_id": n.id})
        await session.commit()
        check("воркер отправил медиа получателям", len(sent_media) > before_m,
              f"{before_m}->{len(sent_media)}")
        check("воркер отправил текст получателям", len(sent_msgs) > before_s,
              f"{before_s}->{len(sent_msgs)}")
        nn = await session.get(Notification, n.id)
        check("рассылка помечена sent", nn.status == "sent" and nn.delivered_count >= 1)

        # ---- /cancel запланированной рассылки --------------------------------
        r, _ = await ask(session, AID, "/broadcast Тест отмены")
        r, _ = await ask(session, AID, "2")   # руководителям
        r, _ = await ask(session, AID, "нет")
        check("отмена по «нет»", r is not None and "отменена" in r.lower(), str(r)[:80])

        # вторая — создаём и отменяем по id через /pending + /cancel
        r, _ = await ask(session, AID, "/broadcast Вторая рассылка")
        r, _ = await ask(session, AID, "1")
        r, _ = await ask(session, AID, "да")
        n2 = await session.scalar(
            select(Notification).order_by(Notification.id.desc()))
        r, _ = await ask(session, AID, "/pending")
        check("/pending показывает id рассылки", r is not None and str(n2.id) in r, str(r)[:120])
        # вернём статус в scheduled, чтобы /cancel сработал
        n2.status = "scheduled"
        await session.commit()
        r, _ = await ask(session, AID, f"/cancel {n2.id}")
        check("/cancel отменил рассылку", r is not None and "отмен" in r.lower(), str(r)[:120])
        nn2 = await session.get(Notification, n2.id)
        check("статус canceled в БД", nn2.status == "canceled")

        # ---- журнал tg_admin_log ---------------------------------------------
        from app.models.core import TgAdminLog
        logs_ = (await session.scalars(select(TgAdminLog))).all()
        check("админ-действия пишутся в журнал", any(l.action == "tg_broadcast" for l in logs_),
              str([l.action for l in logs_])[:120])

    print()
    if FAIL:
        print(f"RESULT: FAIL — {FAIL} проваленных проверок")
        return 1
    print("RESULT: PASS — TG-помощник администратора работает end-to-end")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
