"""
pipeline.py
===========
Daily Revel → PostgreSQL ingestion pipeline for Laynes Chicken Fingers.

This single script:
  1. Logs into Revel (with session caching)
  2. For each of 11 establishments, fetches yesterday's orders
  3. Fetches order items in batches of 200 using order__in=
  4. Inserts everything directly into PostgreSQL (no JSON files on disk)
  5. Logs each run to the ingestion_log table

Run daily via cron at e.g. 2:00 AM:
    0 2 * * * /usr/bin/python3 /opt/laynes/pipeline.py >> /var/log/laynes/pipeline.log 2>&1

Requirements:
    pip install playwright psycopg2-binary
    playwright install chromium

Environment variables (put in /etc/environment or a .env file):
    REVEL_USER        Revel login username
    REVEL_PASS        Revel login password
    DB_HOST           (default: localhost)
    DB_PORT           (default: 5432)
    DB_NAME           (default: laynes)
    DB_USER           (default: postgres)
    DB_PASS           PostgreSQL password (required)
    ESTABLISHMENTS    Comma-separated Revel establishment IDs (default: 32,14,48,7,6,25,36,26,20,40,15)
"""

import os
import sys
import json
import time
import logging
from datetime import datetime, date, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
load_dotenv()

from playwright.sync_api import sync_playwright
import psycopg2
from psycopg2.extras import execute_values

import raw_archive

# ─── Config ──────────────────────────────────────────────────────────────────
REVEL_USER   = os.getenv("REVEL_USER")
REVEL_PASS   = os.getenv("REVEL_PASS")
BASE_URL     = "https://laynes.revelup.com"
STATE_FILE   = "/tmp/revel_session.json"   # browser session cache
BATCH_SIZE   = 200                         # order IDs per order__in request
SCRIPT_VERSION = "1.0.0"

# Establishments loaded from .env — add new location IDs there, no code change needed
_raw = os.getenv("ESTABLISHMENTS", "32,14,48,7,6,25,36,26,20,40,15")
ESTABLISHMENTS = [int(x.strip()) for x in _raw.split(",") if x.strip()]

# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ─── Database ────────────────────────────────────────────────────────────────
def db_connect():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 5432)),
        dbname=os.getenv("DB_NAME", "laynes"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASS"),
    )


# ─── Helpers: parsing ────────────────────────────────────────────────────────
def extract_id(val) -> Optional[int]:
    """Extract integer ID from Revel URI or plain int. Returns None if empty."""
    if val is None:
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, str) and val.strip():
        try:
            return int(val.strip("/").split("/")[-1])
        except (ValueError, IndexError):
            return None
    return None


_REVEL_TZ = ZoneInfo("America/Chicago")

def parse_dt(s) -> Optional[datetime]:
    """Parse Revel datetime string into UTC-aware datetime.

    Revel's API returns naive local timestamps (America/Chicago), not UTC —
    confirmed against Revel's own UI and this establishment's real open/close
    hours. Naive strings are localized as Chicago time and converted to UTC;
    an explicit offset (rare) is trusted as-is.
    """
    if not s:
        return None
    for fmt in (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_REVEL_TZ)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def to_float(val, default=0.0) -> float:
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def with_retries(fn, attempts=3, delay=5, label="operation", non_retryable=()):
    """
    Retry fn() on any exception, with a fixed delay between attempts.

    raw_archive.ArchiveError is ALWAYS non-retryable, unconditionally, on top
    of whatever the caller passes — a local disk/storage failure must fail
    loudly and immediately, not be treated as "the kind of flakiness worth
    retrying the Revel API call for". This is hard-coded here (not left to
    each call site to remember) specifically because with_retries is nested
    in this codebase — e.g. main() wraps fetch_and_upsert_products() in its
    own outer with_retries, which would otherwise happily swallow an
    ArchiveError raised deep inside and retry the whole fetch anyway.
    """
    non_retryable = tuple(non_retryable) + (raw_archive.ArchiveError,)
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except non_retryable:
            raise
        except Exception as exc:
            if attempt == attempts:
                raise
            log.warning("  %s failed (attempt %d/%d): %s — retrying in %ds",
                        label, attempt, attempts, exc, delay)
            time.sleep(delay)


# ─── Revel API Helpers ────────────────────────────────────────────────────────
def login_and_save(context, page):
    """Login to Revel and save browser session."""
    log.info("Logging into Revel...")
    page.goto(BASE_URL)
    page.wait_for_url("**authentication.revelup.com**")
    page.wait_for_selector('input[name="username"]', timeout=60000)
    page.fill('input[name="username"]', REVEL_USER)
    page.click('button[type="submit"]')
    page.wait_for_selector('input[name="password"]', timeout=60000)
    page.fill('input[name="password"]', REVEL_PASS)
    page.click('button[type="submit"]')
    # networkidle can stall past 30s on Revel's dashboard (background polling
    # keeps the network non-idle) — give it real headroom to avoid spurious
    # timeouts that abort the whole run before any establishment is fetched.
    page.wait_for_load_state("networkidle", timeout=60000)
    context.storage_state(path=STATE_FILE)
    log.info("Logged in and session saved")


