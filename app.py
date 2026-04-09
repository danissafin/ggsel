import os
import re
import html
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
GGSEL_API_KEY = os.environ.get("GGSEL_API_KEY")
APP_URL = os.environ.get("APP_URL", "").rstrip("/")  # например https://ggsel-ggsel.up.railway.app

if not BOT_TOKEN or not CHAT_ID:
    raise RuntimeError("BOT_TOKEN and CHAT_ID must be set")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
GGSEL_BASE = "https://seller.ggsel.com"
DB_PATH = "crm.sqlite3"

NEGATIVE_PATTERNS = [
    "не работает",
    "не пришло",
    "обман",
    "верните деньги",
    "возврат",
    "refund",
    "scam",
    "bad",
    "problem",
    "ошибка",
    "не могу",
    "не получается",
]

# -------------------- DB --------------------

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        invoice_id TEXT PRIMARY KEY,
        product_name TEXT,
        buyer_email TEXT,
        buyer_account TEXT,
        amount TEXT,
        currency_type TEXT,
        payment_method TEXT,
        purchase_date TEXT,
        external_order_id TEXT,
        item_id TEXT,
        raw_json TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        debate_id TEXT,
        invoice_id TEXT,
        message_date TEXT,
        message_text TEXT,
        image_path TEXT,
        is_negative INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        review_id TEXT UNIQUE,
        invoice_id TEXT,
        text TEXT,
        rating TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        raw_json TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS sales (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        invoice_id TEXT UNIQUE,
        product_name TEXT,
        amount TEXT,
        currency_type TEXT,
        purchase_date TEXT,
        raw_json TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    conn.commit()
    conn.close()

# -------------------- Utils --------------------

def safe(value: Any) -> str:
    return html.escape(str(value)) if value not in (None, "") else "—"

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def is_negative_text(text: str) -> bool:
    lower = (text or "").lower()
    return any(p in lower for p in NEGATIVE_PATTERNS)

def shorten(text: str, limit: int = 3500) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit - 3] + "..."

# -------------------- Telegram --------------------

