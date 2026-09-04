"""
export_raw.py
==============
Standalone raw-data exporter for the Nederland (est 26) competitor investigation.
The owner wants raw rows, not analysis — this script fetches, does not clean,
filter, dedupe, round, or aggregate anything. See the accompanying report for
the field-mapping findings this script's column choices are based on.

This is independent of the daily pipeline: it imports helpers from pipeline.py
(extract_id, parse_dt, login_and_save) rather than copying them, but never
calls pipeline.py's main(), never writes to Postgres, and never modifies
pipeline.py, aggregate_features.py, dashboard.py, sales_report.py, or
database_design.sql. Reads from Postgres are read-only (establishments.name,
products.product_class) — reference joins only, same tables pipeline.py
already maintains.

Output: one Parquet file (zstd) per store per month, for orders (File A),
and for Nederland/Beaumont only, order items (File B) and modifiers (File C).
Money fields are Decimal end to end — parsed out of the raw JSON text with
parse_float=Decimal so a float never touches a cent, and written as
Parquet decimal128.

Usage:
    python export_raw.py --establishments 26 --start 2026-06-01 --end 2026-06-30 --out exports/
"""

import argparse
import json
import logging
import os
import random
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import psycopg2
import pyarrow as pa
import pyarrow.parquet as pq
from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pipeline as pl  # reuse extract_id / parse_dt / login_and_save — do not copy-paste

BASE_URL = pl.BASE_URL
STATE_FILE = pl.STATE_FILE
BATCH_SIZE = 200  # order IDs per order__in request — matches pipeline.py's proven batch size

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("export_raw")

ESTABLISHMENTS = {
    32: "Airtex", 14: "Beaumont", 48: "Downtown Houston", 7: "Ella",
    6: "Katy", 25: "Mission Bend", 36: "Missouri City", 26: "Nederland",
    20: "Pasadena", 40: "Rosenberg", 15: "Shepherd", 54: "Cypress",
}

DINING_CHANNELS = {
    0: "To Go", 1: "Eat In", 2: "Delivery", 3: "Catering", 4: "Drive Through",
    5: "Online Ordering", 6: "Spirit Night", 7: "Shipping", 8: "Pickup",
    9: "DoorDash Drive", 100: "DD Marketplace", 101: "Uber Eats",
    102: "Eat In Fun", 103: "To Go Fun", 104: "Drive Thru Fun",
    105: "Lane A", 106: "Lane B",
}

# Item/modifier files (B & C) are scoped to these two stores only, per the
# owner's ask — File A (orders) is not restricted.
ITEM_LEVEL_ESTABLISHMENTS = {26, 14}

SCALE6 = Decimal("0.000001")   # money columns
SCALE3 = Decimal("0.001")      # quantity columns (weighted items can be fractional)


class FetchFailure(RuntimeError):
    """Raised when a Revel request could not be completed after retries, or
    when pagination produced fewer records than Revel's own total_count.
    Never swallowed — a store-month that hits this writes no Parquet file."""


# ─── Decimal-safe JSON + money helpers ───────────────────────────────────────
def parse_json_decimal(resp):
    """Parse response body with parse_float=Decimal so numeric JSON literals
    (Revel sends some money fields unquoted, e.g. "final_total": 15.14) are
    decoded straight from the wire text into Decimal — never routed through
    a Python float, even transiently."""
    return json.loads(resp.text(), parse_float=Decimal)


def to_decimal(val, scale=SCALE6):
    if val is None:
        return None
    if isinstance(val, float):
        # Should be unreachable — parse_json_decimal already eliminates floats.
        # Kept as a hard failure, not a silent float->Decimal cast, because a
        # cast here would be exactly the "stray float() destroys cents" bug.
        raise TypeError(f"float leaked into to_decimal(): {val!r}")
    if isinstance(val, Decimal):
        d = val
    elif isinstance(val, str):
        val = val.strip()
        if not val:
            return None
        try:
            d = Decimal(val)
        except InvalidOperation:
            return None
    elif isinstance(val, int):
        d = Decimal(val)
    else:
        raise TypeError(f"Unexpected numeric type {type(val)} for {val!r}")
    return d.quantize(scale)


# ─── Read-only Postgres reference joins ──────────────────────────────────────
def db_connect_readonly():
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"), port=int(os.getenv("DB_PORT", 5432)),
        dbname=os.getenv("DB_NAME", "laynes"), user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASS"),
    )
    conn.set_session(readonly=True)
    return conn


