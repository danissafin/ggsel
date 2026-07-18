from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from typing import Any, Iterable, Optional
from urllib.parse import parse_qsl

import requests
from flask import Flask, jsonify, render_template, request


# ============================================================
# Configuration
# ============================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
OWNER_ID = os.environ.get("OWNER_ID", os.environ.get("CHAT_ID", "")).strip()
APP_URL = os.environ.get("APP_URL", "").rstrip("/")

GGSEL_SELLER_ID = os.environ.get("GGSEL_SELLER_ID", "").strip()
GGSEL_API_KEY = os.environ.get("GGSEL_API_KEY", "").strip()
GGSEL_BASE_URL = os.environ.get("GGSEL_BASE_URL", "https://seller.ggsel.com").rstrip("/")

DB_PATH = os.environ.get("DB_PATH", "/data/ggsel_bot.sqlite3")
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "15"))
RECEIPTS_MAX_PAGES = int(os.environ.get("RECEIPTS_MAX_PAGES", "15"))
MINIAPP_AUTH_MAX_AGE = int(os.environ.get("MINIAPP_AUTH_MAX_AGE", "86400"))
MINIAPP_OFFERS_MAX_PAGES = int(os.environ.get("MINIAPP_OFFERS_MAX_PAGES", "20"))
MINIAPP_OFFERS_CACHE_SECONDS = int(os.environ.get("MINIAPP_OFFERS_CACHE_SECONDS", "30"))
# GGSEL V2 may return HTTP 500 for an oversized page instead of a validation error.
# Keep this conservative; the client also retries compatible pagination formats.
MINIAPP_OFFERS_API_PAGE_SIZE = max(5, min(int(os.environ.get("MINIAPP_OFFERS_API_PAGE_SIZE", "20")), 50))

TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip()
GGSEL_WEBHOOK_SECRET = os.environ.get("GGSEL_WEBHOOK_SECRET", "").strip()
SETUP_SECRET = os.environ.get("SETUP_SECRET", "").strip()

if not BOT_TOKEN or not OWNER_ID:
    raise RuntimeError("BOT_TOKEN and OWNER_ID (or CHAT_ID) must be set")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("ggsel-bot")

app = Flask(__name__)
executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="bot-worker")


# ============================================================
# Errors and helpers
# ============================================================


class APIError(RuntimeError):
    def __init__(self, service: str, status: int, message: str, payload: Any = None):
        super().__init__(f"{service}: HTTP {status}: {message}")
        self.service = service
        self.status = status
        self.payload = payload


def safe(value: Any) -> str:
    if value is None or value == "":
        return "—"
    return html.escape(str(value))


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return default


