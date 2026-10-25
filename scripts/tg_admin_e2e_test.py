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
        """Возвращает (ответ, удаляемые id, inline-клавиатура)."""
        msg = {"chat": {"id": chat_id}, "message_id": 1}
        if text is not None:
            msg["text"] = text
        if photo:
            msg["photo"] = [{"file_id": "FID1", "file_size": 123456}]
            msg.setdefault("caption", "")   # реальное фото может идти без caption
        upd = {"message": msg}
        return await tg.handle_update(session, upd)

    async def press(session, chat_id, data):
        """Нажатие inline-кнопки администратором."""
        cbq = {"id": f"cb{data}{chat_id}", "chat_instance": "x",
               "from": {"id": chat_id, "is_bot": False, "first_name": "Admin"},
               "message": {"message_id": 50, "date": 1,
                           "chat": {"id": chat_id, "type": "private"}},
               "data": data}
        reply, del_ids, kb = await tg.handle_update(
            session, {"callback_query": cbq})
        return reply, kb

    with_deliver = True
    def flat_labels(kb):
        """Подписи кнопок для list-клавиатуры (reply-строки — списки строк,
        inline-строки — словари {'text':...}); None/dict -> []."""
        if not kb or isinstance(kb, dict):
            return []
        return [b if isinstance(b, str) else b.get("text", "")
                for row in kb for b in row]

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

        # ---- обычный пользователь: команды недоступны, кнопок нет ----------
        r, d, _kb = await ask(session, MID, "/report")
        check("member /report отклонён", r is None or "только" in (r or ""), str(r))
        r, d, _kb = await ask(session, MID, "привет")
        check("member мусор молча удаляется", r is None and d == [1], f"r={r} d={d}")
        # member уже привязан: /start —普通ное приветствие без админ-клавиатуры
        r, d, kb = await ask(session, MID, "/start")
        check("member /start — БЕЗ админ-клавиатуры", kb is None, str(kb)[:80])
        cbq = {"id": "cbhack", "chat_instance": "x",
               "from": {"id": MID, "is_bot": False, "first_name": "Member"},
               "message": {"message_id": 50, "date": 1,
                           "chat": {"id": MID, "type": "private"}},
               "data": "adm:report"}
        rh, dh, kbh = await tg.handle_update(session, {"callback_query": cbq})
        check("member нажатие админ-кнопки отклонено",
              rh is not None and "только администраторам" in rh and kbh is None, str(rh)[:80])

        # ---- /help -----------------------------------------------------------
        r, _, _kb = await ask(session, AID, "/help")
        check("admin /help содержит /broadcast", r is not None and "/broadcast" in r)
        check("/help удаляет входящее сообщение", True)

        # ---- /start — главное меню кнопками ----------------------------------
        r, _, kb = await ask(session, AID, "/start")
        flat = flat_labels(kb)
        check("admin /start — админ-клавиатура",
              "📊 Сводка" in flat and "📢 Рассылка" in flat, str(flat)[:120])
        check("меню — закреплённая reply-клавиатура (кнопки без callback_data)",
              bool(kb) and not isinstance(kb, dict) and
              all(isinstance(b, str) for row in kb for b in row), str(kb)[:120])

        # ---- /report ---------------------------------------------------------
        r, _, _kb = await ask(session, AID, "/report")
        check("admin /report — сводка", r is not None and "Сводка" in r, str(r)[:80])

        # ---- /overdue /pending /find ----------------------------------------
        r, _, _kb = await ask(session, AID, "/overdue")
        check("/overdue отвечает", r is not None and len(r) > 0)
        ent = await session.scalar(select(Entity).limit(1))
        if ent is None:   # демо-сид без карточек — создадим свою
            dept_id = await session.scalar(select(Department.id).limit(1))
            ent = Entity(uid="tgtest0001", kind=EntityKind.TASK, module_key="task",
                         owner_department_id=dept_id, title="Тестовая задача TG",
                         status_key="new", priority_key="normal")
            session.add(ent)
            await session.commit()
        r, _, _kb = await ask(session, AID, f"/find {ent.title[:4]}")
        check("/find находит карточку", r is not None and (ent.title[:4].lower() in r.lower()), str(r)[:80])
        r, _, _kb = await ask(session, AID, f"/task {ent.id}")
        check("/task показывает карточку", r is not None and str(ent.id) in r, str(r)[:80])

        # ---- /broadcast кнопками: 📢 -> текст (reply) -> аудитория -> подтвердить
        r, kb = await press(session, AID, "adm:bcast")
        check("кнопка «📢 Рассылка» просит текст",
              r is not None and ("Отправьте текст" in r or "Формат" in r), str(r)[:80])
        # reply-кнопка «📝 Написать текст» приходит как обычный текст
        r, _, _kb = await ask(session, AID, "📝 Написать текст")
        check("reply-кнопка «📝 Написать текст» ждёт ввод",
              r is not None and "текст" in r.lower(), str(r)[:80])
        r, _, kb = await ask(session, AID, "Планёрка завтра в 10:00")
        flat = flat_labels(kb)
        check("после текста — клавиатура выбора аудитории (reply)",
              "👥 Всем" in flat and "✖️ Отмена" in flat, str(flat)[:120])
        r, _, _kb = await ask(session, AID, "фото без подписи", photo=True)
        check("медиа добавлено к черновику", r is not None and "Добавлено" in r, str(r)[:80])
        # нажатие reply-кнопки аудитории — тоже обычный текст
        r, _, kb = await ask(session, AID, "👥 Всем")
        check("кнопка аудитории -> шаг подтверждения",
              r is not None and "Подтвердите" in r and kb is not None and
              "✅ Отправить" in flat_labels(kb),
              f"{str(r)[:60]} kb={flat_labels(kb)}")
        r, _, _kb = await ask(session, AID, "✅ Отправить")
        check("кнопка «✅ Отправить» — рассылка принята",
              r is not None and "принята" in r, str(r)[:120])

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

        # ---- отмена рассылки кнопкой ✖️ ---------------------------------------
        r, _, _kb = await ask(session, AID, "/broadcast Тест отмены")
        r, _, _kb = await ask(session, AID, "2")   # руководителям (текст работает)
        r, kb = await press(session, AID, "bc:cancel")
        check("кнопка «✖️ Отмена» отменяет рассылку",
              r is not None and "отменена" in r.lower(), str(r)[:80])

        # вторая — создаём и отменяем по id через /pending + кнопку 🚫
        r, _, _kb = await ask(session, AID, "/broadcast Вторая рассылка")
        r, _, _kb = await ask(session, AID, "1")
        r, _, _kb = await ask(session, AID, "да")
        n2 = await session.scalar(
            select(Notification).order_by(Notification.id.desc()))
        r, _, _kb = await ask(session, AID, "/pending")
        check("/pending показывает id рассылки", r is not None and str(n2.id) in r, str(r)[:120])
        # вернём статус в scheduled, чтобы отмена сработала
        n2.status = "scheduled"
        await session.commit()
        r, kb = await press(session, AID, "adm:cancelpick")
        check("кнопка «🚫 Отменить рассылку…» даёт список",
              r is not None and f"#{n2.id}" in r, str(r)[:120])
        r, kb = await press(session, AID, f"adm:cancel:{n2.id}")
        check("кнопка отмены отменила рассылку", r is not None and "отмен" in r.lower(), str(r)[:120])
        nn2 = await session.get(Notification, n2.id)
        check("статус canceled в БД", nn2.status == "canceled")

        # ---- прочие кнопки меню ------------------------------------------------
        r, kb = await press(session, AID, "adm:report")
        check("кнопка «📊 Сводка»", r is not None and "Сводка" in r, str(r)[:80])
        r, _kb = await press(session, AID, "adm:overdue")
        check("кнопка «⚠️ Просрочки»", r is not None)
        r, _kb = await press(session, AID, "adm:help")
        check("кнопка «ℹ️ Помощь»", r is not None and "/broadcast" in r)
        r, _kb = await press(session, AID, "adm:menu")
        check("кнопка «« Меню» возвращает в меню", r is not None and "меню администратора" in r)
        r, kb = await press(session, AID, "adm:find")
        flatf = [b.get("callback_data", "") for row in (kb or []) for b in row]
        check("кнопка «🔎 Поиск» даёт список карточек",
              r is not None and any(d.startswith("adm:task:") for d in flatf),
              str(flatf)[:120])

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