def load_product_classes(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT id, product_class FROM products")
        return {row[0]: row[1] for row in cur.fetchall()}


# ─── Backoff-retrying fetch ───────────────────────────────────────────────────
def get_with_backoff(context, endpoint, params, max_attempts=6, base_delay=1.0, label=""):
    """Exponential backoff + jitter on 429/5xx and network errors. Any other
    non-200 (400/401/403/404...) is not retryable and fails immediately.
    Raises FetchFailure on exhaustion — callers must not treat that as
    'no data', it means 'unknown data, this run is not trustworthy'."""
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = context.request.get(endpoint, params={**params, "format": "json"})
        except Exception as exc:
            last_exc = exc
            delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, base_delay)
            log.warning("  %s -> request exception (attempt %d/%d): %s — backing off %.1fs",
                        label, attempt, max_attempts, last_exc, delay)
            time.sleep(delay)
            continue

        if resp.status == 200:
            return parse_json_decimal(resp)

        if resp.status == 429 or resp.status >= 500:
            retry_after = resp.headers.get("retry-after")
            if retry_after:
                try:
                    delay = float(retry_after)
                except ValueError:
                    delay = base_delay * (2 ** (attempt - 1))
            else:
                delay = base_delay * (2 ** (attempt - 1))
            delay += random.uniform(0, delay * 0.25)
            log.warning("  %s -> HTTP %d (attempt %d/%d) — backing off %.1fs",
                        label, resp.status, attempt, max_attempts, delay)
            time.sleep(delay)
            continue

        # Not retryable: bad filter, auth failure, etc. Fail now, fail loud.
        raise FetchFailure(f"{label}: non-retryable HTTP {resp.status}: {resp.text()[:300]}")

    raise FetchFailure(f"{label}: exhausted {max_attempts} attempts — aborting rather than "
                        f"returning a partial result")


def fetch_all_pages_strict(context, endpoint, params, label="records"):
    """Like pipeline.fetch_all_pages, but raises on any failure instead of
    logging and breaking — a partial page count here must never look like a
    complete pull to the caller."""
    all_records = []
    offset = 0
    expected_total = None
    while True:
        paged = {**params, "limit": 1000, "offset": offset}
        data = get_with_backoff(context, endpoint, paged, label=f"{label} offset={offset}")
        records = data.get("objects", [])
        expected_total = data.get("meta", {}).get("total_count", 0)
        if not records:
            break
        all_records.extend(records)
        offset += 1000
        if len(all_records) >= expected_total:
            break
        time.sleep(0.3)

    if expected_total is not None and len(all_records) != expected_total:
        raise FetchFailure(
            f"{label}: fetched {len(all_records)} records but Revel reported "
            f"total_count={expected_total} — aborting instead of writing a silently "
            f"partial result")
    return all_records, (expected_total or 0)


def fetch_batched_by_order_id(context, endpoint, order_ids, label="records"):
    """order__in batching in groups of BATCH_SIZE — the only reliable way to
    scope OrderItem/Payment to a set of orders (establishment= is broken on
    OrderItem; not assumed safe on Payment either, so order__in is used
    everywhere instead)."""
    all_records = []
    for i in range(0, len(order_ids), BATCH_SIZE):
        batch = order_ids[i:i + BATCH_SIZE]
        ids_str = ",".join(map(str, batch))
        batch_num = i // BATCH_SIZE + 1
        records, _ = fetch_all_pages_strict(
            context, endpoint, {"order__in": ids_str},
            label=f"{label} batch {batch_num}")
        all_records.extend(records)
        time.sleep(0.2)
    return all_records


# ─── Session ──────────────────────────────────────────────────────────────────
def get_context(browser):
    if os.path.exists(STATE_FILE):
        context = browser.new_context(storage_state=STATE_FILE)
    else:
        context = browser.new_context()
    page = context.new_page()
    if not os.path.exists(STATE_FILE):
        pl.login_and_save(context, page)
    else:
        page.goto(BASE_URL)
        if "login" in page.url or "authentication" in page.url:
            log.info("Session expired — re-logging in")
            pl.login_and_save(context, page)
        else:
            log.info("Session valid")
    page.close()
    return context


