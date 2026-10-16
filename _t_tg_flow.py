"""Смоук-тест сценария привязки Telegram «логин -> пароль» (без реальной сети)."""
import asyncio, sys
sys.path.insert(0, ".")
import integrations.telegram_service as tg

SENT = []

async def fake_call_bot(method, **kw):
    if method == "sendMessage":
        SENT.append(kw)
        return {"message_id": 900 + len(SENT)}
    return True

tg.call_bot = fake_call_bot

class U:
    def __init__(self):
        self.login = "ivanov"; self.display_name = "Иван"
        self.password_hash = "x"*60; self.is_active = True
        self.telegram_chat_id = None; self.telegram_bind_code = None
        self.telegram_optout = None

u = U()
class S:
    async def scalar(self, stmt): return u
    async def commit(self): pass

def upd(text, mid):
    return {"update_id": mid, "message": {"message_id": mid, "text": text, "chat": {"id": 777}}}

def run(*args):
    return asyncio.run(tg.handle_update(S(), *args))

fails = []
def check(name, cond):
    print(("OK   " if cond else "FAIL ") + name)
    if not cond: fails.append(name)

# T1: пустой /start -> подсказка, окно НЕ открыто
r, d = run(upd("/start", 1))
check("T1 пустой /start -> подсказка", bool(r and "логин" in r) and 777 not in tg._pending and d == [1])

# T2: неизвестный логин -> молчаливое удаление
r, d = run(upd("bogus", 2))
check("T2 неизвестный логин -> молча", r is None and d == [2] and 777 not in tg._pending)

# T3: верный логин -> ASK_PASSWORD отправлен, pending открыт, ввод удалён
r, d = run(upd("ivanov", 3))
check("T3 логин -> запрос пароля", r is None and 777 in tg._pending and SENT and "пароль" in SENT[-1]["text"] and d == [3])

# T4: неверный пароль -> BAD_CREDENTIALS, диалог очищен (вопрос+ввод), окно закрыто
r, d = run(upd("wrongpass", 4))
ask_id = 900 + len(SENT) - 1  # id последнего sendMessage (ASK_PASSWORD)
check("T4 неверный пароль", bool(r and "не подходят" in r) and 777 not in tg._pending and set(d) >= {4} and ask_id in d)

# T5: успешная привязка
tg.verify_password = lambda p, h: True
run(upd("ivanov", 5))
r, d = run(upd("rightpass", 6))
check("T5 верный пароль -> welcome + bind", bool(r and "Добро пожаловать" in r) and u.telegram_chat_id == "777" and "/unbind" in r)

# T6: после привязки обычные сообщения молча удаляются
r, d = run(upd("привет", 7))
check("T6 mute после привязки", r is None and d == [7])

# T7-T9: команды
r, _ = run(upd("/mute", 8));    check("T7 /mute", "отключены" in (r or "").lower() and u.telegram_optout == "all")
r, _ = run(upd("/unmute", 9));  check("T8 /unmute", "включены" in (r or "").lower() and u.telegram_optout is None)
r, _ = run(upd("/help", 10));   check("T9 /help упоминает /unbind", "/unbind" in (r or ""))

# T10: чужой логин (уже привязан к другому chat_id) — молчание
u.telegram_chat_id = "888"
r, d = run(upd("ivanov", 11))
check("T10 занятый логин -> молча", r is None and d == [11] and 777 not in tg._pending)

# T11: /unbind снимает привязку
u.telegram_chat_id = "777"
r, _ = run(upd("/unbind", 12))
check("T11 /unbind", u.telegram_chat_id is None and bool(r and "отвязан" in r.lower()))

# T12: анти-брутфорс throttle (первый провал — сообщение, второй в 3 сек — молчание)
tg.verify_password = lambda p, h: False
tg._throttle.pop(777, None)
run(upd("ivanov", 13)); r1, _ = run(upd("bad1", 14))
run(upd("ivanov", 15)); r2, _ = run(upd("bad2", 16))
check("T12 throttle", bool(r1) and r2 is None)

print("\nRESULT:", "ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
