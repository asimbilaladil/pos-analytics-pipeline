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
import time
import logging
from datetime import datetime, date, timedelta, timezone
from typing import Optional

from playwright.sync_api import sync_playwright
import psycopg2
from psycopg2.extras import execute_values

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


def parse_dt(s) -> Optional[datetime]:
    """Parse Revel datetime string into UTC-aware datetime."""
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
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def to_float(val, default=0.0) -> float:
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


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
    page.wait_for_load_state("networkidle")
    context.storage_state(path=STATE_FILE)
    log.info("Logged in and session saved")


def fetch_all_pages(context, endpoint: str, params: dict, label="records") -> list:
    """
    Paginate through all results from a Revel endpoint.
    Params are passed as a dict so Playwright correctly URL-encodes
    colons in datetime strings (e.g. T00:00:00 → %3A).
    """
    all_records = []
    offset = 0

    while True:
        paged = {**params, "limit": 1000, "offset": offset, "format": "json"}
        resp = context.request.get(endpoint, params=paged)

        if resp.status != 200:
            log.error("  %s — HTTP %d at offset %d: %s",
                      endpoint, resp.status, offset, resp.text()[:200])
            break

        data = resp.json()
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


def fetch_items_for_orders(context, order_ids: list) -> list:
    """
    Fetch all OrderItems for a list of order IDs.

    WHY batches: establishment= filter is broken on OrderItem endpoint —
    it silently returns all locations. Using order__in= is the only
    reliable way to get items for specific orders.

    BATCH_SIZE=200: keeps URL length safe while minimising API round-trips.
    Each batch is fully paginated to catch large orders.
    """
    all_items = []
    total_batches = (len(order_ids) + BATCH_SIZE - 1) // BATCH_SIZE

    for i in range(0, len(order_ids), BATCH_SIZE):
        batch = order_ids[i: i + BATCH_SIZE]
        batch_num = (i // BATCH_SIZE) + 1
        ids_str = ",".join(map(str, batch))
        offset = 0

        while True:
            resp = context.request.get(
                f"{BASE_URL}/resources/OrderItem/",
                params={
                    "format":   "json",
                    "order__in": ids_str,
                    "limit":    1000,
                    "offset":   offset,
                },
            )

            if resp.status != 200:
                log.error("  Items batch %d/%d — HTTP %d",
                          batch_num, total_batches, resp.status)
                break

            data = resp.json()
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
def fetch_and_upsert_products(context, conn) -> None:
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
            p.get("product_class"),
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
        bool(item.get("is_voided", False)),
        parse_dt(item.get("voided_date")),
        extract_id(item.get("voided_by_user")),
        item.get("voided_reason"),
        bool(item.get("deleted", False)),
        parse_dt(item.get("deleted_date")),
        ingestion_date,
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
def bulk_upsert(cur, table: str, columns: list, rows: list) -> int:
    """Bulk insert with ON CONFLICT DO NOTHING. Returns row count attempted."""
    if not rows:
        return 0
    col_str = ", ".join(columns)
    sql = f"INSERT INTO {table} ({col_str}) VALUES %s ON CONFLICT DO NOTHING"
    execute_values(cur, sql, rows, page_size=500)
    return len(rows)


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
            item_date = item_created_dt.date() if item_created_dt else ingestion_date

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
        "  Inserting: %d orders, %d items, %d modifiers",
        len(order_rows), len(item_rows), len(modifier_rows),
    )

    with conn.cursor() as cur:
        bulk_upsert(cur, "orders",         ORDER_COLS,    order_rows)
        bulk_upsert(cur, "order_items",    ITEM_COLS,     item_rows)
        bulk_upsert(cur, "modifier_items", MODIFIER_COLS, modifier_rows)
    conn.commit()

    return {
        "orders_inserted":  len(order_rows),
        "items_inserted":   len(item_rows),
    }


# ─── Per-Establishment Run ───────────────────────────────────────────────────
def run_establishment(context, conn, est_id: int,
                      range_from: str, range_to: str,
                      ingestion_date: date,
                      modifier_cache: dict) -> None:
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
        )
        log.info("Total orders: %d", len(all_orders))

        if not all_orders:
            log.info("No orders — skipping")
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE ingestion_log SET completed_at=%s, status='success', "
                    "orders_fetched=0, items_fetched=0 WHERE id=%s",
                    (datetime.now(timezone.utc), log_id),
                )
            conn.commit()
            return

        # ── 2. Fetch Items via order__in batches ─────────────────
        order_ids = [o["id"] for o in all_orders]
        log.info("Fetching items for %d orders (batches of %d)...", len(order_ids), BATCH_SIZE)
        all_items = fetch_items_for_orders(context, order_ids)
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
                    completed_at    = %s,
                    status          = 'success',
                    orders_fetched  = %s,
                    items_fetched   = %s,
                    orders_inserted = %s,
                    items_inserted  = %s
                WHERE id = %s
                """,
                (
                    datetime.now(timezone.utc),
                    len(all_orders),
                    len(all_items),
                    stats["orders_inserted"],
                    stats["items_inserted"],
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
    args = parser.parse_args()

    target_date = date.fromisoformat(args.date) if args.date else None
    range_from, range_to, ingestion_date = get_date_range(target_date)
    log.info("Pipeline started — target date: %s", ingestion_date)
    log.info("Date range: %s to %s", range_from, range_to)

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
            login_and_save(context, page)
        else:
            page.goto(BASE_URL)
            if "login" in page.url or "authentication" in page.url:
                log.info("Session expired — re-logging in")
                login_and_save(context, page)
            else:
                log.info("Session valid")

        page.close()

        # Fetch reference data once — products and modifiers shared across all establishments
        fetch_and_upsert_products(context, conn)
        modifier_cache = fetch_and_upsert_modifiers(context, conn)

        # Run each establishment
        for est in establishments:
            try:
                run_establishment(
                    context, conn, est,
                    range_from, range_to,
                    ingestion_date,
                    modifier_cache,
                )
            except Exception as exc:
                log.error("Unhandled error for establishment %d: %s", est, exc)

        browser.close()

    conn.close()
    log.info("\nPipeline complete for %s", ingestion_date)


if __name__ == "__main__":
    main()