def tg_request(method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{TELEGRAM_API}/{method}"
    r = requests.post(url, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()

def send_message(text: str, reply_markup: Optional[Dict[str, Any]] = None):
    payload = {
        "chat_id": CHAT_ID,
        "text": shorten(text, 3900),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg_request("sendMessage", payload)

def send_photo(photo_url: str, caption: str = ""):
    payload = {
        "chat_id": CHAT_ID,
        "photo": photo_url,
        "parse_mode": "HTML",
        "caption": caption[:1024],
    }
    return tg_request("sendPhoto", payload)

def answer_callback(callback_query_id: str, text: str = ""):
    payload = {
        "callback_query_id": callback_query_id,
        "text": text[:200],
        "show_alert": False,
    }
    return tg_request("answerCallbackQuery", payload)

def set_telegram_webhook():
    if not APP_URL:
        return {"ok": False, "error": "APP_URL is not set"}
    payload = {"url": f"{APP_URL}/telegram"}
    return tg_request("setWebhook", payload)

# -------------------- GGSEL --------------------

def ggsel_headers() -> Dict[str, str]:
    # Оставил несколько распространённых вариантов заголовка.
    # Обычно хватает Authorization: Bearer ...
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {GGSEL_API_KEY}" if GGSEL_API_KEY else "",
        "X-API-KEY": GGSEL_API_KEY or "",
        "Api-Key": GGSEL_API_KEY or "",
    }

def ggsel_get(path: str) -> Dict[str, Any]:
    if not GGSEL_API_KEY:
        raise RuntimeError("GGSEL_API_KEY is not set")

    url = f"{GGSEL_BASE}{path}"
    r = requests.get(url, headers=ggsel_headers(), timeout=30)
    r.raise_for_status()
    data = r.json()

    if isinstance(data, dict):
        if isinstance(data.get("content"), (dict, list)):
            return data["content"]
        if isinstance(data.get("data"), (dict, list)):
            return data["data"]
        return data

    return {}

def ggsel_post(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if not GGSEL_API_KEY:
        raise RuntimeError("GGSEL_API_KEY is not set")

    url = f"{GGSEL_BASE}{path}"
    r = requests.post(url, headers=ggsel_headers(), json=payload, timeout=30)
    r.raise_for_status()
    data = r.json()

    if isinstance(data, dict):
        if isinstance(data.get("content"), (dict, list)):
            return data["content"]
        if isinstance(data.get("data"), (dict, list)):
            return data["data"]
        return data

    return {}

def get_order_info(invoice_id: str) -> Dict[str, Any]:
    return ggsel_get(f"/api_sellers/api/purchase/info/{invoice_id}")

def get_balance_info() -> Dict[str, Any]:
    return ggsel_get("/api_sellers/api/sellers/account/balance/info")

def get_receipts() -> Any:
    return ggsel_get("/api_sellers/api/sellers/account/receipts")

def get_last_sales() -> Any:
    return ggsel_get("/api_sellers/api/seller-last-sales")

def get_reviews() -> Any:
    return ggsel_get("/api_sellers/api/reviews")

# Поиск товаров documented as POST /api_sellers/xml/shop_search.asp
def search_products(query: str) -> Any:
    return ggsel_post("/api_sellers/xml/shop_search.asp", {"query": query})

# -------------------- Persistence --------------------

def upsert_order_from_api(order: Dict[str, Any], invoice_id: str):
    buyer_info = order.get("buyer_info", {}) if isinstance(order.get("buyer_info"), dict) else {}

    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO orders (
            invoice_id, product_name, buyer_email, buyer_account, amount, currency_type,
            payment_method, purchase_date, external_order_id, item_id, raw_json, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(invoice_id) DO UPDATE SET
            product_name=excluded.product_name,
            buyer_email=excluded.buyer_email,
            buyer_account=excluded.buyer_account,
            amount=excluded.amount,
            currency_type=excluded.currency_type,
            payment_method=excluded.payment_method,
            purchase_date=excluded.purchase_date,
            external_order_id=excluded.external_order_id,
            item_id=excluded.item_id,
            raw_json=excluded.raw_json,
            updated_at=excluded.updated_at
    """, (
        str(invoice_id),
        str(order.get("name", "") or ""),
        str(buyer_info.get("email", "") or ""),
        str(buyer_info.get("account", "") or ""),
        str(order.get("amount", "") or ""),
        str(order.get("currency_type", "") or ""),
        str(order.get("payment_method", "") or ""),
        str(order.get("purchase_date", "") or ""),
        str(order.get("external_order_id", "") or ""),
        str(order.get("item_id", "") or ""),
        str(order),
        now_iso(),
    ))
    conn.commit()
    conn.close()

def save_message(
    debate_id: str,
    invoice_id: str,
    message_date: str,
    message_text: str,
    image_path: str,
):
    negative = 1 if is_negative_text(message_text) else 0

    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO messages (debate_id, invoice_id, message_date, message_text, image_path, is_negative)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        str(debate_id or ""),
        str(invoice_id or ""),
        str(message_date or ""),
        str(message_text or ""),
        str(image_path or ""),
        negative,
    ))
    conn.commit()
    conn.close()

def save_sales(items: List[Dict[str, Any]]):
    conn = db()
    cur = conn.cursor()
    for item in items:
        invoice_id = str(item.get("invoice_id", "") or item.get("invoiceId", "") or "")
        if not invoice_id:
            continue
        cur.execute("""
            INSERT OR IGNORE INTO sales (invoice_id, product_name, amount, currency_type, purchase_date, raw_json)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            invoice_id,
            str(item.get("name", "") or item.get("product_name", "") or ""),
            str(item.get("amount", "") or ""),
            str(item.get("currency_type", "") or ""),
            str(item.get("purchase_date", "") or ""),
            str(item),
        ))
    conn.commit()
    conn.close()

def save_reviews(items: List[Dict[str, Any]]):
    conn = db()
    cur = conn.cursor()
    for item in items:
        review_id = str(item.get("id", "") or item.get("review_id", "") or "")
        if not review_id:
            continue
        cur.execute("""
            INSERT OR IGNORE INTO reviews (review_id, invoice_id, text, rating, raw_json)
            VALUES (?, ?, ?, ?, ?)
        """, (
            review_id,
            str(item.get("invoice_id", "") or ""),
            str(item.get("text", "") or item.get("review", "") or ""),
            str(item.get("rating", "") or ""),
            str(item),
        ))
    conn.commit()
    conn.close()

# -------------------- Queries --------------------

def find_orders_by_text(query: str) -> List[sqlite3.Row]:
    conn = db()
    cur = conn.cursor()
    like = f"%{query.lower()}%"
    cur.execute("""
        SELECT o.invoice_id, o.product_name, o.buyer_email, o.amount, o.currency_type, o.purchase_date
        FROM orders o
        WHERE lower(o.product_name) LIKE ?
           OR lower(o.buyer_email) LIKE ?
           OR lower(o.buyer_account) LIKE ?
        ORDER BY o.updated_at DESC
        LIMIT 15
    """, (like, like, like))
    rows = cur.fetchall()
    conn.close()
    return rows

def get_order_local(invoice_id: str) -> Optional[sqlite3.Row]:
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM orders WHERE invoice_id = ?
    """, (invoice_id,))
    row = cur.fetchone()
    conn.close()
    return row

def get_messages_for_order(invoice_id: str) -> List[sqlite3.Row]:
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM messages
        WHERE invoice_id = ?
        ORDER BY id DESC
        LIMIT 10
    """, (invoice_id,))
    rows = cur.fetchall()
    conn.close()
    return rows

def top_products() -> List[sqlite3.Row]:
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        SELECT product_name, COUNT(*) as cnt
        FROM orders
        WHERE product_name IS NOT NULL AND product_name != ''
        GROUP BY product_name
        ORDER BY cnt DESC, product_name ASC
        LIMIT 10
    """)
    rows = cur.fetchall()
    conn.close()
    return rows

def recent_negative_messages() -> List[sqlite3.Row]:
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        SELECT invoice_id, debate_id, message_text, message_date
        FROM messages
        WHERE is_negative = 1
        ORDER BY id DESC
        LIMIT 10
    """)
    rows = cur.fetchall()
    conn.close()
    return rows

