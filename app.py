from __future__ import annotations

import csv
import hashlib
import hmac
import io
import html
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import zipfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from typing import Any, Iterable, Optional
from urllib.parse import parse_qsl

import requests
from flask import Flask, jsonify, render_template, request, send_file


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
RECEIPTS_MAX_PAGES = int(os.environ.get("RECEIPTS_MAX_PAGES", "30"))
MINIAPP_AUTH_MAX_AGE = int(os.environ.get("MINIAPP_AUTH_MAX_AGE", "86400"))
MINIAPP_OFFERS_MAX_PAGES = int(os.environ.get("MINIAPP_OFFERS_MAX_PAGES", "20"))
MINIAPP_OFFERS_CACHE_SECONDS = int(os.environ.get("MINIAPP_OFFERS_CACHE_SECONDS", "30"))
DASHBOARD_ANALYTICS_CACHE_SECONDS = int(os.environ.get("DASHBOARD_ANALYTICS_CACHE_SECONDS", "60"))
# GGSEL V2 may return HTTP 500 for an oversized page instead of a validation error.
# Keep this conservative; the client also retries compatible pagination formats.
MINIAPP_OFFERS_API_PAGE_SIZE = max(5, min(int(os.environ.get("MINIAPP_OFFERS_API_PAGE_SIZE", "20")), 50))

TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip()
GGSEL_WEBHOOK_SECRET = os.environ.get("GGSEL_WEBHOOK_SECRET", "").strip()
# Backward-compatible mode accepts the legacy /ggsel URL without ?secret=.
# Disable after updating the GGSEL redirect URL to the signed version.
GGSEL_WEBHOOK_COMPAT_MODE = os.environ.get("GGSEL_WEBHOOK_COMPAT_MODE", "true").strip().lower() in {"1", "true", "yes", "on"}
LOW_STOCK_DEFAULT = max(0, int(os.environ.get("LOW_STOCK_DEFAULT", "3")))
SETUP_SECRET = os.environ.get("SETUP_SECRET", "").strip()
CRON_SECRET = os.environ.get("CRON_SECRET", SETUP_SECRET).strip()
CONTENT_SEARCH_SECRET = os.environ.get("CONTENT_SEARCH_SECRET", BOT_TOKEN).strip()
BACKUP_DIR = os.environ.get("BACKUP_DIR", "/data/backups").strip()
SLA_WARNING_MINUTES = max(1, int(os.environ.get("SLA_WARNING_MINUTES", "30")))
SLA_CRITICAL_MINUTES = max(SLA_WARNING_MINUTES, int(os.environ.get("SLA_CRITICAL_MINUTES", "60")))

if not BOT_TOKEN or not OWNER_ID:
    raise RuntimeError("BOT_TOKEN and OWNER_ID (or CHAT_ID) must be set")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("ggsel-bot")

