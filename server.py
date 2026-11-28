import os
import json
import base64
import sqlite3
import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("Не задана переменная окружения BOT_TOKEN")

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

PACKS = {
    "pack_100":  {"gold": 100,  "bonus": 0,    "stars": 75,   "title": "100 Голды • GURSTEND CASES"},
    "pack_500":  {"gold": 500,  "bonus": 50,   "stars": 350,  "title": "500 + 50 Голды • GURSTEND CASES"},
    "pack_1000": {"gold": 1000, "bonus": 150,  "stars": 650,  "title": "1 000 + 150 Голды • GURSTEND CASES"},
    "pack_3000": {"gold": 3000, "bonus": 500,  "stars": 1800, "title": "3 000 + 500 Голды • GURSTEND CASES"},
    "pack_5000": {"gold": 5000, "bonus": 1000, "stars": 2800, "title": "5 000 + 1 000 Голды • GURSTEND CASES"},
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
db.execute("""
CREATE TABLE IF NOT EXISTS withdrawals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    username TEXT,
    skin_name TEXT NOT NULL,
    skin_price INTEGER NOT NULL,
    pattern TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")
db.commit()


def add_gold(user_id: int, amount: int) -> int:
    db.execute("INSERT OR IGNORE INTO users (user_id, gold) VALUES (?, 0)", (user_id,))
    db.execute("UPDATE users SET gold = gold + ? WHERE user_id = ?", (amount, user_id))
    db.commit()
    row = db.execute("SELECT gold FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return row[0] if row else 0


def get_user_donated_stars(user_id: int) -> int:
    row = db.execute("SELECT COALESCE(SUM(stars), 0) FROM payments WHERE user_id = ?", (user_id,)).fetchone()
    return row[0] if row else 0


@app.post("/api/create-invoice")
async def create_invoice(req: Request):
    data = await req.json()
    pack_id = data.get("pack_id")
    user_id = int(data.get("user_id") or 0)

    if pack_id not in PACKS or user_id <= 0:
        return {"ok": False, "error": "Некорректный пакет или user_id"}

    pack = PACKS[pack_id]
    total_gold = pack["gold"] + pack["bonus"]
    bonus_txt = f" (включая подарок +{pack['bonus']} G)" if pack["bonus"] > 0 else ""
    payload = json.dumps({"user_id": user_id, "pack_id": pack_id})

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{API_URL}/createInvoiceLink",
            json={
                "title": pack["title"],
                "description": f"Пополнение: +{total_gold} Голды{bonus_txt}",
                "payload": payload,
                "provider_token": "",
                "currency": "XTR",
                "prices": [{"label": f"{total_gold} Голды", "amount": pack["stars"]}],
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


@app.post("/api/withdraw")
async def create_withdrawal(req: Request):
    data = await req.json()
    user_id = int(data.get("user_id") or 0)
    username = str(data.get("username") or "без ника").strip()
    skin_name = str(data.get("skin_name") or "").strip()
    skin_price = int(data.get("skin_price") or 0)
    pattern = str(data.get("pattern") or "не указан").strip()
    avatar_b64 = str(data.get("avatar_data") or "").strip()

    if not skin_name or skin_price <= 0:
        return {"ok": False, "error": "Не указан скин или цена"}

    cur = db.execute(
        "INSERT INTO withdrawals (user_id, username, skin_name, skin_price, pattern) VALUES (?, ?, ?, ?, ?)",
        (user_id, username, skin_name, skin_price, pattern),
    )
    db.commit()
    req_id = cur.lastrowid

    donated_stars = get_user_donated_stars(user_id) if user_id > 0 else 0

    caption = (
        f"🎯 ЗАЯВКА НА ВЫВОД #{req_id}\n\n"
        f"👤 Игрок: {username} (ID: {user_id})\n"
        f"⭐ Всего задонатил: {donated_stars} Stars\n"
        f"🔫 Скин: {skin_name}\n"
        f"💰 Выставлен на рынке за: {skin_price} G\n"
        f"🎨 Паттерн: {pattern}"
    )

    if ADMIN_CHAT_ID:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                if avatar_b64.startswith("data:image") and "," in avatar_b64:
                    img_bytes = base64.b64decode(avatar_b64.split(",", 1)[1])
                    await client.post(
                        f"{API_URL}/sendPhoto",
                        data={"chat_id": ADMIN_CHAT_ID, "caption": caption},
                        files={"photo": ("avatar.jpg", img_bytes, "image/jpeg")},
                    )
                else:
                    await client.post(
                        f"{API_URL}/sendMessage",
                        json={"chat_id": ADMIN_CHAT_ID, "text": caption},
                    )
        except Exception:
            pass

    return {"ok": True, "request_id": req_id}


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
                    total_gold = pack["gold"] + pack["bonus"]
                    db.execute(
                        "INSERT INTO payments (charge_id, user_id, pack_id, gold, stars) VALUES (?, ?, ?, ?, ?)",
                        (charge_id, user_id, meta["pack_id"], total_gold, pack["stars"]),
                    )
                    new_gold = add_gold(user_id, total_gold)
                    bonus_line = f" (включая подарок +{pack['bonus']} G!)" if pack["bonus"] > 0 else ""
                    async with httpx.AsyncClient(timeout=10) as client:
                        await client.post(
                            f"{API_URL}/sendMessage",
                            json={
                                "chat_id": msg.get("chat", {}).get("id", user_id),
                                "text": f"✅ Оплата прошла! Начислено +{total_gold} Голды{bonus_line}. Баланс донатной голды: {new_gold} G.",
                            },
                        )
        except Exception:
            pass

    return {"ok": True}