# -------------------- Formatters --------------------

def format_order_block(order: Dict[str, Any]) -> str:
    buyer_info = order.get("buyer_info", {}) if isinstance(order.get("buyer_info"), dict) else {}
    return (
        f"<b>Товар:</b> {safe(order.get('name'))}\n"
        f"<b>Почта:</b> {safe(buyer_info.get('email'))}\n"
        f"<b>Аккаунт:</b> {safe(buyer_info.get('account'))}\n"
        f"<b>Заказ:</b> {safe(order.get('invoice_id') or order.get('id'))}\n"
        f"<b>Внешний ID:</b> {safe(order.get('external_order_id'))}\n"
        f"<b>Дата покупки:</b> {safe(order.get('purchase_date'))}\n"
        f"<b>Оплата:</b> {safe(order.get('payment_method'))}\n"
        f"<b>Сумма:</b> {safe(order.get('amount'))} {safe(order.get('currency_type'))}"
    )

def format_local_order(row: sqlite3.Row) -> str:
    return (
        f"<b>Товар:</b> {safe(row['product_name'])}\n"
        f"<b>Почта:</b> {safe(row['buyer_email'])}\n"
        f"<b>Аккаунт:</b> {safe(row['buyer_account'])}\n"
        f"<b>Заказ:</b> {safe(row['invoice_id'])}\n"
        f"<b>Дата покупки:</b> {safe(row['purchase_date'])}\n"
        f"<b>Оплата:</b> {safe(row['payment_method'])}\n"
        f"<b>Сумма:</b> {safe(row['amount'])} {safe(row['currency_type'])}"
    )

