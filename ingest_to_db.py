"""
ingest_to_db.py
===============
Reads orders_with_items_<est>.json files that were downloaded by order_fixed.py
and inserts them into the PostgreSQL database designed in database_design.sql.

Usage:
    python ingest_to_db.py                     # process all JSON files in current dir
    python ingest_to_db.py --file orders_with_items_32.json   # single file
    python ingest_to_db.py --date 2026-05-04   # tag inserts with a specific date

Requirements:
    pip install psycopg2-binary

Environment variables:
    DB_HOST     (default: localhost)
    DB_PORT     (default: 5432)
    DB_NAME     (default: laynes)
    DB_USER     (default: postgres)
    DB_PASS     (required)
"""

import json
import os
import sys
import glob
import argparse
import logging
from datetime import datetime, date
from typing import Optional

import psycopg2
from psycopg2.extras import execute_values

# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

SCRIPT_VERSION = "1.0.0"


# ─── DB Connection ───────────────────────────────────────────────────────────
def get_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 5432)),
        dbname=os.getenv("DB_NAME", "laynes"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASS"),
    )


# ─── Helpers ─────────────────────────────────────────────────────────────────
def extract_id(uri_or_int) -> Optional[int]:
    """
    Extracts integer ID from Revel resource URI or plain int.
    Handles:
      - 12345
      - "/resources/Order/12345/"
      - "/enterprise/User/12345/"
    Returns None if input is None or unparseable.
    """
    if uri_or_int is None:
        return None
    if isinstance(uri_or_int, int):
        return uri_or_int
    if isinstance(uri_or_int, str) and uri_or_int.strip():
        try:
            return int(uri_or_int.strip("/").split("/")[-1])
        except (ValueError, IndexError):
            return None
    return None