def compact_json(value: Any, limit: int = 3500) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def parse_json_argument(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Некорректный JSON: {exc.msg}, позиция {exc.pos}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("JSON должен быть объектом {...}")
    return parsed


def parse_id_list(value: str) -> list[int]:
    result: list[int] = []
    for part in value.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        item = int(part)
        if item <= 0:
            raise ValueError("ID должен быть положительным числом")
        result.append(item)
    if not result:
        raise ValueError("Не указано ни одного ID")
    # preserve order, remove duplicates
    return list(dict.fromkeys(result))


def parse_iso_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def extract_list(data: Any, preferred: Iterable[str] = ()) -> list[Any]:
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []

    keys = list(preferred) + [
        "items",
        "rows",
        "offers",
        "products",
        "sales",
        "reviews",
        "category",
        "categories",
        "data",
        "content",
        "result",
    ]
    for key in keys:
        value = data.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = extract_list(value, preferred)
            if nested:
                return nested
    return []


def unwrap_v1(data: Any) -> Any:
    if isinstance(data, dict) and "content" in data:
        return data["content"]
    return data


def run_background(func, *args) -> None:
    future = executor.submit(func, *args)

    def done_callback(done):
        try:
            done.result()
        except Exception:
            logger.exception("Background task failed")

    future.add_done_callback(done_callback)


# ============================================================
# SQLite
# ============================================================


def db_connect() -> sqlite3.Connection:
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=20, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with db_connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS webhook_events (
                event_key TEXT PRIMARY KEY,
                received_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS orders (
                invoice_id TEXT PRIMARY KEY,
                item_id TEXT,
                product_name TEXT,
                buyer_email TEXT,
                buyer_account TEXT,
                buyer_phone TEXT,
                amount REAL,
                currency TEXT,
                profit REAL,
                invoice_state INTEGER,
                purchase_date TEXT,
                date_pay TEXT,
                external_order_id TEXT,
                raw_json TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT,
                debate_id TEXT,
                invoice_id TEXT,
                sender TEXT,
                message_text TEXT,
                image_url TEXT,
                message_date TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sales (
                invoice_id TEXT PRIMARY KEY,
                sale_date TEXT,
                product_id TEXT,
                product_name TEXT,
                price_rub REAL,
                price_usd REAL,
                price_eur REAL,
                raw_json TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS pending_actions (
                action_id TEXT PRIMARY KEY,
                action_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS miniapp_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                action TEXT NOT NULL,
                target TEXT,
                payload_json TEXT,
                created_at TEXT NOT NULL
            );
            """
        )


def remember_event(event_key: str) -> bool:
    try:
        with db_connect() as conn:
            conn.execute(
                "INSERT INTO webhook_events(event_key, received_at) VALUES (?, ?)",
                (event_key, datetime.now(timezone.utc).isoformat()),
            )
        return True
    except sqlite3.IntegrityError:
        return False


def save_message_record(
    *,
    message_id: str,
    debate_id: str,
    invoice_id: str,
    sender: str,
    message_text: str,
    image_url: str,
    message_date: str,
) -> None:
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO messages(
                message_id, debate_id, invoice_id, sender, message_text,
                image_url, message_date, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                debate_id,
                invoice_id,
                sender,
                message_text,
                image_url,
                message_date,
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def upsert_order(invoice_id: str, response: dict[str, Any]) -> dict[str, Any]:
    order = response.get("content") if isinstance(response.get("content"), dict) else response
    if not isinstance(order, dict):
        return {}
    buyer = order.get("buyer_info") if isinstance(order.get("buyer_info"), dict) else {}
    now = datetime.now(timezone.utc).isoformat()
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO orders(
                invoice_id, item_id, product_name, buyer_email, buyer_account,
                buyer_phone, amount, currency, profit, invoice_state,
                purchase_date, date_pay, external_order_id, raw_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(invoice_id) DO UPDATE SET
                item_id=excluded.item_id,
                product_name=excluded.product_name,
                buyer_email=excluded.buyer_email,
                buyer_account=excluded.buyer_account,
                buyer_phone=excluded.buyer_phone,
                amount=excluded.amount,
                currency=excluded.currency,
                profit=excluded.profit,
                invoice_state=excluded.invoice_state,
                purchase_date=excluded.purchase_date,
                date_pay=excluded.date_pay,
                external_order_id=excluded.external_order_id,
                raw_json=excluded.raw_json,
                updated_at=excluded.updated_at
            """,
            (
                invoice_id,
                str(order.get("item_id") or ""),
                str(order.get("name") or ""),
                str(buyer.get("email") or ""),
                str(buyer.get("account") or ""),
                str(buyer.get("phone") or ""),
                as_float(order.get("amount")),
                str(order.get("currency_type") or ""),
                as_float(order.get("profit")),
                as_int(order.get("invoice_state")),
                str(order.get("purchase_date") or ""),
                str(order.get("date_pay") or ""),
                str(order.get("external_order_id") or ""),
                json.dumps(response, ensure_ascii=False, default=str),
                now,
            ),
        )
    return order


def upsert_sales(data: Any) -> int:
    sales = extract_list(data, ("sales",))
    now = datetime.now(timezone.utc).isoformat()
    count = 0
    with db_connect() as conn:
        for sale in sales:
            if not isinstance(sale, dict):
                continue
            invoice_id = str(sale.get("invoice_id") or "")
            product = sale.get("product") if isinstance(sale.get("product"), dict) else {}
            if not invoice_id:
                continue
            conn.execute(
                """
                INSERT INTO sales(
                    invoice_id, sale_date, product_id, product_name,
                    price_rub, price_usd, price_eur, raw_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(invoice_id) DO UPDATE SET
                    sale_date=excluded.sale_date,
                    product_id=excluded.product_id,
                    product_name=excluded.product_name,
                    price_rub=excluded.price_rub,
                    price_usd=excluded.price_usd,
                    price_eur=excluded.price_eur,
                    raw_json=excluded.raw_json,
                    updated_at=excluded.updated_at
                """,
                (
                    invoice_id,
                    str(sale.get("date") or ""),
                    str(product.get("id") or ""),
                    str(product.get("name") or ""),
                    as_float(product.get("price_rub")),
                    as_float(product.get("price_usd")),
                    as_float(product.get("price_eur")),
                    json.dumps(sale, ensure_ascii=False, default=str),
                    now,
                ),
            )
            count += 1
    return count


def create_pending_action(action_type: str, payload: dict[str, Any], ttl: int = 600) -> str:
    action_id = secrets.token_hex(6)
    now = int(time.time())
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO pending_actions(action_id, action_type, payload_json, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (action_id, action_type, json.dumps(payload, ensure_ascii=False), now, now + ttl),
        )
    return action_id


def pop_pending_action(action_id: str) -> Optional[tuple[str, dict[str, Any]]]:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT action_type, payload_json, expires_at FROM pending_actions WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        if row is None:
            return None
        conn.execute("DELETE FROM pending_actions WHERE action_id = ?", (action_id,))
    if int(row["expires_at"]) < int(time.time()):
        return None
    return str(row["action_type"]), json.loads(row["payload_json"])


def cancel_pending_action(action_id: str) -> bool:
    with db_connect() as conn:
        cursor = conn.execute("DELETE FROM pending_actions WHERE action_id = ?", (action_id,))
        return cursor.rowcount > 0


# ============================================================
# GGSEL API client
# ============================================================


@dataclass
class CachedToken:
    value: str = ""
    valid_until: float = 0.0


class GGSELClient:
    def __init__(self) -> None:
        self.session = requests.Session()
        self._token = CachedToken()
        self._token_lock = threading.Lock()

    def _decode_response(self, response: requests.Response, service: str) -> Any:
        try:
            payload = response.json()
        except ValueError:
            payload = response.text
        if not response.ok:
            message = compact_json(payload, 700) if not isinstance(payload, str) else payload[:700]
            raise APIError(service, response.status_code, message, payload)
        if isinstance(payload, dict):
            retval = payload.get("retval")
            if retval not in (None, 0, "0"):
                raise APIError(service, response.status_code, compact_json(payload, 700), payload)
        return payload

    def _parse_valid_thru(self, value: Any) -> float:
        dt = parse_iso_datetime(value)
        if dt is None:
            return time.time() + 20 * 60
        return max(time.time() + 30, dt.timestamp() - 60)

    def get_v1_token(self, force: bool = False) -> str:
        if not GGSEL_SELLER_ID or not GGSEL_API_KEY:
            raise RuntimeError("GGSEL_SELLER_ID and GGSEL_API_KEY must be set")

        with self._token_lock:
            if not force and self._token.value and time.time() < self._token.valid_until:
                return self._token.value

            timestamp = str(int(time.time() * 1000))
            sign = hashlib.sha256((GGSEL_API_KEY + timestamp).encode("utf-8")).hexdigest()
            payload = {
                "seller_id": int(GGSEL_SELLER_ID),
                "timestamp": timestamp,
                "sign": sign,
            }
            response = self.session.post(
                f"{GGSEL_BASE_URL}/api_sellers/api/apilogin",
                json=payload,
                timeout=REQUEST_TIMEOUT,
            )
            data = self._decode_response(response, "GGSEL ApiLogin")
            if not isinstance(data, dict) or not data.get("token"):
                raise RuntimeError(f"GGSEL did not return token: {compact_json(data, 700)}")
            self._token = CachedToken(
                value=str(data["token"]),
                valid_until=self._parse_valid_thru(data.get("valid_thru")),
            )
            return self._token.value

    def v1_request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Any = None,
        form_body: Optional[dict[str, Any]] = None,
        retry_auth: bool = True,
    ) -> Any:
        token = self.get_v1_token()
        url = f"{GGSEL_BASE_URL}{path}"

        # Documentation UI calls the authorization field "Authorization".
        # Raw token is tried first; Bearer is a compatibility fallback.
        header_variants = [
            {"Authorization": token, "Accept": "application/json"},
            {"Authorization": f"Bearer {token}", "Accept": "application/json"},
        ]
        last_error: Optional[APIError] = None

        for headers in header_variants:
            response = self.session.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
                data=form_body,
                timeout=REQUEST_TIMEOUT,
            )
            try:
                return self._decode_response(response, "GGSEL V1")
            except APIError as exc:
                last_error = exc
                if exc.status != 401:
                    raise

        if retry_auth and last_error and last_error.status == 401:
            self.get_v1_token(force=True)
            return self.v1_request(
                method,
                path,
                params=params,
                json_body=json_body,
                form_body=form_body,
                retry_auth=False,
            )
        raise last_error or RuntimeError("GGSEL V1 request failed")

    def v2_request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Any = None,
    ) -> Any:
        if not GGSEL_API_KEY:
            raise RuntimeError("GGSEL_API_KEY must be set")
        url = f"{GGSEL_BASE_URL}{path}"
        response = self.session.request(
            method,
            url,
            headers={"Authorization": GGSEL_API_KEY, "Accept": "application/json"},
            params=params,
            json=json_body,
            timeout=REQUEST_TIMEOUT,
        )
        if not response.ok:
            request_id = (
                response.headers.get("x-request-id")
                or response.headers.get("x-correlation-id")
                or response.headers.get("cf-ray")
                or "—"
            )
            logger.warning(
                "GGSEL V2 upstream error: method=%s url=%s params=%s status=%s request_id=%s body=%s",
                method,
                url,
                params,
                response.status_code,
                request_id,
                response.text[:1000],
            )
        return self._decode_response(response, "GGSEL V2")

    # ---------- V1: account/categories ----------

    def balance(self) -> Any:
        return self.v1_request("GET", "/api_sellers/api/sellers/account/balance/info")

    def receipts(self, page: int = 1, count: int = 50) -> Any:
        try:
            return self.v1_request(
                "GET",
                "/api_sellers/api/sellers/account/receipts",
                params={"page": page, "count": count},
            )
        except APIError as exc:
            if exc.status not in (400, 422):
                raise
            return self.v1_request(
                "GET",
                "/api_sellers/api/sellers/account/receipts",
                params={"Page": page, "Count": count},
            )

    def categories_v1(self) -> Any:
        return self.v1_request("GET", "/api_sellers/api/categories")

    # ---------- V1: products ----------

    def products_v1(self, page: int = 1, count: int = 30) -> Any:
        return self.v1_request(
            "GET",
            "/api_sellers/api/products/list",
            params={"page": page, "count": count},
        )

    def product_v1(self, product_id: int) -> Any:
        return self.v1_request("GET", f"/api_sellers/api/products/{product_id}/data")

    def bulk_prices_v1(self, payload: dict[str, Any]) -> Any:
        return self.v1_request(
            "POST", "/api_sellers/api/product/edit/prices", json_body=payload
        )

    def bulk_price_status_v1(self, task_id: str) -> Any:
        return self.v1_request(
            "GET",
            "/api_sellers/api/product/edit/UpdateProductsTaskStatus",
            params={"task_id": task_id},
        )

    # ---------- V1: orders/reviews ----------

    def last_sales(self) -> Any:
        return self.v1_request("GET", "/api_sellers/api/seller-last-sales")

    def order_info(self, invoice_id: str) -> Any:
        return self.v1_request("GET", f"/api_sellers/api/purchase/info/{invoice_id}")

    def unique_code(self, code: str) -> Any:
        return self.v1_request("GET", f"/api_sellers/api/purchases/unique-code/{code}")

    def reviews(self, page: int = 1) -> Any:
        return self.v1_request("GET", "/api_sellers/api/reviews", params={"page": page})

    # ---------- V1: chats ----------

    def chats(self, page: int = 1) -> Any:
        return self.v1_request(
            "GET", "/api_sellers/api/debates/v2/chats", params={"page": page}
        )

    def chat_messages(self, debate_id: str) -> Any:
        return self.v1_request(
            "GET", "/api_sellers/api/debates/v2", params={"id_i": debate_id}
        )

    def send_chat_message(self, debate_id: str, message: str) -> Any:
        path = "/api_sellers/api/debates/v2"
        params = {"id_i": debate_id}
        try:
            return self.v1_request(
                "POST",
                path,
                params=params,
                json_body={"message": message, "files": []},
            )
        except APIError as exc:
            if exc.status not in (400, 415, 422):
                raise
            return self.v1_request(
                "POST", path, params=params, form_body={"message": message}
            )

    # ---------- V2: categories/offers/products ----------

    def categories_v2(self) -> Any:
        return self.v2_request("GET", "/api_sellers/v2/categories")

    def search_categories_v2(self, query: str) -> Any:
        try:
            return self.v2_request(
                "GET", "/api_sellers/v2/categories/search", params={"query": query}
            )
        except APIError as exc:
            if exc.status not in (400, 422):
                raise
            return self.v2_request(
                "GET", "/api_sellers/v2/categories/search", params={"q": query}
            )

    def offers_v2(self, page: int = 1, limit: int = 20) -> Any:
        """List offers with defensive pagination compatibility.

        GGSEL's V2 documentation exposes the endpoint but deployments can differ
        in accepted pagination parameter names. Some versions also respond with
        HTTP 500 when the requested page size is too large. We therefore use a
        conservative page size and retry only this safe GET request.
        """
        page = max(1, int(page))
        safe_limit = max(5, min(int(limit), 50))

        variants: list[Optional[dict[str, Any]]] = [
            {"page": page, "limit": safe_limit},
            {"page": page, "per_page": safe_limit},
            {"page": page},
        ]
        if page == 1:
            variants.append(None)

        last_error: Optional[APIError] = None
        seen_variants: set[str] = set()
        for params in variants:
            marker = json.dumps(params, sort_keys=True) if params is not None else "none"
            if marker in seen_variants:
                continue
            seen_variants.add(marker)
            try:
                return self.v2_request("GET", "/api_sellers/v2/offers", params=params)
            except APIError as exc:
                last_error = exc
                # Authorization/not-found errors are not pagination problems.
                if exc.status in (401, 403, 404):
                    raise
                if exc.status not in (400, 422, 500):
                    raise
                logger.warning(
                    "GGSEL V2 offers retry: page=%s params=%s status=%s",
                    page,
                    params,
                    exc.status,
                )

        raise last_error or RuntimeError("GGSEL V2 offers request failed")

    def offer_v2(self, offer_id: int) -> Any:
        return self.v2_request("GET", f"/api_sellers/v2/offers/{offer_id}")

    def create_offer_v2(self, payload: dict[str, Any]) -> Any:
        return self.v2_request("POST", "/api_sellers/v2/offers", json_body=payload)

    def patch_offer_v2(self, offer_id: int, payload: dict[str, Any]) -> Any:
        return self.v2_request(
            "PATCH", f"/api_sellers/v2/offers/{offer_id}", json_body=payload
        )

    def batch_offers_v2(self, action: str, offer_ids: list[int]) -> Any:
        if action not in {"activate", "pause", "delete"}:
            raise ValueError("Unknown batch action")
        return self.v2_request(
            "POST",
            f"/api_sellers/v2/offers/batch_{action}",
            json_body={"offer_ids": offer_ids},
        )

    def products_v2(self, offer_id: int, page: int = 1, limit: int = 30) -> Any:
        return self.v2_request(
            "GET",
            f"/api_sellers/v2/offers/{offer_id}/products",
            params={"page": page, "limit": limit},
        )

    def add_products_v2(self, offer_id: int, values: list[str]) -> Any:
        return self.v2_request(
            "POST",
            f"/api_sellers/v2/offers/{offer_id}/products",
            json_body={"products": [{"value": value} for value in values]},
        )

    def archive_products_v2(
        self, offer_id: int, product_ids: Optional[list[int]] = None, delete_all: bool = False
    ) -> Any:
        payload: dict[str, Any] = {
            "product_ids": product_ids or [],
            "delete_all": "true" if delete_all else "false",
        }
        return self.v2_request(
            "DELETE", f"/api_sellers/v2/offers/{offer_id}/products", json_body=payload
        )

    def async_job_v2(self, job_id: str) -> Any:
        return self.v2_request("GET", f"/api_sellers/v2/async_job_results/{job_id}")