def main_menu():
    return {
        "inline_keyboard": [
            [
                {"text": "📦 Последние продажи", "callback_data": "sales"},
                {"text": "💰 Баланс", "callback_data": "balance"},
            ],
            [
                {"text": "⭐ Отзывы", "callback_data": "reviews"},
                {"text": "🏆 Топ товаров", "callback_data": "top"},
            ],
            [
                {"text": "🚨 Проблемные", "callback_data": "negative"},
            ],
        ]
    }

# -------------------- Sync helpers --------------------

def sync_last_sales() -> str:
    data = get_last_sales()
    items = data if isinstance(data, list) else data.get("items", []) if isinstance(data, dict) else []
    if not isinstance(items, list):
        items = []
    save_sales(items)

    lines = ["<b>Последние продажи</b>\n"]
    if not items:
        lines.append("Нет данных.")
        return "\n".join(lines)

    for item in items[:10]:
        lines.append(
            f"• {safe(item.get('name') or item.get('product_name'))}\n"
            f"  Заказ: {safe(item.get('invoice_id') or item.get('invoiceId'))}\n"
            f"  Сумма: {safe(item.get('amount'))} {safe(item.get('currency_type'))}\n"
            f"  Дата: {safe(item.get('purchase_date'))}\n"
        )
    return "\n".join(lines)

def sync_reviews() -> str:
    data = get_reviews()
    items = data if isinstance(data, list) else data.get("items", []) if isinstance(data, dict) else []
    if not isinstance(items, list):
        items = []
    save_reviews(items)

    lines = ["<b>Последние отзывы</b>\n"]
    if not items:
        lines.append("Нет данных.")
        return "\n".join(lines)

    for item in items[:10]:
        lines.append(
            f"• Рейтинг: {safe(item.get('rating'))}\n"
            f"  Заказ: {safe(item.get('invoice_id'))}\n"
            f"  {safe(item.get('text') or item.get('review'))}\n"
        )
    return "\n".join(lines)

# -------------------- Handlers --------------------

@app.get("/")
def home():
    return "OK", 200

@app.get("/setup-telegram-webhook")
def setup_telegram_webhook_route():
    try:
        result = set_telegram_webhook()
        return jsonify(result), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/ggsel", methods=["POST", "GET"])
