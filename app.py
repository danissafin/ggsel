import os
import html
import sqlite3
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")  # твой telegram user/chat id
APP_URL = os.environ.get("APP_URL", "").rstrip("/")

if not BOT_TOKEN or not CHAT_ID:
    raise RuntimeError("BOT_TOKEN and CHAT_ID must be set")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
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

REPLY_TEMPLATES = {
    "instruction": "Здравствуйте! Отправляю инструкцию. Выполните, пожалуйста, все шаги по порядку и напишите результат.",
    "replace": "Здравствуйте! Сейчас проверю ситуацию. Если проблема подтвердится, выдам замену.",
    "wait": "Здравствуйте! Принял ваш запрос. Пожалуйста, ожидайте, я проверяю информацию.",
    "photo": "Здравствуйте! Пожалуйста, отправьте фото или скриншот ошибки, чтобы я быстрее помог.",
    "details": "Здравствуйте! Уточните, пожалуйста, в чем именно проблема: что вы делаете и на каком шаге возникает ошибка?",
}


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
        buyer_phone TEXT,
        buyer_skype TEXT,
        buyer_whatsapp TEXT,
        buyer_ip_address TEXT,
        payment_aggregator TEXT,
        amount TEXT,
        currency_type TEXT,
        invoice_state TEXT,
        purchase_date TEXT,
        date_pay TEXT,
        external_order_id TEXT,
        item_id TEXT,
        content_id TEXT,
        profit TEXT,
        unique_code_state TEXT,
        feedback_text TEXT,
        feedback_type TEXT,
        feedback_comment TEXT,
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
        message_id TEXT,
        message_date TEXT,
        message_text TEXT,
        image_path TEXT,
        is_buyer INTEGER DEFAULT 0,
        is_seller INTEGER DEFAULT 0,
        is_file INTEGER DEFAULT 0,
        is_img INTEGER DEFAULT 0,
        filename TEXT,
        file_url TEXT,
        preview TEXT,
        is_negative INTEGER DEFAULT 0,
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


def shorten(text: str, limit: int = 3900) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit - 3] + "..."


def is_owner_telegram_update(update: Dict[str, Any]) -> bool:
    owner = str(CHAT_ID)

    if "callback_query" in update:
        cq = update.get("callback_query", {})
        from_user = cq.get("from", {}) or {}
        message = cq.get("message", {}) or {}
        chat = message.get("chat", {}) or {}

        return str(from_user.get("id", "")) == owner and str(chat.get("id", "")) == owner

    message = update.get("message", {}) or {}
    from_user = message.get("from", {}) or {}
    chat = message.get("chat", {}) or {}

    return str(from_user.get("id", "")) == owner and str(chat.get("id", "")) == owner


# -------------------- Telegram --------------------