# ─── Row builders ─────────────────────────────────────────────────────────────
def build_order_record(o, est_id, est_name):
    created_raw = o.get("created_date")
    return {
        "id": o.get("id"),
        "uuid": o.get("uuid"),
        "local_id": o.get("local_id"),
        "establishment_id": est_id,
        "establishment_name": est_name,
        "created_date_raw": created_raw,                        # exact string, untouched
        "created_date_utc": pl.parse_dt(created_raw),           # via pipeline.parse_dt
        "dining_option_code": o.get("dining_option"),
        "dining_option_name": DINING_CHANNELS.get(o.get("dining_option")),
        "subtotal": to_decimal(o.get("subtotal")),
        "discount_total_amount": to_decimal(o.get("discount_total_amount")),
        "tax": to_decimal(o.get("tax")),
        "final_total": to_decimal(o.get("final_total")),
        "gratuity": to_decimal(o.get("gratuity")),
        "number_of_people": o.get("number_of_people"),
        "closed": bool(o.get("closed", False)),
        "deleted": bool(o.get("deleted", False)),
        "is_unpaid": bool(o.get("is_unpaid", False)),
        "is_discounted": bool(o.get("is_discounted", False)),
        "web_order": bool(o.get("web_order", False)),
        "pos_mode": o.get("pos_mode"),
        "pickup_time_utc": pl.parse_dt(o.get("pickup_time")),
        "customer_id": pl.extract_id(o.get("customer")),
        "updated_date_utc": pl.parse_dt(o.get("updated_date")),
        "created_by_user_id": pl.extract_id(o.get("created_by")),
        "updated_by_user_id": pl.extract_id(o.get("updated_by")),
        # Order-level void and a closed *datetime* do not exist in this Revel
        # configuration (verified — see report). Not included as always-null
        # columns, which would be indistinguishable from "we looked and found
        # nothing" vs. "we didn't check."
        "discount_reason": o.get("discount_reason") or None,    # raw text; comps live here, unclassified
        "payments": [],   # filled in by attach_payments() — 1:N, kept as a nested list, not collapsed
    }


def attach_payments(order_records_by_id, payments):
    for p in payments:
        oid = pl.extract_id(p.get("order"))
        if oid not in order_records_by_id:
            continue
        order_records_by_id[oid]["payments"].append({
            "payment_id": p.get("id"),
            "payment_type": p.get("payment_type"),
            "other_payment_type": p.get("other_payment_type"),
            "refunded": bool(p.get("refunded", False)),
            "refund_transaction_id": p.get("refund_transaction_id"),
            "amount": to_decimal(p.get("amount")),
            "transaction_status": p.get("transaction_status"),
        })


def build_item_record(item, est_id, product_classes):
    created_raw = item.get("created_date")
    start_utc = pl.parse_dt(item.get("start_time"))
    kitchen_utc = pl.parse_dt(item.get("kitchen_completed"))
    kitchen_seconds = (int((kitchen_utc - start_utc).total_seconds())
                        if start_utc and kitchen_utc else None)
    product_id = pl.extract_id(item.get("product"))
    return {
        "order_item_id": item.get("id"),
        "uuid": item.get("uuid"),
        "order_id": pl.extract_id(item.get("order")),
        "establishment_id": est_id,
        "product_id": product_id,
        "product_name": item.get("product_name_override") or item.get("name"),
        "product_class": product_classes.get(product_id),
        "quantity": to_decimal(item.get("quantity"), scale=SCALE3),
        "price": to_decimal(item.get("price")),
        "pure_sales": to_decimal(item.get("pure_sales")),
        "tax_amount": to_decimal(item.get("tax_amount")),
        "modifier_amount": to_decimal(item.get("modifier_amount")),
        "is_voided": bool(item.get("is_voided", False)),
        "voided_date_utc": pl.parse_dt(item.get("voided_date")),
        "voided_reason": item.get("voided_reason") or None,
        "voided_by_user_id": pl.extract_id(item.get("voided_by_user")),
        "combo_product_id": pl.extract_id(item.get("combo_product")),
        "combo_uuid": item.get("combo_uuid"),
        "created_date_raw": created_raw,
        "created_date_utc": pl.parse_dt(created_raw),
        "start_time_utc": start_utc,
        "kitchen_completed_utc": kitchen_utc,
        "kitchen_seconds": kitchen_seconds,   # EXTRACT(EPOCH FROM kitchen_completed-start_time), same as DB
        "deleted": bool(item.get("deleted", False)),
        "deleted_date_utc": pl.parse_dt(item.get("deleted_date")),
    }


