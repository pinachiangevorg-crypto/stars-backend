import os
import json
import sqlite3
import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("Не задана переменная окружения BOT_TOKEN")

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

PACKS = {
    "pack_100":  {"gold": 100,  "stars": 20,  "title": "100 Голды • GURSTEND CASES"},
    "pack_500":  {"gold": 500,  "stars": 100, "title": "500 Голды • GURSTEND CASES"},
    "pack_1000": {"gold": 1000, "stars": 189, "title": "1 000 Голды • GURSTEND CASES"},
    "pack_3000": {"gold": 3000, "stars": 250, "title": "3 000 Голды • GURSTEND CASES"},
    "pack_5000": {"gold": 5000, "stars": 300, "title": "5 000 Голды • GURSTEND CASES"},
}

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

db = sqlite3.connect("stars_shop.db", check_same_thread=False)
db.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    gold INTEGER NOT NULL DEFAULT 0
)
""")
db.execute("""
CREATE TABLE IF NOT EXISTS payments (
    charge_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    pack_id TEXT NOT NULL,
    gold INTEGER NOT NULL,
    stars INTEGER NOT NULL
)
""")
db.commit()


def add_gold(user_id: int, amount: int) -> int:
    db.execute("INSERT OR IGNORE INTO users (user_id, gold) VALUES (?, 0)", (user_id,))
    db.execute("UPDATE users SET gold = gold + ? WHERE user_id = ?", (amount, user_id))
    db.commit()
    row = db.execute("SELECT gold FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return row[0] if row else 0


@app.post("/api/create-invoice")
async def create_invoice(req: Request):
    data = await req.json()
    pack_id = data.get("pack_id")
    user_id = int(data.get("user_id") or 0)

    if pack_id not in PACKS or user_id <= 0:
        return {"ok": False, "error": "Некорректный пакет или user_id"}

    pack = PACKS[pack_id]
    payload = json.dumps({"user_id": user_id, "pack_id": pack_id})

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{API_URL}/createInvoiceLink",
            json={
                "title": pack["title"],
                "description": f"Пополнение внутриигровой валюты: +{pack['gold']} Голды",
                "payload": payload,
                "provider_token": "",
                "currency": "XTR",
                "prices": [{"label": f"{pack['gold']} Голды", "amount": pack["stars"]}],
            },
        )
        res = r.json()

    if not res.get("ok"):
        return {"ok": False, "error": res.get("description", "Ошибка создания счёта")}

    return {"ok": True, "invoice_link": res["result"]}


@app.get("/api/balance/{user_id}")
async def get_balance(user_id: int):
    row = db.execute("SELECT gold FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return {"ok": True, "gold": row[0] if row else 0}


@app.post("/webhook/telegram")
async def telegram_webhook(req: Request):
    update = await req.json()

    if "pre_checkout_query" in update:
        pcq = update["pre_checkout_query"]
        query_id = pcq["id"]
        ok = False
        error_msg = "Пакет не найден"

        try:
            meta = json.loads(pcq.get("invoice_payload", "{}"))
            pack = PACKS.get(meta.get("pack_id"))
            if pack and pcq.get("currency") == "XTR" and int(pcq.get("total_amount", 0)) == pack["stars"]:
                ok = True
        except Exception:
            error_msg = "Ошибка проверки заказа"

        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"{API_URL}/answerPreCheckoutQuery",
                json={"pre_checkout_query_id": query_id, "ok": ok, "error_message": error_msg},
            )
        return {"ok": True}

    msg = update.get("message") or {}
    sp = msg.get("successful_payment")
    if sp:
        charge_id = sp.get("telegram_payment_charge_id")
        try:
            meta = json.loads(sp.get("invoice_payload", "{}"))
            user_id = int(meta.get("user_id") or msg.get("from", {}).get("id") or 0)
            pack = PACKS.get(meta.get("pack_id"))

            if charge_id and user_id > 0 and pack:
                exists = db.execute("SELECT 1 FROM payments WHERE charge_id = ?", (charge_id,)).fetchone()
                if not exists:
                    db.execute(
                        "INSERT INTO payments (charge_id, user_id, pack_id, gold, stars) VALUES (?, ?, ?, ?, ?)",
                        (charge_id, user_id, meta["pack_id"], pack["gold"], pack["stars"]),
                    )
                    new_gold = add_gold(user_id, pack["gold"])
                    async with httpx.AsyncClient(timeout=10) as client:
                        await client.post(
                            f"{API_URL}/sendMessage",
                            json={
                                "chat_id": msg.get("chat", {}).get("id", user_id),
                                "text": f"✅ Оплата прошла! Начислено +{pack['gold']} Голды. Баланс донатной голды: {new_gold} G.",
                            },
                        )
        except Exception:
            pass

    return {"ok": True}