def tg_request(method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{TELEGRAM_API}/{method}"
    r = requests.post(url, json=payload, timeout=20)
    r.raise_for_status()
    return r.json()


def send_message(text: str, reply_markup: Optional[Dict[str, Any]] = None):
    payload = {
        "chat_id": CHAT_ID,
        "text": shorten(text),
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


def main_menu():
    return {
        "inline_keyboard": [
            [
                {"text": "🏆 Топ товаров", "callback_data": "top"},
                {"text": "🚨 Проблемные", "callback_data": "negative"},
            ],
            [
                {"text": "🧩 Шаблоны", "callback_data": "templates"},
            ],
        ]
    }


def templates_menu():
    return {
        "inline_keyboard": [
            [
                {"text": "Инструкция", "callback_data": "tpl:instruction"},
                {"text": "Замена", "callback_data": "tpl:replace"},
            ],
            [
                {"text": "Подождать", "callback_data": "tpl:wait"},
                {"text": "Фото ошибки", "callback_data": "tpl:photo"},
            ],
            [
                {"text": "Уточнить детали", "callback_data": "tpl:details"},
            ],
        ]
    }


# -------------------- Persistence --------------------

def upsert_order_from_webhook(invoice_id: str, raw: Dict[str, Any]):
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO orders (
            invoice_id, raw_json, updated_at
        ) VALUES (?, ?, ?)
        ON CONFLICT(invoice_id) DO UPDATE SET
            raw_json=excluded.raw_json,
            updated_at=excluded.updated_at
    """, (
        str(invoice_id),
        str(raw),
        now_iso(),
    ))

    conn.commit()
    conn.close()


def upsert_order_from_api_shape(order: Dict[str, Any], invoice_id: str):
    buyer_info = order.get("buyer_info", {}) if isinstance(order.get("buyer_info"), dict) else {}
    feedback = order.get("feedback", {}) if isinstance(order.get("feedback"), dict) else {}

    conn = db()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO orders (
            invoice_id, product_name, buyer_email, buyer_account, buyer_phone,
            buyer_skype, buyer_whatsapp, buyer_ip_address, payment_aggregator,
            amount, currency_type, invoice_state, purchase_date, date_pay,
            external_order_id, item_id, content_id, profit, unique_code_state,
            feedback_text, feedback_type, feedback_comment, raw_json, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(invoice_id) DO UPDATE SET
            product_name=excluded.product_name,
            buyer_email=excluded.buyer_email,
            buyer_account=excluded.buyer_account,
            buyer_phone=excluded.buyer_phone,
            buyer_skype=excluded.buyer_skype,
            buyer_whatsapp=excluded.buyer_whatsapp,
            buyer_ip_address=excluded.buyer_ip_address,
            payment_aggregator=excluded.payment_aggregator,
            amount=excluded.amount,
            currency_type=excluded.currency_type,
            invoice_state=excluded.invoice_state,
            purchase_date=excluded.purchase_date,
            date_pay=excluded.date_pay,
            external_order_id=excluded.external_order_id,
            item_id=excluded.item_id,
            content_id=excluded.content_id,
            profit=excluded.profit,
            unique_code_state=excluded.unique_code_state,
            feedback_text=excluded.feedback_text,
            feedback_type=excluded.feedback_type,
            feedback_comment=excluded.feedback_comment,
            raw_json=excluded.raw_json,
            updated_at=excluded.updated_at
    """, (
        str(invoice_id),
        str(order.get("name", "") or ""),
        str(buyer_info.get("email", "") or ""),
        str(buyer_info.get("account", "") or ""),
        str(buyer_info.get("phone", "") or ""),
        str(buyer_info.get("skype", "") or ""),
        str(buyer_info.get("whatsapp", "") or ""),
        str(buyer_info.get("ip_address", "") or ""),
        str(buyer_info.get("payment_aggregator", "") or ""),
        str(order.get("amount", "") or ""),
        str(order.get("currency_type", "") or ""),
        str(order.get("invoice_state", "") or ""),
        str(order.get("purchase_date", "") or ""),
        str(order.get("date_pay", "") or ""),
        str(order.get("external_order_id", "") or ""),
        str(order.get("item_id", "") or ""),
        str(order.get("content_id", "") or ""),
        str(order.get("profit", "") or ""),
        str(order.get("unique_code_state", "") or ""),
        str(feedback.get("feedback", "") or ""),
        str(feedback.get("feedback_type", "") or ""),
        str(feedback.get("comment", "") or ""),
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
    message_id: str = "",
    is_buyer: int = 0,
    is_seller: int = 0,
    is_file: int = 0,
    is_img: int = 0,
    filename: str = "",
    file_url: str = "",
    preview: str = "",
):
    negative = 1 if is_negative_text(message_text) else 0

    conn = db()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO messages (
            debate_id, invoice_id, message_id, message_date, message_text, image_path,
            is_buyer, is_seller, is_file, is_img, filename, file_url, preview, is_negative
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        str(debate_id or ""),
        str(invoice_id or ""),
        str(message_id or ""),
        str(message_date or ""),
        str(message_text or ""),
        str(image_path or ""),
        int(is_buyer or 0),
        int(is_seller or 0),
        int(is_file or 0),
        int(is_img or 0),
        str(filename or ""),
        str(file_url or ""),
        str(preview or ""),
        negative,
    ))

    conn.commit()
    conn.close()


# -------------------- Queries --------------------

def find_orders_by_text(query: str) -> List[sqlite3.Row]:
    conn = db()
    cur = conn.cursor()
    like = f"%{query.lower()}%"
    cur.execute("""
        SELECT invoice_id, product_name, buyer_email, buyer_account,
               amount, currency_type, purchase_date, invoice_state
        FROM orders
        WHERE lower(coalesce(product_name, '')) LIKE ?
           OR lower(coalesce(buyer_email, '')) LIKE ?
           OR lower(coalesce(buyer_account, '')) LIKE ?
           OR lower(coalesce(external_order_id, '')) LIKE ?
        ORDER BY updated_at DESC
        LIMIT 15
    """, (like, like, like, like))
    rows = cur.fetchall()
    conn.close()
    return rows


def get_order_local(invoice_id: str) -> Optional[sqlite3.Row]:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM orders WHERE invoice_id = ?", (invoice_id,))
    row = cur.fetchone()
    conn.close()
    return row


def get_messages_for_order(invoice_id: str) -> List[sqlite3.Row]:
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        SELECT *
        FROM messages
        WHERE invoice_id = ?
        ORDER BY id DESC
        LIMIT 10
    """, (invoice_id,))
    rows = cur.fetchall()
    conn.close()
    return rows


def get_latest_debate_id_for_order(invoice_id: str) -> Optional[str]:
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        SELECT debate_id
        FROM messages
        WHERE invoice_id = ? AND debate_id IS NOT NULL AND debate_id != ''
        ORDER BY id DESC
        LIMIT 1
    """, (invoice_id,))
    row = cur.fetchone()
    conn.close()
    return row["debate_id"] if row else None


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