def fetch_all_pages(context, endpoint: str, params: dict, label="records",
                     *, resource: str = None, run_id: str = None,
                     establishment_id: int = None, date_window: tuple = None,
                     archive_date: date = None, window_key: str = None) -> list:
    """
    Paginate through all results from a Revel endpoint.
    Params are passed as a dict so Playwright correctly URL-encodes
    colons in datetime strings (e.g. T00:00:00 → %3A).

    Task 06: when `resource` is given, every attempt's untouched response
    body is archived (including malformed ones — see raw_archive) BEFORE
    json.loads is even attempted, with a unique filename per (page, attempt)
    so retries never collide. Order per attempt: HTTP response received ->
    archive raw body -> json.loads -> return parsed payload. A JSONDecodeError
    is retried like any other API flakiness (existing behavior, unchanged).
    An ArchiveError (a storage failure, not a content problem) is never
    retried — see with_retries — and propagates out of this function
    entirely rather than being treated like an ordinary fetch failure: a
    page must not be silently skipped (leaving `all_records` looking
    "complete" to the caller) just because local disk/archive storage broke.

    window_key (Task 09): pass through to raw_archive.archive_response when
    a single script invocation fetches multiple independent historical
    windows for the same (resource, establishment) — e.g. the Orders
    backfill's establishment x month chunks, which otherwise share run_id
    and archive_date (today's real date, not the chunk's own historical
    period) and would collide at page 1. Every other existing caller fetches
    exactly one window per (resource, establishment, run_id) and leaves this
    unset — no behavior change for them.
    """
    all_records = []
    offset = 0

    while True:
        paged = {**params, "limit": 1000, "offset": offset, "format": "json"}
        page = (offset // 1000) + 1
        attempt_box = [0]

        def _get_page():
            attempt_box[0] += 1
            r = context.request.get(endpoint, params=paged)
            if r.status != 200:
                raise RuntimeError(f"HTTP {r.status} at offset {offset}: {r.text()[:200]}")
            raw_text = r.text()  # untouched body

            if resource:
                # Archive BEFORE parsing, unconditionally — even a malformed
                # body gets archived as evidence of this failed attempt.
                # ArchiveError here is a storage failure and must propagate
                # immediately (with_retries treats it as non-retryable).
                raw_archive.archive_response(
                    resource, run_id, raw_text,
                    endpoint=endpoint, query_params=params, page=page,
                    offset=offset, attempt=attempt_box[0], window_key=window_key,
                    archive_date=archive_date or datetime.now(timezone.utc).date(),
                    fetch_time=datetime.now(timezone.utc), establishment_id=establishment_id,
                    date_window=date_window, pipeline_version=SCRIPT_VERSION,
                )

            return json.loads(raw_text)  # JSONDecodeError -> retryable, archive already preserved

        try:
            data = with_retries(_get_page, attempts=3, delay=5,
                                 label=f"{endpoint} offset {offset}")
        except raw_archive.ArchiveError:
            log.error("  Archive storage failure for %s page %d — aborting, "
                      "NOT retrying Revel and NOT processing partial results", resource, page)
            raise
        except Exception as exc:
            log.error("  %s — giving up at offset %d: %s", endpoint, offset, exc)
            break

        records = data.get("objects", [])
        total = data.get("meta", {}).get("total_count", 0)

        if not records:
            break

        all_records.extend(records)
        log.info("  Fetched %d/%d %s", len(all_records), total, label)

        if len(all_records) >= total:
            break

        offset += 1000
        time.sleep(0.3)

    return all_records


def fetch_items_for_orders(context, order_ids: list, *, run_id: str = None,
                            establishment_id: int = None,
                            archive_date: date = None,
                            window_key: str = None) -> list:
    """
    Fetch all OrderItems for a list of order IDs.

    WHY batches: establishment= filter is broken on OrderItem endpoint —
    it silently returns all locations. Using order__in= is the only
    reliable way to get items for specific orders.

    BATCH_SIZE=200: keeps URL length safe while minimising API round-trips.
    Each batch is fully paginated to catch large orders.

    Task 06: archives each page's untouched response (resource="order_items")
    before parsing — this includes ModifierItems, which arrive nested inside
    each OrderItem's "modifieritems" field; there is no separate top-level
    ModifierItem fetch in the live pipeline to archive independently.

    window_key (Task 10): same passthrough as fetch_all_pages' window_key
    (Task 09) — lets a single script invocation fetch multiple independent
    historical (establishment, month) windows without their page/batch
    counters colliding under one shared run_id/archive_date. Every existing
    caller fetches items for exactly one window per run and leaves this
    unset — no behavior change for them.

    Task 10: each page fetch is retried via the same with_retries(attempts=3,
    delay=5) used by fetch_all_pages — a lone slow/failed response (e.g. the
    30s request timeout hit live during Task 10) no longer aborts the whole
    order_ids list on the first blip. Mirrors fetch_all_pages' _get_page
    structure exactly: request -> status check -> archive (untouched body,
    even a malformed one) -> parse, all inside the retried closure, so a
    persistent failure (exhausts all 3 attempts) still raises out of
    with_retries and propagates to the caller unchanged — this function
    never silently returns partial results for a batch that failed outright.
    ArchiveError remains always non-retryable (with_retries' own hard-coded
    rule) and propagates immediately, same as fetch_all_pages.
    """
    all_items = []
    total_batches = (len(order_ids) + BATCH_SIZE - 1) // BATCH_SIZE
    page_num = 0

    for i in range(0, len(order_ids), BATCH_SIZE):
        batch = order_ids[i: i + BATCH_SIZE]
        batch_num = (i // BATCH_SIZE) + 1
        ids_str = ",".join(map(str, batch))
        offset = 0

        while True:
            query_params = {
                "format":   "json",
                "order__in": ids_str,
                "limit":    1000,
                "offset":   offset,
            }
            attempt_box = [0]

            def _get_page():
                attempt_box[0] += 1
                r = context.request.get(
                    f"{BASE_URL}/resources/OrderItem/",
                    params=query_params,
                )
                if r.status != 200:
                    raise RuntimeError(f"HTTP {r.status} at offset {offset}: {r.text()[:200]}")
                raw_text = r.text()  # untouched body
                if run_id:
                    raw_archive.archive_response(
                        "order_items", run_id, raw_text,
                        endpoint=f"{BASE_URL}/resources/OrderItem/",
                        query_params=query_params, page=page_num + 1, offset=offset,
                        attempt=attempt_box[0], window_key=window_key,
                        archive_date=archive_date or datetime.now(timezone.utc).date(),
                        fetch_time=datetime.now(timezone.utc), establishment_id=establishment_id,
                        pipeline_version=SCRIPT_VERSION,
                    )
                return json.loads(raw_text)  # JSONDecodeError -> retryable, archive already preserved

            try:
                data = with_retries(_get_page, attempts=3, delay=5,
                                     label=f"OrderItem batch {batch_num}/{total_batches} offset {offset}")
            except raw_archive.ArchiveError as exc:
                log.error("  Archive failed for order_items batch %d — aborting: %s", batch_num, exc)
                raise
            except Exception as exc:
                log.error("  Items batch %d/%d — giving up at offset %d: %s",
                          batch_num, total_batches, offset, exc)
                raise

            page_num += 1
            records = data.get("objects", [])
            total = data.get("meta", {}).get("total_count", 0)

            if not records:
                break

            all_items.extend(records)
            offset += 1000

            if offset >= total:
                break

        log.info("  Items batch %d/%d done — %d items so far",
                 batch_num, total_batches, len(all_items))
        time.sleep(0.2)

    return all_items


# ─── Product Reference Cache ─────────────────────────────────────────────────
def fetch_and_upsert_products(context, conn, run_id: str = None) -> None:
    """
    Fetch all products from /resources/Product/ and upsert into the products
    reference table. Called once per pipeline run before the establishment loop
    to satisfy the order_items.product_id FK constraint.
    """
    log.info("Fetching product reference data from Revel...")
    records = fetch_all_pages(
        context,
        endpoint=f"{BASE_URL}/resources/Product/",
        params={},
        label="products",
        resource="products",
        run_id=run_id,
    )

    if not records:
        log.warning("No products returned — order_items inserts may fail on FK")
        return

    rows = []
    for p in records:
        pid = p.get("id")
        if not pid:
            continue
        rows.append((
            pid,
            p.get("resource_uri"),
            (p.get("name") or "").strip() or f"Product #{pid}",
            p.get("productclass"),  # Revel's raw field is "productclass" (a
                                     # ProductClass URI ref), not "product_class"
                                     # — confirmed live; stored as-is (raw value,
                                     # column is VARCHAR, no ProductClass table).
            bool(p.get("is_combo", False)),
            bool(p.get("is_modifier", False)),
            to_float(p.get("price")),
            bool(p.get("active", True)),
        ))

    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO products (id, revel_uri, name, product_class, is_combo, is_modifier, base_price, active)
            VALUES %s
            ON CONFLICT (id) DO UPDATE SET
                name          = EXCLUDED.name,
                product_class = EXCLUDED.product_class,
                is_combo      = EXCLUDED.is_combo,
                is_modifier   = EXCLUDED.is_modifier,
                base_price    = EXCLUDED.base_price,
                active        = EXCLUDED.active,
                updated_at    = NOW()
            """,
            rows,
            page_size=500,
        )
    conn.commit()
    log.info("Product cache ready: %d products upserted", len(rows))


# ─── Modifier Reference Cache ────────────────────────────────────────────────
def fetch_and_upsert_modifiers(context, conn) -> dict:
    """
    Fetch all modifiers from /resources/Modifier/, upsert into the modifiers
    reference table, and return a {modifier_id: name} dict for use this run.
    Called once per pipeline run before any establishment loop.
    """
    log.info("Fetching modifier reference data from Revel...")
    records = fetch_all_pages(
        context,
        endpoint=f"{BASE_URL}/resources/Modifier/",
        params={},
        label="modifiers",
    )

    if not records:
        log.warning("No modifiers returned — modifier_name will be NULL this run")
        return {}

    rows = []
    cache = {}
    for m in records:
        mid = m.get("id")
        name = (m.get("name") or "").strip() or f"Modifier #{mid}"
        if not mid:
            continue
        rows.append((
            mid,
            m.get("resource_uri"),
            name,
            to_float(m.get("price")),
            bool(m.get("active", True)),
        ))
        cache[mid] = name

    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO modifiers (id, revel_uri, name, base_price, active)
            VALUES %s
            ON CONFLICT (id) DO UPDATE SET
                name       = EXCLUDED.name,
                base_price = EXCLUDED.base_price,
                active     = EXCLUDED.active,
                updated_at = NOW()
            """,
            rows,
            page_size=500,
        )
    conn.commit()
    log.info("Modifier cache ready: %d modifiers upserted", len(rows))
    return cache


# ─── Row Builders ─────────────────────────────────────────────────────────────
ORDER_COLS = [
    "id", "uuid", "establishment_id", "local_id",
    "created_date", "updated_date", "pickup_time",
    "dining_option", "pos_mode",
    "final_total", "subtotal", "tax", "gratuity", "discount_total_amount",
    "closed", "is_unpaid", "deleted", "is_discounted", "web_order",
    "customer_id", "number_of_people",
    "ingestion_date",
    # Task 04 fields, appended (not inserted mid-list, matching ITEM_COLS'
    # convention) so nothing that appends to an order tuple positionally
    # breaks. Were always in upsert_orders' DO UPDATE SET (COALESCE(EXCLUDED.
    # col, t.col)) but silently a no-op — EXCLUDED.col resolves to that
    # column's default (NULL) when the column isn't in the INSERT list at
    # all, not an error — until now (Task 09). Revel's raw Order response
    # already carries all of these on both the created-mode and updated-mode
    # paths (confirmed live), so populating them here benefits both, not
    # just the historical backfill.
    "created_by_user_id", "updated_by_user_id", "discount_amount",
    "discount_reason", "discounted_by_user_id", "exchanged",
    "service_charge", "surcharge", "remaining_due", "notes",
]

ITEM_COLS = [
    "id", "uuid", "order_id", "establishment_id",
    "product_id", "product_name", "quantity", "dining_option",
    "combo_product_id", "combo_uuid",
    "price", "pure_sales", "tax_amount", "modifier_amount", "is_discounted",
    "created_date", "start_time", "kitchen_completed",
    # kitchen_seconds is GENERATED ALWAYS AS — never insert it
    "is_voided", "voided_date", "voided_by_user_id", "voided_reason",
    "deleted", "deleted_date",
    "ingestion_date",
    # Task 04 fields, appended (not inserted mid-list) so created_date stays
    # at a fixed tuple index — insert_establishment_data and sync_updated.py
    # both index into item_tuple[15] for it. Populated by any caller that
    # has this detail per item (Task 07's updated-mode sync); the default
    # created-mode path's Revel responses already carry these fields too,
    # so they start getting captured here rather than staying NULL forever.
    "ervc_type", "item_type", "initial_price", "discount_amount",
    "discount_reason", "discounted_by_user_id", "cost", "exchanged",
    "void_ref_uuid",
]

MODIFIER_COLS = [
    "id", "order_item_id", "order_id", "establishment_id",
    "modifier_id", "modifier_name", "qty", "modifier_price",
    "is_discounted", "created_date",
]


def build_order_row(o: dict, est_id: int, ingestion_date: date) -> tuple:
    created = parse_dt(o.get("created_date"))
    return (
        o["id"],
        o.get("uuid"),
        est_id,
        o.get("local_id"),
        created,
        parse_dt(o.get("updated_date")),
        parse_dt(o.get("pickup_time")),
        o.get("dining_option"),
        o.get("pos_mode"),
        to_float(o.get("final_total")),
        to_float(o.get("subtotal")),
        to_float(o.get("tax")),
        to_float(o.get("gratuity")),
        to_float(o.get("discount_total_amount")),
        bool(o.get("closed", False)),
        bool(o.get("is_unpaid", False)),
        bool(o.get("deleted", False)),
        bool(o.get("is_discounted", False)),
        bool(o.get("web_order", False)),
        extract_id(o.get("customer")),
        o.get("number_of_people") or 0,
        ingestion_date,
        # Task 04 fields — see ORDER_COLS above for why they're appended.
        extract_id(o.get("created_by")),
        extract_id(o.get("updated_by")),
        to_float(o.get("discount_amount")) if o.get("discount_amount") is not None else None,
        o.get("discount_reason") or None,
        extract_id(o.get("discounted_by")),
        o.get("exchanged"),
        to_float(o.get("service_charge")) if o.get("service_charge") is not None else None,
        to_float(o.get("surcharge")) if o.get("surcharge") is not None else None,
        to_float(o.get("remaining_due")) if o.get("remaining_due") is not None else None,
        o.get("notes") or None,
    )


def build_item_row(item: dict, est_id: int, ingestion_date: date,
                   fallback_dt: datetime) -> tuple:
    created = parse_dt(item.get("created_date")) or fallback_dt
    return (
        item["id"],
        item.get("uuid"),
        extract_id(item.get("order")),
        est_id,
        extract_id(item.get("product")),
        item.get("product_name_override") or item.get("name"),
        to_float(item.get("quantity"), 1.0),
        item.get("dining_option"),
        extract_id(item.get("combo_product")),
        item.get("combo_uuid"),
        to_float(item.get("price")),
        to_float(item.get("pure_sales")),
        to_float(item.get("tax_amount")),
        to_float(item.get("modifier_amount")),
        bool(item.get("is_discounted", False)),
        created,
        parse_dt(item.get("start_time")),
        parse_dt(item.get("kitchen_completed")),
        # Revel's raw is_voided field is always null — voided_date is the
        # only reliable signal (verified live 2026-08-10).
        item.get("voided_date") is not None,
        parse_dt(item.get("voided_date")),
        # Fixed: Revel's raw field is "voided_by" (a User URI ref), not
        # "voided_by_user" — confirmed live against a known void item
        # (voided_by="/enterprise/User/88846/", voided_by_user=None).
        extract_id(item.get("voided_by")),
        item.get("voided_reason"),
        bool(item.get("deleted", False)),
        parse_dt(item.get("deleted_date")),
        ingestion_date,
        # Task 04 fields (Task 07 is the first caller with per-item detail
        # to populate these from) — see ITEM_COLS above for why they're
        # appended, not inserted mid-tuple.
        item.get("ervc_type"),
        item.get("item_type"),
        to_float(item.get("initial_price")) if item.get("initial_price") is not None else None,
        to_float(item.get("discount_amount")) if item.get("discount_amount") is not None else None,
        item.get("discount_reason") or None,
        extract_id(item.get("discounted_by")),
        to_float(item.get("cost")) if item.get("cost") is not None else None,
        item.get("exchanged"),
        item.get("void_ref_uuid"),
    )


def build_modifier_row(mod: dict, item_id: int, order_id: int,
                       est_id: int, item_date: date,
                       modifier_cache: dict) -> tuple:
    mid = extract_id(mod.get("modifier"))
    return (
        mod["id"],
        item_id,
        order_id,
        est_id,
        mid,
        modifier_cache.get(mid),           # name resolved from /resources/Modifier/
        to_float(mod.get("qty") or mod.get("quantity"), 1.0),
        to_float(mod.get("modifier_price")),
        bool(mod.get("is_discounted", False)),
        item_date,
    )


# ─── DB Insert ────────────────────────────────────────────────────────────────
def _upsert_stats(cur, sql: str, rows: list) -> dict:
    """
    Run an INSERT ... ON CONFLICT ... DO UPDATE (no DO NOTHING branch).

    Deliberately does not split inserted vs. updated. Postgres's usual trick
    for that — RETURNING (xmax = 0) — raises "cannot retrieve a system column
    in this context" on a partitioned table's INSERT ON CONFLICT DO UPDATE
    (confirmed live against orders/order_items); the alternative, a
    pre-upsert existence check, adds a query per batch for information that
    isn't needed. With DO UPDATE and no DO NOTHING, every row in a batch
    either inserts or updates — there is no third outcome — so "affected"
    is definitionally equal to "fetched". Correctness of the UPSERT itself
    does not depend on knowing which.
    """
    if not rows:
        return {"fetched": 0, "affected": 0}
    execute_values(cur, sql, rows, page_size=500)
    return {"fetched": len(rows), "affected": len(rows)}


def bulk_upsert_do_nothing(cur, table: str, columns: list, rows: list) -> dict:
    """
    INSERT ... ON CONFLICT DO NOTHING, for tables classified as immutable
    once written (see Task 05 classification below). Returns rows attempted;
    Postgres does not report insert-vs-skip counts for DO NOTHING without an
    extra RETURNING round trip, which isn't worth it for append-only data.
    """
    if not rows:
        return {"fetched": 0}
    col_str = ", ".join(columns)
    sql = f"INSERT INTO {table} ({col_str}) VALUES %s ON CONFLICT DO NOTHING"
    execute_values(cur, sql, rows, page_size=500)
    return {"fetched": len(rows)}


# Task 05 — UPSERT classification
# ─────────────────────────────────────────────────────────────────────────────
# mutable   → fields that can legitimately change after first ingestion get an
#             explicit DO UPDATE column list (never "update every column").
# immutable → fields describing WHAT the record fundamentally is (identity,
#             channel, who/what it belongs to) are set once at INSERT and never
#             overwritten by a later fetch — Revel doesn't move an order to a
#             different establishment or change what product a line item is.
# monotonic → event-completion fields (voided_*, refunded) that can go from
#             unset -> set but must never revert set -> unset. Implemented as
#             `col = table.col OR EXCLUDED.col` (booleans) or
#             `col = COALESCE(table.col, EXCLUDED.col)` (the rest), so a stale
#             or malformed re-fetch can never erase a real event.
# new nullable Task 04 columns (ervc_type, discount_amount, cost, etc.) use
# plain COALESCE(EXCLUDED.col, table.col): nothing populates them in the
# INSERT column list yet, so EXCLUDED.col is always NULL today and this is a
# no-op — but it's correct and ready the moment Task 09+ starts fetching them,
# without a second migration. This is also what "never overwrite a useful
# stored value with NULL merely because a response omitted a field" requires:
# Revel's OrderItem/Order responses are always full objects (verified live,
# not sparse patches), so a real NULL from Revel is trustworthy — but our own
# INSERT can still omit a column entirely (as it does today), and COALESCE is
# what protects against that gap.

# Task 07.4 — shadow-table targeting for the orders_v2/order_items_v2
# canonical-key validation (Task 07.3). Fixed internal allowlist, never a
# user-controlled table name. Default "production" is byte-for-byte the
# pre-existing behavior; every other caller (pipeline.main, sync_updated's
# default calls) is unaffected since they don't pass target= at all.
#
# Task 11 adds "payments" to both targets. Unlike orders/order_items,
# production `payments` already uses PRIMARY KEY(id) alone -- it never had
# the duplicate-row identity bug orders_v2/order_items_v2 exist to fix.
# payments_v2 exists purely so the historical Payments backfill runs
# completely isolated from the ongoing updated-mode sync writing to
# production `payments` concurrently, not to correct a schema problem.
#
# Task 12 adds "order_history" the same way, for the same reason -- isolation
# from the concurrent updated-mode sync, not an identity-bug fix.
#
# Task 13 adds "modifier_items" the same way. Phase 2 found zero mutations
# across 32 sampled OrderItems / 65 ModifierItems (4 establishments, ~6
# months old), so DO NOTHING semantics are retained unchanged -- this only
# adds table routing, not new UPSERT behavior.
UPSERT_TARGETS = {
    "production": {"orders": "orders", "order_items": "order_items", "payments": "payments",
                   "order_history": "order_history", "modifier_items": "modifier_items"},
    "shadow_v2": {"orders": "orders_v2", "order_items": "order_items_v2", "payments": "payments_v2",
                  "order_history": "order_history_v2", "modifier_items": "modifier_items_v2"},
}


def upsert_orders(cur, rows: list, target: str = "production") -> dict:
    if not rows:
        return {"fetched": 0, "inserted": 0, "updated": 0}
    if target not in UPSERT_TARGETS:
        raise ValueError(f"unknown upsert target {target!r}")
    table = UPSERT_TARGETS[target]["orders"]
    # _v2 tables use PRIMARY KEY(id) alone -- no partitioning, no
    # created_date in the conflict/identity key. That's the entire fix for
    # Task 07.2/07.3's duplicate-row problem: created_date becomes a normal
    # mutable field like every other column below, so a later fetch with a
    # revised created_date UPDATEs the existing row instead of creating a
    # second one. Everything else (NULL-safety, monotonic/COALESCE
    # classification, the canonical row-building in build_order_row) is
    # completely unchanged and shared with the production path.
    conflict_cols = "id" if target == "shadow_v2" else "id, created_date"
    created_date_set = "created_date           = EXCLUDED.created_date,\n            " if target == "shadow_v2" else ""
    col_str = ", ".join(ORDER_COLS)
    sql = f"""
        INSERT INTO {table} AS t ({col_str}) VALUES %s
        ON CONFLICT ({conflict_cols}) DO UPDATE SET
            {created_date_set}updated_date           = EXCLUDED.updated_date,
            pickup_time             = EXCLUDED.pickup_time,
            final_total             = EXCLUDED.final_total,
            subtotal                = EXCLUDED.subtotal,
            tax                     = EXCLUDED.tax,
            gratuity                = EXCLUDED.gratuity,
            discount_total_amount   = EXCLUDED.discount_total_amount,
            closed                  = EXCLUDED.closed,
            is_unpaid               = EXCLUDED.is_unpaid,
            deleted                 = EXCLUDED.deleted,
            is_discounted           = EXCLUDED.is_discounted,
            -- Task 05.1: reclassified from immutable -> potentially mutable.
            -- Staff-correctable channel/party fields; freezing them defeats
            -- the point of the updated_date sync (making old rows reflect
            -- later Revel corrections). customer_id uses COALESCE (never
            -- revert an attached customer back to anonymous on a later
            -- anonymous-looking fetch); dining_option/number_of_people
            -- plainly overwrite since Revel always returns a real value.
            -- "t" is a fixed alias for whichever table this targets (see
            -- Task 07.4's target= param) so these self-references work
            -- against orders_v2 too, not just orders.
            dining_option           = EXCLUDED.dining_option,
            customer_id             = COALESCE(EXCLUDED.customer_id, t.customer_id),
            number_of_people        = EXCLUDED.number_of_people,
            created_by_user_id      = COALESCE(EXCLUDED.created_by_user_id, t.created_by_user_id),
            updated_by_user_id      = COALESCE(EXCLUDED.updated_by_user_id, t.updated_by_user_id),
            discount_amount         = COALESCE(EXCLUDED.discount_amount, t.discount_amount),
            discount_reason         = COALESCE(EXCLUDED.discount_reason, t.discount_reason),
            discounted_by_user_id   = COALESCE(EXCLUDED.discounted_by_user_id, t.discounted_by_user_id),
            exchanged               = COALESCE(EXCLUDED.exchanged, t.exchanged),
            service_charge          = COALESCE(EXCLUDED.service_charge, t.service_charge),
            surcharge               = COALESCE(EXCLUDED.surcharge, t.surcharge),
            remaining_due           = COALESCE(EXCLUDED.remaining_due, t.remaining_due),
            notes                   = COALESCE(EXCLUDED.notes, t.notes),
            ingested_at             = NOW()
    """
    # Task 05.1 classification of every excluded field:
    #   id, created_date  -- conflict/partition key, structurally can't be here.
    #   uuid, establishment_id  -- identity/definitely immutable: an order
    #     doesn't get a new UUID or move to a different establishment.
    #   local_id  -- identity/definitely immutable: Revel's own per-
    #     establishment sequential order number, assigned once.
    #   ingestion_date  -- first-seen marker by design, not a Revel field.
    #   web_order  -- identity/likely immutable: describes HOW the order was
    #     created (web vs. POS), not a business decision staff would revise.
    #     Moderate confidence only, no live test performed -- revisit if
    #     evidence emerges.
    #   pos_mode  -- unknown: every order observed so far is "Q" (quick
    #     service); no evidence it varies at all, let alone changes after
    #     creation. Left excluded pending real variation to test against,
    #     not because it's confirmed immutable.
    return _upsert_stats(cur, sql, rows)


def upsert_order_items(cur, rows: list, target: str = "production") -> dict:
    if not rows:
        return {"fetched": 0, "inserted": 0, "updated": 0}
    if target not in UPSERT_TARGETS:
        raise ValueError(f"unknown upsert target {target!r}")
    table = UPSERT_TARGETS[target]["order_items"]
    conflict_cols = "id" if target == "shadow_v2" else "id, created_date"
    created_date_set = "created_date            = EXCLUDED.created_date,\n            " if target == "shadow_v2" else ""
    col_str = ", ".join(ITEM_COLS)
    sql = f"""
        INSERT INTO {table} AS t ({col_str}) VALUES %s
        ON CONFLICT ({conflict_cols}) DO UPDATE SET
            {created_date_set}price                   = EXCLUDED.price,
            pure_sales              = EXCLUDED.pure_sales,
            tax_amount              = EXCLUDED.tax_amount,
            modifier_amount         = EXCLUDED.modifier_amount,
            is_discounted           = EXCLUDED.is_discounted,
            start_time              = EXCLUDED.start_time,
            kitchen_completed       = EXCLUDED.kitchen_completed,
            -- Task 05.1: reclassified from immutable -> potentially mutable.
            -- Both are staff-correctable ("rang the wrong channel", "meant
            -- to ring 1 not 2"), same reasoning as orders.dining_option/
            -- number_of_people. Plain overwrite: Revel always returns a
            -- real value for both.
            -- "t" is a fixed alias for whichever table this targets (see
            -- Task 07.4's target= param) so these self-references work
            -- against order_items_v2 too, not just order_items.
            quantity                = EXCLUDED.quantity,
            dining_option           = EXCLUDED.dining_option,
            -- monotonic: once voided/deleted, a later fetch can never revert it
            is_voided               = t.is_voided OR EXCLUDED.is_voided,
            voided_date             = COALESCE(t.voided_date, EXCLUDED.voided_date),
            voided_by_user_id       = COALESCE(t.voided_by_user_id, EXCLUDED.voided_by_user_id),
            voided_reason           = COALESCE(t.voided_reason, EXCLUDED.voided_reason),
            deleted                 = t.deleted OR EXCLUDED.deleted,
            deleted_date            = COALESCE(t.deleted_date, EXCLUDED.deleted_date),
            -- new Task 04 columns, no-op today (see classification note above)
            ervc_type               = COALESCE(EXCLUDED.ervc_type, t.ervc_type),
            item_type               = COALESCE(EXCLUDED.item_type, t.item_type),
            initial_price           = COALESCE(EXCLUDED.initial_price, t.initial_price),
            discount_amount         = COALESCE(EXCLUDED.discount_amount, t.discount_amount),
            discount_reason         = COALESCE(EXCLUDED.discount_reason, t.discount_reason),
            discounted_by_user_id   = COALESCE(EXCLUDED.discounted_by_user_id, t.discounted_by_user_id),
            cost                    = COALESCE(EXCLUDED.cost, t.cost),
            exchanged               = COALESCE(EXCLUDED.exchanged, t.exchanged),
            void_ref_uuid           = COALESCE(EXCLUDED.void_ref_uuid, t.void_ref_uuid),
            ingested_at             = NOW()
    """
    # Task 05.1 classification of every excluded field:
    #   id, created_date  -- conflict/partition key, structurally can't be here.
    #   uuid, order_id, establishment_id  -- identity/definitely immutable:
    #     a line item doesn't get reparented to a different order or location.
    #   ingestion_date  -- first-seen marker by design, not a Revel field.
    #   product_id, combo_product_id, combo_uuid  -- identity/definitely
    #     immutable: this IS what the line item is and how it groups into a
    #     combo. A product substitution would show up as a different line
    #     item in Revel, not a mutation of this one.
    #   product_name  -- unknown: could plausibly be corrected (typo,
    #     re-labelled item) independently of product_id via
    #     product_name_override, but no live example was found to confirm
    #     either way. Left excluded pending a real example, not because
    #     immutability is confirmed.
    return _upsert_stats(cur, sql, rows)


def upsert_modifier_items(cur, rows: list, target: str = "production") -> dict:
    """
    Kept as DO NOTHING. Task 13 Phase 2 verified live: zero mutations across
    32 sampled OrderItems / 65 ModifierItems, 4 establishments, up to ~6
    months old -- modifier rows are immutable once written in practice, not
    just assumed. DO NOTHING remains correct.

    target= (Task 13): same UPSERT_TARGETS allowlist as every other upsert_*
    function. Default "production" is byte-for-byte the pre-existing
    behavior; the daily pipeline's existing call site doesn't pass target=
    at all, so it is unaffected.
    """
    if target not in UPSERT_TARGETS:
        raise ValueError(f"unknown upsert target {target!r}")
    table = UPSERT_TARGETS[target]["modifier_items"]
    return bulk_upsert_do_nothing(cur, table, MODIFIER_COLS, rows)


# ─── Dormant upsert helpers for tables with no fetch/write path yet ──────────
# payments, order_history, product_categories were created in Task 03/04.
# Nothing calls these yet — Task 07+ adds the Revel fetch code that will.
# Defined now so the UPSERT design for every raw-ingestion table is decided
# and tested together, per Task 05's requirements, rather than improvised
# piecemeal when each fetch path is built.

PAYMENT_COLS = [
    "id", "uuid", "order_id", "establishment_id",
    "payment_type", "other_payment_type",
    "amount", "amount_authorized", "tip", "gratuity", "change",
    "refunded", "refund_transaction_id",
    "transaction_id", "transaction_status", "transaction_captured", "processor_accepted",
    "card_type", "online", "source_type",
    "executed", "deleted", "exchanged",
    "station_id", "payment_date", "created_date", "updated_date",
    "created_by_user_id", "updated_by_user_id",
    "ingestion_date",
]

def upsert_payments(cur, rows: list, target: str = "production") -> dict:
    if not rows:
        return {"fetched": 0, "inserted": 0, "updated": 0}
    if target not in UPSERT_TARGETS:
        raise ValueError(f"unknown upsert target {target!r}")
    table = UPSERT_TARGETS[target]["payments"]
    col_str = ", ".join(PAYMENT_COLS)
    sql = f"""
        INSERT INTO {table} AS t ({col_str}) VALUES %s
        ON CONFLICT (id) DO UPDATE SET
            amount                  = EXCLUDED.amount,
            amount_authorized       = EXCLUDED.amount_authorized,
            tip                     = EXCLUDED.tip,
            gratuity                = EXCLUDED.gratuity,
            change                  = EXCLUDED.change,
            -- monotonic: a refund doesn't reverse
            refunded                = t.refunded OR EXCLUDED.refunded,
            refund_transaction_id   = COALESCE(t.refund_transaction_id, EXCLUDED.refund_transaction_id),
            transaction_id          = EXCLUDED.transaction_id,
            transaction_status      = EXCLUDED.transaction_status,
            transaction_captured    = EXCLUDED.transaction_captured,
            processor_accepted      = EXCLUDED.processor_accepted,
            executed                = EXCLUDED.executed,
            deleted                 = t.deleted OR EXCLUDED.deleted,
            exchanged               = EXCLUDED.exchanged,
            updated_date            = EXCLUDED.updated_date,
            payment_date            = EXCLUDED.payment_date,
            updated_by_user_id      = EXCLUDED.updated_by_user_id,
            ingested_at             = NOW()
    """
    # Immutable: id (conflict key), uuid, order_id, establishment_id,
    # payment_type, other_payment_type, card_type, online, source_type,
    # station_id, created_date, created_by_user_id, ingestion_date.
    # "t" is a fixed alias for whichever table target= resolves to (Task 07.4/
    # Task 11 convention, same as upsert_orders/upsert_order_items) so the
    # monotonic self-references work against payments_v2 too, not just
    # payments. Default target="production" is byte-for-byte the pre-existing
    # behavior; sync_updated.py's existing call site doesn't pass target= at
    # all, so it is unaffected.
    return _upsert_stats(cur, sql, rows)


ORDER_HISTORY_COLS = [
    "id", "uuid", "order_id",
    "opened_at", "closed_at",
    "opened_by_user_id", "closed_by_user_id",
    "opened_at_station_id", "closed_at_station_id",
]

def upsert_order_history(cur, rows: list, target: str = "production") -> dict:
    """
    DO UPDATE (Task 05.1). Task 02 live verification only established
    cardinality (exactly one OrderHistory row per closed order) — it did not
    prove those rows are immutable once written, and nothing has verified a
    correction can't happen (e.g. a manager fixing who closed an order).
    Per Task 05.1: don't default to DO NOTHING without proof of immutability.
    Plain overwrite on every non-key field — no monotonic/COALESCE concerns
    identified for open/close event data the way there were for voids/refunds.

    target= (Task 12): same UPSERT_TARGETS allowlist as upsert_orders/
    upsert_order_items/upsert_payments. Default "production" is byte-for-byte
    the pre-existing behavior; sync_updated.py's existing call site doesn't
    pass target= at all, so it is unaffected. No self-referencing monotonic/
    COALESCE columns here, so no "t" alias is needed the way payments needed
    one -- a plain overwrite works identically against either table.
    """
    if not rows:
        return {"fetched": 0, "affected": 0}
    if target not in UPSERT_TARGETS:
        raise ValueError(f"unknown upsert target {target!r}")
    table = UPSERT_TARGETS[target]["order_history"]
    col_str = ", ".join(ORDER_HISTORY_COLS)
    sql = f"""
        INSERT INTO {table} ({col_str}) VALUES %s
        ON CONFLICT (id) DO UPDATE SET
            opened_at              = EXCLUDED.opened_at,
            closed_at              = EXCLUDED.closed_at,
            opened_by_user_id      = EXCLUDED.opened_by_user_id,
            closed_by_user_id      = EXCLUDED.closed_by_user_id,
            opened_at_station_id   = EXCLUDED.opened_at_station_id,
            closed_at_station_id   = EXCLUDED.closed_at_station_id,
            ingested_at            = NOW()
    """
    # Immutable: id (conflict key), uuid, order_id.
    return _upsert_stats(cur, sql, rows)


PRODUCT_CATEGORY_COLS = [
    "id", "name", "parent_id", "active", "sorting", "description",
    "created_date", "updated_date",
]

def upsert_product_categories(cur, rows: list) -> dict:
    """
    Two passes, not one: product_categories.parent_id is a self-referencing
    FK. execute_values splits >page_size rows across multiple INSERT
    statements (confirmed live with 2,595 real categories at page_size=500)
    — Postgres only defers FK checks to end-of-STATEMENT, not end of the
    whole execute_values call, so a child landing in an earlier batch than
    its parent raises ForeignKeyViolation regardless of row order within
    the input list (sorting rows wouldn't help once it spans batches).

    Pass 1 upserts every row with parent_id forced NULL — no row can
    reference another row that doesn't already exist yet, so this is
    immune to batching/ordering entirely. Pass 2 UPDATEs parent_id for
    every row from a single VALUES join, by which point every id already
    exists in the table, so the FK is always satisfiable.
    """
    if not rows:
        return {"fetched": 0, "affected": 0}
    col_str = ", ".join(PRODUCT_CATEGORY_COLS)
    rows_no_parent = [(r[0], r[1], None, r[3], r[4], r[5], r[6], r[7]) for r in rows]
    sql = f"""
        INSERT INTO product_categories ({col_str}) VALUES %s
        ON CONFLICT (id) DO UPDATE SET
            name         = EXCLUDED.name,
            active       = EXCLUDED.active,
            sorting      = EXCLUDED.sorting,
            description  = EXCLUDED.description,
            updated_date = EXCLUDED.updated_date,
            ingested_at  = NOW()
    """
    # Reference/dimension data, same treatment as products and modifiers
    # (both already DO UPDATE (id) — categories can be renamed/reorganized).
    stats = _upsert_stats(cur, sql, rows_no_parent)

    # Pass 2: every id from this batch now exists in the table (either just
    # inserted, or already present), so parent_id can be set/cleared freely
    # in one statement without regard to insertion order.
    # template casts both columns explicitly: when a whole batch's parent_id
    # values happen to be NULL (e.g. a page of nothing but root categories),
    # Postgres can't infer a type for an all-NULL VALUES column and raises
    # DatatypeMismatch ("... is of type text") on the bare UPDATE assignment
    # without this.
    execute_values(cur, """
        UPDATE product_categories AS pc SET parent_id = v.parent_id
        FROM (VALUES %s) AS v(id, parent_id)
        WHERE pc.id = v.id
    """, [(r[0], r[2]) for r in rows], template="(%s::integer, %s::integer)", page_size=500)

    # Pre-commit validation, same transaction/cursor as both passes above —
    # if this ever finds a dangling parent_id, the whole category refresh
    # must not commit (caller's conn.commit() happens after this returns;
    # raising here means it never gets called, so pass 1 and pass 2 both
    # roll back together rather than committing a half-consistent table).
    cur.execute("""
        SELECT COUNT(*)
        FROM product_categories c
        WHERE c.parent_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM product_categories p WHERE p.id = c.parent_id
          )
    """)
    dangling = cur.fetchone()[0]
    if dangling:
        raise RuntimeError(
            f"upsert_product_categories: {dangling} rows have a parent_id "
            f"with no matching category — refusing to commit"
        )

    return stats


def insert_establishment_data(
    conn,
    est_id: int,
    ingestion_date: date,
    all_orders: list,
    items_by_order: dict,
    modifier_cache: dict,
) -> dict:
    """
    Insert all orders, items, modifiers for one establishment into the DB.
    Returns stats dict.
    """
    order_rows    = []
    item_rows     = []
    modifier_rows = []

    for order in all_orders:
        if not order.get("id"):
            continue

        order_created = parse_dt(order.get("created_date"))
        if order_created is None:
            continue  # can't route to partition without a date

        order_rows.append(build_order_row(order, est_id, ingestion_date))

        for item in items_by_order.get(order["id"], []):
            if not item.get("id"):
                continue

            item_tuple = build_item_row(item, est_id, ingestion_date, order_created)
            item_rows.append(item_tuple)

            # The created_date is the 16th element (index 15) in item_tuple
            item_created_dt = item_tuple[15]
            item_date = (item_created_dt.astimezone(_REVEL_TZ).date()
                         if item_created_dt else ingestion_date)

            for mod in item.get("modifieritems", []):
                if not mod.get("id"):
                    continue
                modifier_rows.append(
                    build_modifier_row(mod, item["id"],
                                       extract_id(item.get("order")),
                                       est_id, item_date,
                                       modifier_cache)
                )

    log.info(
        "  Upserting: %d orders, %d items, %d modifiers",
        len(order_rows), len(item_rows), len(modifier_rows),
    )

    with conn.cursor() as cur:
        order_stats    = upsert_orders(cur, order_rows)
        item_stats     = upsert_order_items(cur, item_rows)
        modifier_stats = upsert_modifier_items(cur, modifier_rows)
    conn.commit()

    log.info(
        "  orders: %d fetched / %d affected — items: %d fetched / %d affected — modifiers: %d fetched",
        order_stats.get("fetched", 0), order_stats.get("affected", 0),
        item_stats.get("fetched", 0), item_stats.get("affected", 0),
        modifier_stats.get("fetched", 0),
    )

    return {
        # ingestion_log.orders_affected/items_affected (Task 05.1) — NOT the
        # legacy orders_inserted/items_inserted columns. Those have always
        # actually held "rows attempted" (both the old bulk_upsert and even
        # legacy ingest_to_db.py's bulk_insert returned len(rows), never a
        # true insert-only count), but naming them "_inserted" now sits
        # wrong next to DO UPDATE semantics where affected == fetched by
        # construction. See migrations/05_1_ingestion_log_affected_columns.sql.
        "orders_affected":  order_stats.get("affected", 0),
        "items_affected":   item_stats.get("affected", 0),
    }


# ─── Per-Establishment Run ───────────────────────────────────────────────────
def run_establishment(context, conn, est_id: int,
                      range_from: str, range_to: str,
                      ingestion_date: date,
                      modifier_cache: dict,
                      run_id: str = None) -> None:
    log.info("\n--- Establishment %d ---", est_id)
    started_at = datetime.now(timezone.utc)

    # Start ingestion log entry
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ingestion_log
                (establishment_id, run_date, started_at, status, script_version)
            VALUES (%s, %s, %s, 'running', %s)
            RETURNING id
            """,
            (est_id, ingestion_date, started_at, SCRIPT_VERSION),
        )
        log_id = cur.fetchone()[0]
    conn.commit()

    try:
        # ── 1. Fetch Orders ──────────────────────────────────────
        all_orders = fetch_all_pages(
            context,
            endpoint=f"{BASE_URL}/resources/Order/",
            params={
                "establishment":      est_id,
                "created_date__gte":  range_from,
                "created_date__lte":  range_to,
            },
            label="orders",
            resource="orders",
            run_id=run_id,
            establishment_id=est_id,
            date_window=(range_from, range_to),
            archive_date=ingestion_date,
        )
        log.info("Total orders: %d", len(all_orders))

        if not all_orders:
            log.info("No orders — skipping")
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE ingestion_log SET completed_at=%s, status='success', "
                    "orders_fetched=0, items_fetched=0, orders_affected=0, items_affected=0, "
                    "orders_inserted=NULL, items_inserted=NULL "
                    "WHERE id=%s",
                    (datetime.now(timezone.utc), log_id),
                )
            conn.commit()
            return

        # ── 2. Fetch Items via order__in batches ─────────────────
        order_ids = [o["id"] for o in all_orders]
        log.info("Fetching items for %d orders (batches of %d)...", len(order_ids), BATCH_SIZE)
        all_items = fetch_items_for_orders(context, order_ids, run_id=run_id,
                                            establishment_id=est_id, archive_date=ingestion_date)
        log.info("Total items fetched: %d", len(all_items))

        # ── 3. Group items by order ID ───────────────────────────
        items_by_order = {}
        for item in all_items:
            oid = extract_id(item.get("order"))
            if oid:
                items_by_order.setdefault(oid, []).append(item)

        orders_with_items = sum(1 for o in all_orders if o["id"] in items_by_order)
        log.info(
            "Orders with items: %d/%d (%.1f%%)",
            orders_with_items, len(all_orders),
            orders_with_items / len(all_orders) * 100,
        )

        # ── 4. Insert into DB ────────────────────────────────────
        stats = insert_establishment_data(
            conn, est_id, ingestion_date, all_orders, items_by_order, modifier_cache
        )

        # ── 5. Update log — success ──────────────────────────────
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE ingestion_log SET
                    completed_at     = %s,
                    status           = 'success',
                    orders_fetched   = %s,
                    items_fetched    = %s,
                    orders_affected  = %s,
                    items_affected   = %s,
                    orders_inserted  = NULL,
                    items_inserted   = NULL
                WHERE id = %s
                """,
                (
                    datetime.now(timezone.utc),
                    len(all_orders),
                    len(all_items),
                    stats["orders_affected"],
                    stats["items_affected"],
                    log_id,
                ),
            )
        conn.commit()
        log.info("Establishment %d done", est_id)

    except Exception as exc:
        conn.rollback()
        log.error("ERROR on establishment %d: %s", est_id, exc)
        import traceback
        traceback.print_exc()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE ingestion_log SET completed_at=%s, status='failed', "
                "error_message=%s WHERE id=%s",
                (datetime.now(timezone.utc), str(exc)[:2000], log_id),
            )
        conn.commit()