def parse_dt(s) -> Optional[datetime]:
    """
    Parse Revel datetime strings. Handles:
      - "2026-05-04T15:32:11"
      - "2026-05-04T15:32:11+00:00"
      - None / ""
    Returns a timezone-aware UTC datetime or None.
    """
    if not s:
        return None
    # Revel sends UTC timestamps without Z suffix sometimes
    for fmt in (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            # Make timezone-aware if not already (Revel times are UTC)
            if dt.tzinfo is None:
                from datetime import timezone
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    log.warning("Could not parse datetime: %s", s)
    return None


def parse_decimal(val, default=0):
    """Safely parse decimal/float from Revel response."""
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


# ─── Order Parser ────────────────────────────────────────────────────────────
def parse_order(order: dict, establishment_id: int, ingestion_date: date) -> dict:
    """Map Revel Order JSON → orders table row dict."""
    return {
        "id":                   order["id"],
        "uuid":                 order.get("uuid"),
        "establishment_id":     establishment_id,
        "local_id":             order.get("local_id"),
        "created_date":         parse_dt(order.get("created_date")),
        "updated_date":         parse_dt(order.get("updated_date")),
        "pickup_time":          parse_dt(order.get("pickup_time")),
        "dining_option":        order.get("dining_option"),
        "pos_mode":             order.get("pos_mode"),
        "final_total":          parse_decimal(order.get("final_total")),
        "subtotal":             parse_decimal(order.get("subtotal")),
        "tax":                  parse_decimal(order.get("tax")),
        "gratuity":             parse_decimal(order.get("gratuity")),
        "discount_total_amount":parse_decimal(order.get("discount_total_amount")),
        "closed":               bool(order.get("closed", False)),
        "is_unpaid":            bool(order.get("is_unpaid", False)),
        "deleted":              bool(order.get("deleted", False)),
        "is_discounted":        bool(order.get("is_discounted", False)),
        "web_order":            bool(order.get("web_order", False)),
        "customer_id":          extract_id(order.get("customer")),
        "number_of_people":     order.get("number_of_people") or 0,
        "ingestion_date":       ingestion_date,
    }


ORDER_COLUMNS = [
    "id", "uuid", "establishment_id", "local_id",
    "created_date", "updated_date", "pickup_time",
    "dining_option", "pos_mode",
    "final_total", "subtotal", "tax", "gratuity", "discount_total_amount",
    "closed", "is_unpaid", "deleted", "is_discounted", "web_order",
    "customer_id", "number_of_people",
    "ingestion_date",
]


# ─── Order Item Parser ────────────────────────────────────────────────────────
def parse_order_item(item: dict, establishment_id: int, ingestion_date: date) -> dict:
    """Map Revel OrderItem JSON → order_items table row dict."""
    return {
        "id":               item["id"],
        "uuid":             item.get("uuid"),
        "order_id":         extract_id(item.get("order")),
        "establishment_id": establishment_id,
        "product_id":       extract_id(item.get("product")),
        "product_name":     item.get("product_name_override") or item.get("name"),
        "quantity":         parse_decimal(item.get("quantity"), 1),
        "dining_option":    item.get("dining_option"),
        "combo_product_id": extract_id(item.get("combo_product")),
        "combo_uuid":       item.get("combo_uuid"),
        "price":            parse_decimal(item.get("price")),
        "pure_sales":       parse_decimal(item.get("pure_sales")),
        "tax_amount":       parse_decimal(item.get("tax_amount")),
        "modifier_amount":  parse_decimal(item.get("modifier_amount")),
        "is_discounted":    bool(item.get("is_discounted", False)),
        "created_date":     parse_dt(item.get("created_date")),
        "start_time":       parse_dt(item.get("start_time")),
        "kitchen_completed":parse_dt(item.get("kitchen_completed")),
        # kitchen_seconds is GENERATED ALWAYS — do NOT insert it
        "is_voided":        bool(item.get("is_voided", False)),
        "voided_date":      parse_dt(item.get("voided_date")),
        "voided_by_user_id":extract_id(item.get("voided_by_user")),
        "voided_reason":    item.get("voided_reason"),
        "deleted":          bool(item.get("deleted", False)),
        "deleted_date":     parse_dt(item.get("deleted_date")),
        "ingestion_date":   ingestion_date,
    }


ORDER_ITEM_COLUMNS = [
    "id", "uuid", "order_id", "establishment_id",
    "product_id", "product_name", "quantity", "dining_option",
    "combo_product_id", "combo_uuid",
    "price", "pure_sales", "tax_amount", "modifier_amount", "is_discounted",
    "created_date", "start_time", "kitchen_completed",
    # kitchen_seconds excluded — it's GENERATED ALWAYS AS
    "is_voided", "voided_date", "voided_by_user_id", "voided_reason",
    "deleted", "deleted_date",
    "ingestion_date",
]


# ─── Modifier Item Parser ─────────────────────────────────────────────────────
def parse_modifier(mod: dict, order_item_id: int, order_id: int,
                   establishment_id: int, item_date: date) -> dict:
    """Map modifier sub-object → modifier_items table row dict."""
    return {
        "id":               mod["id"],
        "order_item_id":    order_item_id,
        "order_id":         order_id,
        "establishment_id": establishment_id,
        "modifier_id":      extract_id(mod.get("modifier")),
        "modifier_name":    mod.get("name") or mod.get("modifier_name"),
        "qty":              parse_decimal(mod.get("qty") or mod.get("quantity"), 1),
        "modifier_price":   parse_decimal(mod.get("price")),
        "is_discounted":    bool(mod.get("is_discounted", False)),
        "created_date":     item_date,
    }


MODIFIER_COLUMNS = [
    "id", "order_item_id", "order_id", "establishment_id",
    "modifier_id", "modifier_name", "qty", "modifier_price",
    "is_discounted", "created_date",
]


# ─── Bulk Insert Helpers ──────────────────────────────────────────────────────
def rows_to_tuples(rows: list[dict], columns: list[str]) -> list[tuple]:
    return [tuple(r[col] for col in columns) for r in rows]


def bulk_insert(cur, table: str, columns: list[str], rows: list[dict]) -> int:
    """
    Bulk-insert rows using execute_values for speed.
    ON CONFLICT DO NOTHING for idempotent daily runs.
    Returns number of rows actually inserted.
    """
    if not rows:
        return 0

    col_str = ", ".join(columns)
    sql = f"INSERT INTO {table} ({col_str}) VALUES %s ON CONFLICT DO NOTHING"

    tuples = rows_to_tuples(rows, columns)

    # execute_values sends in one round-trip (much faster than executemany)
    execute_values(cur, sql, tuples, page_size=500)

    # rowcount is -1 after execute_values in psycopg2 — use a workaround
    # We'll return len(rows) as an upper bound; duplicates are silently skipped
    return len(rows)


# ─── Process One JSON File ───────────────────────────────────────────────────
def process_file(filepath: str, conn, ingestion_date: date) -> dict:
    """
    Parse one orders_with_items_<est>.json file and insert into DB.
    Returns stats dict.
    """
    filename = os.path.basename(filepath)
    log.info("Processing %s ...", filename)

    # Extract establishment ID from filename: orders_with_items_32.json → 32
    try:
        est_id = int(filename.replace("orders_with_items_", "").replace(".json", ""))
    except ValueError:
        log.error("Cannot extract establishment ID from filename: %s", filename)
        return {}

    with open(filepath, "r") as f:
        data = json.load(f)

    if not isinstance(data, list):
        log.error("Expected list in JSON, got %s", type(data))
        return {}

    log.info("  Establishment %d — %d order records", est_id, len(data))

    # ── Log start ──────────────────────────────────────────────
    started_at = datetime.utcnow()
    cur = conn.cursor()
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

    # ── Parse ──────────────────────────────────────────────────
    order_rows = []
    item_rows = []
    modifier_rows = []

    orders_fetched = 0
    items_fetched = 0

    for record in data:
        order = record.get("order", {})
        items = record.get("items", [])

        if not order or not order.get("id"):
            continue

        # Determine the date of this order (used for partition routing)
        created_dt = parse_dt(order.get("created_date"))
        if created_dt is None:
            log.warning("Order %s has no created_date, skipping", order.get("id"))
            continue

        order_row = parse_order(order, est_id, ingestion_date)
        order_rows.append(order_row)
        orders_fetched += 1

        for item in items:
            if not item.get("id"):
                continue

            item_row = parse_order_item(item, est_id, ingestion_date)

            # If item has no created_date, inherit from order
            if item_row["created_date"] is None:
                item_row["created_date"] = created_dt

            item_rows.append(item_row)
            items_fetched += 1

            # Parse modifiers (they're usually inside item["modifiers"])
            for mod in item.get("modifiers", []):
                if not mod.get("id"):
                    continue
                item_date = (item_row["created_date"].date()
                             if item_row["created_date"] else ingestion_date)
                mod_row = parse_modifier(
                    mod,
                    order_item_id=item["id"],
                    order_id=extract_id(item.get("order")),
                    establishment_id=est_id,
                    item_date=item_date,
                )
                modifier_rows.append(mod_row)

    log.info(
        "  Parsed: %d orders, %d items, %d modifiers",
        len(order_rows), len(item_rows), len(modifier_rows),
    )

    # ── Insert ─────────────────────────────────────────────────
    try:
        orders_inserted = bulk_insert(cur, "orders", ORDER_COLUMNS, order_rows)
        items_inserted = bulk_insert(cur, "order_items", ORDER_ITEM_COLUMNS, item_rows)
        bulk_insert(cur, "modifier_items", MODIFIER_COLUMNS, modifier_rows)

        conn.commit()

        # Update ingestion log — success
        cur.execute(
            """
            UPDATE ingestion_log SET
                completed_at    = %s,
                status          = 'success',
                orders_fetched  = %s,
                items_fetched   = %s,
                orders_inserted = %s,
                items_inserted  = %s,
                orders_skipped  = %s
            WHERE id = %s
            """,
            (
                datetime.utcnow(),
                orders_fetched,
                items_fetched,
                orders_inserted,
                items_inserted,
                orders_fetched - orders_inserted,  # upper bound of duplicates
                log_id,
            ),
        )
        conn.commit()

        log.info(
            "  Inserted: %d orders, %d items into DB",
            orders_inserted, items_inserted,
        )

        return {
            "establishment_id": est_id,
            "orders_fetched":   orders_fetched,
            "items_fetched":    items_fetched,
            "orders_inserted":  orders_inserted,
            "items_inserted":   items_inserted,
        }

    except Exception as exc:
        conn.rollback()
        log.error("  DB error for establishment %d: %s", est_id, exc)

        # Update ingestion log — failed
        cur.execute(
            """
            UPDATE ingestion_log SET
                completed_at  = %s,
                status        = 'failed',
                error_message = %s
            WHERE id = %s
            """,
            (datetime.utcnow(), str(exc)[:2000], log_id),
        )
        conn.commit()
        raise


# ─── Main ────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Ingest Revel JSON files into PostgreSQL")
    parser.add_argument(
        "--file", type=str, default=None,
        help="Path to a specific orders_with_items_<est>.json file"
    )
    parser.add_argument(
        "--date", type=str, default=None,
        help="Ingestion date override YYYY-MM-DD (default: today)"
    )
    parser.add_argument(
        "--dir", type=str, default=".",
        help="Directory to scan for orders_with_items_*.json files (default: current dir)"
    )
    args = parser.parse_args()

    # Resolve ingestion date
    if args.date:
        ingestion_date = date.fromisoformat(args.date)
    else:
        ingestion_date = date.today()

    log.info("Ingestion date: %s", ingestion_date)

    # Find files to process
    if args.file:
        files = [args.file]
    else:
        pattern = os.path.join(args.dir, "orders_with_items_*.json")
        files = sorted(glob.glob(pattern))

    if not files:
        log.warning("No JSON files found to process.")
        sys.exit(0)

    log.info("Found %d file(s) to process", len(files))

    if not os.getenv("DB_PASS"):
        log.error("DB_PASS environment variable not set")
        sys.exit(1)

    # Connect once, reuse for all files
    conn = get_connection()
    log.info("Connected to database '%s' on %s", os.getenv("DB_NAME", "laynes"), os.getenv("DB_HOST", "localhost"))

    total_orders = 0
    total_items = 0
    errors = 0

    for filepath in files:
        try:
            stats = process_file(filepath, conn, ingestion_date)
            if stats:
                total_orders += stats.get("orders_inserted", 0)
                total_items  += stats.get("items_inserted", 0)
        except Exception as exc:
            log.error("Failed to process %s: %s", filepath, exc)
            errors += 1

    conn.close()

    log.info("")
    log.info("=" * 50)
    log.info("DONE — %d file(s) processed", len(files))
    log.info("Total orders inserted: %d", total_orders)
    log.info("Total items inserted:  %d", total_items)
    if errors:
        log.warning("Errors on %d file(s)", errors)
    log.info("=" * 50)


if __name__ == "__main__":
    main()
