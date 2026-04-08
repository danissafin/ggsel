import os
import html
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

if not BOT_TOKEN or not CHAT_ID:
    raise RuntimeError("BOT_TOKEN and CHAT_ID must be set")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

def send_message(text: str):
    url = f"{TELEGRAM_API}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    r = requests.post(url, json=payload, timeout=20)
    r.raise_for_status()

@app.get("/")
def home():
    return "OK", 200

@app.post("/ggsel")
def ggsel_webhook():
    data = request.get_json(silent=True) or {}

    debate_id = data.get("DebateId", "")
    message_date = data.get("MessageDate", "")
    message = data.get("Message", "")
    invoice_id = data.get("InvoiceId", "")
    image_path = data.get("ImagePath", "")

    text = (
        "📩 <b>Новое сообщение с GGSEL</b>\n\n"
        f"<b>Заказ:</b> {html.escape(str(invoice_id)) or '—'}\n"
        f"<b>Диалог:</b> {html.escape(str(debate_id)) or '—'}\n"
        f"<b>Дата:</b> {html.escape(str(message_date)) or '—'}\n\n"
        f"<b>Текст:</b>\n{html.escape(str(message)) or '—'}"
    )

    send_message(text)

    if image_path:
        send_message(f"🖼 <b>Изображение:</b>\n{html.escape(str(image_path))}")

    return jsonify({"ok": True}), 200