def ggsel_webhook():
    try:
        data = request.get_json(silent=True)
        if not data:
            data = request.form.to_dict()
        if not data:
            data = request.args.to_dict()

        debate_id = str(data.get("DebateId", "") or "")
        message_date = str(data.get("MessageDate", "") or "")
        message = str(data.get("Message", "") or "")
        invoice_id = str(data.get("InvoiceId", "") or "")
        image_path = str(data.get("ImagePath", "") or "")

        order = {}
        if invoice_id:
            try:
                order = get_order_info(invoice_id)
                if isinstance(order, dict):
                    order["invoice_id"] = invoice_id
                    upsert_order_from_api(order, invoice_id)
            except Exception as e:
                send_message(
                    f"⚠️ <b>GGSEL API</b>\n"
                    f"Не удалось получить детали заказа {safe(invoice_id)}\n"
                    f"<code>{safe(e)}</code>"
                )

        save_message(debate_id, invoice_id, message_date, message, image_path)

        buyer_info = order.get("buyer_info", {}) if isinstance(order.get("buyer_info"), dict) else {}
        product_name = order.get("name", "")
        buyer_email = buyer_info.get("email", "")
        buyer_account = buyer_info.get("account", "")
        amount = order.get("amount", "")
        currency_type = order.get("currency_type", "")
        payment_method = order.get("payment_method", "")
        purchase_date = order.get("purchase_date", "")
        external_order_id = order.get("external_order_id", "")

        danger = "🚨 <b>ПРОБЛЕМНОЕ СООБЩЕНИЕ</b>\n\n" if is_negative_text(message) else ""

        text = (
            f"{danger}"
            "📩 <b>Новое сообщение с GGSEL</b>\n\n"
            f"<b>Товар:</b> {safe(product_name)}\n"
            f"<b>Почта покупателя:</b> {safe(buyer_email)}\n"
            f"<b>Аккаунт покупателя:</b> {safe(buyer_account)}\n"
            f"<b>Заказ:</b> {safe(invoice_id)}\n"
            f"<b>Внешний ID:</b> {safe(external_order_id)}\n"
            f"<b>Диалог:</b> {safe(debate_id)}\n"
            f"<b>Дата сообщения:</b> {safe(message_date)}\n"
            f"<b>Дата покупки:</b> {safe(purchase_date)}\n"
            f"<b>Оплата:</b> {safe(payment_method)}\n"
            f"<b>Сумма:</b> {safe(amount)} {safe(currency_type)}\n\n"
            f"<b>Сообщение:</b>\n{safe(message)}"
        )

        send_message(text, reply_markup=main_menu())

        if image_path:
            caption = (
                ("🚨 Проблемное сообщение\n\n" if is_negative_text(message) else "")
                + "🖼 <b>Изображение от покупателя</b>\n\n"
                + f"<b>Товар:</b> {safe(product_name)}\n"
                + f"<b>Почта:</b> {safe(buyer_email)}\n"
                + f"<b>Аккаунт:</b> {safe(buyer_account)}\n"
                + f"<b>Заказ:</b> {safe(invoice_id)}\n"
                + f"<b>Диалог:</b> {safe(debate_id)}\n"
                + f"<b>Дата:</b> {safe(message_date)}"
            )
            try:
                send_photo(image_path, caption=caption)
            except Exception:
                send_message(
                    f"{caption}\n\n<b>Ссылка на изображение:</b>\n{safe(image_path)}"
                )

        return jsonify({"ok": True}), 200

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