# ─── Date Range ──────────────────────────────────────────────────────────────
def get_date_range(target_date: date = None):
    """
    Returns (range_from, range_to) strings for the target date.
    Defaults to yesterday (complete day).
    """
    if target_date is None:
        target_date = date.today() - timedelta(days=1)
    range_from = target_date.strftime("%Y-%m-%dT00:00:00")
    range_to   = target_date.strftime("%Y-%m-%dT23:59:59")
    return range_from, range_to, target_date


# ─── Entry Point ─────────────────────────────────────────────────────────────
def main():
    # Validate env vars
    if not REVEL_USER or not REVEL_PASS:
        log.error("REVEL_USER and REVEL_PASS must be set")
        sys.exit(1)
    if not os.getenv("DB_PASS"):
        log.error("DB_PASS must be set")
        sys.exit(1)

    # Parse optional --date override for backfilling
    import argparse
    parser = argparse.ArgumentParser(description="Laynes Revel → PostgreSQL daily pipeline")
    parser.add_argument(
        "--date", type=str, default=None,
        help="Target date YYYY-MM-DD (default: yesterday). Use for backfilling."
    )
    parser.add_argument(
        "--establishments", type=str, default=None,
        help="Comma-separated list of establishment IDs to run (default: all 11)"
    )
    parser.add_argument(
        "--skip-reference", action="store_true",
        help="Skip product/modifier re-fetch from Revel (use DB cache). Speeds up backfills."
    )
    args = parser.parse_args()

    target_date = date.fromisoformat(args.date) if args.date else None
    range_from, range_to, ingestion_date = get_date_range(target_date)
    log.info("Pipeline started — target date: %s", ingestion_date)
    log.info("Date range: %s to %s", range_from, range_to)

    run_id = raw_archive.new_run_id()
    log.info("Raw archive run_id: %s (archive root: %s)", run_id, raw_archive.RAW_ARCHIVE_DIR)

    establishments = ESTABLISHMENTS
    if args.establishments:
        establishments = [int(x.strip()) for x in args.establishments.split(",")]
        log.info("Running only establishments: %s", establishments)

    # Connect to DB
    conn = db_connect()
    log.info("DB connected")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        # Session handling
        if os.path.exists(STATE_FILE):
            log.info("Using saved Revel session")
            context = browser.new_context(storage_state=STATE_FILE)
        else:
            context = browser.new_context()

        page = context.new_page()

        # Validate / refresh session
        if not os.path.exists(STATE_FILE):
            with_retries(lambda: login_and_save(context, page), attempts=2, delay=10, label="login")
        else:
            page.goto(BASE_URL)
            if "login" in page.url or "authentication" in page.url:
                log.info("Session expired — re-logging in")
                with_retries(lambda: login_and_save(context, page), attempts=2, delay=10, label="login")
            else:
                log.info("Session valid")

        page.close()

        # Fetch reference data once — products and modifiers shared across all establishments
        if args.skip_reference:
            log.info("--skip-reference: loading modifier cache from DB (skipping Revel fetch)")
            with conn.cursor() as cur:
                cur.execute("SELECT id, name FROM modifiers")
                modifier_cache = {row[0]: row[1] for row in cur.fetchall()}
            log.info("Modifier cache loaded from DB: %d modifiers", len(modifier_cache))
        else:
            with_retries(lambda: fetch_and_upsert_products(context, conn, run_id=run_id), attempts=2, delay=10,
                         label="fetch_and_upsert_products")
            modifier_cache = with_retries(lambda: fetch_and_upsert_modifiers(context, conn), attempts=2, delay=10,
                                           label="fetch_and_upsert_modifiers")

        # Task 07 — opt-in update-aware sync. Default (unset or "created")
        # runs the exact same loop as always; the else branch below is
        # untouched. "updated" is a separate, additive path — never wired
        # in as the default, per Task 07's requirement.
        sync_mode = os.getenv("REVEL_SYNC_MODE", "created")
        log.info("REVEL_SYNC_MODE=%s", sync_mode)

        if sync_mode == "updated":
            import sync_updated  # deferred: sync_updated imports pipeline, avoids a circular import at module load
            # Task 16 prerequisite: REVEL_WRITE_TARGET is a separate opt-in
            # flag from REVEL_SYNC_MODE, same reasoning -- default "production"
            # is byte-for-byte the pre-existing behavior; not set in .env yet,
            # so this line has no effect until Task 16 cutover deliberately
            # sets it.
            write_target = os.getenv("REVEL_WRITE_TARGET", "production")
            for est in establishments:
                try:
                    report = sync_updated.run_establishment_updated(context, conn, est, run_id, modifier_cache, target=write_target)
                    log.info("Establishment %d (updated mode, target=%s) done: %s", est, write_target, report)
                except Exception as exc:
                    log.error("Unhandled error for establishment %d (updated mode): %s", est, exc)
            try:
                cat_stats, prod_stats = sync_updated.sync_reference_data(context, conn, run_id)
                log.info("Reference data (updated mode): categories=%s products=%s", cat_stats, prod_stats)
            except Exception as exc:
                log.error("Unhandled error refreshing products/product_categories (updated mode): %s", exc)
        else:
            # Run each establishment
            for est in establishments:
                try:
                    run_establishment(
                        context, conn, est,
                        range_from, range_to,
                        ingestion_date,
                        modifier_cache,
                        run_id=run_id,
                    )
                except Exception as exc:
                    log.error("Unhandled error for establishment %d: %s", est, exc)

        browser.close()

    conn.close()
    log.info("\nPipeline complete for %s", ingestion_date)


if __name__ == "__main__":
    main()