def build_modifier_records(item, order_id, est_id, modifier_cache):
    out = []
    for mod in item.get("modifieritems", []):
        if not mod.get("id"):
            continue
        mid = pl.extract_id(mod.get("modifier"))
        out.append({
            "modifier_row_id": mod["id"],
            "order_item_id": item.get("id"),
            "order_id": order_id,
            "establishment_id": est_id,
            "modifier_id": mid,
            "modifier_name": modifier_cache.get(mid),
            "qty": to_decimal(mod.get("qty") or mod.get("quantity"), scale=SCALE3),
            "modifier_price": to_decimal(mod.get("modifier_price")),
        })
    return out


# ─── Parquet schemas ──────────────────────────────────────────────────────────
PAYMENT_STRUCT = pa.struct([
    ("payment_id", pa.int64()),
    ("payment_type", pa.int32()),
    ("other_payment_type", pa.string()),
    ("refunded", pa.bool_()),
    ("refund_transaction_id", pa.string()),
    ("amount", pa.decimal128(12, 6)),
    ("transaction_status", pa.string()),
])

ORDER_SCHEMA = pa.schema([
    ("id", pa.int64()), ("uuid", pa.string()), ("local_id", pa.string()),
    ("establishment_id", pa.int32()), ("establishment_name", pa.string()),
    ("created_date_raw", pa.string()), ("created_date_utc", pa.timestamp("us", tz="UTC")),
    ("dining_option_code", pa.int32()), ("dining_option_name", pa.string()),
    ("subtotal", pa.decimal128(12, 6)), ("discount_total_amount", pa.decimal128(12, 6)),
    ("tax", pa.decimal128(12, 6)), ("final_total", pa.decimal128(12, 6)),
    ("gratuity", pa.decimal128(12, 6)), ("number_of_people", pa.int32()),
    ("closed", pa.bool_()), ("deleted", pa.bool_()), ("is_unpaid", pa.bool_()),
    ("is_discounted", pa.bool_()), ("web_order", pa.bool_()), ("pos_mode", pa.string()),
    ("pickup_time_utc", pa.timestamp("us", tz="UTC")), ("customer_id", pa.int64()),
    ("updated_date_utc", pa.timestamp("us", tz="UTC")),
    ("created_by_user_id", pa.int64()), ("updated_by_user_id", pa.int64()),
    ("discount_reason", pa.string()),
    ("payments", pa.list_(PAYMENT_STRUCT)),
])

ITEM_SCHEMA = pa.schema([
    ("order_item_id", pa.int64()), ("uuid", pa.string()), ("order_id", pa.int64()),
    ("establishment_id", pa.int32()), ("product_id", pa.int64()),
    ("product_name", pa.string()), ("product_class", pa.string()),
    ("quantity", pa.decimal128(10, 3)), ("price", pa.decimal128(12, 6)),
    ("pure_sales", pa.decimal128(12, 6)), ("tax_amount", pa.decimal128(12, 6)),
    ("modifier_amount", pa.decimal128(12, 6)),
    ("is_voided", pa.bool_()), ("voided_date_utc", pa.timestamp("us", tz="UTC")),
    ("voided_reason", pa.string()), ("voided_by_user_id", pa.int64()),
    ("combo_product_id", pa.int64()), ("combo_uuid", pa.string()),
    ("created_date_raw", pa.string()), ("created_date_utc", pa.timestamp("us", tz="UTC")),
    ("start_time_utc", pa.timestamp("us", tz="UTC")),
    ("kitchen_completed_utc", pa.timestamp("us", tz="UTC")),
    ("kitchen_seconds", pa.int32()),
    ("deleted", pa.bool_()), ("deleted_date_utc", pa.timestamp("us", tz="UTC")),
])

MODIFIER_SCHEMA = pa.schema([
    ("modifier_row_id", pa.int64()), ("order_item_id", pa.int64()), ("order_id", pa.int64()),
    ("establishment_id", pa.int32()), ("modifier_id", pa.int64()), ("modifier_name", pa.string()),
    ("qty", pa.decimal128(10, 3)), ("modifier_price", pa.decimal128(12, 6)),
])


