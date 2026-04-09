import os
import html
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

# Данные GGSEL API
GGSEL_TOKEN = os.environ.get("GGSEL_TOKEN")  # если у тебя уже есть API token
GGSEL_API_BASE = os.environ.get("GGSEL_API_BASE", "https://seller.ggsel.com")

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


def send_photo(photo_url: str, caption: str = ""):
    url = f"{TELEGRAM_API}/sendPhoto"
    payload = {
        "chat_id": CHAT_ID,
        "photo": photo_url,
        "parse_mode": "HTML",
    }
    if caption:
        payload["caption"] = caption[:1024]  # лимит caption у Telegram
    r = requests.post(url, json=payload, timeout=30)
    r.raise_for_status()


def get_order_info(invoice_id: str) -> dict:
    """
    Получаем подробности заказа по InvoiceId.
    ВАЖНО:
    Ниже header Authorization может отличаться в зависимости от того,
    как именно GGSEL выдает токен в твоем кабинете/API.
    Если что, подправим под твой формат.
    """
    if not GGSEL_TOKEN or not invoice_id:
        return {}

    url = f"{GGSEL_API_BASE}/api_sellers/api/purchase/info/{invoice_id}"
    headers = {
        "Authorization": f"Bearer {GGSEL_TOKEN}",
        "Accept": "application/json",
    }

    r = requests.get(url, headers=headers, timeout=20)
    r.raise_for_status()

    data = r.json()
    return data.get("content", {}) if isinstance(data, dict) else {}


def safe(value):
    return html.escape(str(value)) if value not in (None, "") else "—"


@app.get("/")
def home():
    return "OK", 200


@app.route("/ggsel", methods=["POST", "GET"])
def ggsel_webhook():
    try:
        data = request.get_json(silent=True)
        if not data:
            data = request.form.to_dict()
        if not data:
            data = request.args.to_dict()

        debate_id = data.get("DebateId", "")
        message_date = data.get("MessageDate", "")
        message = data.get("Message", "")
        invoice_id = data.get("InvoiceId", "")
        image_path = data.get("ImagePath", "")

        order = {}
        try:
            if invoice_id:
                order = get_order_info(str(invoice_id))
        except Exception as api_error:
            # если заказ не подтянулся — просто продолжаем без него
            send_message(
                f"⚠️ Не удалось получить детали заказа {safe(invoice_id)}\n"
                f"<code>{safe(api_error)}</code>"
            )

        product_name = order.get("name", "")
        buyer_info = order.get("buyer_info", {}) if isinstance(order.get("buyer_info"), dict) else {}

        buyer_email = buyer_info.get("email", "")
        buyer_account = buyer_info.get("account", "")
        payment_method = buyer_info.get("payment_method", "")
        amount = order.get("amount", "")
        currency_type = order.get("currency_type", "")
        purchase_date = order.get("purchase_date", "")
        external_order_id = order.get("external_order_id", "")

        text = (
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
            f"<b>Текст:</b>\n{safe(message)}"
        )

        send_message(text)

        if image_path:
            caption = (
                "🖼 <b>Изображение от покупателя</b>\n\n"
                f"<b>Товар:</b> {safe(product_name)}\n"
                f"<b>Почта:</b> {safe(buyer_email)}\n"
                f"<b>Аккаунт:</b> {safe(buyer_account)}\n"
                f"<b>Заказ:</b> {safe(invoice_id)}\n"
                f"<b>Диалог:</b> {safe(debate_id)}\n"
                f"<b>Дата:</b> {safe(message_date)}"
            )
            try:
                send_photo(str(image_path), caption=caption)
            except Exception:
                send_message(
                    f"{caption}\n\n"
                    f"<b>Ссылка на изображение:</b>\n{safe(image_path)}"
                )

        return jsonify({"ok": True}), 200

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