ggsel = GGSELClient()

# ============================================================
# Telegram Mini App backend
# ============================================================

_offer_cache_lock = threading.Lock()
_offer_cache: dict[str, Any] = {"expires_at": 0.0, "items": []}


def validate_telegram_init_data(init_data: str) -> dict[str, Any]:
    """Validate Telegram.WebApp.initData and return the verified user object."""
    if not init_data:
        raise APIError("Mini App", 401, "Откройте панель из Telegram-бота")

    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = str(pairs.pop("hash", ""))
    # For bot-token validation Telegram excludes only the received hash.
    # The optional signature field remains part of the alphabetically sorted data-check-string.
    if not received_hash:
        raise APIError("Mini App", 401, "В initData отсутствует hash")

    data_check_string = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
    calculated_hash = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(calculated_hash, received_hash):
        raise APIError("Mini App", 403, "Неверная подпись Telegram")

    auth_date = as_int(pairs.get("auth_date"))
    now = int(time.time())
    if auth_date <= 0 or abs(now - auth_date) > MINIAPP_AUTH_MAX_AGE:
        raise APIError("Mini App", 401, "Сессия Telegram устарела. Закройте и откройте панель заново")

    try:
        user = json.loads(pairs.get("user", "{}"))
    except json.JSONDecodeError as exc:
        raise APIError("Mini App", 401, "Некорректные данные пользователя Telegram") from exc
    if not isinstance(user, dict) or str(user.get("id") or "") != OWNER_ID:
        raise APIError("Mini App", 403, "Доступ разрешён только владельцу бота")
    return user


def miniapp_api(func):
    @wraps(func)
    def wrapped(*args, **kwargs):
        try:
            authorization = request.headers.get("Authorization", "")
            init_data = authorization[4:] if authorization.startswith("tma ") else ""
            user = validate_telegram_init_data(init_data)
            return func(user, *args, **kwargs)
        except APIError as exc:
            return jsonify({"ok": False, "error": str(exc), "service": exc.service}), exc.status
        except (ValueError, RuntimeError, requests.RequestException) as exc:
            logger.warning("Mini App request failed: %s", exc)
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:
            logger.exception("Unexpected Mini App error")
            return jsonify({"ok": False, "error": str(exc)}), 500

    return wrapped