def handle_command(text: str):
    text = (text or "").strip()

    if text == "/start":
        send_message(
            "Готово ✅\n\n"
            "Команды:\n"
            "/find слово — поиск по товару/email\n"
            "/order 123456 — карточка заказа\n"
            "/user email@example.com — поиск по покупателю\n"
            "/top — топ товаров\n"
            "/balance — баланс\n"
            "/sales — последние продажи\n"
            "/reviews — отзывы\n"
            "/negative — проблемные сообщения",
            reply_markup=main_menu()
        )
        return

    if text == "/help":
        send_message(
            "Команды:\n"
            "/find windows\n"
            "/order 123456\n"
            "/user buyer@mail.com\n"
            "/top\n"
            "/balance\n"
            "/sales\n"
            "/reviews\n"
            "/negative"
        )
        return

    if text.startswith("/find "):
        query = text[6:].strip()
        rows = find_orders_by_text(query)
        if not rows:
            send_message(f"Ничего не найдено по запросу: <b>{safe(query)}</b>")
            return

        lines = [f"<b>Результаты поиска:</b> {safe(query)}\n"]
        for row in rows:
            lines.append(
                f"• <b>{safe(row['product_name'])}</b>\n"
                f"  Заказ: {safe(row['invoice_id'])}\n"
                f"  Почта: {safe(row['buyer_email'])}\n"
                f"  Сумма: {safe(row['amount'])} {safe(row['currency_type'])}\n"
                f"  Дата: {safe(row['purchase_date'])}\n"
            )
        send_message("\n".join(lines))
        return

    if text.startswith("/user "):
        query = text[6:].strip()
        rows = find_orders_by_text(query)
        if not rows:
            send_message(f"По покупателю ничего не найдено: <b>{safe(query)}</b>")
            return

        lines = [f"<b>Заказы покупателя:</b> {safe(query)}\n"]
        for row in rows:
            lines.append(
                f"• {safe(row['product_name'])}\n"
                f"  Заказ: {safe(row['invoice_id'])}\n"
                f"  Сумма: {safe(row['amount'])} {safe(row['currency_type'])}\n"
                f"  Дата: {safe(row['purchase_date'])}\n"
            )
        send_message("\n".join(lines))
        return

    if text.startswith("/order "):
        invoice_id = text[7:].strip()
        local = get_order_local(invoice_id)

        # если нет локально — попробуем дотянуть из GGSEL
        if not local:
            try:
                order = get_order_info(invoice_id)
                if isinstance(order, dict):
                    order["invoice_id"] = invoice_id
                    upsert_order_from_api(order, invoice_id)
                local = get_order_local(invoice_id)
            except Exception as e:
                send_message(
                    f"Не удалось получить заказ {safe(invoice_id)}\n<code>{safe(e)}</code>"
                )
                return

        if not local:
            send_message(f"Заказ не найден: <b>{safe(invoice_id)}</b>")
            return

        msgs = get_messages_for_order(invoice_id)
        lines = [f"<b>Карточка заказа</b>\n\n{format_local_order(local)}\n"]

        if msgs:
            lines.append("<b>Последние сообщения:</b>")
            for m in msgs[:5]:
                body = m["message_text"] or "[изображение]"
                lines.append(
                    f"• {safe(m['message_date'])}\n"
                    f"  {safe(body)}"
                )

        send_message("\n".join(lines))
        return

    if text == "/top":
        rows = top_products()
        if not rows:
            send_message("Пока нет данных по товарам.")
            return

        lines = ["<b>Топ товаров</b>\n"]
        for i, row in enumerate(rows, start=1):
            lines.append(f"{i}. {safe(row['product_name'])} — {safe(row['cnt'])}")
        send_message("\n".join(lines))
        return

    if text == "/balance":
        try:
            data = get_balance_info()
            send_message(f"<b>Баланс</b>\n\n<code>{safe(data)}</code>")
        except Exception as e:
            send_message(f"Не удалось получить баланс\n<code>{safe(e)}</code>")
        return

    if text == "/sales":
        try:
            send_message(sync_last_sales())
        except Exception as e:
            send_message(f"Не удалось получить продажи\n<code>{safe(e)}</code>")
        return

    if text == "/reviews":
        try:
            send_message(sync_reviews())
        except Exception as e:
            send_message(f"Не удалось получить отзывы\n<code>{safe(e)}</code>")
        return

    if text == "/negative":
        rows = recent_negative_messages()
        if not rows:
            send_message("Проблемных сообщений пока нет.")
            return

        lines = ["<b>Проблемные сообщения</b>\n"]
        for row in rows:
            lines.append(
                f"• Заказ: {safe(row['invoice_id'])}\n"
                f"  Диалог: {safe(row['debate_id'])}\n"
                f"  Дата: {safe(row['message_date'])}\n"
                f"  Текст: {safe(row['message_text'])}\n"
            )
        send_message("\n".join(lines))
        return

    send_message("Не понял команду. Нажми /help")

@app.post("/telegram")
def telegram_webhook():
    try:
        update = request.get_json(silent=True) or {}

        if "callback_query" in update:
            cq = update["callback_query"]
            data = cq.get("data", "")
            callback_id = cq.get("id")

            if callback_id:
                answer_callback(callback_id, "Открываю…")

            if data == "balance":
                handle_command("/balance")
            elif data == "sales":
                handle_command("/sales")
            elif data == "reviews":
                handle_command("/reviews")
            elif data == "top":
                handle_command("/top")
            elif data == "negative":
                handle_command("/negative")

            return jsonify({"ok": True}), 200

        message = update.get("message", {})
        text = message.get("text", "")
        chat = message.get("chat", {})
        chat_id = str(chat.get("id", ""))

        if chat_id != str(CHAT_ID):
            return jsonify({"ok": True, "ignored": "not owner chat"}), 200

        if text:
            handle_command(text)

        return jsonify({"ok": True}), 200

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))