def format_local_order(row: sqlite3.Row) -> str:
    parts = [
        f"<b>Товар:</b> {safe(row['product_name'])}",
        f"<b>Заказ:</b> {safe(row['invoice_id'])}",
        f"<b>Внешний ID:</b> {safe(row['external_order_id'])}",
        f"<b>Статус:</b> {safe(row['invoice_state'])}",
        f"<b>Дата покупки:</b> {safe(row['purchase_date'])}",
        f"<b>Дата оплаты:</b> {safe(row['date_pay'])}",
        f"<b>Сумма:</b> {safe(row['amount'])} {safe(row['currency_type'])}",
        f"<b>Прибыль:</b> {safe(row['profit'])}",
        f"<b>Item ID:</b> {safe(row['item_id'])}",
        f"<b>Content ID:</b> {safe(row['content_id'])}",
        "",
        f"<b>Почта:</b> {safe(row['buyer_email'])}",
        f"<b>Аккаунт:</b> {safe(row['buyer_account'])}",
        f"<b>Телефон:</b> {safe(row['buyer_phone'])}",
        f"<b>Skype:</b> {safe(row['buyer_skype'])}",
        f"<b>WhatsApp:</b> {safe(row['buyer_whatsapp'])}",
        f"<b>IP:</b> {safe(row['buyer_ip_address'])}",
        f"<b>Агрегатор оплаты:</b> {safe(row['payment_aggregator'])}",
        "",
        f"<b>Отзыв:</b> {safe(row['feedback_text'])}",
        f"<b>Тип отзыва:</b> {safe(row['feedback_type'])}",
        f"<b>Комментарий:</b> {safe(row['feedback_comment'])}",
    ]
    return "\n".join(parts)


# -------------------- Web routes --------------------

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
        message_id = str(data.get("MessageId", "") or "")

        if invoice_id:
            upsert_order_from_webhook(invoice_id, data)

        save_message(
            debate_id=debate_id,
            invoice_id=invoice_id,
            message_date=message_date,
            message_text=message,
            image_path=image_path,
            message_id=message_id,
            is_buyer=1,
            is_img=1 if image_path else 0,
            preview=image_path,
        )

        danger = "🚨 <b>ПРОБЛЕМНОЕ СООБЩЕНИЕ</b>\n\n" if is_negative_text(message) else ""

        text = (
            f"{danger}"
            "📩 <b>Новое сообщение с GGSEL</b>\n\n"
            f"<b>Заказ:</b> {safe(invoice_id)}\n"
            f"<b>Диалог:</b> {safe(debate_id)}\n"
            f"<b>Дата сообщения:</b> {safe(message_date)}\n\n"
            f"<b>Сообщение:</b>\n{safe(message)}"
        )

        send_message(text, reply_markup=main_menu())

        if image_path:
            caption = (
                ("🚨 Проблемное сообщение\n\n" if is_negative_text(message) else "")
                + "🖼 <b>Изображение от покупателя</b>\n\n"
                + f"<b>Заказ:</b> {safe(invoice_id)}\n"
                + f"<b>Диалог:</b> {safe(debate_id)}\n"
                + f"<b>Дата:</b> {safe(message_date)}"
            )
            try:
                send_photo(image_path, caption=caption)
            except Exception:
                send_message(f"{caption}\n\n<b>Ссылка на изображение:</b>\n{safe(image_path)}")

        return jsonify({"ok": True}), 200

    except Exception as e:
        print("GGSEL WEBHOOK ERROR:", traceback.format_exc(), flush=True)
        return jsonify({"ok": False, "error": str(e)}), 500