def write_parquet(records, schema, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = {f.name: [r.get(f.name) for r in records] for f in schema}
    table = pa.table(cols, schema=schema)
    pq.write_table(table, path, compression="zstd")
    return len(records)


# ─── Day/month iteration ──────────────────────────────────────────────────────
def month_range(start: date, end: date):
    cur = date(start.year, start.month, 1)
    while cur <= end:
        nxt = date(cur.year + (cur.month == 12), (cur.month % 12) + 1, 1)
        # Clamp both ends to the requested range — the first iteration must
        # start at `start`, not day 1 of its month, or a mid-month --start
        # silently pulls extra days that were never asked for.
        yield max(cur, start), min(nxt - timedelta(days=1), end)
        cur = nxt


def day_range(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


# ─── Per-store-month pull ─────────────────────────────────────────────────────
def pull_store_month(context, product_classes, modifier_cache, est_id, month_start, month_end,
                      out_dir: Path, recon_rows: list):
    est_name = ESTABLISHMENTS.get(est_id, f"Establishment {est_id}")
    log.info("=== %s (%d) — %s to %s ===", est_name, est_id, month_start, month_end)

    order_records_by_id = {}
    all_order_ids_in_order = []

    for day in day_range(month_start, month_end):
        gte = f"{day.isoformat()}T00:00:00"
        lte = f"{day.isoformat()}T23:59:59"
        day_orders, day_total = fetch_all_pages_strict(
            context, f"{BASE_URL}/resources/Order/",
            {"establishment": est_id, "created_date__gte": gte, "created_date__lte": lte},
            label=f"{est_name} orders {day}")

        for o in day_orders:
            if not o.get("id"):
                continue
            order_records_by_id[o["id"]] = build_order_record(o, est_id, est_name)
            all_order_ids_in_order.append(o["id"])

        recon_rows.append({
            "establishment_id": est_id, "establishment_name": est_name, "date": day.isoformat(),
            "orders_fetched": len(day_orders), "orders_total_count": day_total,
            "orders_match": len(day_orders) == day_total,
            # items/orders_with_items filled in below once items are fetched
            "items_fetched": None, "orders_with_items_pct": None,
        })
        log.info("  %s: %d/%d orders", day, len(day_orders), day_total)

    n_orders = len(all_order_ids_in_order)
    log.info("  total orders for month: %d", n_orders)

    # Payments — every order in File A gets its payments, regardless of establishment
    if all_order_ids_in_order:
        payments = fetch_batched_by_order_id(
            context, f"{BASE_URL}/resources/Payment/", all_order_ids_in_order,
            label=f"{est_name} payments")
        attach_payments(order_records_by_id, payments)
        log.info("  fetched %d payment records", len(payments))

    # Items + modifiers — Nederland/Beaumont only
    item_records, modifier_records = [], []
    items_by_day = defaultdict(int)
    orders_with_items_by_day = defaultdict(set)

    if est_id in ITEM_LEVEL_ESTABLISHMENTS and all_order_ids_in_order:
        items = fetch_batched_by_order_id(
            context, f"{BASE_URL}/resources/OrderItem/", all_order_ids_in_order,
            label=f"{est_name} items")
        log.info("  fetched %d order items", len(items))
        for item in items:
            oid = pl.extract_id(item.get("order"))
            order = order_records_by_id.get(oid)
            day_key = (order["created_date_utc"].date().isoformat()
                       if order and order["created_date_utc"] else None)
            if day_key:
                items_by_day[day_key] += 1
                orders_with_items_by_day[day_key].add(oid)

            item_records.append(build_item_record(item, est_id, product_classes))
            modifier_records.extend(
                build_modifier_records(item, oid, est_id, modifier_cache))

        orders_by_day = defaultdict(int)
        for oid, o in order_records_by_id.items():
            if o["created_date_utc"]:
                orders_by_day[o["created_date_utc"].date().isoformat()] += 1

        for row in recon_rows:
            if row["establishment_id"] != est_id:
                continue
            day_key = row["date"]
            row["items_fetched"] = items_by_day.get(day_key, 0)
            n_day_orders = orders_by_day.get(day_key, 0)
            n_with_items = len(orders_with_items_by_day.get(day_key, set()))
            row["orders_with_items_pct"] = (round(n_with_items / n_day_orders * 100, 1)
                                             if n_day_orders else None)

    # ── Write Parquet: one file per store per month ─────────────────────────
    tag = f"{est_id}_{month_start:%Y-%m}"
    n_written_orders = write_parquet(
        list(order_records_by_id.values()), ORDER_SCHEMA, out_dir / "orders" / f"orders_{tag}.parquet")
    log.info("  wrote %d order rows -> orders/orders_%s.parquet", n_written_orders, tag)

    if est_id in ITEM_LEVEL_ESTABLISHMENTS:
        n_written_items = write_parquet(
            item_records, ITEM_SCHEMA, out_dir / "items" / f"items_{tag}.parquet")
        n_written_mods = write_parquet(
            modifier_records, MODIFIER_SCHEMA, out_dir / "modifiers" / f"modifiers_{tag}.parquet")
        log.info("  wrote %d item rows, %d modifier rows -> items_%s.parquet / modifiers_%s.parquet",
                  n_written_items, n_written_mods, tag, tag)

    # ── Join integrity check ─────────────────────────────────────────────────
    if est_id in ITEM_LEVEL_ESTABLISHMENTS:
        order_ids_set = set(order_records_by_id.keys())
        item_ids_set = {r["order_item_id"] for r in item_records}
        orphan_items = [r["order_id"] for r in item_records if r["order_id"] not in order_ids_set]
        orphan_mods = [r["order_item_id"] for r in modifier_records if r["order_item_id"] not in item_ids_set]
        if orphan_items:
            raise FetchFailure(f"{est_name} {month_start:%Y-%m}: {len(orphan_items)} order_items "
                                f"reference an order_id not present in File A — join is broken")
        if orphan_mods:
            raise FetchFailure(f"{est_name} {month_start:%Y-%m}: {len(orphan_mods)} modifier rows "
                                f"reference an order_item_id not present in File B — join is broken")
        log.info("  join integrity OK: every order_id in File B exists in File A, "
                  "every order_item_id in File C exists in File B")

    return {
        "orders": n_written_orders,
        "items": len(item_records) if est_id in ITEM_LEVEL_ESTABLISHMENTS else None,
        "modifiers": len(modifier_records) if est_id in ITEM_LEVEL_ESTABLISHMENTS else None,
    }


# ─── Entry point ──────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Raw Revel export for owner-driven analysis")
    parser.add_argument("--establishments", type=str, required=True,
                         help="Comma-separated establishment IDs, e.g. 26 or 26,14")
    parser.add_argument("--start", type=str, required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", type=str, required=True, help="YYYY-MM-DD")
    parser.add_argument("--out", type=str, default="exports", help="Output directory")
    args = parser.parse_args()

    if not pl.REVEL_USER or not pl.REVEL_PASS:
        log.error("REVEL_USER and REVEL_PASS must be set (.env)")
        sys.exit(1)

    est_ids = [int(x.strip()) for x in args.establishments.split(",")]
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    out_dir = Path(args.out)

    t_start = time.time()

    conn = db_connect_readonly()
    product_classes = load_product_classes(conn)
    conn.close()

    recon_rows = []
    summary = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = get_context(browser)

        log.info("Fetching modifier reference data (read-only, no DB write)...")
        modifier_records, _ = fetch_all_pages_strict(
            context, f"{BASE_URL}/resources/Modifier/", {}, label="modifiers")
        modifier_cache = {m["id"]: (m.get("name") or f"Modifier #{m['id']}") for m in modifier_records}
        log.info("  %d modifiers cached", len(modifier_cache))

        for est_id in est_ids:
            for month_start, month_end in month_range(start, end):
                try:
                    stats = pull_store_month(
                        context, product_classes, modifier_cache, est_id,
                        month_start, month_end, out_dir, recon_rows)
                    summary[(est_id, month_start)] = {"status": "OK", **stats}
                except FetchFailure as exc:
                    log.error("STORE-MONTH FAILED, NO FILE WRITTEN: %s", exc)
                    summary[(est_id, month_start)] = {"status": "FAILED", "error": str(exc)}

        browser.close()

    # ── Reconciliation log ───────────────────────────────────────────────────
    import csv
    recon_path = out_dir / "reconciliation_log.csv"
    recon_path.parent.mkdir(parents=True, exist_ok=True)
    with open(recon_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["establishment_id", "establishment_name", "date",
                                           "orders_fetched", "orders_total_count", "orders_match",
                                           "items_fetched", "orders_with_items_pct"])
        w.writeheader()
        w.writerows(recon_rows)
    log.info("Reconciliation log -> %s", recon_path)

    elapsed = time.time() - t_start
    log.info("\n=== SUMMARY ===")
    for (est_id, month_start), s in summary.items():
        log.info("  %s %s: %s", ESTABLISHMENTS.get(est_id, est_id), month_start.strftime("%Y-%m"), s)
    log.info("Wall-clock time: %.1fs", elapsed)

    any_failed = any(s["status"] == "FAILED" for s in summary.values())
    if any_failed:
        log.error("One or more store-months FAILED — see above. Exiting non-zero.")
        sys.exit(1)


if __name__ == "__main__":
    main()