def audit_miniapp(user_id: Any, action: str, target: Any = "", payload: Any = None) -> None:
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO miniapp_audit(user_id, action, target, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                str(user_id),
                str(action),
                str(target or ""),
                json.dumps(payload, ensure_ascii=False, default=str) if payload is not None else "",
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def _offer_quantity(item: dict[str, Any]) -> int:
    for key in ("quantity", "num_in_stock", "in_stock", "products_count", "stock"):
        if item.get(key) is not None:
            return as_int(item.get(key))
    return 0


def _offer_status(item: dict[str, Any]) -> str:
    value: Any = "unknown"
    for key in ("status", "state", "offer_status", "active"):
        if key in item and item.get(key) is not None:
            value = item.get(key)
            break
    text = str(value).strip().lower()
    aliases = {
        "1": "active",
        "0": "paused",
        "enabled": "active",
        "disabled": "paused",
        "pause": "paused",
        "stopped": "paused",
        "deleted": "archived",
        "archive": "archived",
    }
    return aliases.get(text, text or "unknown")


def normalize_offer_item(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    category = item.get("category") if isinstance(item.get("category"), dict) else {}
    return {
        "id": offer_id(item),
        "title": offer_name(item),
        "status": _offer_status(item),
        "price": as_float(item.get("price") if item.get("price") is not None else item.get("price_rur")),
        "currency": str(item.get("currency") or "RUB"),
        "quantity": _offer_quantity(item),
        "category": category.get("title") or category.get("name") or item.get("category_title") or "",
        "category_id": category.get("id") or item.get("category_id"),
        "is_autoselling": bool(item.get("is_autoselling")),
        "sold_products_count": as_int(item.get("sold_products_count")),
        "updated_at": item.get("updated_at"),
        "created_at": item.get("created_at"),
    }


def _has_next_page(data: Any, items: list[Any], page: int, limit: int) -> bool:
    candidates: list[dict[str, Any]] = []
    if isinstance(data, dict):
        candidates.append(data)
        for key in ("content", "data", "pagination", "meta"):
            if isinstance(data.get(key), dict):
                candidates.append(data[key])
    for obj in candidates:
        for key in ("has_next_page", "has_next", "next_page"):
            if key in obj:
                value = obj.get(key)
                if isinstance(value, bool):
                    return value
                return value not in (None, "", 0, "0", False)
        total_pages = as_int(obj.get("total_pages") or obj.get("pages"))
        if total_pages:
            return page < total_pages
        total = as_int(obj.get("total") or obj.get("total_count"))
        if total:
            return page * limit < total
    return len(items) >= limit


def _collect_offers_v1_fallback() -> list[dict[str, Any]]:
    """Load seller products through V1 when GGSEL's V2 list endpoint is broken.

    The V2 offer ID and the legacy product ID represent the same seller item for
    the operations used by this app. We normalize both schemas into one GUI model.
    """
    collected: list[dict[str, Any]] = []
    seen: set[str] = set()
    count = 100
    for page in range(1, MINIAPP_OFFERS_MAX_PAGES + 1):
        data = ggsel.products_v1(page=page, count=count)
        items = extract_list(data, ("products", "items", "rows"))
        new_count = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            normalized = normalize_offer_item(item)
            normalized["api_source"] = "v1-fallback"
            identifier = str(normalized.get("id") or "")
            if not identifier or identifier in seen:
                continue
            seen.add(identifier)
            collected.append(normalized)
            new_count += 1
        if not items or new_count == 0 or len(items) < count:
            break
    logger.info("Loaded %s offers through GGSEL V1 fallback", len(collected))
    return collected


def collect_all_offers(force: bool = False) -> list[dict[str, Any]]:
    now = time.time()
    with _offer_cache_lock:
        if not force and _offer_cache["items"] and now < float(_offer_cache["expires_at"]):
            return list(_offer_cache["items"])

    limit = MINIAPP_OFFERS_API_PAGE_SIZE
    collected: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        for page in range(1, MINIAPP_OFFERS_MAX_PAGES + 1):
            try:
                data = ggsel.offers_v2(page=page, limit=limit)
            except APIError:
                if page == 1 or not collected:
                    raise
                logger.exception("GGSEL V2 offers page %s failed; returning partial list", page)
                break
            items = extract_list(data, ("items", "offers", "rows"))
            new_count = 0
            for item in items:
                normalized = normalize_offer_item(item)
                normalized["api_source"] = "v2"
                identifier = str(normalized.get("id") or "")
                if not identifier or identifier in seen:
                    continue
                seen.add(identifier)
                collected.append(normalized)
                new_count += 1
            if not items or new_count == 0 or not _has_next_page(data, items, page, limit):
                break
    except APIError as exc:
        if exc.status != 500:
            raise
        logger.warning(
            "GGSEL V2 /offers returned HTTP 500; switching GUI list to V1 products API"
        )
        collected = _collect_offers_v1_fallback()

    with _offer_cache_lock:
        _offer_cache["items"] = list(collected)
        _offer_cache["expires_at"] = now + MINIAPP_OFFERS_CACHE_SECONDS
    return collected


def invalidate_offer_cache() -> None:
    with _offer_cache_lock:
        _offer_cache["expires_at"] = 0.0
        _offer_cache["items"] = []


def normalize_product_item(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    value = str(item.get("value") or "")
    masked = value if len(value) <= 22 else value[:9] + "…" + value[-7:]
    return {
        "id": item.get("id"),
        "status": item.get("status"),
        "value": masked,
        "created_at": item.get("created_at"),
    }


def miniapp_success(data: Any = None, **extra: Any):
    payload = {"ok": True, "data": data}
    payload.update(extra)
    return jsonify(payload)


def require_confirmation(body: dict[str, Any]) -> None:
    if body.get("confirm") is not True:
        raise ValueError("Операция требует подтверждения")


# ============================================================
# Telegram API
# ============================================================


def telegram_call(method: str, payload: dict[str, Any]) -> Any:
    response = requests.post(
        f"{TELEGRAM_API}/{method}", json=payload, timeout=REQUEST_TIMEOUT
    )
    try:
        data = response.json()
    except ValueError:
        data = response.text
    if not response.ok or not isinstance(data, dict) or not data.get("ok"):
        raise APIError("Telegram", response.status_code, compact_json(data, 700), data)
    return data


def split_text(text: str, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


def send_text(
    text: str,
    *,
    reply_markup: Optional[dict[str, Any]] = None,
    parse_mode: str = "HTML",
) -> None:
    chunks = split_text(text)
    for index, chunk in enumerate(chunks):
        payload: dict[str, Any] = {
            "chat_id": OWNER_ID,
            "text": chunk,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        if reply_markup and index == len(chunks) - 1:
            payload["reply_markup"] = reply_markup
        telegram_call("sendMessage", payload)


def send_photo(photo_url: str, caption: str) -> None:
    telegram_call(
        "sendPhoto",
        {
            "chat_id": OWNER_ID,
            "photo": photo_url,
            "caption": caption[:1024],
            "parse_mode": "HTML",
        },
    )


def answer_callback(callback_id: str, text: str = "") -> None:
    telegram_call(
        "answerCallbackQuery",
        {"callback_query_id": callback_id, "text": text[:200], "show_alert": False},
    )


def confirm_keyboard(action_id: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Подтвердить", "callback_data": f"confirm:{action_id}"},
                {"text": "❌ Отмена", "callback_data": f"cancel:{action_id}"},
            ]
        ]
    }


def main_menu() -> dict[str, Any]:
    rows: list[list[dict[str, Any]]] = []
    if APP_URL:
        rows.append([{"text": "🛍 Открыть панель управления", "web_app": {"url": f"{APP_URL}/app"}}])
    rows.extend([
            [
                {"text": "💰 Баланс", "callback_data": "cmd:balance"},
                {"text": "🧾 Продажи", "callback_data": "cmd:lastsales"},
            ],
            [
                {"text": "📦 Офферы", "callback_data": "cmd:offers"},
                {"text": "💬 Чаты", "callback_data": "cmd:chats"},
            ],
            [
                {"text": "⭐ Отзывы", "callback_data": "cmd:reviews"},
                {"text": "📚 Категории", "callback_data": "cmd:categories"},
            ],
        ])
    return {"inline_keyboard": rows}


# ============================================================
# Formatters
# ============================================================


INVOICE_STATES = {
    1: "создан",
    2: "отменён",
    3: "оплачен",
    4: "выполнен",
    5: "возвращён",
}


def format_balance(data: Any) -> str:
    content = unwrap_v1(data)
    if not isinstance(content, dict):
        return f"<b>Баланс</b>\n<pre>{safe(compact_json(data))}</pre>"
    return (
        "<b>💰 Баланс GGSEL</b>\n\n"
        f"Доступно: <b>{safe(content.get('amount_t_free'))}</b> WMT\n"
        f"Заблокировано: {safe(content.get('amount_t_lock'))} WMT\n"
        f"С ограничением: {safe(content.get('amount_t_plus'))} WMT"
    )


def localized_name(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        ru = next(
            (
                item.get("value")
                for item in value
                if isinstance(item, dict) and item.get("locale") in ("ru", "ru-RU")
            ),
            None,
        )
        if ru:
            return str(ru)
        for item in value:
            if isinstance(item, dict) and item.get("value"):
                return str(item["value"])
    return "—"


def format_receipts(data: Any) -> str:
    content = unwrap_v1(data)
    items = extract_list(content, ("items",))
    if not items:
        return "<b>🧾 Чеки</b>\nНет данных."
    lines = ["<b>🧾 Последние чеки</b>"]
    for item in items[:20]:
        if not isinstance(item, dict):
            continue
        operation = item.get("operation") if isinstance(item.get("operation"), dict) else {}
        product = item.get("product") if isinstance(item.get("product"), dict) else {}
        lines.append(
            "\n"
            f"<b>{safe(localized_name(product.get('name')))}</b>\n"
            f"Дата: {safe(operation.get('datetime'))}\n"
            f"Операция: {safe(operation.get('type'))}\n"
            f"Сумма: {safe(operation.get('price'))} {safe(operation.get('currency'))}\n"
            f"На счёт: {safe(operation.get('on_account'))}"
        )
    return "\n".join(lines)


def category_title(category: dict[str, Any]) -> str:
    return str(
        category.get("title")
        or category.get("name")
        or category.get("title_ru")
        or category.get("tree")
        or "—"
    )


def format_categories(data: Any, query: str = "") -> str:
    categories = extract_list(data, ("category", "categories", "items"))
    if query:
        needle = query.casefold()
        categories = [
            item
            for item in categories
            if isinstance(item, dict) and needle in category_title(item).casefold()
        ]
    if not categories:
        return "<b>📚 Категории</b>\nНичего не найдено."
    lines = ["<b>📚 Категории</b>"]
    for item in categories[:50]:
        if not isinstance(item, dict):
            continue
        lines.append(
            f"• <code>{safe(item.get('id'))}</code> — {safe(category_title(item))}"
            + (f" ({safe(item.get('cnt'))})" if item.get("cnt") is not None else "")
        )
    if len(categories) > 50:
        lines.append(f"\nПоказано 50 из {len(categories)}.")
    return "\n".join(lines)


def offer_name(item: dict[str, Any]) -> str:
    return str(item.get("title_ru") or item.get("name_goods") or item.get("name") or "—")


def offer_id(item: dict[str, Any]) -> Any:
    return item.get("id") or item.get("id_goods") or item.get("product_id")


def format_offers(data: Any, title: str = "📦 Офферы") -> str:
    offers = extract_list(data, ("items", "offers", "rows"))
    if not offers:
        return f"<b>{title}</b>\nНет данных."
    lines = [f"<b>{title}</b>"]
    for item in offers[:30]:
        if not isinstance(item, dict):
            continue
        category = item.get("category") if isinstance(item.get("category"), dict) else {}
        lines.append(
            "\n"
            f"<b>{safe(offer_name(item))}</b>\n"
            f"ID: <code>{safe(offer_id(item))}</code>"
            + (f" · {safe(item.get('status'))}" if item.get("status") else "")
            + "\n"
            f"Цена: {safe(item.get('price') or item.get('price_rur'))} {safe(item.get('currency') or 'RUB')}\n"
            f"Остаток: {safe(item.get('quantity') or item.get('num_in_stock') or item.get('in_stock'))}"
            + (f"\nКатегория: {safe(category.get('title'))}" if category else "")
        )
    return "\n".join(lines)


def format_offer(data: Any) -> str:
    item = data.get("content") if isinstance(data, dict) and isinstance(data.get("content"), dict) else data
    if isinstance(item, dict) and isinstance(item.get("product"), dict):
        item = item["product"]
    if not isinstance(item, dict):
        return f"<pre>{safe(compact_json(data))}</pre>"
    category = item.get("category") if isinstance(item.get("category"), dict) else {}
    return (
        f"<b>📦 {safe(offer_name(item))}</b>\n\n"
        f"ID: <code>{safe(offer_id(item))}</code>\n"
        f"Статус: {safe(item.get('status'))}\n"
        f"Цена: {safe(item.get('price'))} {safe(item.get('currency'))}\n"
        f"Остаток: {safe(item.get('quantity') or item.get('num_in_stock'))}\n"
        f"Автовыдача: {safe(item.get('is_autoselling'))}\n"
        f"Категория: {safe(category.get('title') or category.get('id'))}\n"
        f"Продано: {safe(item.get('sold_products_count'))}\n"
        f"Создан: {safe(item.get('created_at'))}\n"
        f"Обновлён: {safe(item.get('updated_at'))}\n\n"
        f"{safe(item.get('description_ru') or item.get('info'))}"
    )


def format_sales(data: Any) -> str:
    sales = extract_list(data, ("sales",))
    if not sales:
        return "<b>🧾 Последние продажи</b>\nНет данных."
    lines = ["<b>🧾 Последние продажи</b>"]
    total_rub = 0.0
    for sale in sales[:30]:
        if not isinstance(sale, dict):
            continue
        product = sale.get("product") if isinstance(sale.get("product"), dict) else {}
        price_rub = as_float(product.get("price_rub"))
        total_rub += price_rub
        lines.append(
            "\n"
            f"<b>{safe(product.get('name'))}</b>\n"
            f"Заказ: <code>{safe(sale.get('invoice_id'))}</code>\n"
            f"Дата: {safe(sale.get('date'))}\n"
            f"Сумма: {price_rub:g} ₽"
        )
    lines.append(f"\n<b>Итого в показанном списке: {total_rub:g} ₽</b>")
    return "\n".join(lines)


def format_order(invoice_id: str, data: Any) -> str:
    order = data.get("content") if isinstance(data, dict) and isinstance(data.get("content"), dict) else data
    if not isinstance(order, dict):
        return f"<pre>{safe(compact_json(data))}</pre>"
    buyer = order.get("buyer_info") if isinstance(order.get("buyer_info"), dict) else {}
    feedback = order.get("feedback") if isinstance(order.get("feedback"), dict) else {}
    state = as_int(order.get("invoice_state"))
    return (
        f"<b>📦 Заказ {safe(invoice_id)}</b>\n\n"
        f"Товар: <b>{safe(order.get('name'))}</b>\n"
        f"ID товара: <code>{safe(order.get('item_id'))}</code>\n"
        f"Статус: {safe(INVOICE_STATES.get(state, state))}\n"
        f"Зачислено: <b>{safe(order.get('amount'))} {safe(order.get('currency_type'))}</b>\n"
        f"Прибыль: {safe(order.get('profit'))}\n"
        f"Покупка: {safe(order.get('purchase_date'))}\n"
        f"Оплата: {safe(order.get('date_pay'))}\n"
        f"Внешний ID: {safe(order.get('external_order_id'))}\n\n"
        f"Почта: {safe(buyer.get('email'))}\n"
        f"Аккаунт: {safe(buyer.get('account'))}\n"
        f"Телефон: {safe(buyer.get('phone'))}\n"
        f"Способ оплаты: {safe(buyer.get('payment_method'))}\n"
        f"Агрегатор: {safe(buyer.get('payment_aggregator'))}\n\n"
        f"Отзыв: {safe(feedback.get('feedback'))}\n"
        f"Тип: {safe(feedback.get('feedback_type'))}\n"
        f"Ответ продавца: {safe(feedback.get('comment'))}"
    )


def format_unique_code(data: Any) -> str:
    item = unwrap_v1(data)
    if not isinstance(item, dict):
        item = data if isinstance(data, dict) else {}
    state = item.get("unique_code_state") if isinstance(item.get("unique_code_state"), dict) else {}
    return (
        "<b>🔐 Уникальный код</b>\n\n"
        f"Заказ: <code>{safe(item.get('inv'))}</code>\n"
        f"Товар ID: {safe(item.get('id_goods'))}\n"
        f"Сумма: {safe(item.get('amount'))} {safe(item.get('type_curr'))}\n"
        f"Прибыль: {safe(item.get('profit'))}\n"
        f"Почта: {safe(item.get('email'))}\n"
        f"Статус кода: {safe(state.get('state'))}\n"
        f"Проверен: {safe(state.get('date_check'))}\n"
        f"Доставлен: {safe(state.get('date_delivery'))}\n"
        f"Подтверждён: {safe(state.get('date_confirmed'))}"
    )


def format_reviews(data: Any) -> str:
    reviews = extract_list(data, ("reviews",))
    if not reviews:
        return "<b>⭐ Отзывы</b>\nНет данных."
    lines = [
        "<b>⭐ Отзывы</b>",
        f"Всего: {safe(data.get('totalItems') if isinstance(data, dict) else '')} · "
        f"Положительных: {safe(data.get('totalGood') if isinstance(data, dict) else '')} · "
        f"Отрицательных: {safe(data.get('totalBad') if isinstance(data, dict) else '')}",
    ]
    for item in reviews[:20]:
        if not isinstance(item, dict):
            continue
        icon = "👍" if as_int(item.get("good")) else "👎"
        lines.append(
            "\n"
            f"{icon} <b>{safe(item.get('name'))}</b>\n"
            f"{safe(item.get('info'))}\n"
            f"Заказ: <code>{safe(item.get('invoice_id'))}</code> · {safe(item.get('date'))}\n"
            f"Ответ: {safe(item.get('comment'))}"
        )
    return "\n".join(lines)


def format_chats(data: Any) -> str:
    chats = extract_list(data, ("items",))
    if not chats:
        return "<b>💬 Чаты</b>\nНет данных."
    lines = ["<b>💬 Чаты покупателей</b>"]
    for item in chats[:30]:
        if not isinstance(item, dict):
            continue
        lines.append(
            "\n"
            f"Диалог: <code>{safe(item.get('id_i'))}</code>\n"
            f"Email: {safe(item.get('email'))}\n"
            f"Товар ID: {safe(item.get('product'))}\n"
            f"Новых: <b>{safe(item.get('cnt_new'))}</b> · Всего: {safe(item.get('cnt_msg'))}\n"
            f"Последнее: {safe(item.get('last_message'))}"
        )
    return "\n".join(lines)


def format_messages(debate_id: str, data: Any) -> str:
    messages = extract_list(data)
    if not messages:
        return f"<b>💬 Диалог {safe(debate_id)}</b>\nСообщений нет."
    lines = [f"<b>💬 Диалог {safe(debate_id)}</b>"]
    for item in messages[-30:]:
        if not isinstance(item, dict):
            continue
        sender = "Покупатель" if as_int(item.get("buyer")) else "Вы"
        text = item.get("message") or "[файл/изображение]"
        lines.append(
            "\n"
            f"<b>{sender}</b> · {safe(item.get('date_written'))}\n"
            f"{safe(text)}"
        )
        if item.get("filename"):
            lines.append(f"Файл: {safe(item.get('filename'))}")
        if item.get("url"):
            lines.append(f"URL: {safe(item.get('url'))}")
        elif item.get("preview"):
            lines.append(f"Изображение: {safe(item.get('preview'))}")
    return "\n".join(lines)


def format_products(data: Any, offer_id_value: int) -> str:
    products = extract_list(data, ("items", "products"))
    if not products:
        return f"<b>🔑 Склад оффера {offer_id_value}</b>\nНет товаров."
    lines = [f"<b>🔑 Склад оффера {offer_id_value}</b>"]
    for item in products[:50]:
        if not isinstance(item, dict):
            continue
        value = str(item.get("value") or "")
        masked = value if len(value) <= 18 else value[:8] + "…" + value[-6:]
        lines.append(
            f"• <code>{safe(item.get('id'))}</code> · {safe(item.get('status'))} · {safe(masked)}"
        )
    return "\n".join(lines)


# ============================================================
# Revenue calculation from receipts
# ============================================================


def parse_period(args: list[str]) -> tuple[date, date, str]:
    today = datetime.now().date()
    if not args or args[0].lower() == "today":
        return today, today, "сегодня"
    keyword = args[0].lower()
    if keyword in {"7d", "7", "week"}:
        return today - timedelta(days=6), today, "за 7 дней"
    if keyword in {"30d", "30", "month"}:
        return today - timedelta(days=29), today, "за 30 дней"
    if len(args) == 1:
        start = datetime.strptime(args[0], "%Y-%m-%d").date()
        return start, start, start.isoformat()
    start = datetime.strptime(args[0], "%Y-%m-%d").date()
    end = datetime.strptime(args[1], "%Y-%m-%d").date()
    if end < start:
        start, end = end, start
    return start, end, f"{start.isoformat()} — {end.isoformat()}"


def calculate_receipts_period(start: date, end: date) -> tuple[float, float, int, bool]:
    total_received = 0.0
    total_price = 0.0
    matched = 0
    complete = False

    for page in range(1, RECEIPTS_MAX_PAGES + 1):
        data = ggsel.receipts(page=page, count=100)
        content = unwrap_v1(data)
        items = extract_list(content, ("items",))
        if not items:
            complete = True
            break

        oldest: Optional[date] = None
        for item in items:
            if not isinstance(item, dict):
                continue
            operation = item.get("operation") if isinstance(item.get("operation"), dict) else {}
            product = item.get("product") if isinstance(item.get("product"), dict) else None
            dt = parse_iso_datetime(operation.get("datetime"))
            if dt is None:
                continue
            operation_date = dt.date()
            oldest = operation_date if oldest is None else min(oldest, operation_date)
            if start <= operation_date <= end and product:
                received = as_float(operation.get("on_account"))
                price = as_float(operation.get("price"))
                if received > 0 or price > 0:
                    total_received += received
                    total_price += price
                    matched += 1

        has_next = bool(content.get("has_next_page")) if isinstance(content, dict) else False
        if oldest is not None and oldest < start:
            complete = True
            break
        if not has_next:
            complete = True
            break

    return total_received, total_price, matched, complete


# ============================================================
# Pending action execution
# ============================================================


def action_description(action_type: str, payload: dict[str, Any]) -> str:
    if action_type == "reply":
        return (
            f"Отправить ответ в чат <code>{safe(payload['debate_id'])}</code>?\n\n"
            f"{safe(payload['message'])}"
        )
    if action_type == "prices":
        pairs = ", ".join(f"{item['id']} → {item['price']} ₽" for item in payload["items"])
        return f"Изменить цены офферов?\n\n{safe(pairs)}"
    if action_type in {"activate", "pause", "delete"}:
        return f"Действие <b>{safe(action_type)}</b> для офферов: {safe(payload['offer_ids'])}?"
    if action_type == "stock_add":
        return (
            f"Добавить {len(payload['values'])} позиций на склад оффера "
            f"<code>{safe(payload['offer_id'])}</code>?"
        )
    if action_type == "stock_archive":
        target = "весь склад" if payload.get("delete_all") else payload.get("product_ids")
        return f"Архивировать {safe(target)} у оффера <code>{safe(payload['offer_id'])}</code>?"
    if action_type == "offer_create":
        return f"Создать новый оффер?\n<pre>{safe(compact_json(payload['data'], 2500))}</pre>"
    if action_type == "offer_patch":
        return (
            f"Изменить оффер <code>{safe(payload['offer_id'])}</code>?\n"
            f"<pre>{safe(compact_json(payload['data'], 2500))}</pre>"
        )
    if action_type == "v1_bulk_prices":
        return f"Отправить массовое обновление цен V1?\n<pre>{safe(compact_json(payload['data'], 2500))}</pre>"
    return f"Выполнить действие {safe(action_type)}?"


def execute_action(action_type: str, payload: dict[str, Any]) -> str:
    if action_type == "reply":
        result = ggsel.send_chat_message(str(payload["debate_id"]), str(payload["message"]))
        return f"✅ Сообщение отправлено.\n<pre>{safe(compact_json(result, 1000))}</pre>"

    if action_type == "prices":
        results = []
        for item in payload["items"]:
            results.append(
                {
                    "id": item["id"],
                    "result": ggsel.patch_offer_v2(int(item["id"]), {"price": item["price"]}),
                }
            )
        return f"✅ Цены обновлены.\n<pre>{safe(compact_json(results, 2500))}</pre>"

    if action_type in {"activate", "pause", "delete"}:
        result = ggsel.batch_offers_v2(action_type, [int(x) for x in payload["offer_ids"]])
        return f"✅ Задача создана.\n<pre>{safe(compact_json(result, 2000))}</pre>"

    if action_type == "stock_add":
        result = ggsel.add_products_v2(int(payload["offer_id"]), list(payload["values"]))
        return f"✅ Товары добавлены.\n<pre>{safe(compact_json(result, 1500))}</pre>"

    if action_type == "stock_archive":
        result = ggsel.archive_products_v2(
            int(payload["offer_id"]),
            [int(x) for x in payload.get("product_ids", [])],
            bool(payload.get("delete_all")),
        )
        return f"✅ Архивация запущена.\n<pre>{safe(compact_json(result, 1500))}</pre>"

    if action_type == "offer_create":
        result = ggsel.create_offer_v2(dict(payload["data"]))
        return f"✅ Оффер создан.\n<pre>{safe(compact_json(result, 2500))}</pre>"

    if action_type == "offer_patch":
        result = ggsel.patch_offer_v2(int(payload["offer_id"]), dict(payload["data"]))
        return f"✅ Оффер изменён.\n<pre>{safe(compact_json(result, 2500))}</pre>"

    if action_type == "v1_bulk_prices":
        result = ggsel.bulk_prices_v1(dict(payload["data"]))
        return f"✅ Массовое обновление отправлено.\n<pre>{safe(compact_json(result, 2500))}</pre>"

    raise ValueError(f"Unknown action: {action_type}")


def propose_action(action_type: str, payload: dict[str, Any]) -> None:
    action_id = create_pending_action(action_type, payload)
    send_text(
        "<b>⚠️ Подтверждение операции</b>\n\n" + action_description(action_type, payload),
        reply_markup=confirm_keyboard(action_id),
    )


# ============================================================
# Telegram commands
# ============================================================


HELP_TEXT = """<b>GGSEL Seller Bot</b>

<b>Просмотр</b>
/categories [текст] — категории
/balance — баланс
/receipts [страница] [количество] — чеки
/offers [страница] — офферы V2
/offer ID — карточка оффера
/search текст — поиск среди офферов
/lastsales — последние продажи
/sum today|7d|30d|ДАТА ДАТА — полученные средства
/order INVOICE — заказ
/code UNIQUE_CODE — проверка кода
/reviews [страница] — отзывы
/chats [страница] — чаты
/chat ID — сообщения диалога
/v2products OFFER_ID [страница] — склад
/job JOB_ID — результат фоновой задачи

<b>Изменения с подтверждением</b>
/reply CHAT_ID текст — ответ покупателю
/prices 123=499,124=599 — несколько цен
/activate 1,2,3 — активировать офферы
/pause 1,2,3 — приостановить
/delete 1,2,3 — архивировать офферы
/v2add OFFER_ID + новые строки с товарами
/v2archive OFFER_ID all|1,2,3
/v2create {JSON}
/v2patch OFFER_ID {JSON}
/v1bulk {JSON} — сырой V1 bulk price payload
"""


def normalize_command(text: str) -> tuple[str, str]:
    text = text.strip()
    if not text.startswith("/"):
        return "", text
    first, _, rest = text.partition(" ")
    command = first.split("@", 1)[0].lower()
    return command, rest.strip()


def handle_command(text: str) -> None:
    command, arg_text = normalize_command(text)
    try:
        if command in {"/start", "/help"}:
            send_text(HELP_TEXT, reply_markup=main_menu())
            return

        if command == "/categories":
            if arg_text:
                data = ggsel.search_categories_v2(arg_text)
                send_text(format_categories(data, arg_text))
            else:
                data = ggsel.categories_v2()
                send_text(format_categories(data))
            return

        if command == "/balance":
            send_text(format_balance(ggsel.balance()))
            return

        if command == "/receipts":
            parts = arg_text.split()
            page = int(parts[0]) if parts else 1
            count = int(parts[1]) if len(parts) > 1 else 20
            count = max(1, min(count, 100))
            send_text(format_receipts(ggsel.receipts(page, count)))
            return

        if command == "/offers":
            page = int(arg_text or "1")
            send_text(format_offers(ggsel.offers_v2(page=page, limit=30)))
            return

        if command == "/offer":
            if not arg_text:
                raise ValueError("Формат: /offer ID")
            send_text(format_offer(ggsel.offer_v2(int(arg_text))))
            return

        if command == "/search":
            if not arg_text:
                raise ValueError("Формат: /search название")
            needle = arg_text.casefold()
            found: list[dict[str, Any]] = []
            for page in range(1, 11):
                data = ggsel.offers_v2(page=page, limit=100)
                items = [x for x in extract_list(data, ("items", "offers")) if isinstance(x, dict)]
                found.extend([x for x in items if needle in offer_name(x).casefold()])
                pagination = data.get("pagination") if isinstance(data, dict) else None
                total_pages = as_int(pagination.get("total_pages")) if isinstance(pagination, dict) else 0
                if not items or (total_pages and page >= total_pages):
                    break
            send_text(format_offers(found, title=f"🔎 Поиск: {safe(arg_text)}"))
            return

        if command == "/lastsales":
            data = ggsel.last_sales()
            upsert_sales(data)
            send_text(format_sales(data))
            return

        if command == "/sum":
            args = arg_text.split()
            start, end, label = parse_period(args)
            received, price, count, complete = calculate_receipts_period(start, end)
            warning = "" if complete else "\n⚠️ Достигнут лимит страниц; результат может быть неполным."
            send_text(
                f"<b>💵 Средства {safe(label)}</b>\n\n"
                f"Зачислено на счёт: <b>{received:.2f}</b>\n"
                f"Сумма операций: {price:.2f}\n"
                f"Товарных операций: {count}{warning}"
            )
            return

        if command == "/order":
            if not arg_text:
                raise ValueError("Формат: /order НОМЕР_ЗАКАЗА")
            data = ggsel.order_info(arg_text)
            upsert_order(arg_text, data if isinstance(data, dict) else {})
            send_text(format_order(arg_text, data))
            return

        if command == "/code":
            if not arg_text:
                raise ValueError("Формат: /code УНИКАЛЬНЫЙ_КОД")
            send_text(format_unique_code(ggsel.unique_code(arg_text)))
            return

        if command == "/reviews":
            page = int(arg_text or "1")
            send_text(format_reviews(ggsel.reviews(page)))
            return

        if command == "/chats":
            page = int(arg_text or "1")
            send_text(format_chats(ggsel.chats(page)))
            return

        if command == "/chat":
            if not arg_text:
                raise ValueError("Формат: /chat ID_ДИАЛОГА")
            send_text(format_messages(arg_text, ggsel.chat_messages(arg_text)))
            return

        if command == "/reply":
            debate_id, separator, message = arg_text.partition(" ")
            if not separator or not message.strip():
                raise ValueError("Формат: /reply ID_ДИАЛОГА текст")
            propose_action("reply", {"debate_id": debate_id, "message": message.strip()})
            return

        if command == "/prices":
            if not arg_text:
                raise ValueError("Формат: /prices 123=499,124=599")
            items = []
            for pair in arg_text.split(","):
                item_id, separator, price = pair.partition("=")
                if not separator:
                    raise ValueError(f"Нет '=' в {pair}")
                items.append({"id": int(item_id.strip()), "price": as_float(price.strip())})
            propose_action("prices", {"items": items})
            return

        if command in {"/activate", "/pause", "/delete"}:
            action = command[1:]
            propose_action(action, {"offer_ids": parse_id_list(arg_text)})
            return

        if command == "/v2products":
            parts = arg_text.split()
            if not parts:
                raise ValueError("Формат: /v2products OFFER_ID [страница]")
            offer = int(parts[0])
            page = int(parts[1]) if len(parts) > 1 else 1
            send_text(format_products(ggsel.products_v2(offer, page, 50), offer))
            return

        if command == "/v2add":
            first_line, separator, values_text = arg_text.partition("\n")
            if not separator:
                raise ValueError("Формат: /v2add OFFER_ID, затем каждый товар с новой строки")
            offer = int(first_line.strip())
            values = [line.strip() for line in values_text.splitlines() if line.strip()]
            if not values:
                raise ValueError("Список товаров пуст")
            propose_action("stock_add", {"offer_id": offer, "values": values})
            return

        if command == "/v2archive":
            parts = arg_text.split(maxsplit=1)
            if len(parts) != 2:
                raise ValueError("Формат: /v2archive OFFER_ID all|1,2,3")
            offer = int(parts[0])
            if parts[1].strip().lower() == "all":
                payload = {"offer_id": offer, "delete_all": True, "product_ids": []}
            else:
                payload = {
                    "offer_id": offer,
                    "delete_all": False,
                    "product_ids": parse_id_list(parts[1]),
                }
            propose_action("stock_archive", payload)
            return

        if command == "/job":
            if not arg_text:
                raise ValueError("Формат: /job JOB_ID")
            send_text(f"<pre>{safe(compact_json(ggsel.async_job_v2(arg_text), 3500))}</pre>")
            return

        if command == "/v2create":
            if not arg_text:
                raise ValueError("Формат: /v2create {JSON}")
            propose_action("offer_create", {"data": parse_json_argument(arg_text)})
            return

        if command == "/v2patch":
            offer_text, separator, json_text = arg_text.partition(" ")
            if not separator:
                raise ValueError("Формат: /v2patch OFFER_ID {JSON}")
            propose_action(
                "offer_patch",
                {"offer_id": int(offer_text), "data": parse_json_argument(json_text)},
            )
            return

        if command == "/v1bulk":
            if not arg_text:
                raise ValueError("Формат: /v1bulk {JSON}")
            propose_action("v1_bulk_prices", {"data": parse_json_argument(arg_text)})
            return

        if command == "/v1task":
            if not arg_text:
                raise ValueError("Формат: /v1task TASK_ID")
            send_text(
                f"<pre>{safe(compact_json(ggsel.bulk_price_status_v1(arg_text), 3500))}</pre>"
            )
            return

        if command:
            send_text("Неизвестная команда. Нажми /help")
    except (ValueError, APIError, RuntimeError, requests.RequestException) as exc:
        logger.warning("Command failed: %s", exc)
        send_text(f"❌ <b>Ошибка</b>\n<code>{safe(exc)}</code>")
    except Exception as exc:
        logger.exception("Unexpected command error")
        send_text(f"❌ Непредвиденная ошибка: <code>{safe(exc)}</code>")


# ============================================================
# GGSEL incoming message webhook
# ============================================================


def process_ggsel_event(data: dict[str, Any]) -> None:
    debate_id = str(data.get("DebateId") or "")
    message_id = str(data.get("MessageId") or "")
    invoice_id = str(data.get("InvoiceId") or "")
    message_date = str(data.get("MessageDate") or "")
    message_text = str(data.get("Message") or "")
    image_url = str(data.get("ImagePath") or "")

    save_message_record(
        message_id=message_id,
        debate_id=debate_id,
        invoice_id=invoice_id,
        sender="buyer",
        message_text=message_text,
        image_url=image_url,
        message_date=message_date,
    )

    order: dict[str, Any] = {}
    if invoice_id:
        try:
            response = ggsel.order_info(invoice_id)
            if isinstance(response, dict):
                order = upsert_order(invoice_id, response)
        except Exception:
            logger.exception("Unable to load order %s", invoice_id)

    buyer = order.get("buyer_info") if isinstance(order.get("buyer_info"), dict) else {}
    prefix = "🚨 <b>ПРОБЛЕМНОЕ СООБЩЕНИЕ</b>\n\n" if any(
        word in message_text.casefold()
        for word in ("не работает", "возврат", "обман", "ошибка", "не пришло")
    ) else ""

    text = (
        prefix
        + "📩 <b>Новое сообщение GGSEL</b>\n\n"
        + f"Товар: <b>{safe(order.get('name'))}</b>\n"
        + f"Заказ: <code>{safe(invoice_id)}</code>\n"
        + f"Диалог: <code>{safe(debate_id)}</code>\n"
        + f"Почта: {safe(buyer.get('email'))}\n"
        + f"Аккаунт: {safe(buyer.get('account'))}\n"
        + f"Зачислено: {safe(order.get('amount'))} {safe(order.get('currency_type'))}\n"
        + f"Дата: {safe(message_date)}\n\n"
        + f"<b>Сообщение:</b>\n{safe(message_text or '[изображение]')}"
    )
    send_text(text)

    if image_url:
        caption = (
            "🖼 <b>Изображение покупателя</b>\n\n"
            f"Товар: {safe(order.get('name'))}\n"
            f"Заказ: <code>{safe(invoice_id)}</code>\n"
            f"Диалог: <code>{safe(debate_id)}</code>\n"
            f"Почта: {safe(buyer.get('email'))}"
        )
        try:
            send_photo(image_url, caption)
        except Exception:
            logger.exception("Unable to send photo")
            send_text(caption + f"\n\nСсылка: {safe(image_url)}")


# ============================================================
# Mini App routes
# ============================================================


def _balance_payload(data: Any) -> dict[str, Any]:
    content = unwrap_v1(data)
    return content if isinstance(content, dict) else {"raw": data}


def _sales_payload(data: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for sale in extract_list(data, ("sales", "items")):
        if not isinstance(sale, dict):
            continue
        product = sale.get("product") if isinstance(sale.get("product"), dict) else {}
        result.append(
            {
                "invoice_id": sale.get("invoice_id") or sale.get("inv"),
                "date": sale.get("date") or sale.get("purchase_date"),
                "name": product.get("name") or sale.get("name"),
                "price_rub": as_float(product.get("price_rub") or sale.get("price_rub")),
                "price_usd": as_float(product.get("price_usd") or sale.get("price_usd")),
                "price_eur": as_float(product.get("price_eur") or sale.get("price_eur")),
            }
        )
    return result


def _order_payload(invoice_id: str, data: Any) -> dict[str, Any]:
    item = data.get("content") if isinstance(data, dict) and isinstance(data.get("content"), dict) else data
    if not isinstance(item, dict):
        return {"invoice_id": invoice_id, "raw": data}
    buyer = item.get("buyer_info") if isinstance(item.get("buyer_info"), dict) else {}
    feedback = item.get("feedback") if isinstance(item.get("feedback"), dict) else {}
    return {
        "invoice_id": invoice_id,
        "name": item.get("name"),
        "item_id": item.get("item_id"),
        "amount": item.get("amount"),
        "currency": item.get("currency_type"),
        "profit": item.get("profit"),
        "invoice_state": item.get("invoice_state"),
        "purchase_date": item.get("purchase_date"),
        "date_pay": item.get("date_pay"),
        "external_order_id": item.get("external_order_id"),
        "buyer": buyer,
        "feedback": feedback,
        "options": item.get("options") if isinstance(item.get("options"), list) else [],
    }


def _review_payload(data: Any) -> list[dict[str, Any]]:
    return [item for item in extract_list(data, ("reviews", "items")) if isinstance(item, dict)]


def _chat_payload(data: Any) -> list[dict[str, Any]]:
    return [item for item in extract_list(data, ("chats", "items")) if isinstance(item, dict)]


def _message_payload(data: Any) -> list[dict[str, Any]]:
    return [item for item in extract_list(data, ("messages", "items")) if isinstance(item, dict)]


@app.get("/app")
def miniapp_page():
    return render_template("miniapp.html", app_url=APP_URL)


@app.get("/app/api/me")
@miniapp_api
def miniapp_me(user: dict[str, Any]):
    return miniapp_success(
        {
            "id": user.get("id"),
            "first_name": user.get("first_name"),
            "username": user.get("username"),
            "seller_id": GGSEL_SELLER_ID,
        }
    )


@app.get("/app/api/dashboard")
@miniapp_api
def miniapp_dashboard(user: dict[str, Any]):
    errors: dict[str, str] = {}
    balance_data: dict[str, Any] = {}
    sales_data: list[dict[str, Any]] = []
    offers_data: list[dict[str, Any]] = []

    try:
        balance_data = _balance_payload(ggsel.balance())
    except Exception as exc:
        errors["balance"] = str(exc)
    try:
        sales_data = _sales_payload(ggsel.last_sales())[:10]
    except Exception as exc:
        errors["sales"] = str(exc)
    try:
        offers_data = collect_all_offers()
    except Exception as exc:
        errors["offers"] = str(exc)

    stats = {
        "offers": len(offers_data),
        "active": sum(1 for x in offers_data if x.get("status") == "active"),
        "paused": sum(1 for x in offers_data if x.get("status") == "paused"),
        "out_of_stock": sum(1 for x in offers_data if as_int(x.get("quantity")) <= 0),
    }
    return miniapp_success(
        {"balance": balance_data, "sales": sales_data, "stats": stats}, errors=errors
    )


@app.get("/app/api/offers")
@miniapp_api
def miniapp_offers(user: dict[str, Any]):
    query = str(request.args.get("q") or "").strip().casefold()
    status = str(request.args.get("status") or "all").strip().lower()
    force = request.args.get("refresh") == "1"
    page = max(1, as_int(request.args.get("page"), 1))
    per_page = max(10, min(as_int(request.args.get("per_page"), 30), 100))

    offers = collect_all_offers(force=force)
    if query:
        offers = [
            item
            for item in offers
            if query in str(item.get("title") or "").casefold()
            or query in str(item.get("id") or "").casefold()
            or query in str(item.get("category") or "").casefold()
        ]
    if status == "out_of_stock":
        offers = [item for item in offers if as_int(item.get("quantity")) <= 0]
    elif status != "all":
        offers = [item for item in offers if str(item.get("status")) == status]

    total = len(offers)
    start = (page - 1) * per_page
    items = offers[start : start + per_page]
    return miniapp_success(
        items,
        pagination={
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": max(1, (total + per_page - 1) // per_page),
        },
    )


@app.get("/app/api/offers/<int:offer_id_value>")
@miniapp_api
def miniapp_offer(user: dict[str, Any], offer_id_value: int):
    data = ggsel.offer_v2(offer_id_value)
    item = data.get("content") if isinstance(data, dict) and isinstance(data.get("content"), dict) else data
    if isinstance(item, dict) and isinstance(item.get("product"), dict):
        item = item["product"]
    return miniapp_success({"normalized": normalize_offer_item(item), "raw": item})


@app.get("/app/api/offers/<int:offer_id_value>/products")
@miniapp_api
def miniapp_products(user: dict[str, Any], offer_id_value: int):
    page = max(1, as_int(request.args.get("page"), 1))
    limit = max(10, min(as_int(request.args.get("limit"), 50), 100))
    data = ggsel.products_v2(offer_id_value, page, limit)
    items = [normalize_product_item(x) for x in extract_list(data, ("items", "products")) if isinstance(x, dict)]
    return miniapp_success(items, raw_meta=data if isinstance(data, dict) else {})


@app.post("/app/api/offers/<int:offer_id_value>/products")
@miniapp_api
def miniapp_add_products(user: dict[str, Any], offer_id_value: int):
    body = request.get_json(silent=True) or {}
    require_confirmation(body)
    raw_values = body.get("values")
    if not isinstance(raw_values, list):
        raise ValueError("values должен быть массивом строк")
    values = list(dict.fromkeys(str(value).strip() for value in raw_values if str(value).strip()))
    if not values:
        raise ValueError("Список содержимого пуст")
    if len(values) > 2000:
        raise ValueError("За один раз можно загрузить не более 2000 строк")

    results: list[Any] = []
    for index in range(0, len(values), 100):
        results.append(ggsel.add_products_v2(offer_id_value, values[index : index + 100]))
    invalidate_offer_cache()
    audit_miniapp(user.get("id"), "stock_add", offer_id_value, {"count": len(values)})
    return miniapp_success({"added": len(values), "batches": len(results), "results": results})


@app.delete("/app/api/offers/<int:offer_id_value>/products")
@miniapp_api
def miniapp_archive_products(user: dict[str, Any], offer_id_value: int):
    body = request.get_json(silent=True) or {}
    require_confirmation(body)
    delete_all = body.get("delete_all") is True
    product_ids = body.get("product_ids") if isinstance(body.get("product_ids"), list) else []
    product_ids = [int(x) for x in product_ids]
    if not delete_all and not product_ids:
        raise ValueError("Выберите позиции или укажите delete_all")
    result = ggsel.archive_products_v2(offer_id_value, product_ids, delete_all)
    invalidate_offer_cache()
    audit_miniapp(
        user.get("id"), "stock_archive", offer_id_value, {"delete_all": delete_all, "product_ids": product_ids}
    )
    return miniapp_success(result)


@app.patch("/app/api/offers/<int:offer_id_value>")
@miniapp_api
def miniapp_patch_offer(user: dict[str, Any], offer_id_value: int):
    body = request.get_json(silent=True) or {}
    require_confirmation(body)
    patch = body.get("patch")
    if not isinstance(patch, dict) or not patch:
        raise ValueError("Не переданы изменения")
    allowed = {
        "title_ru", "title_en", "description_ru", "description_en", "instructions_ru",
        "instructions_en", "price", "is_autoselling", "category_id", "min_quantity",
        "max_quantity", "quantity", "is_unlimited_quantity", "delivery", "post_payment_url",
        "notification_settings", "pre_payment_settings",
    }
    unknown = set(patch) - allowed
    if unknown:
        raise ValueError("Недопустимые поля: " + ", ".join(sorted(unknown)))
    result = ggsel.patch_offer_v2(offer_id_value, patch)
    invalidate_offer_cache()
    audit_miniapp(user.get("id"), "offer_patch", offer_id_value, patch)
    return miniapp_success(result)


@app.post("/app/api/offers")
@miniapp_api
def miniapp_create_offer(user: dict[str, Any]):
    body = request.get_json(silent=True) or {}
    require_confirmation(body)
    data = body.get("data")
    if not isinstance(data, dict):
        raise ValueError("Не переданы данные оффера")
    for field in ("title_ru", "price", "category_id"):
        if data.get(field) in (None, ""):
            raise ValueError(f"Обязательное поле: {field}")
    result = ggsel.create_offer_v2(data)
    invalidate_offer_cache()
    audit_miniapp(user.get("id"), "offer_create", "", data)
    return miniapp_success(result)


@app.post("/app/api/offers/<int:offer_id_value>/action")
@miniapp_api
def miniapp_offer_action(user: dict[str, Any], offer_id_value: int):
    body = request.get_json(silent=True) or {}
    require_confirmation(body)
    action = str(body.get("action") or "")
    if action not in {"activate", "pause", "delete"}:
        raise ValueError("Недопустимое действие")
    result = ggsel.batch_offers_v2(action, [offer_id_value])
    invalidate_offer_cache()
    audit_miniapp(user.get("id"), f"offer_{action}", offer_id_value, result)
    return miniapp_success(result)


@app.post("/app/api/offers/batch-action")
@miniapp_api
def miniapp_batch_action(user: dict[str, Any]):
    body = request.get_json(silent=True) or {}
    require_confirmation(body)
    action = str(body.get("action") or "")
    offer_ids = [int(x) for x in body.get("offer_ids", [])]
    if action not in {"activate", "pause", "delete"}:
        raise ValueError("Недопустимое действие")
    if not offer_ids or len(offer_ids) > 100:
        raise ValueError("Выберите от 1 до 100 офферов")
    result = ggsel.batch_offers_v2(action, offer_ids)
    invalidate_offer_cache()
    audit_miniapp(user.get("id"), f"offers_{action}", ",".join(map(str, offer_ids)), result)
    return miniapp_success(result)


@app.get("/app/api/categories")
@miniapp_api
def miniapp_categories(user: dict[str, Any]):
    query = str(request.args.get("q") or "").strip()
    data = ggsel.search_categories_v2(query) if query else ggsel.categories_v2()
    items = []
    for category in extract_list(data, ("category", "categories", "items")):
        if isinstance(category, dict):
            items.append({"id": category.get("id"), "title": category_title(category), "count": category.get("cnt")})
    return miniapp_success(items)


@app.get("/app/api/balance")
@miniapp_api
def miniapp_balance(user: dict[str, Any]):
    return miniapp_success(_balance_payload(ggsel.balance()))


@app.get("/app/api/receipts")
@miniapp_api
def miniapp_receipts(user: dict[str, Any]):
    page = max(1, as_int(request.args.get("page"), 1))
    count = max(1, min(as_int(request.args.get("count"), 30), 100))
    data = ggsel.receipts(page, count)
    content = unwrap_v1(data)
    return miniapp_success(
        extract_list(content, ("items",)), raw_meta=content if isinstance(content, dict) else {}
    )


@app.get("/app/api/revenue")
@miniapp_api
def miniapp_revenue(user: dict[str, Any]):
    start_text = str(request.args.get("start") or date.today().isoformat())
    end_text = str(request.args.get("end") or start_text)
    start = datetime.strptime(start_text, "%Y-%m-%d").date()
    end = datetime.strptime(end_text, "%Y-%m-%d").date()
    if end < start:
        start, end = end, start
    received, gross, count, complete = calculate_receipts_period(start, end)
    return miniapp_success(
        {"start": start.isoformat(), "end": end.isoformat(), "received": received, "gross": gross, "count": count, "complete": complete}
    )


@app.get("/app/api/sales")
@miniapp_api
def miniapp_sales(user: dict[str, Any]):
    return miniapp_success(_sales_payload(ggsel.last_sales()))


@app.get("/app/api/orders/<invoice_id>")
@miniapp_api
def miniapp_order(user: dict[str, Any], invoice_id: str):
    return miniapp_success(_order_payload(invoice_id, ggsel.order_info(invoice_id)))


@app.get("/app/api/reviews")
@miniapp_api
def miniapp_reviews(user: dict[str, Any]):
    page = max(1, as_int(request.args.get("page"), 1))
    return miniapp_success(_review_payload(ggsel.reviews(page)))


@app.get("/app/api/chats")
@miniapp_api
def miniapp_chats(user: dict[str, Any]):
    page = max(1, as_int(request.args.get("page"), 1))
    return miniapp_success(_chat_payload(ggsel.chats(page)))


@app.get("/app/api/chats/<debate_id>")
@miniapp_api
def miniapp_chat_messages(user: dict[str, Any], debate_id: str):
    return miniapp_success(_message_payload(ggsel.chat_messages(debate_id)))


@app.post("/app/api/chats/<debate_id>/messages")
@miniapp_api
def miniapp_send_chat_message(user: dict[str, Any], debate_id: str):
    body = request.get_json(silent=True) or {}
    require_confirmation(body)
    message = str(body.get("message") or "").strip()
    if not message:
        raise ValueError("Сообщение пустое")
    if len(message) > 4000:
        raise ValueError("Сообщение слишком длинное")
    result = ggsel.send_chat_message(debate_id, message)
    audit_miniapp(user.get("id"), "chat_reply", debate_id, {"length": len(message)})
    return miniapp_success(result)


@app.get("/app/api/audit")
@miniapp_api
def miniapp_audit_log(user: dict[str, Any]):
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT action, target, payload_json, created_at FROM miniapp_audit ORDER BY id DESC LIMIT 50"
        ).fetchall()
    return miniapp_success([dict(row) for row in rows])


# ============================================================
# Flask routes
# ============================================================


@app.get("/")
def health():
    return jsonify({"ok": True, "service": "ggsel-telegram-bot-miniapp-v4"})


@app.get("/setup-telegram-webhook")
def setup_telegram_webhook():
    if SETUP_SECRET and request.args.get("secret") != SETUP_SECRET:
        return jsonify({"ok": False, "error": "forbidden"}), 403
    if not APP_URL:
        return jsonify({"ok": False, "error": "APP_URL is not set"}), 400
    payload: dict[str, Any] = {
        "url": f"{APP_URL}/telegram",
        "allowed_updates": ["message", "callback_query"],
        "drop_pending_updates": False,
    }
    if TELEGRAM_WEBHOOK_SECRET:
        payload["secret_token"] = TELEGRAM_WEBHOOK_SECRET
    webhook_result = telegram_call("setWebhook", payload)
    menu_result = telegram_call(
        "setChatMenuButton",
        {
            "chat_id": OWNER_ID,
            "menu_button": {
                "type": "web_app",
                "text": "Управление",
                "web_app": {"url": f"{APP_URL}/app"},
            },
        },
    )
    commands_result = telegram_call(
        "setMyCommands",
        {
            "commands": [
                {"command": "start", "description": "Открыть меню"},
                {"command": "balance", "description": "Баланс GGSEL"},
                {"command": "lastsales", "description": "Последние продажи"},
                {"command": "offers", "description": "Список офферов"},
                {"command": "chats", "description": "Чаты покупателей"},
                {"command": "help", "description": "Справка"},
            ],
            "scope": {"type": "chat", "chat_id": OWNER_ID},
        },
    )
    return jsonify({"ok": True, "webhook": webhook_result, "menu": menu_result, "commands": commands_result})


@app.post("/telegram")
def telegram_webhook():
    if TELEGRAM_WEBHOOK_SECRET:
        received = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not secrets.compare_digest(received, TELEGRAM_WEBHOOK_SECRET):
            return jsonify({"ok": False}), 403

    update = request.get_json(silent=True) or {}

    if "callback_query" in update:
        query = update.get("callback_query") or {}
        user_id = str((query.get("from") or {}).get("id") or "")
        chat_id = str((((query.get("message") or {}).get("chat") or {}).get("id")) or "")
        if user_id != OWNER_ID or chat_id != OWNER_ID:
            return jsonify({"ok": True, "ignored": True})

        callback_id = str(query.get("id") or "")
        data = str(query.get("data") or "")
        try:
            answer_callback(callback_id, "Принято")
        except Exception:
            logger.exception("Unable to answer callback")

        if data.startswith("confirm:"):
            action_id = data.split(":", 1)[1]

            def confirm_task():
                action = pop_pending_action(action_id)
                if action is None:
                    send_text("⌛ Операция не найдена или подтверждение истекло.")
                    return
                action_type, payload = action
                try:
                    send_text(execute_action(action_type, payload))
                except Exception as exc:
                    logger.exception("Action failed")
                    send_text(f"❌ Операция не выполнена: <code>{safe(exc)}</code>")

            run_background(confirm_task)
        elif data.startswith("cancel:"):
            cancel_pending_action(data.split(":", 1)[1])
            run_background(send_text, "Операция отменена.")
        elif data.startswith("cmd:"):
            run_background(handle_command, "/" + data.split(":", 1)[1])
        return jsonify({"ok": True})

    message = update.get("message") or {}
    user_id = str((message.get("from") or {}).get("id") or "")
    chat_id = str((message.get("chat") or {}).get("id") or "")
    if user_id != OWNER_ID or chat_id != OWNER_ID:
        return jsonify({"ok": True, "ignored": True})

    text = str(message.get("text") or "")
    if text:
        run_background(handle_command, text)
    return jsonify({"ok": True})


@app.route("/ggsel", methods=["POST", "GET"])
def ggsel_webhook():
    if GGSEL_WEBHOOK_SECRET:
        provided = request.args.get("secret") or request.headers.get("X-GGSEL-Webhook-Secret", "")
        if not secrets.compare_digest(str(provided), GGSEL_WEBHOOK_SECRET):
            return jsonify({"ok": False}), 403

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = request.form.to_dict() or request.args.to_dict()

    identity = "|".join(
        str(data.get(key) or "")
        for key in ("MessageId", "DebateId", "InvoiceId", "MessageDate", "Message", "ImagePath")
    )
    event_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    if remember_event(event_key):
        run_background(process_ggsel_event, data)
    return jsonify({"ok": True})


init_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "3000")))