# -------------------- Telegram commands --------------------

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
            "/negative — проблемные сообщения\n"
            "/balance — временно отключено\n"
            "/sales — временно отключено\n"
            "/reply — временно отключено",
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
            "/negative\n\n"
            "GGSEL API-команды пока отключены, чтобы бот работал стабильно."
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
                f"  Аккаунт: {safe(row['buyer_account'])}\n"
                f"  Статус: {safe(row['invoice_state'])}\n"
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
                f"  Статус: {safe(row['invoice_state'])}\n"
                f"  Сумма: {safe(row['amount'])} {safe(row['currency_type'])}\n"
                f"  Дата: {safe(row['purchase_date'])}\n"
            )
        send_message("\n".join(lines))
        return

    if text.startswith("/order "):
        invoice_id = text[7:].strip()
        local = get_order_local(invoice_id)

        if not local:
            send_message(f"Заказ не найден локально: <b>{safe(invoice_id)}</b>")
            return

        msgs = get_messages_for_order(invoice_id)
        lines = [f"<b>Карточка заказа</b>\n\n{format_local_order(local)}\n"]

        debate_id = get_latest_debate_id_for_order(invoice_id)
        if debate_id:
            lines.append(f"\n<b>DebateId:</b> {safe(debate_id)}\n")

        if msgs:
            lines.append("<b>Последние сообщения:</b>")
            for m in msgs[:5]:
                body = m["message_text"] or "[файл/изображение]"
                flags = []
                if m["is_buyer"]:
                    flags.append("buyer")
                if m["is_seller"]:
                    flags.append("seller")
                if m["is_img"]:
                    flags.append("img")
                if m["is_file"]:
                    flags.append("file")

                suffix = f" ({', '.join(flags)})" if flags else ""
                lines.append(
                    f"• {safe(m['message_date'])}{suffix}\n"
                    f"  {safe(body)}"
                )

                if m["filename"]:
                    lines.append(f"  Файл: {safe(m['filename'])}")
                if m["file_url"]:
                    lines.append(f"  URL: {safe(m['file_url'])}")

        send_message("\n".join(lines))
        return

    if text.startswith("/reply "):
        send_message("Отправка ответа в GGSEL пока отключена, пока не будет точной авторизации API.")
        return

    if text.startswith("/tpl "):
        send_message("Шаблонные ответы в GGSEL пока отключены, пока не будет точной авторизации API.")
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
        send_message("Команда временно отключена: GGSEL API авторизация ещё не настроена.")
        return

    if text == "/sales":
        send_message("Команда временно отключена: GGSEL API авторизация ещё не настроена.")
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


# -------------------- Telegram webhook --------------------

@app.post("/telegram")
def telegram_webhook():
    try:
        update = request.get_json(silent=True) or {}

        if not is_owner_telegram_update(update):
            return jsonify({"ok": True, "ignored": "not owner"}), 200

        if "callback_query" in update:
            cq = update["callback_query"]
            data = cq.get("data", "")
            callback_id = cq.get("id")

            if callback_id:
                answer_callback(callback_id, "Открываю…")

            if data == "top":
                handle_command("/top")
            elif data == "negative":
                handle_command("/negative")
            elif data == "templates":
                send_message(
                    "Шаблоны быстрых ответов сохранены, но отправка в GGSEL пока выключена.\n\n"
                    "Доступно:\n"
                    "instruction\nreplace\nwait\nphoto\ndetails",
                    reply_markup=templates_menu()
                )
            elif data.startswith("tpl:"):
                template_name = data.split(":", 1)[1]
                send_message(
                    f"Шаблон <b>{safe(template_name)}</b>\n\n"
                    f"Текст:\n{safe(REPLY_TEMPLATES.get(template_name, ''))}"
                )

            return jsonify({"ok": True}), 200

        message = update.get("message", {}) or {}
        text = message.get("text", "") or ""

        if text:
            handle_command(text)

        return jsonify({"ok": True}), 200

    except Exception as e:
        print("TELEGRAM WEBHOOK ERROR:", traceback.format_exc(), flush=True)
        return jsonify({"ok": False, "error": str(e)}), 500


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))