_dashboard_cache_lock = threading.Lock()
_dashboard_cache: dict[tuple[str, str, str], dict[str, Any]] = {}

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
    if value in (None, ""):
        return None
    text = str(value).strip()
    # GGSEL webhook installations may send Unix time in seconds or milliseconds.
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        try:
            numeric = float(text)
            if numeric > 10_000_000_000:  # milliseconds
                numeric /= 1000.0
            if numeric > 0:
                return datetime.fromtimestamp(numeric, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            pass
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def normalize_datetime_text(value: Any) -> str:
    parsed = parse_iso_datetime(value)
    return parsed.isoformat() if parsed else str(value or "")


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

            CREATE TABLE IF NOT EXISTS offer_settings (
                offer_id TEXT PRIMARY KEY,
                min_stock INTEGER NOT NULL DEFAULT 3,
                auto_activate INTEGER NOT NULL DEFAULT 0,
                auto_pause INTEGER NOT NULL DEFAULT 0,
                alert_sent INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS async_operations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT,
                operation TEXT NOT NULL,
                target TEXT,
                status TEXT NOT NULL DEFAULT 'queued',
                result_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS chat_labels (
                debate_id TEXT PRIMARY KEY,
                label TEXT NOT NULL DEFAULT 'new',
                note TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS offer_favorites (
                offer_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS recent_views (
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                title TEXT,
                metadata_json TEXT,
                viewed_at TEXT NOT NULL,
                PRIMARY KEY(entity_type, entity_id)
            );

            CREATE TABLE IF NOT EXISTS order_notes (
                invoice_id TEXT PRIMARY KEY,
                tag TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS inventory_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                offer_id TEXT,
                product_name TEXT,
                content_hash TEXT NOT NULL,
                content_masked TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'in_stock',
                invoice_id TEXT,
                source TEXT NOT NULL DEFAULT 'upload',
                added_at TEXT NOT NULL,
                sold_at TEXT,
                replaced_at TEXT,
                metadata_json TEXT,
                UNIQUE(content_hash, offer_id)
            );

            CREATE TABLE IF NOT EXISTS automation_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                trigger_type TEXT NOT NULL,
                condition_json TEXT NOT NULL DEFAULT '{}',
                action_json TEXT NOT NULL DEFAULT '{}',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS reply_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL DEFAULT 'Общие',
                name TEXT NOT NULL,
                body TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS api_errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                service TEXT NOT NULL,
                endpoint TEXT,
                status INTEGER,
                message TEXT NOT NULL,
                context_json TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS report_settings (
                id INTEGER PRIMARY KEY CHECK(id=1),
                morning_enabled INTEGER NOT NULL DEFAULT 0,
                evening_enabled INTEGER NOT NULL DEFAULT 0,
                include_finance INTEGER NOT NULL DEFAULT 1,
                include_stock INTEGER NOT NULL DEFAULT 1,
                include_chats INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS conversations (
                conversation_id TEXT PRIMARY KEY,
                customer_key TEXT NOT NULL,
                buyer_email TEXT,
                buyer_account TEXT,
                latest_debate_id TEXT,
                latest_invoice_id TEXT,
                product_name TEXT,
                last_message TEXT,
                last_message_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS conversation_debates (
                debate_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                invoice_id TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS customer_profiles (
                conversation_id TEXT PRIMARY KEY,
                note TEXT NOT NULL DEFAULT '',
                tags_json TEXT NOT NULL DEFAULT '[]',
                pinned INTEGER NOT NULL DEFAULT 0,
                favorite INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_inventory_hash ON inventory_ledger(content_hash);
            CREATE INDEX IF NOT EXISTS idx_inventory_invoice ON inventory_ledger(invoice_id);
            CREATE INDEX IF NOT EXISTS idx_inventory_offer ON inventory_ledger(offer_id, status);
            CREATE INDEX IF NOT EXISTS idx_api_errors_created ON api_errors(created_at);

            CREATE INDEX IF NOT EXISTS idx_messages_debate ON messages(debate_id, id);
            CREATE INDEX IF NOT EXISTS idx_messages_invoice ON messages(invoice_id, id);
            CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);
            CREATE INDEX IF NOT EXISTS idx_operations_created ON async_operations(created_at);
            """
        )
        # Lightweight migrations for installations created before CRM v6.0.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
        if "conversation_id" not in columns:
            conn.execute("ALTER TABLE messages ADD COLUMN conversation_id TEXT")
        if "buyer_email" not in columns:
            conn.execute("ALTER TABLE messages ADD COLUMN buyer_email TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_conversations_updated ON conversations(updated_at)")


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
    conversation_id: str = "",
    buyer_email: str = "",
) -> None:
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO messages(
                message_id, debate_id, invoice_id, sender, message_text,
                image_url, message_date, created_at, conversation_id, buyer_email
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                debate_id,
                invoice_id,
                sender,
                message_text,
                image_url,
                normalize_datetime_text(message_date),
                datetime.now(timezone.utc).isoformat(),
                conversation_id or None,
                buyer_email or None,
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
        # The official endpoint documents no query parameters and returns the
        # unread conversation list. Some installations accept page, others do
        # not, so use the documented request first and keep a compatibility
        # fallback.
        try:
            return self.v1_request("GET", "/api_sellers/api/debates/v2/chats")
        except APIError as exc:
            if exc.status not in (400, 422):
                raise
            return self.v1_request(
                "GET", "/api_sellers/api/debates/v2/chats", params={"page": page}
            )

    def chat_messages(self, debate_id: str) -> Any:
        return self.v1_request(
            "GET", "/api_sellers/api/debates/v2", params={"id_i": debate_id}
        )

    def send_chat_message(self, debate_id: str, message: str) -> Any:
        """Send a text message to a GGSEL conversation.

        The V1 endpoint expects the fields named exactly ``id_i`` and ``text``.
        Older builds incorrectly sent ``id_i`` as a query parameter and used
        ``message`` in the body, which produces:
        ``invalid id_i or text parameter``.
        """
        path = "/api_sellers/api/debates/v2"
        debate_text = str(debate_id).strip()
        message_text = str(message).strip()
        if not debate_text or not message_text:
            raise ValueError("Для отправки нужны id_i и текст сообщения")

        # GGSEL documents id_i as an integer. Keep a numeric value whenever
        # possible, but retain compatibility with installations returning it
        # as a numeric string.
        try:
            normalized_id: Any = int(debate_text)
        except ValueError:
            normalized_id = debate_text

        payload = {"id_i": normalized_id, "text": message_text}
        try:
            return self.v1_request("POST", path, json_body=payload)
        except APIError as exc:
            if exc.status not in (400, 415, 422):
                raise
            # Some GGSEL installations accept the same fields only as
            # application/x-www-form-urlencoded. Retry without changing names.
            return self.v1_request("POST", path, form_body=payload)

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
            record_api_error(exc.service, request.path, exc.status, str(exc))
            return jsonify({"ok": False, "error": str(exc), "service": exc.service}), exc.status
        except (ValueError, RuntimeError, requests.RequestException) as exc:
            logger.warning("Mini App request failed: %s", exc)
            record_api_error("Mini App", request.path, 400, str(exc))
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:
            logger.exception("Unexpected Mini App error")
            record_api_error("Mini App", request.path, 500, str(exc))
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


def get_favorite_offer_ids() -> set[str]:
    with db_connect() as conn:
        rows = conn.execute("SELECT offer_id FROM offer_favorites").fetchall()
    return {str(row["offer_id"]) for row in rows}


def set_offer_favorite(offer_id_value: Any, favorite: bool) -> bool:
    key = str(offer_id_value)
    with db_connect() as conn:
        if favorite:
            conn.execute(
                "INSERT OR REPLACE INTO offer_favorites(offer_id, created_at) VALUES (?, ?)",
                (key, datetime.now(timezone.utc).isoformat()),
            )
        else:
            conn.execute("DELETE FROM offer_favorites WHERE offer_id = ?", (key,))
    return favorite


def remember_recent_view(entity_type: str, entity_id: Any, title: str = "", metadata: Any = None) -> None:
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO recent_views(entity_type, entity_id, title, metadata_json, viewed_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(entity_type, entity_id) DO UPDATE SET
                title=excluded.title,
                metadata_json=excluded.metadata_json,
                viewed_at=excluded.viewed_at
            """,
            (
                str(entity_type),
                str(entity_id),
                str(title or ""),
                json.dumps(metadata, ensure_ascii=False, default=str) if metadata is not None else "",
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def get_recent_views(limit: int = 8) -> list[dict[str, Any]]:
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT entity_type, entity_id, title, metadata_json, viewed_at
            FROM recent_views ORDER BY viewed_at DESC LIMIT ?
            """,
            (max(1, min(limit, 30)),),
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        metadata: Any = {}
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        result.append({
            "type": row["entity_type"],
            "id": row["entity_id"],
            "title": row["title"],
            "metadata": metadata,
            "viewed_at": row["viewed_at"],
        })
    return result


def get_order_note(invoice_id: Any) -> dict[str, Any]:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT tag, note, updated_at FROM order_notes WHERE invoice_id = ?",
            (str(invoice_id),),
        ).fetchone()
    return dict(row) if row else {"tag": "", "note": "", "updated_at": ""}


def save_order_note(invoice_id: Any, tag: str, note: str) -> dict[str, Any]:
    allowed_tags = {"", "check", "replacement", "waiting", "vip", "resolved"}
    clean_tag = str(tag or "").strip().lower()
    if clean_tag not in allowed_tags:
        raise ValueError("Неизвестная метка заказа")
    clean_note = str(note or "").strip()[:4000]
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO order_notes(invoice_id, tag, note, updated_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(invoice_id) DO UPDATE SET
                tag=excluded.tag, note=excluded.note, updated_at=excluded.updated_at
            """,
            (str(invoice_id), clean_tag, clean_note, datetime.now(timezone.utc).isoformat()),
        )
    return get_order_note(invoice_id)


def get_offer_settings_map() -> dict[str, dict[str, Any]]:
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT offer_id, min_stock, auto_activate, auto_pause, alert_sent FROM offer_settings"
        ).fetchall()
    return {
        str(row["offer_id"]): {
            "min_stock": as_int(row["min_stock"], LOW_STOCK_DEFAULT),
            "auto_activate": bool(row["auto_activate"]),
            "auto_pause": bool(row["auto_pause"]),
            "alert_sent": bool(row["alert_sent"]),
        }
        for row in rows
    }


def get_offer_settings(offer_id_value: Any) -> dict[str, Any]:
    key = str(offer_id_value)
    with db_connect() as conn:
        row = conn.execute(
            "SELECT min_stock, auto_activate, auto_pause, alert_sent FROM offer_settings WHERE offer_id = ?",
            (key,),
        ).fetchone()
    if not row:
        return {
            "min_stock": LOW_STOCK_DEFAULT,
            "auto_activate": False,
            "auto_pause": False,
            "alert_sent": False,
        }
    return {
        "min_stock": as_int(row["min_stock"], LOW_STOCK_DEFAULT),
        "auto_activate": bool(row["auto_activate"]),
        "auto_pause": bool(row["auto_pause"]),
        "alert_sent": bool(row["alert_sent"]),
    }


def save_offer_settings(offer_id_value: Any, settings: dict[str, Any]) -> dict[str, Any]:
    current = get_offer_settings(offer_id_value)
    min_stock = max(0, as_int(settings.get("min_stock"), current["min_stock"]))
    auto_activate = bool(settings.get("auto_activate", current["auto_activate"]))
    auto_pause = bool(settings.get("auto_pause", current["auto_pause"]))
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO offer_settings(offer_id, min_stock, auto_activate, auto_pause, alert_sent, updated_at)
            VALUES (?, ?, ?, ?, 0, ?)
            ON CONFLICT(offer_id) DO UPDATE SET
                min_stock=excluded.min_stock,
                auto_activate=excluded.auto_activate,
                auto_pause=excluded.auto_pause,
                alert_sent=0,
                updated_at=excluded.updated_at
            """,
            (
                str(offer_id_value),
                min_stock,
                int(auto_activate),
                int(auto_pause),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    return get_offer_settings(offer_id_value)


def enrich_offer_settings(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    settings_map = get_offer_settings_map()
    favorites = get_favorite_offer_ids()
    enriched: list[dict[str, Any]] = []
    for item in items:
        copy = dict(item)
        offer_key = str(copy.get("id") or "")
        settings = settings_map.get(offer_key, {
            "min_stock": LOW_STOCK_DEFAULT,
            "auto_activate": False,
            "auto_pause": False,
            "alert_sent": False,
        })
        copy["settings"] = settings
        copy["favorite"] = offer_key in favorites
        copy["low_stock"] = str(copy.get("status") or "") != "archived" and as_int(copy.get("quantity")) <= as_int(settings.get("min_stock"), LOW_STOCK_DEFAULT)
        enriched.append(copy)
    return enriched


def extract_job_id(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("job_id", "id", "task_id"):
            value = payload.get(key)
            if value not in (None, ""):
                return str(value)
        for key in ("content", "data", "result"):
            nested = payload.get(key)
            if isinstance(nested, dict):
                found = extract_job_id(nested)
                if found:
                    return found
    return ""


def record_operation(operation: str, target: Any, result: Any, status: str = "queued") -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    job_id = extract_job_id(result)
    with db_connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO async_operations(job_id, operation, target, status, result_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                str(operation),
                str(target or ""),
                str(status),
                json.dumps(result, ensure_ascii=False, default=str),
                now,
                now,
            ),
        )
        operation_id = cursor.lastrowid
    return {"id": operation_id, "job_id": job_id, "status": status}


def refresh_operations() -> list[dict[str, Any]]:
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT id, job_id, operation, target, status, result_json, created_at, updated_at FROM async_operations ORDER BY id DESC LIMIT 100"
        ).fetchall()
    output: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if item.get("job_id") and item.get("status") in {"queued", "running"}:
            try:
                result = ggsel.async_job_v2(str(item["job_id"]))
                text = json.dumps(result, ensure_ascii=False, default=str).casefold()
                if any(word in text for word in ("completed", "success", "done", "успеш")):
                    status = "completed"
                elif any(word in text for word in ("failed", "error", "ошиб")):
                    status = "failed"
                else:
                    status = "running"
                now = datetime.now(timezone.utc).isoformat()
                with db_connect() as conn:
                    conn.execute(
                        "UPDATE async_operations SET status=?, result_json=?, updated_at=? WHERE id=?",
                        (status, json.dumps(result, ensure_ascii=False, default=str), now, item["id"]),
                    )
                item.update({"status": status, "result_json": json.dumps(result, ensure_ascii=False, default=str), "updated_at": now})
            except Exception as exc:
                logger.info("Async operation %s not ready: %s", item.get("job_id"), exc)
        try:
            item["result"] = json.loads(item.get("result_json") or "{}")
        except json.JSONDecodeError:
            item["result"] = item.get("result_json") or ""
        output.append(item)
    return output


def maybe_send_low_stock_alerts(offers: list[dict[str, Any]]) -> None:
    configured = get_offer_settings_map()
    for item in enrich_offer_settings(offers):
        offer_id_value = str(item.get("id") or "")
        if offer_id_value not in configured:
            continue
        settings = item.get("settings") or {}
        quantity = as_int(item.get("quantity"))
        threshold = as_int(settings.get("min_stock"), LOW_STOCK_DEFAULT)
        low = quantity <= threshold
        if not offer_id_value:
            continue
        if low and not settings.get("alert_sent"):
            try:
                send_text(
                    "⚠️ <b>Заканчивается товар</b>\n\n"
                    f"{safe(item.get('title'))}\n"
                    f"Остаток: <b>{quantity}</b>\n"
                    f"Минимум: <b>{threshold}</b>\n"
                    f"ID: <code>{safe(offer_id_value)}</code>"
                )
            except Exception:
                logger.exception("Unable to send low stock alert")
            with db_connect() as conn:
                conn.execute(
                    """
                    INSERT INTO offer_settings(offer_id, min_stock, auto_activate, auto_pause, alert_sent, updated_at)
                    VALUES (?, ?, ?, ?, 1, ?)
                    ON CONFLICT(offer_id) DO UPDATE SET alert_sent=1, updated_at=excluded.updated_at
                    """,
                    (
                        offer_id_value,
                        threshold,
                        int(bool(settings.get("auto_activate"))),
                        int(bool(settings.get("auto_pause"))),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
        elif not low and settings.get("alert_sent"):
            with db_connect() as conn:
                conn.execute(
                    "UPDATE offer_settings SET alert_sent=0, updated_at=? WHERE offer_id=?",
                    (datetime.now(timezone.utc).isoformat(), offer_id_value),
                )


def _offer_quantity(item: dict[str, Any]) -> int:
    for key in ("quantity", "num_in_stock", "in_stock", "products_count", "stock"):
        if item.get(key) is not None:
            return as_int(item.get(key))
    return 0


def _offer_status(item: dict[str, Any]) -> str:
    value: Any = "unknown"
    # V2 usually returns status/state, while the legacy V1 product list uses
    # visible: 1 for active and 0 for paused/hidden.
    for key in ("status", "state", "offer_status", "visible", "active"):
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


def _v1_products_pagination(data: Any) -> tuple[int, int, int]:
    """Return (current_page, total_pages, total_items) from the V1 response."""
    candidates: list[dict[str, Any]] = []
    if isinstance(data, dict):
        candidates.append(data)
        for key in ("content", "data", "result", "pagination", "meta"):
            nested = data.get(key)
            if isinstance(nested, dict):
                candidates.append(nested)

    current_page = 0
    total_pages = 0
    total_items = 0
    for obj in candidates:
        current_page = current_page or as_int(obj.get("page") or obj.get("current_page"))
        total_pages = total_pages or as_int(obj.get("pages") or obj.get("total_pages"))
        total_items = total_items or as_int(
            obj.get("cnt_goods") or obj.get("total") or obj.get("total_count")
        )
    return current_page, total_pages, total_items


def _collect_offers_v1_fallback() -> list[dict[str, Any]]:
    """Load the complete seller catalogue through the stable V1 products list.

    GGSEL may ignore a requested ``count`` and return its own fixed page size
    (currently often 20). Therefore pagination must use the response's ``pages``
    and ``cnt_goods`` fields, not ``len(rows) < requested_count``.
    """
    collected: list[dict[str, Any]] = []
    seen: set[str] = set()
    count = 20
    expected_total = 0

    for page in range(1, MINIAPP_OFFERS_MAX_PAGES + 1):
        data = ggsel.products_v1(page=page, count=count)
        items = extract_list(data, ("products", "items", "rows"))
        current_page, total_pages, total_items = _v1_products_pagination(data)
        expected_total = max(expected_total, total_items)

        new_count = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            normalized = normalize_offer_item(item)
            normalized["api_source"] = "v1-list"
            identifier = str(normalized.get("id") or "")
            if not identifier or identifier in seen:
                continue
            seen.add(identifier)
            collected.append(normalized)
            new_count += 1

        logger.info(
            "GGSEL V1 products page loaded: requested_page=%s response_page=%s "
            "rows=%s new=%s total_pages=%s total_items=%s collected=%s",
            page,
            current_page or page,
            len(items),
            new_count,
            total_pages,
            total_items,
            len(collected),
        )

        if not items or new_count == 0:
            break
        if total_pages and (current_page or page) >= total_pages:
            break
        if expected_total and len(collected) >= expected_total:
            break
        # Only use row count as a last-resort signal when the API omitted all
        # pagination metadata. Never compare it with the requested count when
        # metadata is present because GGSEL may enforce a smaller page size.
        if not total_pages and not expected_total and len(items) < count:
            break

    if expected_total and len(collected) < expected_total:
        logger.warning(
            "GGSEL catalogue pagination stopped early: expected=%s collected=%s "
            "max_pages=%s",
            expected_total,
            len(collected),
            MINIAPP_OFFERS_MAX_PAGES,
        )
    logger.info("Loaded %s seller products through GGSEL V1", len(collected))
    return collected


def collect_all_offers(force: bool = False) -> list[dict[str, Any]]:
    """Return the complete catalogue for the GUI.

    The legacy V1 list exposes reliable ``pages``/``cnt_goods`` metadata, while
    the V2 list endpoint has returned HTTP 500 or repeated its first page for
    some seller accounts. We therefore use V1 for catalogue browsing and keep
    V2 for offer details and write operations.
    """
    now = time.time()
    with _offer_cache_lock:
        if not force and _offer_cache["items"] and now < float(_offer_cache["expires_at"]):
            return list(_offer_cache["items"])

    try:
        collected = _collect_offers_v1_fallback()
    except APIError:
        logger.exception("GGSEL V1 products list failed; trying V2 list as fallback")
        limit = MINIAPP_OFFERS_API_PAGE_SIZE
        collected = []
        seen: set[str] = set()
        for page in range(1, MINIAPP_OFFERS_MAX_PAGES + 1):
            data = ggsel.offers_v2(page=page, limit=limit)
            items = extract_list(data, ("items", "offers", "rows"))
            new_count = 0
            for item in items:
                if not isinstance(item, dict):
                    continue
                normalized = normalize_offer_item(item)
                normalized["api_source"] = "v2-fallback"
                identifier = str(normalized.get("id") or "")
                if not identifier or identifier in seen:
                    continue
                seen.add(identifier)
                collected.append(normalized)
                new_count += 1
            if not items or new_count == 0 or not _has_next_page(data, items, page, limit):
                break

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
    payload = _order_payload(invoice_id, data)
    if "raw" in payload:
        return f"<pre>{safe(compact_json(data))}</pre>"
    buyer = payload.get("buyer") if isinstance(payload.get("buyer"), dict) else {}
    feedback = payload.get("feedback") if isinstance(payload.get("feedback"), dict) else {}
    currency = str(payload.get("currency") or "RUB").strip()
    symbol = "₽" if currency.upper() in {"RUB", "RUR"} else currency
    return (
        f"<b>📦 Заказ {safe(invoice_id)}</b>\n\n"
        f"Товар: <b>{safe(payload.get('name'))}</b>\n"
        f"ID товара: <code>{safe(payload.get('item_id'))}</code>\n"
        f"Статус: {safe(payload.get('invoice_state_label'))}\n"
        f"Зачислено: <b>{safe(payload.get('amount'))} {safe(symbol)}</b>\n"
        f"Прибыль: <b>{safe(payload.get('profit'))} {safe(symbol)}</b>\n"
        f"Покупка: {safe(payload.get('purchase_date'))}\n"
        f"Оплата: {safe(payload.get('date_pay'))}\n"
        f"Внешний ID: {safe(payload.get('external_order_id'))}\n\n"
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


def calculate_receipts_analytics(start: date, end: date) -> dict[str, Any]:
    received = 0.0
    gross = 0.0
    count = 0
    complete = False
    daily: dict[str, dict[str, float]] = {}
    products: dict[str, dict[str, Any]] = {}

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
            product = item.get("product") if isinstance(item.get("product"), dict) else {}
            dt = parse_iso_datetime(operation.get("datetime"))
            if dt is None:
                continue
            day = dt.date()
            oldest = day if oldest is None else min(oldest, day)
            if not (start <= day <= end) or not product:
                continue
            received_value = as_float(operation.get("on_account"))
            gross_value = as_float(operation.get("price"))
            if received_value <= 0 and gross_value <= 0:
                continue
            received += received_value
            gross += gross_value
            count += 1
            day_key = day.isoformat()
            bucket = daily.setdefault(day_key, {"received": 0.0, "gross": 0.0, "count": 0})
            bucket["received"] += received_value
            bucket["gross"] += gross_value
            bucket["count"] += 1
            name_value = product.get("name")
            if isinstance(name_value, dict):
                name_value = name_value.get("value") or name_value.get("ru")
            if isinstance(name_value, list) and name_value:
                first = name_value[0]
                name_value = first.get("value") if isinstance(first, dict) else first
            product_name = str(name_value or product.get("title") or product.get("id") or "Товар")
            p = products.setdefault(product_name, {"name": product_name, "received": 0.0, "gross": 0.0, "count": 0})
            p["received"] += received_value
            p["gross"] += gross_value
            p["count"] += 1

        has_next = bool(content.get("has_next_page")) if isinstance(content, dict) else False
        if oldest is not None and oldest < start:
            complete = True
            break
        if not has_next:
            complete = True
            break

    top_products = sorted(products.values(), key=lambda item: (item["received"], item["count"]), reverse=True)[:15]
    daily_rows = [{"date": key, **value} for key, value in sorted(daily.items())]
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "received": round(received, 2),
        "gross": round(gross, 2),
        "count": count,
        "average": round(received / count, 2) if count else 0.0,
        "complete": complete,
        "daily": daily_rows,
        "top_products": top_products,
    }


_RU_MONTHS_SHORT = ("янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")


def _receipt_product_name(product: dict[str, Any]) -> str:
    value: Any = product.get("name")
    if isinstance(value, dict):
        value = value.get("value") or value.get("ru") or value.get("title")
    elif isinstance(value, list) and value:
        first = value[0]
        value = first.get("value") if isinstance(first, dict) else first
    return str(value or product.get("title") or product.get("id") or "Товар")


def _receipt_product_id(product: dict[str, Any]) -> str:
    return str(product.get("id") or product.get("item_id") or product.get("product_id") or "")


def _empty_period_metrics() -> dict[str, float | int]:
    return {"received": 0.0, "gross": 0.0, "count": 0}


def _finalize_period_metrics(metrics: dict[str, float | int]) -> dict[str, Any]:
    received = float(metrics.get("received") or 0.0)
    gross = float(metrics.get("gross") or 0.0)
    count = int(metrics.get("count") or 0)
    return {
        "received": round(received, 2),
        "gross": round(gross, 2),
        "count": count,
        "average": round(gross / count, 2) if count else 0.0,
        "average_received": round(received / count, 2) if count else 0.0,
        "net_rate": round(received / gross * 100, 2) if gross else 0.0,
    }


def _percent_delta(current: float, previous: float) -> Optional[float]:
    if previous == 0:
        return None
    return round((current - previous) / abs(previous) * 100, 2)


def _dashboard_bucket(day: date, span_days: int) -> tuple[str, date, date, str]:
    if span_days <= 14:
        return day.isoformat(), day, day, day.strftime("%d.%m")
    if span_days <= 120:
        bucket_start = day - timedelta(days=day.weekday())
        bucket_end = bucket_start + timedelta(days=6)
        label = f"{bucket_start.strftime('%d.%m')}–{bucket_end.strftime('%d.%m')}"
        return bucket_start.isoformat(), bucket_start, bucket_end, label
    bucket_start = day.replace(day=1)
    if bucket_start.month == 12:
        next_month = bucket_start.replace(year=bucket_start.year + 1, month=1)
    else:
        next_month = bucket_start.replace(month=bucket_start.month + 1)
    bucket_end = next_month - timedelta(days=1)
    label = f"{_RU_MONTHS_SHORT[bucket_start.month - 1]} {str(bucket_start.year)[2:]}"
    return bucket_start.strftime("%Y-%m"), bucket_start, bucket_end, label


def _dashboard_chat_stats() -> dict[str, Any]:
    result = {"new": 0, "waiting": 0, "replacement": 0, "resolved": 0, "messages_today": 0}
    try:
        today_prefix = datetime.now().date().isoformat()
        with db_connect() as conn:
            rows = conn.execute(
                """
                SELECT COALESCE(cl.label, 'new') AS label, COUNT(DISTINCT m.debate_id) AS cnt
                FROM messages AS m
                LEFT JOIN chat_labels AS cl ON cl.debate_id = m.debate_id
                WHERE m.debate_id IS NOT NULL AND m.debate_id != ''
                GROUP BY COALESCE(cl.label, 'new')
                """
            ).fetchall()
            for row in rows:
                label = str(row["label"] or "new")
                if label in result:
                    result[label] = as_int(row["cnt"])
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM messages WHERE sender='buyer' AND (message_date LIKE ? OR created_at LIKE ?)",
                (today_prefix + "%", today_prefix + "%"),
            ).fetchone()
            result["messages_today"] = as_int(row["cnt"] if row else 0)
    except Exception:
        logger.exception("Unable to calculate dashboard chat stats")
    return result


def calculate_dashboard_analytics(
    start: date,
    end: date,
    *,
    product_id: str = "",
    product_name: str = "",
    force: bool = False,
) -> dict[str, Any]:
    if end < start:
        start, end = end, start
    product_id = str(product_id or "").strip()
    product_name_key = str(product_name or "").strip().casefold()
    cache_key = (start.isoformat(), end.isoformat(), product_id or product_name_key)
    now = time.time()
    if not force:
        with _dashboard_cache_lock:
            cached = _dashboard_cache.get(cache_key)
            if cached and float(cached.get("_expires_at", 0)) > now:
                return {key: value for key, value in cached.items() if key != "_expires_at"}

    span_days = max(1, (end - start).days + 1)
    previous_end = start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=span_days - 1)
    scan_start = previous_start

    current_raw = _empty_period_metrics()
    previous_raw = _empty_period_metrics()
    daily: dict[str, dict[str, float | int]] = {}
    products: dict[str, dict[str, Any]] = {}
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
            product = item.get("product") if isinstance(item.get("product"), dict) else {}
            dt = parse_iso_datetime(operation.get("datetime"))
            if dt is None:
                continue
            day = dt.date()
            oldest = day if oldest is None else min(oldest, day)
            if day < scan_start or day > end or not product:
                continue

            receipt_product_id = _receipt_product_id(product)
            receipt_product_name = _receipt_product_name(product)
            if product_id and receipt_product_id != product_id:
                if not product_name_key or receipt_product_name.casefold() != product_name_key:
                    continue

            received_value = as_float(operation.get("on_account"))
            gross_value = as_float(operation.get("price"))
            if received_value <= 0 and gross_value <= 0:
                continue

            target = current_raw if start <= day <= end else previous_raw if previous_start <= day <= previous_end else None
            if target is None:
                continue
            target["received"] = float(target["received"]) + received_value
            target["gross"] = float(target["gross"]) + gross_value
            target["count"] = int(target["count"]) + 1

            if start <= day <= end:
                day_key = day.isoformat()
                bucket = daily.setdefault(day_key, {"received": 0.0, "gross": 0.0, "count": 0})
                bucket["received"] = float(bucket["received"]) + received_value
                bucket["gross"] = float(bucket["gross"]) + gross_value
                bucket["count"] = int(bucket["count"]) + 1
                product_key = receipt_product_id or receipt_product_name
                product_bucket = products.setdefault(
                    product_key,
                    {
                        "id": receipt_product_id,
                        "name": receipt_product_name,
                        "received": 0.0,
                        "gross": 0.0,
                        "count": 0,
                    },
                )
                product_bucket["received"] += received_value
                product_bucket["gross"] += gross_value
                product_bucket["count"] += 1

        has_next = bool(content.get("has_next_page")) if isinstance(content, dict) else False
        if oldest is not None and oldest < scan_start:
            complete = True
            break
        if not has_next:
            complete = True
            break

    current = _finalize_period_metrics(current_raw)
    previous = _finalize_period_metrics(previous_raw)
    deltas = {
        "gross": _percent_delta(current["gross"], previous["gross"]),
        "received": _percent_delta(current["received"], previous["received"]),
        "count": _percent_delta(float(current["count"]), float(previous["count"])),
        "average": _percent_delta(current["average"], previous["average"]),
        "net_rate": round(current["net_rate"] - previous["net_rate"], 2),
    }

    series_map: dict[str, dict[str, Any]] = {}
    cursor = start
    while cursor <= end:
        bucket_key, bucket_start, bucket_end, label = _dashboard_bucket(cursor, span_days)
        bucket = series_map.setdefault(
            bucket_key,
            {
                "key": bucket_key,
                "label": label,
                "start": max(bucket_start, start).isoformat(),
                "end": min(bucket_end, end).isoformat(),
                "received": 0.0,
                "gross": 0.0,
                "count": 0,
            },
        )
        raw = daily.get(cursor.isoformat())
        if raw:
            bucket["received"] += float(raw.get("received") or 0)
            bucket["gross"] += float(raw.get("gross") or 0)
            bucket["count"] += int(raw.get("count") or 0)
        cursor += timedelta(days=1)

    series = list(series_map.values())
    for row in series:
        row["received"] = round(row["received"], 2)
        row["gross"] = round(row["gross"], 2)

    top_products = sorted(
        products.values(),
        key=lambda item: (float(item["gross"]), int(item["count"])),
        reverse=True,
    )[:10]
    for item in top_products:
        item["received"] = round(float(item["received"]), 2)
        item["gross"] = round(float(item["gross"]), 2)
        item["average"] = round(float(item["gross"]) / int(item["count"]), 2) if item["count"] else 0.0

    result = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "previous_start": previous_start.isoformat(),
        "previous_end": previous_end.isoformat(),
        "product_id": product_id,
        "product_name": product_name,
        "current": current,
        "previous": previous,
        "deltas": deltas,
        "series": series,
        "top_products": top_products,
        "complete": complete,
        "granularity": "day" if span_days <= 14 else "week" if span_days <= 120 else "month",
        "api_limits": {
            "views": False,
            "checkout_clicks": False,
            "conversion": False,
            "note": "Seller API не передаёт просмотры и конверсию. Панель показывает продажи, зачисления и операционные показатели.",
        },
    }
    with _dashboard_cache_lock:
        _dashboard_cache[cache_key] = {**result, "_expires_at": now + DASHBOARD_ANALYTICS_CACHE_SECONDS}
    return result


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
# CRM conversations and customer profiles
# ============================================================


def _clean_identity(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip().casefold())


def _conversation_identity(email: str = "", account: str = "", invoice_id: str = "", debate_id: str = "") -> str:
    email_key = _clean_identity(email)
    account_key = _clean_identity(account)
    if email_key:
        return f"email:{email_key}"
    if account_key:
        return f"account:{account_key}"
    if invoice_id:
        return f"invoice:{str(invoice_id).strip()}"
    return f"debate:{str(debate_id).strip()}"


def _conversation_id(identity: str) -> str:
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def ensure_conversation(*, debate_id: str, invoice_id: str = "", email: str = "", account: str = "", product_name: str = "", preview: str = "", when: str = "") -> str:
    identity = _conversation_identity(email, account, invoice_id, debate_id)
    conversation_id = _conversation_id(identity)
    now = datetime.now(timezone.utc).isoformat()
    normalized_when = normalize_datetime_text(when) or now
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO conversations(
                conversation_id, customer_key, buyer_email, buyer_account,
                latest_debate_id, latest_invoice_id, product_name, last_message,
                last_message_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(conversation_id) DO UPDATE SET
                buyer_email=COALESCE(NULLIF(excluded.buyer_email,''), conversations.buyer_email),
                buyer_account=COALESCE(NULLIF(excluded.buyer_account,''), conversations.buyer_account),
                latest_debate_id=COALESCE(NULLIF(excluded.latest_debate_id,''), conversations.latest_debate_id),
                latest_invoice_id=COALESCE(NULLIF(excluded.latest_invoice_id,''), conversations.latest_invoice_id),
                product_name=COALESCE(NULLIF(excluded.product_name,''), conversations.product_name),
                last_message=CASE WHEN excluded.last_message_at >= COALESCE(conversations.last_message_at,'') THEN excluded.last_message ELSE conversations.last_message END,
                last_message_at=MAX(COALESCE(conversations.last_message_at,''), excluded.last_message_at),
                updated_at=excluded.updated_at
            """,
            (conversation_id, identity, email, account, debate_id, invoice_id, product_name, preview, normalized_when, now, now),
        )
        if debate_id:
            conn.execute(
                """
                INSERT INTO conversation_debates(debate_id, conversation_id, invoice_id, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(debate_id) DO UPDATE SET
                    conversation_id=excluded.conversation_id,
                    invoice_id=COALESCE(NULLIF(excluded.invoice_id,''), conversation_debates.invoice_id),
                    updated_at=excluded.updated_at
                """,
                (debate_id, conversation_id, invoice_id, now),
            )
        conn.execute(
            "UPDATE messages SET conversation_id=?, buyer_email=COALESCE(NULLIF(buyer_email,''), ?) WHERE debate_id=?",
            (conversation_id, email, debate_id),
        )
    return conversation_id


def rebuild_conversations() -> None:
    """Merge historical messages by customer email/account, then by order/debate."""
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT m.debate_id, m.invoice_id, m.message_text, m.image_url,
                   COALESCE(NULLIF(m.message_date,''),m.created_at) AS when_at,
                   o.buyer_email, o.buyer_account, o.product_name
            FROM messages m
            LEFT JOIN orders o ON o.invoice_id=m.invoice_id
            WHERE COALESCE(m.debate_id,'')!=''
            ORDER BY m.id
            """
        ).fetchall()
    for row in rows:
        ensure_conversation(
            debate_id=str(row['debate_id'] or ''), invoice_id=str(row['invoice_id'] or ''),
            email=str(row['buyer_email'] or ''), account=str(row['buyer_account'] or ''),
            product_name=str(row['product_name'] or ''),
            preview=str(row['message_text'] or ('[Изображение]' if row['image_url'] else '')),
            when=str(row['when_at'] or ''),
        )


def _customer_profile(conversation_id: str) -> dict[str, Any]:
    with db_connect() as conn:
        row = conn.execute("SELECT note,tags_json,pinned,favorite,updated_at FROM customer_profiles WHERE conversation_id=?", (conversation_id,)).fetchone()
    if not row:
        return {"note":"", "tags":[], "pinned":False, "favorite":False}
    try:
        tags = json.loads(row['tags_json'] or '[]')
    except json.JSONDecodeError:
        tags = []
    return {"note":row['note'] or '', "tags":tags if isinstance(tags,list) else [], "pinned":bool(row['pinned']), "favorite":bool(row['favorite']), "updated_at":row['updated_at']}


def _conversation_orders(conversation_id: str) -> list[dict[str, Any]]:
    with db_connect() as conn:
        conv = conn.execute("SELECT buyer_email,buyer_account FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
        if not conv:
            return []
        rows = conn.execute(
            """
            SELECT invoice_id,item_id,product_name,amount,currency,profit,invoice_state,purchase_date,date_pay
            FROM orders
            WHERE (COALESCE(?, '')!='' AND lower(buyer_email)=lower(?))
               OR (COALESCE(?, '')!='' AND lower(buyer_account)=lower(?))
            ORDER BY COALESCE(NULLIF(date_pay,''),purchase_date,updated_at) DESC
            LIMIT 100
            """,
            (conv['buyer_email'], conv['buyer_email'], conv['buyer_account'], conv['buyer_account']),
        ).fetchall()
    return [dict(row) for row in rows]


def _conversation_summary(conversation_id: str) -> dict[str, Any]:
    with db_connect() as conn:
        conv = conn.execute("SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
        debates = conn.execute("SELECT debate_id,invoice_id FROM conversation_debates WHERE conversation_id=? ORDER BY updated_at DESC", (conversation_id,)).fetchall()
    if not conv:
        return {}
    orders = _conversation_orders(conversation_id)
    total_spent = sum(as_float(item.get('amount')) for item in orders)
    return {
        **dict(conv),
        "debate_ids":[str(row['debate_id']) for row in debates],
        "orders_count":len(orders),
        "total_spent":total_spent,
        "orders":orders,
        "profile":_customer_profile(conversation_id),
    }

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

    order: dict[str, Any] = {}
    if invoice_id:
        try:
            response = ggsel.order_info(invoice_id)
            if isinstance(response, dict):
                order = upsert_order(invoice_id, response)
        except Exception:
            logger.exception("Unable to load order %s", invoice_id)

    buyer = order.get("buyer_info") if isinstance(order.get("buyer_info"), dict) else {}
    conversation_id = ensure_conversation(
        debate_id=debate_id, invoice_id=invoice_id,
        email=str(buyer.get("email") or ""), account=str(buyer.get("account") or ""),
        product_name=str(order.get("name") or ""), preview=message_text or ("[Изображение]" if image_url else ""),
        when=message_date,
    )
    save_message_record(
        message_id=message_id, debate_id=debate_id, invoice_id=invoice_id, sender="buyer",
        message_text=message_text, image_url=image_url, message_date=message_date,
        conversation_id=conversation_id, buyer_email=str(buyer.get("email") or ""),
    )
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
                "item_id": product.get("id") or product.get("item_id") or sale.get("item_id"),
                "name": product.get("name") or sale.get("name"),
                "price_rub": as_float(product.get("price_rub") or sale.get("price_rub")),
                "price_usd": as_float(product.get("price_usd") or sale.get("price_usd")),
                "price_eur": as_float(product.get("price_eur") or sale.get("price_eur")),
            }
        )
    return result


UUID_LIKE_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _looks_like_uuid(value: Any) -> bool:
    return bool(UUID_LIKE_RE.fullmatch(str(value or "").strip()))


def _find_text_in_nested(data: Any, keys: tuple[str, ...]) -> str:
    """Find the first useful non-UUID text value in a nested API response."""
    queue: list[Any] = [data]
    seen: set[int] = set()
    while queue:
        current = queue.pop(0)
        marker = id(current)
        if marker in seen:
            continue
        seen.add(marker)
        if isinstance(current, dict):
            for key in keys:
                value = current.get(key)
                if isinstance(value, str) and value.strip() and not _looks_like_uuid(value):
                    return value.strip()
            queue.extend(current.values())
        elif isinstance(current, list):
            queue.extend(current)
    return ""


def _local_sale_name(invoice_id: str, item_id: Any = "") -> str:
    with db_connect() as conn:
        row = conn.execute(
            """
            SELECT product_name
            FROM sales
            WHERE invoice_id = ? OR (? != '' AND product_id = ?)
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (str(invoice_id), str(item_id or ""), str(item_id or "")),
        ).fetchone()
    value = str(row["product_name"] or "") if row else ""
    return "" if _looks_like_uuid(value) else value


def _resolve_order_name(invoice_id: str, item: dict[str, Any]) -> tuple[str, str]:
    """Return (human product title, opaque reference if GGSEL put a UUID in name)."""
    raw_name = str(item.get("name") or "").strip()
    opaque_reference = raw_name if _looks_like_uuid(raw_name) else ""
    if raw_name and not opaque_reference:
        return raw_name, ""

    item_id = item.get("item_id")
    cached_name = _local_sale_name(invoice_id, item_id)
    if cached_name:
        return cached_name, opaque_reference

    if as_int(item_id) > 0:
        try:
            product_data = ggsel.product_v1(as_int(item_id))
            product_name = _find_text_in_nested(
                product_data,
                ("title_ru", "name_goods", "product_name", "title", "name"),
            )
            if product_name:
                return product_name, opaque_reference
        except Exception as exc:
            logger.warning("Unable to resolve product title for order %s: %s", invoice_id, exc)

    return "Товар", opaque_reference


def _order_payload(invoice_id: str, data: Any) -> dict[str, Any]:
    item = data.get("content") if isinstance(data, dict) and isinstance(data.get("content"), dict) else data
    if not isinstance(item, dict):
        return {"invoice_id": invoice_id, "raw": data}
    buyer = item.get("buyer_info") if isinstance(item.get("buyer_info"), dict) else {}
    feedback = item.get("feedback") if isinstance(item.get("feedback"), dict) else {}
    state = as_int(item.get("invoice_state"))
    name, opaque_reference = _resolve_order_name(invoice_id, item)
    external_order_id = item.get("external_order_id") or opaque_reference or item.get("cart_uid")
    return {
        "invoice_id": invoice_id,
        "name": name,
        "item_id": item.get("item_id"),
        "amount": item.get("amount"),
        "currency": str(item.get("currency_type") or "RUB").strip(),
        "profit": item.get("profit"),
        "invoice_state": state,
        "invoice_state_label": INVOICE_STATES.get(state, f"Неизвестный статус ({state})"),
        "purchase_date": item.get("purchase_date"),
        "date_pay": item.get("date_pay"),
        "external_order_id": external_order_id,
        "buyer": buyer,
        "feedback": feedback,
        "options": item.get("options") if isinstance(item.get("options"), list) else [],
    }


def _review_payload(data: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in extract_list(data, ("reviews", "items")):
        if not isinstance(item, dict):
            continue
        review_type = str(item.get("type") or "").strip().lower()
        good_value = as_int(item.get("good"), -1)
        positive = review_type in {"good", "positive", "положительный"} or (
            not review_type and good_value > 0
        )
        negative = review_type in {"bad", "negative", "отрицательный"} or (
            not review_type and good_value == 0
        )
        rating_label = "Положительный" if positive else "Отрицательный" if negative else (review_type or "Отзыв")
        result.append(
            {
                **item,
                "text": item.get("info") or item.get("text") or item.get("review") or item.get("feedback") or "",
                "product_name": item.get("name") or item.get("name_goods") or "Товар",
                "rating_label": rating_label,
                "is_positive": positive,
                "seller_comment": item.get("comment") or "",
            }
        )
    return result



def _conversation_chat_payload(query: str = "") -> list[dict[str, Any]]:
    rebuild_conversations()
    folded = str(query or '').strip().casefold()
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT c.*, cp.note AS customer_note, cp.tags_json, COALESCE(cp.pinned,0) AS pinned,
                   COALESCE(cp.favorite,0) AS favorite,
                   COALESCE(cl.label,'new') AS label, COALESCE(cl.note,'') AS label_note,
                   (SELECT COUNT(*) FROM conversation_debates d WHERE d.conversation_id=c.conversation_id) AS debate_count,
                   (SELECT COUNT(*) FROM messages m WHERE m.conversation_id=c.conversation_id AND m.sender='buyer') AS buyer_messages
            FROM conversations c
            LEFT JOIN customer_profiles cp ON cp.conversation_id=c.conversation_id
            LEFT JOIN chat_labels cl ON cl.debate_id=c.conversation_id
            ORDER BY pinned DESC, COALESCE(c.last_message_at,c.updated_at) DESC
            LIMIT 500
            """
        ).fetchall()
        matched_ids: set[str] = set()
        if folded:
            pattern = f"%{folded}%"
            for row in conn.execute(
                """SELECT DISTINCT conversation_id FROM messages
                   WHERE conversation_id IS NOT NULL AND (lower(message_text) LIKE ? OR lower(buyer_email) LIKE ?)""",
                (pattern, pattern),
            ).fetchall():
                matched_ids.add(str(row['conversation_id']))
    result=[]
    for row in rows:
        item=dict(row)
        hay=' '.join(str(item.get(k) or '') for k in ('buyer_email','buyer_account','latest_invoice_id','product_name','last_message','customer_note')).casefold()
        if folded and folded not in hay and item['conversation_id'] not in matched_ids:
            continue
        try: tags=json.loads(item.get('tags_json') or '[]')
        except json.JSONDecodeError: tags=[]
        result.append({
            **item,
            'id_i':item.get('latest_debate_id') or item['conversation_id'],
            'conversation_id':item['conversation_id'],
            'invoice_id':item.get('latest_invoice_id') or '',
            'email':item.get('buyer_email') or item.get('buyer_account') or '',
            'preview':item.get('last_message') or '',
            'last_message':item.get('last_message_at') or item.get('updated_at'),
            'tags':tags if isinstance(tags,list) else [],
            'pinned':bool(item.get('pinned')), 'favorite':bool(item.get('favorite')),
        })
    return result


def _conversation_messages(conversation_id: str) -> list[dict[str, Any]]:
    with db_connect() as conn:
        rows=conn.execute(
            """SELECT message_id,sender,message_text,image_url,message_date,created_at,debate_id,invoice_id
               FROM messages WHERE conversation_id=?
               ORDER BY COALESCE(NULLIF(message_date,''),created_at),id""", (conversation_id,)
        ).fetchall()
    return [{
        'id':row['message_id'] or '', 'seller':str(row['sender'] or '').lower()=='seller',
        'buyer':str(row['sender'] or '').lower()=='buyer', 'message':row['message_text'] or '',
        'url':row['image_url'] or '', 'is_img':bool(row['image_url']),
        'date_written':row['message_date'] or row['created_at'], 'debate_id':row['debate_id'] or '',
        'invoice_id':row['invoice_id'] or '', 'source':'local',
    } for row in rows]


def _local_chat_payload() -> list[dict[str, Any]]:
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT
                m.debate_id,
                m.invoice_id,
                m.message_text,
                m.image_url,
                COALESCE(NULLIF(m.message_date, ''), m.created_at) AS last_message_at,
                o.item_id,
                o.product_name,
                o.buyer_email,
                o.buyer_account
            FROM messages AS m
            LEFT JOIN orders AS o ON o.invoice_id = m.invoice_id
            WHERE m.debate_id IS NOT NULL
              AND m.debate_id != ''
              AND m.id IN (
                  SELECT MAX(id)
                  FROM messages
                  WHERE debate_id IS NOT NULL AND debate_id != ''
                  GROUP BY debate_id
              )
            ORDER BY COALESCE(NULLIF(m.message_date, ''), m.created_at) DESC, m.id DESC
            LIMIT 300
            """
        ).fetchall()

    offer_titles: dict[str, str] = {}
    try:
        offer_titles = {
            str(item.get("id")): str(item.get("title") or "")
            for item in collect_all_offers()
            if item.get("id") is not None
        }
    except Exception as exc:
        logger.warning("Unable to enrich local chats with offers: %s", exc)

    result: list[dict[str, Any]] = []
    for row in rows:
        product_name = str(row["product_name"] or "")
        if not product_name or _looks_like_uuid(product_name):
            product_name = offer_titles.get(str(row["item_id"] or ""), "")
        preview = str(row["message_text"] or "").strip()
        if not preview and row["image_url"]:
            preview = "[Изображение]"
        result.append(
            {
                "id_i": str(row["debate_id"]),
                "invoice_id": str(row["invoice_id"] or ""),
                "email": str(row["buyer_email"] or row["buyer_account"] or ""),
                "product": str(row["item_id"] or ""),
                "product_name": product_name,
                "last_message": str(row["last_message_at"] or ""),
                "preview": preview,
                "source": "local",
            }
        )
    return result


def _chat_payload(data: Any) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}

    for item in extract_list(data, ("chats", "items")):
        if not isinstance(item, dict):
            continue
        debate_id = str(item.get("id_i") or item.get("debate_id") or item.get("id") or "")
        if not debate_id:
            continue
        merged[debate_id] = {
            **item,
            "id_i": debate_id,
            "last_message": item.get("last_message") or item.get("date") or "",
            "preview": "",
            "source": "api",
        }

    for local in _local_chat_payload():
        debate_id = str(local.get("id_i") or "")
        current = merged.get(debate_id, {})
        combined = {**current}
        for key, value in local.items():
            if value not in (None, "", 0, "0") or key not in combined:
                combined[key] = value
        # Preserve unread counters from the API while using the newer local timestamp/preview.
        combined["cnt_new"] = current.get("cnt_new", combined.get("cnt_new"))
        combined["cnt_msg"] = current.get("cnt_msg", combined.get("cnt_msg"))
        merged[debate_id] = combined

    with db_connect() as conn:
        label_rows = conn.execute("SELECT debate_id, label, note FROM chat_labels").fetchall()
    labels = {str(row["debate_id"]): {"label": row["label"], "note": row["note"] or ""} for row in label_rows}
    for debate_id, item in merged.items():
        item.update(labels.get(debate_id, {"label": "new", "note": ""}))

    def sort_key(item: dict[str, Any]) -> tuple[float, int]:
        # GGSEL and webhook payloads use several date formats. Normalize all of
        # them before sorting so the newest conversation is always first.
        candidates = (
            item.get("last_message"),
            item.get("date"),
            item.get("date_written"),
            item.get("message_date"),
            item.get("created_at"),
        )
        parsed = next((parse_iso_datetime(value) for value in candidates if parse_iso_datetime(value)), None)
        # Debate ID is only a deterministic tie-breaker; chronology is primary.
        return (parsed.timestamp() if parsed else 0.0, as_int(item.get("id_i"), 0))

    result = sorted(merged.values(), key=sort_key, reverse=True)
    for position, item in enumerate(result):
        item["sort_position"] = position
        parsed = parse_iso_datetime(item.get("last_message"))
        if parsed:
            item["last_message_iso"] = parsed.isoformat()
    return result


def _local_messages(debate_id: str) -> list[dict[str, Any]]:
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT message_id, sender, message_text, image_url, message_date, created_at
            FROM messages
            WHERE debate_id = ?
            ORDER BY COALESCE(NULLIF(message_date, ''), created_at), id
            """,
            (str(debate_id),),
        ).fetchall()
    return [
        {
            "id": row["message_id"] or "",
            "seller": str(row["sender"] or "").lower() == "seller",
            "buyer": str(row["sender"] or "").lower() == "buyer",
            "message": row["message_text"] or "",
            "url": row["image_url"] or "",
            "is_img": bool(row["image_url"]),
            "date_written": row["message_date"] or row["created_at"],
            "source": "local",
        }
        for row in rows
    ]


def _message_payload(data: Any, debate_id: str) -> list[dict[str, Any]]:
    combined: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()

    api_items = [item for item in extract_list(data, ("messages", "items")) if isinstance(item, dict)]
    for source_item in [*api_items, *_local_messages(debate_id)]:
        message = str(source_item.get("message") or source_item.get("text") or "")
        when = str(source_item.get("date_written") or source_item.get("date") or source_item.get("created_at") or "")
        seller = bool(source_item.get("seller") is True or source_item.get("is_seller") is True)
        sender = "seller" if seller else "buyer"
        url = str(source_item.get("url") or source_item.get("image_url") or "")
        key = (str(source_item.get("id") or source_item.get("message_id") or ""), when, sender, message or url)
        if key in seen:
            continue
        seen.add(key)
        combined.append(
            {
                **source_item,
                "message": message,
                "date_written": when,
                "seller": seller,
                "buyer": not seller,
                "url": url,
                "is_img": bool(source_item.get("is_img") or url),
            }
        )

    combined.sort(key=lambda item: (parse_iso_datetime(item.get("date_written")) or datetime.min.replace(tzinfo=timezone.utc)))
    return combined


def global_search_payload(query: str) -> dict[str, list[dict[str, Any]]]:
    clean = str(query or "").strip()
    if len(clean) < 2:
        return {"offers": [], "orders": [], "chats": []}
    folded = clean.casefold()

    offers: list[dict[str, Any]] = []
    try:
        for item in enrich_offer_settings(collect_all_offers()):
            haystack = " ".join(str(item.get(key) or "") for key in ("id", "title", "category")).casefold()
            if folded in haystack:
                offers.append({
                    "type": "offer",
                    "id": str(item.get("id") or ""),
                    "title": str(item.get("title") or "Товар"),
                    "subtitle": f"ID {item.get('id') or '—'} · остаток {as_int(item.get('quantity'))}",
                    "status": item.get("status"),
                    "favorite": bool(item.get("favorite")),
                })
            if len(offers) >= 10:
                break
    except Exception as exc:
        logger.warning("Global offer search failed: %s", exc)

    like = f"%{clean}%"
    with db_connect() as conn:
        order_rows = conn.execute(
            """
            SELECT invoice_id, item_id, product_name, buyer_email, amount, currency, purchase_date
            FROM orders
            WHERE invoice_id LIKE ? OR item_id LIKE ? OR product_name LIKE ? OR buyer_email LIKE ?
            ORDER BY updated_at DESC LIMIT 10
            """,
            (like, like, like, like),
        ).fetchall()
    orders = [
        {
            "type": "order",
            "id": str(row["invoice_id"]),
            "title": str(row["product_name"] or f"Заказ #{row['invoice_id']}"),
            "subtitle": f"#{row['invoice_id']} · {row['buyer_email'] or 'без email'}",
            "amount": as_float(row["amount"]),
            "currency": str(row["currency"] or "RUB"),
            "date": row["purchase_date"],
        }
        for row in order_rows
    ]

    chats: list[dict[str, Any]] = []
    try:
        for item in _chat_payload({}):
            haystack = " ".join(str(item.get(key) or "") for key in ("id_i", "invoice_id", "email", "product_name", "preview")).casefold()
            if folded in haystack:
                chats.append({
                    "type": "chat",
                    "id": str(item.get("id_i") or ""),
                    "title": str(item.get("email") or item.get("product_name") or "Диалог"),
                    "subtitle": str(item.get("preview") or item.get("product_name") or "Открыть переписку"),
                    "invoice_id": str(item.get("invoice_id") or ""),
                    "label": str(item.get("label") or "new"),
                    "date": item.get("last_message"),
                })
            if len(chats) >= 10:
                break
    except Exception as exc:
        logger.warning("Global chat search failed: %s", exc)

    return {"offers": offers, "orders": orders, "chats": chats}


def attention_center_payload() -> dict[str, Any]:
    offers: list[dict[str, Any]] = []
    try:
        offers = enrich_offer_settings(collect_all_offers())
    except Exception as exc:
        logger.warning("Attention center offers failed: %s", exc)
    low_stock = sorted(
        [item for item in offers if item.get("low_stock")],
        key=lambda item: (as_int(item.get("quantity")), str(item.get("title") or "").casefold()),
    )[:8]

    with db_connect() as conn:
        chat_rows = conn.execute(
            "SELECT label, COUNT(*) AS total FROM chat_labels WHERE label IN ('new','waiting','replacement') GROUP BY label"
        ).fetchall()
        unlabeled_new = conn.execute(
            """
            SELECT COUNT(DISTINCT m.debate_id) AS total
            FROM messages AS m
            LEFT JOIN chat_labels AS c ON c.debate_id = m.debate_id
            WHERE m.debate_id != '' AND c.debate_id IS NULL
            """
        ).fetchone()
        operation_rows = conn.execute(
            """
            SELECT operation, target, status, updated_at, result_json
            FROM async_operations WHERE status IN ('failed','queued','running')
            ORDER BY updated_at DESC LIMIT 8
            """
        ).fetchall()
    chat_counts = {str(row["label"]): as_int(row["total"]) for row in chat_rows}
    chat_counts["new"] = chat_counts.get("new", 0) + as_int(unlabeled_new["total"] if unlabeled_new else 0)
    operations = [dict(row) for row in operation_rows]

    items: list[dict[str, Any]] = []
    for item in low_stock:
        items.append({
            "kind": "low_stock",
            "severity": "danger" if as_int(item.get("quantity")) <= 0 else "warning",
            "title": str(item.get("title") or "Товар"),
            "subtitle": f"Остаток {as_int(item.get('quantity'))}, минимум {as_int(item.get('min_stock'), LOW_STOCK_DEFAULT)}",
            "entity_id": str(item.get("id") or ""),
            "action": "offer",
        })
    for label, title in (("replacement", "Нужна замена"), ("new", "Новые обращения"), ("waiting", "Ждём клиента")):
        count = chat_counts.get(label, 0)
        if count:
            items.append({
                "kind": "chat", "severity": "warning", "title": title,
                "subtitle": f"{count} диалогов", "entity_id": label, "action": "chats",
            })
    for operation in operations:
        status = str(operation.get("status") or "queued")
        items.append({
            "kind": "operation",
            "severity": "danger" if status == "failed" else "info",
            "title": str(operation.get("operation") or "Операция GGSEL"),
            "subtitle": f"{operation.get('target') or '—'} · {status}",
            "entity_id": str(operation.get("target") or ""),
            "action": "operations",
        })
    return {
        "count": len(items),
        "items": items[:20],
        "summary": {
            "low_stock": len(low_stock),
            "new_chats": chat_counts.get("new", 0),
            "replacement": chat_counts.get("replacement", 0),
            "operations": len(operations),
        },
    }


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


@app.get("/app/api/search")
@miniapp_api
def miniapp_global_search(user: dict[str, Any]):
    query = str(request.args.get("q") or "").strip()
    return miniapp_success(global_search_payload(query))


@app.get("/app/api/attention")
@miniapp_api
def miniapp_attention(user: dict[str, Any]):
    return miniapp_success(attention_center_payload())


@app.get("/app/api/recent")
@miniapp_api
def miniapp_recent(user: dict[str, Any]):
    return miniapp_success(get_recent_views(as_int(request.args.get("limit"), 8)))


@app.get("/app/api/dashboard")
@miniapp_api
def miniapp_dashboard(user: dict[str, Any]):
    errors: dict[str, str] = {}
    force = request.args.get("refresh") == "1"
    end_text = str(request.args.get("end") or date.today().isoformat())
    start_text = str(request.args.get("start") or (date.today() - timedelta(days=29)).isoformat())
    try:
        start_date = datetime.strptime(start_text, "%Y-%m-%d").date()
        end_date = datetime.strptime(end_text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("Дата должна быть в формате YYYY-MM-DD") from exc
    if end_date < start_date:
        start_date, end_date = end_date, start_date
    # Protect the API from accidental multi-year scans in a mobile UI.
    if (end_date - start_date).days > 370:
        start_date = end_date - timedelta(days=370)

    product_id = str(request.args.get("product_id") or "").strip()
    balance_data: dict[str, Any] = {}
    sales_data: list[dict[str, Any]] = []
    offers_data: list[dict[str, Any]] = []

    balance_future = executor.submit(lambda: _balance_payload(ggsel.balance()))
    sales_future = executor.submit(lambda: _sales_payload(ggsel.last_sales()))
    offers_future = executor.submit(lambda: collect_all_offers(force=force))

    try:
        balance_data = balance_future.result(timeout=max(REQUEST_TIMEOUT * 2, 20))
    except Exception as exc:
        errors["balance"] = str(exc)
    try:
        sales_data = sales_future.result(timeout=max(REQUEST_TIMEOUT * 2, 20))
    except Exception as exc:
        errors["sales"] = str(exc)
    try:
        offers_data = offers_future.result(timeout=max(REQUEST_TIMEOUT * 3, 30))
    except Exception as exc:
        errors["offers"] = str(exc)

    enriched_offers = enrich_offer_settings(offers_data)
    product_options = [
        {"id": str(item.get("id") or ""), "title": str(item.get("title") or item.get("id") or "Товар")}
        for item in enriched_offers
        if item.get("id")
    ]
    product_options.sort(key=lambda item: item["title"].casefold())
    selected_offer = next((item for item in enriched_offers if str(item.get("id") or "") == product_id), None)
    product_name = str(selected_offer.get("title") or "") if selected_offer else ""

    try:
        analytics = calculate_dashboard_analytics(
            start_date,
            end_date,
            product_id=product_id,
            product_name=product_name,
            force=force,
        )
    except Exception as exc:
        errors["analytics"] = str(exc)
        analytics = {
            "start": start_date.isoformat(),
            "end": end_date.isoformat(),
            "current": _finalize_period_metrics(_empty_period_metrics()),
            "previous": _finalize_period_metrics(_empty_period_metrics()),
            "deltas": {},
            "series": [],
            "top_products": [],
            "complete": False,
            "api_limits": {
                "views": False,
                "checkout_clicks": False,
                "conversion": False,
                "note": "Аналитика временно недоступна.",
            },
        }

    if product_id:
        sales_data = [item for item in sales_data if str(item.get("item_id") or "") == product_id]

    stats = {
        "offers": len(enriched_offers),
        "active": sum(1 for x in enriched_offers if x.get("status") == "active"),
        "paused": sum(1 for x in enriched_offers if x.get("status") == "paused"),
        "out_of_stock": sum(1 for x in enriched_offers if as_int(x.get("quantity")) <= 0),
        "low_stock": sum(1 for x in enriched_offers if x.get("low_stock")),
    }
    low_stock_items = sorted(
        [item for item in enriched_offers if item.get("low_stock")],
        key=lambda item: (as_int(item.get("quantity")), str(item.get("title") or "").casefold()),
    )[:8]
    low_stock_payload = [
        {
            "id": item.get("id"),
            "title": item.get("title"),
            "quantity": as_int(item.get("quantity")),
            "min_stock": as_int(item.get("min_stock"), LOW_STOCK_DEFAULT),
            "status": item.get("status"),
        }
        for item in low_stock_items
    ]
    support = _dashboard_chat_stats()
    run_background(maybe_send_low_stock_alerts, offers_data)
    return miniapp_success(
        {
            "balance": balance_data,
            "sales": sales_data[:12],
            "stats": stats,
            "analytics": analytics,
            "products": product_options,
            "selected_product": {"id": product_id, "title": product_name},
            "low_stock_items": low_stock_payload,
            "support": support,
        },
        errors=errors,
    )


@app.get("/app/api/offers")
@miniapp_api
def miniapp_offers(user: dict[str, Any]):
    query = str(request.args.get("q") or "").strip().casefold()
    status = str(request.args.get("status") or "all").strip().lower()
    force = request.args.get("refresh") == "1"
    page = max(1, as_int(request.args.get("page"), 1))
    per_page = max(10, min(as_int(request.args.get("per_page"), 30), 100))

    offers = enrich_offer_settings(collect_all_offers(force=force))
    run_background(maybe_send_low_stock_alerts, offers)
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
    elif status == "low_stock":
        offers = [item for item in offers if item.get("low_stock")]
    elif status == "favorite":
        offers = [item for item in offers if item.get("favorite")]
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


@app.put("/app/api/offers/<int:offer_id_value>/favorite")
@miniapp_api
def miniapp_offer_favorite(user: dict[str, Any], offer_id_value: int):
    body = request.get_json(silent=True) or {}
    favorite = set_offer_favorite(offer_id_value, bool(body.get("favorite", True)))
    audit_miniapp(user.get("id"), "offer_favorite", offer_id_value, {"favorite": favorite})
    return miniapp_success({"offer_id": offer_id_value, "favorite": favorite})


def _extract_offer_detail(data: Any, offer_id_value: int) -> dict[str, Any]:
    """Find the most useful offer dictionary inside a V1/V2 response.

    GGSEL currently returns different wrappers for different seller accounts.
    Some responses use ``content``, others ``data``/``product``/``offer``.
    We walk the response and prefer a dictionary whose ID matches the requested
    offer and which contains actual card fields rather than a thin wrapper.
    """
    candidates: list[tuple[int, dict[str, Any]]] = []

    def walk(value: Any, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(value, dict):
            candidate_id = offer_id(value)
            score = 0
            if candidate_id is not None:
                score += 8
                if str(candidate_id) == str(offer_id_value):
                    score += 100
            for key in (
                "title_ru", "name_goods", "name", "price", "price_rur",
                "quantity", "num_in_stock", "in_stock", "visible", "status",
                "description_ru", "description", "info", "category", "category_id",
            ):
                if value.get(key) not in (None, "", [], {}):
                    score += 3
            if score:
                candidates.append((score, value))
            for nested in value.values():
                if isinstance(nested, (dict, list)):
                    walk(nested, depth + 1)
        elif isinstance(value, list):
            for nested in value[:100]:
                walk(nested, depth + 1)

    walk(data)
    if not candidates:
        return {}
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return dict(candidates[0][1])


def _deep_merge_offer_raw(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    """Merge useful values without replacing good data by empty placeholders."""
    result = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge_offer_raw(dict(result[key]), value)
        elif value not in (None, "", [], {}):
            result[key] = value
    return result


def _raw_contains(raw: dict[str, Any], keys: tuple[str, ...]) -> bool:
    return any(key in raw and raw.get(key) is not None for key in keys)


def _merge_offer_normalized(
    catalogue_item: dict[str, Any],
    detail_items: list[dict[str, Any]],
    offer_id_value: int,
) -> dict[str, Any]:
    """Keep reliable V1 catalogue values and enrich them with detail fields."""
    merged = dict(catalogue_item or {})
    merged.setdefault("id", offer_id_value)

    for raw in detail_items:
        if not raw:
            continue
        normalized = normalize_offer_item(raw)
        if normalized.get("id"):
            merged["id"] = normalized["id"]
        if _raw_contains(raw, ("title_ru", "name_goods", "name", "title")) and normalized.get("title") not in (None, "", "—"):
            merged["title"] = normalized["title"]
        if _raw_contains(raw, ("status", "state", "offer_status", "visible", "active")) and normalized.get("status") != "unknown":
            merged["status"] = normalized["status"]
        if _raw_contains(raw, ("price", "price_rur")):
            merged["price"] = normalized.get("price")
        if _raw_contains(raw, ("currency", "currency_type")) and normalized.get("currency"):
            merged["currency"] = normalized["currency"]
        if _raw_contains(raw, ("quantity", "num_in_stock", "in_stock", "products_count", "stock")):
            merged["quantity"] = normalized.get("quantity")
        if _raw_contains(raw, ("category", "category_title", "category_id")):
            if normalized.get("category"):
                merged["category"] = normalized["category"]
            if normalized.get("category_id") is not None:
                merged["category_id"] = normalized["category_id"]
        if "is_autoselling" in raw:
            merged["is_autoselling"] = bool(raw.get("is_autoselling"))
        if "sold_products_count" in raw:
            merged["sold_products_count"] = as_int(raw.get("sold_products_count"))
        for field in ("updated_at", "created_at"):
            if raw.get(field) not in (None, ""):
                merged[field] = raw.get(field)

    merged.setdefault("title", "—")
    merged.setdefault("status", "unknown")
    merged.setdefault("currency", "RUB")
    merged.setdefault("quantity", 0)
    merged.setdefault("price", 0.0)
    merged.setdefault("is_autoselling", False)
    merged.setdefault("sold_products_count", 0)
    return merged


@app.get("/app/api/offers/<int:offer_id_value>")
@miniapp_api
def miniapp_offer(user: dict[str, Any], offer_id_value: int):
    # The catalogue list is currently the most reliable source for title,
    # price, stock and status. Do not throw those values away when GGSEL's V2
    # detail endpoint returns only a thin object with an ID.
    catalogue_item = next(
        (
            dict(item)
            for item in collect_all_offers()
            if str(item.get("id") or "") == str(offer_id_value)
        ),
        {},
    )

    detail_items: list[dict[str, Any]] = []
    source_errors: dict[str, str] = {}

    try:
        v1_data = ggsel.product_v1(offer_id_value)
        v1_item = _extract_offer_detail(v1_data, offer_id_value)
        if v1_item:
            detail_items.append(v1_item)
    except Exception as exc:
        source_errors["v1"] = str(exc)
        logger.warning("GGSEL V1 offer detail failed for %s: %s", offer_id_value, exc)

    try:
        v2_data = ggsel.offer_v2(offer_id_value)
        v2_item = _extract_offer_detail(v2_data, offer_id_value)
        if v2_item:
            detail_items.append(v2_item)
    except Exception as exc:
        source_errors["v2"] = str(exc)
        logger.warning("GGSEL V2 offer detail failed for %s: %s", offer_id_value, exc)

    raw: dict[str, Any] = {}
    for detail in detail_items:
        raw = _deep_merge_offer_raw(raw, detail)

    normalized = _merge_offer_normalized(catalogue_item, detail_items, offer_id_value)

    # Give the edit form dependable aliases even if a GGSEL detail response did
    # not contain them. Description/instructions still come from the raw detail
    # response when available.
    if normalized.get("title") not in (None, "", "—"):
        raw.setdefault("title_ru", normalized["title"])
    if normalized.get("price") is not None:
        raw.setdefault("price", normalized["price"])
    if normalized.get("category_id") is not None:
        raw.setdefault("category_id", normalized["category_id"])
    raw.setdefault("id", normalized.get("id") or offer_id_value)

    settings = get_offer_settings(offer_id_value)
    normalized["settings"] = settings
    normalized["favorite"] = str(offer_id_value) in get_favorite_offer_ids()
    normalized["low_stock"] = str(normalized.get("status") or "") != "archived" and as_int(normalized.get("quantity")) <= as_int(settings.get("min_stock"), LOW_STOCK_DEFAULT)
    remember_recent_view(
        "offer", offer_id_value, str(normalized.get("title") or f"Товар {offer_id_value}"),
        {"quantity": as_int(normalized.get("quantity")), "status": normalized.get("status"), "price": normalized.get("price")},
    )
    return miniapp_success(
        {"normalized": normalized, "raw": raw, "settings": settings},
        source_errors=source_errors,
    )


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
    record_inventory_upload(offer_id_value, values)
    operation = record_operation("stock_add", offer_id_value, {"batches": results, "count": len(values)}, "completed")
    settings = get_offer_settings(offer_id_value)
    activation_result: Any = None
    activate_once = body.get("auto_activate") is True
    if settings.get("auto_activate") or activate_once:
        activation_result = ggsel.batch_offers_v2("activate", [offer_id_value])
        record_operation("offer_activate", offer_id_value, activation_result)
    invalidate_offer_cache()
    audit_miniapp(user.get("id"), "stock_add", offer_id_value, {"count": len(values), "auto_activate": bool(activation_result)})
    return miniapp_success({"added": len(values), "batches": len(results), "results": results, "operation": operation, "auto_activation": activation_result})


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
    record_operation("stock_archive", offer_id_value, result)
    pause_result: Any = None
    if delete_all and get_offer_settings(offer_id_value).get("auto_pause"):
        pause_result = ggsel.batch_offers_v2("pause", [offer_id_value])
        record_operation("offer_pause", offer_id_value, pause_result)
    invalidate_offer_cache()
    audit_miniapp(
        user.get("id"), "stock_archive", offer_id_value, {"delete_all": delete_all, "product_ids": product_ids, "auto_pause": bool(pause_result)}
    )
    return miniapp_success({"result": result, "auto_pause": pause_result})


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
    record_operation("offer_patch", offer_id_value, result, "completed")
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
    record_operation("offer_create", "", result)
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
    operation = record_operation(f"offer_{action}", offer_id_value, result)
    invalidate_offer_cache()
    audit_miniapp(user.get("id"), f"offer_{action}", offer_id_value, result)
    return miniapp_success({"result": result, "operation": operation})


@app.get("/app/api/offers/<int:offer_id_value>/settings")
@miniapp_api
def miniapp_offer_settings_get(user: dict[str, Any], offer_id_value: int):
    return miniapp_success(get_offer_settings(offer_id_value))


@app.put("/app/api/offers/<int:offer_id_value>/settings")
@miniapp_api
def miniapp_offer_settings_put(user: dict[str, Any], offer_id_value: int):
    body = request.get_json(silent=True) or {}
    require_confirmation(body)
    settings = save_offer_settings(offer_id_value, body)
    audit_miniapp(user.get("id"), "offer_settings", offer_id_value, settings)
    return miniapp_success(settings)


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
    operation = record_operation(f"offers_{action}", ",".join(map(str, offer_ids)), result)
    invalidate_offer_cache()
    audit_miniapp(user.get("id"), f"offers_{action}", ",".join(map(str, offer_ids)), result)
    return miniapp_success({"result": result, "operation": operation})


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


@app.get("/app/api/analytics")
@miniapp_api
def miniapp_analytics(user: dict[str, Any]):
    start_text = str(request.args.get("start") or date.today().isoformat())
    end_text = str(request.args.get("end") or start_text)
    start = datetime.strptime(start_text, "%Y-%m-%d").date()
    end = datetime.strptime(end_text, "%Y-%m-%d").date()
    if end < start:
        start, end = end, start
    return miniapp_success(calculate_receipts_analytics(start, end))


@app.get("/app/api/operations")
@miniapp_api
def miniapp_operations(user: dict[str, Any]):
    refresh = request.args.get("refresh") == "1"
    if refresh:
        items = refresh_operations()
    else:
        with db_connect() as conn:
            rows = conn.execute(
                "SELECT id, job_id, operation, target, status, result_json, created_at, updated_at FROM async_operations ORDER BY id DESC LIMIT 100"
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            try:
                item["result"] = json.loads(item.get("result_json") or "{}")
            except json.JSONDecodeError:
                item["result"] = item.get("result_json") or ""
            items.append(item)
    return miniapp_success(items)


@app.get("/app/api/sales")
@miniapp_api
def miniapp_sales(user: dict[str, Any]):
    raw = ggsel.last_sales()
    upsert_sales(raw)
    return miniapp_success(_sales_payload(raw))


@app.get("/app/api/orders/<invoice_id>")
@miniapp_api
def miniapp_order(user: dict[str, Any], invoice_id: str):
    raw = ggsel.order_info(invoice_id)
    if isinstance(raw, dict):
        upsert_order(invoice_id, raw)
        index_sold_content_from_order(invoice_id, raw)
    payload = _order_payload(invoice_id, raw)
    payload["note"] = get_order_note(invoice_id)
    remember_recent_view("order", invoice_id, str(payload.get("name") or f"Заказ #{invoice_id}"), {"amount": payload.get("amount"), "currency": payload.get("currency")})
    return miniapp_success(payload)


@app.put("/app/api/orders/<invoice_id>/note")
@miniapp_api
def miniapp_order_note(user: dict[str, Any], invoice_id: str):
    body = request.get_json(silent=True) or {}
    saved = save_order_note(invoice_id, str(body.get("tag") or ""), str(body.get("note") or ""))
    audit_miniapp(user.get("id"), "order_note", invoice_id, saved)
    return miniapp_success(saved)


@app.get("/app/api/reviews")
@miniapp_api
def miniapp_reviews(user: dict[str, Any]):
    page = max(1, as_int(request.args.get("page"), 1))
    return miniapp_success(_review_payload(ggsel.reviews(page)))


@app.get("/app/api/chats/status")
@miniapp_api
def miniapp_chat_status(user: dict[str, Any]):
    rebuild_conversations()
    with db_connect() as conn:
        row = conn.execute("SELECT message_date,created_at,debate_id,invoice_id FROM messages WHERE sender='buyer' ORDER BY id DESC LIMIT 1").fetchone()
        total = conn.execute("SELECT COUNT(*) AS total FROM conversations").fetchone()
    signed_url=f"{APP_URL}/ggsel"+(f"?secret={GGSEL_WEBHOOK_SECRET}" if GGSEL_WEBHOOK_SECRET else "")
    return miniapp_success({"last_webhook_message":dict(row) if row else None,"local_chats":as_int(total['total'] if total else 0),"webhook_url":signed_url,"compat_mode":GGSEL_WEBHOOK_COMPAT_MODE,"note":"Сообщения объединяются по email/аккаунту покупателя. Один клиент отображается одной перепиской, даже если GGSEL создаёт новые ID диалогов."})


@app.get("/app/api/chats")
@miniapp_api
def miniapp_chats(user: dict[str, Any]):
    return miniapp_success(_conversation_chat_payload(str(request.args.get('query') or '')))


@app.get("/app/api/conversations/<conversation_id>")
@miniapp_api
def miniapp_conversation(user: dict[str, Any], conversation_id: str):
    rebuild_conversations()
    summary=_conversation_summary(conversation_id)
    if not summary:
        raise ValueError("Диалог не найден")
    # Pull the latest GGSEL thread once and merge it into local history.
    latest=str(summary.get('latest_debate_id') or '')
    api_data={}
    if latest:
        try: api_data=ggsel.chat_messages(latest)
        except Exception as exc: logger.warning("Unable to load GGSEL chat %s: %s", latest, exc)
    messages=_message_payload(api_data, latest) if api_data else []
    local=_conversation_messages(conversation_id)
    merged=[]; seen=set()
    for item in [*messages,*local]:
        key=(str(item.get('id') or ''),str(item.get('date_written') or ''),str(item.get('message') or item.get('url') or ''),bool(item.get('seller')))
        if key in seen: continue
        seen.add(key); merged.append(item)
    merged.sort(key=lambda x: parse_iso_datetime(x.get('date_written')) or datetime.min.replace(tzinfo=timezone.utc))
    remember_recent_view('chat',conversation_id,str(summary.get('buyer_email') or summary.get('product_name') or 'Клиент'),{'invoice_id':summary.get('latest_invoice_id')})
    return miniapp_success({'conversation':summary,'messages':merged})


@app.put("/app/api/conversations/<conversation_id>/profile")
@miniapp_api
def miniapp_conversation_profile(user: dict[str, Any], conversation_id: str):
    body=request.get_json(silent=True) or {}
    note=str(body.get('note') or '').strip()[:4000]
    tags=[str(x).strip()[:40] for x in (body.get('tags') or []) if str(x).strip()][:20]
    pinned=1 if body.get('pinned') else 0; favorite=1 if body.get('favorite') else 0
    now=datetime.now(timezone.utc).isoformat()
    with db_connect() as conn:
        conn.execute("""INSERT INTO customer_profiles(conversation_id,note,tags_json,pinned,favorite,updated_at)
                        VALUES(?,?,?,?,?,?) ON CONFLICT(conversation_id) DO UPDATE SET
                        note=excluded.note,tags_json=excluded.tags_json,pinned=excluded.pinned,favorite=excluded.favorite,updated_at=excluded.updated_at""",
                     (conversation_id,note,json.dumps(tags,ensure_ascii=False),pinned,favorite,now))
    audit_miniapp(user.get('id'),'customer_profile',conversation_id,{'tags':tags,'pinned':bool(pinned),'favorite':bool(favorite)})
    return miniapp_success(_customer_profile(conversation_id))


@app.put("/app/api/conversations/<conversation_id>/label")
@miniapp_api
def miniapp_conversation_label(user: dict[str, Any], conversation_id: str):
    body=request.get_json(silent=True) or {}; require_confirmation(body)
    label=str(body.get('label') or 'new').strip().lower()
    if label not in {'new','waiting','replacement','resolved'}: raise ValueError('Неизвестная метка')
    note=str(body.get('note') or '').strip()[:1000]
    with db_connect() as conn:
        conn.execute("""INSERT INTO chat_labels(debate_id,label,note,updated_at) VALUES(?,?,?,?)
                        ON CONFLICT(debate_id) DO UPDATE SET label=excluded.label,note=excluded.note,updated_at=excluded.updated_at""",
                     (conversation_id,label,note,datetime.now(timezone.utc).isoformat()))
    return miniapp_success({'conversation_id':conversation_id,'label':label,'note':note})


@app.post("/app/api/conversations/<conversation_id>/messages")
@miniapp_api
def miniapp_send_conversation_message(user: dict[str, Any], conversation_id: str):
    body=request.get_json(silent=True) or {}; require_confirmation(body)
    message=str(body.get('message') or '').strip()
    if not message: raise ValueError('Сообщение пустое')
    if len(message)>4000: raise ValueError('Сообщение слишком длинное')
    summary=_conversation_summary(conversation_id); debate_id=str(summary.get('latest_debate_id') or '')
    if not debate_id: raise ValueError('У клиента нет активного ID диалога GGSEL')
    result=ggsel.send_chat_message(debate_id,message)
    save_message_record(message_id='',debate_id=debate_id,invoice_id=str(summary.get('latest_invoice_id') or ''),sender='seller',message_text=message,image_url='',message_date=datetime.now(timezone.utc).isoformat(),conversation_id=conversation_id,buyer_email=str(summary.get('buyer_email') or ''))
    ensure_conversation(debate_id=debate_id,invoice_id=str(summary.get('latest_invoice_id') or ''),email=str(summary.get('buyer_email') or ''),account=str(summary.get('buyer_account') or ''),product_name=str(summary.get('product_name') or ''),preview=message,when=datetime.now(timezone.utc).isoformat())
    audit_miniapp(user.get('id'),'chat_reply',conversation_id,{'debate_id':debate_id,'length':len(message)})
    return miniapp_success(result)


@app.get("/app/api/export/offers")
@miniapp_api
def miniapp_export_offers(user: dict[str, Any]):
    offers = enrich_offer_settings(collect_all_offers())
    return miniapp_success([
        {
            "id": item.get("id"),
            "title": item.get("title"),
            "status": item.get("status"),
            "price": item.get("price"),
            "currency": item.get("currency"),
            "quantity": item.get("quantity"),
            "category": item.get("category"),
            "min_stock": (item.get("settings") or {}).get("min_stock"),
        }
        for item in offers
    ])


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
    return jsonify({"ok": True, "service": "ggsel-telegram-bot-miniapp-v4.8"})


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
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = request.form.to_dict() or request.args.to_dict()

    if GGSEL_WEBHOOK_SECRET:
        provided = request.args.get("secret") or request.headers.get("X-GGSEL-Webhook-Secret", "")
        valid_secret = bool(provided) and secrets.compare_digest(str(provided), GGSEL_WEBHOOK_SECRET)
        looks_like_ggsel = any(data.get(key) not in (None, "") for key in ("DebateId", "InvoiceId", "MessageDate", "Message", "ImagePath"))
        if not valid_secret:
            if GGSEL_WEBHOOK_COMPAT_MODE and not provided and looks_like_ggsel:
                logger.warning("Accepted legacy GGSEL webhook without secret; update forwarding URL to the signed URL shown in Mini App")
            else:
                return jsonify({"ok": False}), 403

    identity = "|".join(
        str(data.get(key) or "")
        for key in ("MessageId", "DebateId", "InvoiceId", "MessageDate", "Message", "ImagePath")
    )
    event_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    if remember_event(event_key):
        run_background(process_ggsel_event, data)
    return jsonify({"ok": True})


# ============================================================
# Seller workspace v5.0: inventory search, automation and ops
# ============================================================

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_api_error(service: str, endpoint: str, status: int, message: str, context: Any = None) -> None:
    try:
        with db_connect() as conn:
            conn.execute(
                "INSERT INTO api_errors(service, endpoint, status, message, context_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (str(service), str(endpoint), int(status or 0), str(message)[:2000], json.dumps(context, ensure_ascii=False, default=str) if context is not None else None, _utc_now()),
            )
    except Exception:
        logger.exception("Unable to store API error")


def normalize_secret_content(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip()).upper()


def content_fingerprint(value: Any) -> str:
    normalized = normalize_secret_content(value)
    return hmac.new(CONTENT_SEARCH_SECRET.encode("utf-8"), normalized.encode("utf-8"), hashlib.sha256).hexdigest()


def mask_secret_content(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) <= 8:
        return text[:2] + "…" + text[-2:] if len(text) > 4 else "••••"
    return text[:5] + "…" + text[-5:]


def record_inventory_upload(offer_id: Any, values: Iterable[str], product_name: str = "") -> int:
    now = _utc_now()
    inserted = 0
    with db_connect() as conn:
        for value in values:
            normalized = normalize_secret_content(value)
            if not normalized:
                continue
            cur = conn.execute(
                """INSERT OR IGNORE INTO inventory_ledger
                (offer_id, product_name, content_hash, content_masked, status, source, added_at, metadata_json)
                VALUES (?, ?, ?, ?, 'in_stock', 'upload', ?, ?)""",
                (str(offer_id or ""), product_name, content_fingerprint(normalized), mask_secret_content(value), now, json.dumps({"length": len(normalized)}, ensure_ascii=False)),
            )
            inserted += max(0, cur.rowcount)
    return inserted


def _extract_delivered_values(obj: Any, parent_key: str = "") -> list[str]:
    found: list[str] = []
    exact_keys = {"content", "delivered_content", "delivered", "code", "key", "serial", "unique_code", "product_value", "value"}
    if isinstance(obj, dict):
        for key, value in obj.items():
            key_l = str(key).lower()
            if key_l in exact_keys or any(token in key_l for token in ("delivered_content", "product_content", "license_key")):
                if isinstance(value, str) and 4 <= len(value.strip()) <= 10000:
                    found.extend(line.strip() for line in value.splitlines() if line.strip())
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, str) and item.strip(): found.append(item.strip())
                        elif isinstance(item, dict): found.extend(_extract_delivered_values(item, key_l))
            elif isinstance(value, (dict, list)):
                found.extend(_extract_delivered_values(value, key_l))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_extract_delivered_values(item, parent_key))
    return list(dict.fromkeys(found))


def index_sold_content_from_order(invoice_id: str, raw: dict[str, Any]) -> int:
    values = _extract_delivered_values(raw)
    if not values:
        return 0
    item_id = str(get_any(raw, ("item_id", "product_id", "id_goods"), ""))
    product_name = str(get_any(raw, ("product_name", "name", "title"), ""))
    sold_at = str(get_any(raw, ("date_pay", "purchase_date", "date"), _utc_now()))
    count = 0
    with db_connect() as conn:
        for value in values:
            fp = content_fingerprint(value)
            row = conn.execute("SELECT id FROM inventory_ledger WHERE content_hash=? ORDER BY id DESC LIMIT 1", (fp,)).fetchone()
            if row:
                conn.execute("UPDATE inventory_ledger SET status='sold', invoice_id=?, sold_at=?, product_name=COALESCE(NULLIF(product_name,''), ?) WHERE id=?", (invoice_id, sold_at, product_name, row["id"]))
            else:
                conn.execute("""INSERT INTO inventory_ledger(offer_id, product_name, content_hash, content_masked, status, invoice_id, source, added_at, sold_at, metadata_json)
                VALUES (?, ?, ?, ?, 'sold', ?, 'order_api', ?, ?, ?)""", (item_id, product_name, fp, mask_secret_content(value), invoice_id, sold_at, sold_at, json.dumps({"indexed_from_order": True}, ensure_ascii=False)))
            count += 1
    return count


def inventory_search(value: str) -> list[dict[str, Any]]:
    normalized = normalize_secret_content(value)
    if len(normalized) < 4:
        raise ValueError("Введите не менее 4 символов содержимого")
    fp = content_fingerprint(normalized)
    with db_connect() as conn:
        rows = conn.execute("SELECT * FROM inventory_ledger WHERE content_hash=? ORDER BY id DESC", (fp,)).fetchall()
    return [dict(row) for row in rows]


def inventory_history(limit: int = 100, offer_id: str = "", status: str = "") -> list[dict[str, Any]]:
    clauses, params = [], []
    if offer_id:
        clauses.append("offer_id=?"); params.append(offer_id)
    if status:
        clauses.append("status=?"); params.append(status)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with db_connect() as conn:
        rows = conn.execute(f"SELECT * FROM inventory_ledger{where} ORDER BY id DESC LIMIT ?", (*params, max(1,min(limit,500)))).fetchall()
    return [dict(row) for row in rows]


def validate_import_values(values: list[str], offer_id: str = "") -> dict[str, Any]:
    cleaned = [str(v).strip() for v in values if str(v).strip()]
    unique = list(dict.fromkeys(cleaned))
    duplicates = len(cleaned) - len(unique)
    malformed = []
    known = []
    valid = []
    with db_connect() as conn:
        for value in unique:
            normalized = normalize_secret_content(value)
            if len(normalized) < 5 or len(normalized) > 5000:
                malformed.append(mask_secret_content(value)); continue
            row = conn.execute("SELECT status, invoice_id, offer_id FROM inventory_ledger WHERE content_hash=? ORDER BY id DESC LIMIT 1", (content_fingerprint(value),)).fetchone()
            if row:
                known.append({"value": mask_secret_content(value), "status": row["status"], "invoice_id": row["invoice_id"], "offer_id": row["offer_id"]})
            else:
                valid.append(value)
    return {"total": len(cleaned), "unique": len(unique), "duplicates": duplicates, "malformed": malformed, "known": known, "valid": valid, "valid_count": len(valid)}


def list_templates() -> list[dict[str, Any]]:
    with db_connect() as conn:
        rows = conn.execute("SELECT * FROM reply_templates ORDER BY category, name").fetchall()
    return [dict(row) for row in rows]


def render_template_text(body: str, context: dict[str, Any]) -> str:
    allowed = {k: str(v or "") for k,v in context.items()}
    return re.sub(r"\{([a-zA-Z0-9_]+)\}", lambda m: allowed.get(m.group(1), m.group(0)), body)


def sla_payload() -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    with db_connect() as conn:
        rows = conn.execute("""SELECT debate_id, MAX(CASE WHEN sender='buyer' THEN message_date END) buyer_at,
            MAX(CASE WHEN sender='seller' THEN message_date END) seller_at,
            MAX(invoice_id) invoice_id, MAX(message_text) last_text
            FROM messages WHERE debate_id!='' GROUP BY debate_id ORDER BY MAX(id) DESC LIMIT 300""").fetchall()
    items=[]; waiting=[]
    for row in rows:
        b=parse_datetime_any(row["buyer_at"]); s=parse_datetime_any(row["seller_at"])
        unanswered = bool(b and (not s or b>s))
        minutes = max(0, int((now-b).total_seconds()/60)) if unanswered and b else 0
        severity = "critical" if minutes>=SLA_CRITICAL_MINUTES else "warning" if minutes>=SLA_WARNING_MINUTES else "ok"
        item={"debate_id":row["debate_id"],"invoice_id":row["invoice_id"],"minutes":minutes,"unanswered":unanswered,"severity":severity,"last_text":row["last_text"]}
        items.append(item)
        if unanswered: waiting.append(item)
    return {"unanswered":len(waiting),"warning":sum(x["severity"]=="warning" for x in waiting),"critical":sum(x["severity"]=="critical" for x in waiting),"items":waiting[:100]}


def product_analytics_payload(days: int = 30) -> list[dict[str, Any]]:
    cutoff=(datetime.now(timezone.utc)-timedelta(days=max(1,days))).isoformat()
    with db_connect() as conn:
        rows=conn.execute("""SELECT COALESCE(NULLIF(product_id,''),'unknown') product_id, COALESCE(NULLIF(product_name,''),'Без названия') product_name,
            COUNT(*) sales_count, SUM(COALESCE(price_rub,0)) revenue_rub, SUM(COALESCE(price_usd,0)) revenue_usd,
            MIN(sale_date) first_sale, MAX(sale_date) last_sale
            FROM sales WHERE COALESCE(sale_date,updated_at)>=? GROUP BY product_id,product_name ORDER BY sales_count DESC LIMIT 100""",(cutoff,)).fetchall()
    result=[]
    for r in rows:
        d=dict(r); d["daily_rate"]=round((d["sales_count"] or 0)/max(1,days),2); result.append(d)
    return result


def recommendations_payload() -> list[dict[str, Any]]:
    offers=get_all_offers_cached(); analytics={str(x["product_id"]):x for x in product_analytics_payload(30)}
    recs=[]
    for offer in offers:
        oid=str(offer.get("id") or offer.get("offer_id") or "")
        stock=as_int(offer.get("stock") or offer.get("count") or offer.get("quantity"),0)
        settings=get_offer_settings(oid); rate=float(analytics.get(oid,{}).get("daily_rate") or 0)
        days_left=(stock/rate) if rate>0 else None
        if stock<=as_int(settings.get("min_stock"),LOW_STOCK_DEFAULT): recs.append({"type":"stock","severity":"critical" if stock==0 else "warning","offer_id":oid,"title":offer.get("title") or offer.get("name"),"text":f"Остаток {stock}; порог {settings.get('min_stock')}"})
        if days_left is not None and days_left<3: recs.append({"type":"forecast","severity":"warning","offer_id":oid,"title":offer.get("title") or offer.get("name"),"text":f"Запаса примерно на {days_left:.1f} дня"})
        if rate==0 and str(offer.get("status"))=="active": recs.append({"type":"no_sales","severity":"info","offer_id":oid,"title":offer.get("title") or offer.get("name"),"text":"Нет продаж за 30 дней — проверьте цену и карточку"})
    sla=sla_payload()
    if sla["critical"]: recs.insert(0,{"type":"sla","severity":"critical","title":"Поддержка","text":f"{sla['critical']} диалогов без ответа более {SLA_CRITICAL_MINUTES} мин"})
    return recs[:100]


def create_database_backup() -> dict[str, Any]:
    os.makedirs(BACKUP_DIR,exist_ok=True)
    stamp=datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    target=os.path.join(BACKUP_DIR,f"ggsel_bot_{stamp}.sqlite3")
    with sqlite3.connect(DB_PATH) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    return {"name":os.path.basename(target),"path":target,"size":os.path.getsize(target),"created_at":_utc_now()}


def list_backups() -> list[dict[str,Any]]:
    os.makedirs(BACKUP_DIR,exist_ok=True)
    out=[]
    for name in sorted(os.listdir(BACKUP_DIR),reverse=True):
        path=os.path.join(BACKUP_DIR,name)
        if os.path.isfile(path) and name.endswith('.sqlite3'):
            out.append({"name":name,"size":os.path.getsize(path),"created_at":datetime.fromtimestamp(os.path.getmtime(path),timezone.utc).isoformat()})
    return out[:50]


def daily_report_payload() -> dict[str,Any]:
    today=date.today(); analytics=calculate_receipts_analytics(today,today)
    sla=sla_payload(); recs=recommendations_payload()
    return {"date":today.isoformat(),"analytics":analytics,"sla":sla,"recommendations":recs[:10],"generated_at":_utc_now()}


def send_daily_report() -> dict[str,Any]:
    data=daily_report_payload(); a=data.get("analytics") or {}
    text=(f"📊 <b>Отчёт за {safe(data['date'])}</b>\n\n"
          f"Продаж: <b>{as_int(a.get('orders') or a.get('count'))}</b>\n"
          f"Сумма: <b>{safe(a.get('sales_total') or a.get('total') or 0)}</b>\n"
          f"Без ответа: <b>{data['sla']['unanswered']}</b>\n"
          f"Критичных: <b>{data['sla']['critical']}</b>\n"
          f"Рекомендаций: <b>{len(data['recommendations'])}</b>")
    send_text(text); return data


def run_automation_event(trigger_type: str, context: dict[str,Any]) -> list[dict[str,Any]]:
    with db_connect() as conn:
        rows=conn.execute("SELECT * FROM automation_rules WHERE enabled=1 AND trigger_type=? ORDER BY id",(trigger_type,)).fetchall()
    executed=[]
    for row in rows:
        try:
            cond=json.loads(row["condition_json"] or '{}'); action=json.loads(row["action_json"] or '{}')
            needle=str(cond.get("contains") or '').lower()
            if needle and needle not in str(context.get("text") or '').lower(): continue
            if cond.get("max_stock") is not None and as_int(context.get("stock"),999999)>as_int(cond.get("max_stock")): continue
            kind=action.get("type")
            if kind=='notify': send_text(str(action.get("text") or row["name"]))
            elif kind=='label_chat' and context.get('debate_id'): save_chat_label(str(context['debate_id']),str(action.get('label') or 'new'),str(action.get('note') or ''))
            executed.append({"id":row["id"],"name":row["name"],"action":kind})
        except Exception as exc:
            record_api_error('Automation',trigger_type,500,str(exc),dict(row))
    return executed


@app.get('/app/api/inventory/search')
@miniapp_api
def miniapp_inventory_search(user: dict[str,Any]):
    query=str(request.args.get('q') or '')
    return miniapp_success(inventory_search(query))


@app.get('/app/api/inventory/history')
@miniapp_api
def miniapp_inventory_history(user: dict[str,Any]):
    return miniapp_success(inventory_history(as_int(request.args.get('limit'),100),str(request.args.get('offer_id') or ''),str(request.args.get('status') or '')))


@app.post('/app/api/inventory/validate')
@miniapp_api
def miniapp_inventory_validate(user: dict[str,Any]):
    body=request.get_json(silent=True) or {}; values=body.get('values') if isinstance(body.get('values'),list) else []
    return miniapp_success(validate_import_values(values,str(body.get('offer_id') or '')))


@app.post('/app/api/inventory/reindex')
@miniapp_api
def miniapp_inventory_reindex(user: dict[str,Any]):
    body=request.get_json(silent=True) or {}; require_confirmation(body)
    invoices=body.get('invoice_ids') if isinstance(body.get('invoice_ids'),list) else []
    if not invoices:
        raw=ggsel.last_sales(); invoices=[str(get_any(x,('invoice_id','invoice','id'),'')) for x in extract_list(raw,('items','sales','content')) if isinstance(x,dict)][:max(1,min(as_int(body.get('limit'),20),50))]
    indexed=0; errors=[]
    for invoice in invoices:
        if not invoice: continue
        try:
            raw=ggsel.order_info(invoice)
            if isinstance(raw,dict): indexed+=index_sold_content_from_order(invoice,raw)
        except Exception as exc: errors.append({'invoice_id':invoice,'error':str(exc)})
    audit_miniapp(user.get('id'),'inventory_reindex',','.join(invoices),{'indexed':indexed,'errors':errors})
    return miniapp_success({'indexed':indexed,'orders':len(invoices),'errors':errors})


@app.get('/app/api/templates')
@miniapp_api
def miniapp_templates(user: dict[str,Any]): return miniapp_success(list_templates())


@app.post('/app/api/templates')
@miniapp_api
def miniapp_template_create(user: dict[str,Any]):
    b=request.get_json(silent=True) or {}; now=_utc_now()
    with db_connect() as conn:
        cur=conn.execute("INSERT INTO reply_templates(category,name,body,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?)",(str(b.get('category') or 'Общие'),str(b.get('name') or 'Шаблон'),str(b.get('body') or ''),1,now,now))
    return miniapp_success({'id':cur.lastrowid})


@app.delete('/app/api/templates/<int:template_id>')
@miniapp_api
def miniapp_template_delete(user: dict[str,Any],template_id:int):
    with db_connect() as conn: conn.execute('DELETE FROM reply_templates WHERE id=?',(template_id,))
    return miniapp_success({'deleted':template_id})


@app.get('/app/api/automations')
@miniapp_api
def miniapp_automations(user:dict[str,Any]):
    with db_connect() as conn: rows=conn.execute('SELECT * FROM automation_rules ORDER BY id DESC').fetchall()
    return miniapp_success([dict(r) for r in rows])


@app.post('/app/api/automations')
@miniapp_api
def miniapp_automation_create(user:dict[str,Any]):
    b=request.get_json(silent=True) or {}; now=_utc_now()
    with db_connect() as conn:
        cur=conn.execute("INSERT INTO automation_rules(name,trigger_type,condition_json,action_json,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",(str(b.get('name') or 'Правило'),str(b.get('trigger_type') or 'message'),json.dumps(b.get('condition') or {},ensure_ascii=False),json.dumps(b.get('action') or {},ensure_ascii=False),1,now,now))
    return miniapp_success({'id':cur.lastrowid})


@app.patch('/app/api/automations/<int:rule_id>')
@miniapp_api
def miniapp_automation_patch(user:dict[str,Any],rule_id:int):
    b=request.get_json(silent=True) or {}
    with db_connect() as conn: conn.execute('UPDATE automation_rules SET enabled=?, updated_at=? WHERE id=?',(1 if b.get('enabled') else 0,_utc_now(),rule_id))
    return miniapp_success({'id':rule_id,'enabled':bool(b.get('enabled'))})


@app.delete('/app/api/automations/<int:rule_id>')
@miniapp_api
def miniapp_automation_delete(user:dict[str,Any],rule_id:int):
    with db_connect() as conn: conn.execute('DELETE FROM automation_rules WHERE id=?',(rule_id,))
    return miniapp_success({'deleted':rule_id})


@app.get('/app/api/sla')
@miniapp_api
def miniapp_sla(user:dict[str,Any]): return miniapp_success(sla_payload())


@app.get('/app/api/product-analytics')
@miniapp_api
def miniapp_product_analytics(user:dict[str,Any]): return miniapp_success(product_analytics_payload(as_int(request.args.get('days'),30)))


@app.get('/app/api/recommendations')
@miniapp_api
def miniapp_recommendations(user:dict[str,Any]): return miniapp_success(recommendations_payload())


@app.get('/app/api/errors')
@miniapp_api
def miniapp_errors(user:dict[str,Any]):
    with db_connect() as conn: rows=conn.execute('SELECT * FROM api_errors ORDER BY id DESC LIMIT 200').fetchall()
    return miniapp_success([dict(r) for r in rows])


@app.post('/app/api/backups')
@miniapp_api
def miniapp_backup_create(user:dict[str,Any]): return miniapp_success(create_database_backup())


@app.get('/app/api/backups')
@miniapp_api
def miniapp_backups(user:dict[str,Any]): return miniapp_success(list_backups())


@app.get('/app/api/backups/<name>')
def miniapp_backup_download(name:str):
    authorization=request.headers.get('Authorization',''); init_data=authorization[4:] if authorization.startswith('tma ') else request.args.get('initData','')
    validate_telegram_init_data(init_data)
    safe_name=os.path.basename(name); path=os.path.join(BACKUP_DIR,safe_name)
    if not os.path.isfile(path): return jsonify({'ok':False,'error':'Копия не найдена'}),404
    return send_file(path,as_attachment=True,download_name=safe_name)


@app.get('/app/api/today')
@miniapp_api
def miniapp_today(user:dict[str,Any]): return miniapp_success(daily_report_payload())


@app.get('/app/api/report-settings')
@miniapp_api
def miniapp_report_settings_get(user:dict[str,Any]):
    with db_connect() as conn: row=conn.execute('SELECT * FROM report_settings WHERE id=1').fetchone()
    return miniapp_success(dict(row) if row else {'morning_enabled':0,'evening_enabled':0,'include_finance':1,'include_stock':1,'include_chats':1})


@app.put('/app/api/report-settings')
@miniapp_api
def miniapp_report_settings_put(user:dict[str,Any]):
    b=request.get_json(silent=True) or {}; now=_utc_now()
    vals=(1,1 if b.get('morning_enabled') else 0,1 if b.get('evening_enabled') else 0,1 if b.get('include_finance',True) else 0,1 if b.get('include_stock',True) else 0,1 if b.get('include_chats',True) else 0,now)
    with db_connect() as conn: conn.execute('INSERT OR REPLACE INTO report_settings(id,morning_enabled,evening_enabled,include_finance,include_stock,include_chats,updated_at) VALUES(?,?,?,?,?,?,?)',vals)
    return miniapp_success({'saved':True})


@app.post('/app/api/bulk-price')
@miniapp_api
def miniapp_bulk_price(user:dict[str,Any]):
    b=request.get_json(silent=True) or {}; require_confirmation(b)
    ids=[int(x) for x in b.get('offer_ids',[])]; mode=str(b.get('mode') or 'set'); value=float(b.get('value') or 0)
    offers={int(x.get('id') or x.get('offer_id')):x for x in get_all_offers_cached() if x.get('id') or x.get('offer_id')}
    results=[]
    for oid in ids[:100]:
        current=float(offers.get(oid,{}).get('price') or 0)
        new=value if mode=='set' else current*(1+value/100) if mode=='percent' else current+value
        new=max(0,round(new,2)); results.append({'offer_id':oid,'old':current,'new':new,'result':ggsel.patch_offer_v2(oid,{'price':new})})
    invalidate_offer_cache(); audit_miniapp(user.get('id'),'bulk_price',','.join(map(str,ids)),{'mode':mode,'value':value})
    return miniapp_success(results)


@app.get('/cron/daily-report')
def cron_daily_report():
    provided=request.args.get('secret') or request.headers.get('X-Cron-Secret','')
    if not CRON_SECRET or not secrets.compare_digest(str(provided),CRON_SECRET): return jsonify({'ok':False}),403
    return jsonify({'ok':True,'data':send_daily_report()})


init_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "3000")))
