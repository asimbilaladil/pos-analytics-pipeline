"""
build_export.py — Nederland (est 26) June 2026 raw-data export for Masroor.

EXPORT/INVESTIGATION ONLY. Reads exclusively from:
  (a) preserved raw Revel archives under /var/lib/laynes/raw_revel/
      (Order, OrderItem, Payment — the exact archive run_ids for est 26's
      2026-06-01..2026-07-01 window, located via backfill_progress)
  (b) a READ-ONLY Postgres connection (conn.set_session(readonly=True)) —
      used only for establishment name and product/category name resolution,
      per the "V2 for completeness/reference only" instruction.

Makes NO live Revel API calls, writes NOTHING to Postgres, does not touch
sync_state/cron/.env, and does not deduplicate or filter out any raw source
row. Money fields are parsed straight from JSON wire text with
parse_float=Decimal so no cent is ever routed through a Python float.

Raw timestamp strings from Revel are naive (no tz suffix) and represent
America/Chicago local time (proven in README.md / field_mapping.md, not
assumed here) — so this script attaches America/Chicago tzinfo directly to
the parsed naive datetime, and separately keeps the exact original string
untouched in a `*_revel_raw` column.
"""

import csv
import gzip
import json
import os
import re
import time
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg2
import pyarrow as pa
import pyarrow.parquet as pq

CHICAGO = ZoneInfo("America/Chicago")
OUT_DIR = Path(__file__).parent
RAW_ROOT = Path("/var/lib/laynes/raw_revel")

EST_ID = 26
JUNE_START_CHI = datetime(2026, 6, 1, 0, 0, 0, tzinfo=CHICAGO)
JULY_START_CHI = datetime(2026, 7, 1, 0, 0, 0, tzinfo=CHICAGO)

# Canonical archive locations for this window — located via backfill_progress
# (see README.md item 3 / investigation notes) rather than assumed.
ARCHIVE_RUNS = {
    "orders": {
        "run_id": "20260811T062636Z",
        "dir": RAW_ROOT / "orders/2026/08/establishment_26/run_20260811T062636Z",
    },
    "order_items": {
        "run_id": "20260811T195437Z",
        "dir": RAW_ROOT / "order_items/2026/08/establishment_26/run_20260811T195437Z/window_20260601_20260701",
    },
    "payments": {
        "run_id": "20260812T153723Z",
        "dir": RAW_ROOT / "payments/2026/08/establishment_26/run_20260812T153723Z/window_20260601_20260701",
    },
}

# Product / ProductCategory reference archives — account-wide (not
# establishment- or June-scoped; Revel has no historical category-assignment
# tracking, so this is the most recent available snapshot, used as a
# best-available proxy for June's categorization). Verified complete
# (summed page records == meta.total_count) before use — see
# investigation notes for category_id_v2_reference / category_name_v2_reference.
CATEGORY_REFERENCE_RUNS = {
    "products": RAW_ROOT / "products/2026/08/run_20260814T090001Z",
    "product_categories_active": RAW_ROOT / "product_categories/2026/08/run_20260814T090001Z_active",
    "product_categories_inactive": RAW_ROOT / "product_categories/2026/08/run_20260814T090001Z_inactive",
}

SCALE6 = Decimal("0.000001")
SCALE3 = Decimal("0.001")

DINING_CHANNELS = {
    0: "To Go", 1: "Eat In", 2: "Delivery", 3: "Catering", 4: "Drive Through",
    5: "Online Ordering", 6: "Spirit Night", 7: "Shipping", 8: "Pickup",
    9: "DoorDash Drive", 100: "DD Marketplace", 101: "Uber Eats",
    102: "Eat In Fun", 103: "To Go Fun", 104: "Drive Thru Fun",
    105: "Lane A", 106: "Lane B",
}

_URI_ID_RE = re.compile(r"/(\d+)/?$")


def extract_id(uri):
    if not uri:
        return None
    m = _URI_ID_RE.search(uri)
    return int(m.group(1)) if m else None


def to_decimal(val, scale=SCALE6):
    if val is None:
        return None
    if isinstance(val, Decimal):
        d = val
    elif isinstance(val, str):
        v = val.strip()
        if not v:
            return None
        try:
            d = Decimal(v)
        except InvalidOperation:
            return None
    elif isinstance(val, (int, float)):
        d = Decimal(str(val))
    else:
        return None
    return d.quantize(scale)


def parse_chicago(raw):
    """Revel naive strings ('YYYY-MM-DDTHH:MM:SS[.ffffff]') represent
    America/Chicago local time — see README.md item 10. Attach tzinfo
    directly; do not treat as UTC."""
    if not raw:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.replace(tzinfo=CHICAGO)
        except ValueError:
            continue
    return None


def json_safe(obj):
    """Decimal-safe JSON string for nested raw structures (applied_taxes,
    orderhistory, modifieritems, split_combo_data, ...). str(Decimal)
    preserves the exact value — never routed through float."""
    if obj is None:
        return None
    return json.dumps(obj, default=str)


# ─── Archive page reading ────────────────────────────────────────────────────
_PAGE_RE = re.compile(r"^page_(\d+)_attempt_(\d+)\.json\.gz$")


def read_archive_pages(pages_dir: Path, label: str):
    """Reads every page in a run directory, keeping only the highest-attempt
    file per page number (retries reuse the same page number). Parses with
    parse_float=Decimal so unquoted money fields never touch a float.
    Returns (records, meta_report)."""
    if not pages_dir.is_dir():
        raise FileNotFoundError(f"{label}: archive directory not found: {pages_dir}")

    best_attempt = {}
    for fn in os.listdir(pages_dir):
        m = _PAGE_RE.match(fn)
        if not m:
            continue
        page_num, attempt = int(m.group(1)), int(m.group(2))
        if page_num not in best_attempt or attempt > best_attempt[page_num][0]:
            best_attempt[page_num] = (attempt, fn)

    records = []
    total_count = None
    pages_read = 0
    for page_num in sorted(best_attempt):
        attempt, fn = best_attempt[page_num]
        with gzip.open(pages_dir / fn, "rb") as f:
            text = f.read().decode("utf-8")
        data = json.loads(text, parse_float=Decimal)
        objs = data.get("objects", [])
        records.extend(objs)
        pages_read += 1
        tc = data.get("meta", {}).get("total_count")
        if tc is not None:
            total_count = tc

    return records, {
        "label": label,
        "pages_read": pages_read,
        "records_read": len(records),
        "revel_total_count": total_count,
    }


# ─── Postgres reference joins (read-only) ────────────────────────────────────
def db_connect_readonly():
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent.parent / ".env")
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"), port=int(os.getenv("DB_PORT", 5432)),
        dbname=os.getenv("DB_NAME", "laynes"), user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASS"),
    )
    conn.set_session(readonly=True)
    return conn


def load_establishment_name(conn, est_id):
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM establishments WHERE id=%s", (est_id,))
        row = cur.fetchone()
        return row[0] if row else None


def load_product_reference(conn):
    """product_id -> (name, category_id, category_name).

    Name comes from the read-only `products` table (fully populated, no
    issue there). Category does NOT: `products.category_id` is NULL for
    all 35,453 rows in this database (an ingestion gap, verified directly
    — not specific to Nederland or to this export), so category is instead
    resolved from the raw Product/ProductCategory archives (see
    CATEGORY_REFERENCE_RUNS / build_category_reference()), which do carry a
    `category` URI on every raw Product record. V2/reference use only —
    never overwrites the raw product_name_override kept in File B.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT id, name FROM products")
        names = {r[0]: r[1] for r in cur.fetchall()}

    product_category_id, category_names = build_category_reference()

    out = {}
    for pid, name in names.items():
        cat_id = product_category_id.get(pid)
        out[pid] = {
            "name": name,
            "category_id": cat_id,
            "category_name": category_names.get(cat_id) if cat_id is not None else None,
        }
    return out


def _flatten_categories(objs, out):
    """ProductCategory records nest child categories under `subcategories`;
    flatten recursively so a subcategory's id/name is captured too, not
    just top-level categories."""
    for c in objs:
        out[c["id"]] = c.get("name")
        children = c.get("subcategories") or []
        if children:
            _flatten_categories(children, out)


def build_category_reference():
    """Returns (product_id -> category_id, category_id -> category_name),
    built entirely from already-archived Product/ProductCategory pages —
    no live API call. This is an account-wide, present-day snapshot
    (archived 2026-08-14); Revel does not expose historical
    category-assignment tracking, so there is no way to get a June-2026-
    dated categorization — documented as a caveat in README/field_mapping."""
    product_records, _ = read_archive_pages(CATEGORY_REFERENCE_RUNS["products"], "products")
    product_category_id = {}
    for p in product_records:
        pid = p.get("id")
        cat_uri = p.get("category")
        if pid is not None and cat_uri:
            product_category_id[pid] = extract_id(cat_uri)

    category_names = {}
    active_records, _ = read_archive_pages(
        CATEGORY_REFERENCE_RUNS["product_categories_active"], "product_categories_active")
    inactive_records, _ = read_archive_pages(
        CATEGORY_REFERENCE_RUNS["product_categories_inactive"], "product_categories_inactive")
    _flatten_categories(active_records, category_names)
    _flatten_categories(inactive_records, category_names)

    return product_category_id, category_names


# ─── Order record shaping ────────────────────────────────────────────────────
ORDER_MONEY_FIELDS = [
    "subtotal", "discount_amount", "discount_total_amount", "discount_rule_amount",
    "discount_tax_amount", "discount_tax_amount_included", "tax", "final_total",
    "gratuity", "service_charge", "surcharge", "tax_rebate", "remaining_due",
    "rounding_delta", "service_fee_tax", "service_fee_taxed", "service_fee_untaxed",
    "tax_excluded_amount", "taxable_surcharge", "taxable_surcharge_excluded",
    "prevailing_tax", "prevailing_surcharge",
]

ORDER_JSON_FIELDS = [
    "applied_taxes", "applied_discounts", "orderhistory", "deleted_discounts",
    "billing_address", "delivery_address", "pickup_data", "registry_data",
    "virtual_data", "fleet_service_data", "gift_reward_data", "drive_through_data",
    "bills_info", "vehicle",
]

ORDER_PASSTHROUGH_FIELDS = [
    "local_id", "closed", "deleted", "is_unpaid", "is_discounted", "web_order",
    "pos_mode", "discount_reason", "discount_code", "discount_rule_type",
    "number_of_people", "has_items", "has_history", "has_delivery_info",
    "kitchen_status", "table", "notes", "printed", "sent", "version",
    "reporting_id", "device_id", "bill_number", "check_sum", "asap",
    "is_invoice", "is_readonly", "smart_order", "external_sync",
]


def build_order_row(o, est_name):
    created_raw = o.get("created_date")
    updated_raw = o.get("updated_date")
    row = {
        "order_id": o.get("id"),
        "uuid": o.get("uuid"),
        "establishment_id": EST_ID,
        "establishment_name": est_name,
        "created_date_revel_raw": created_raw,
        "created_at_chicago": parse_chicago(created_raw),
        "updated_date_revel_raw": updated_raw,
        "updated_at_chicago": parse_chicago(updated_raw),
        "dining_option_code": o.get("dining_option"),
        "dining_option_name": DINING_CHANNELS.get(o.get("dining_option")),
        "created_by_uri": o.get("created_by"),
        "created_by_user_id": extract_id(o.get("created_by")),
        "updated_by_uri": o.get("updated_by"),
        "updated_by_user_id": extract_id(o.get("updated_by")),
        "discounted_by_uri": o.get("discounted_by"),
        "discounted_by_user_id": extract_id(o.get("discounted_by")),
        "customer_id": extract_id(o.get("customer")),
    }
    for f in ORDER_MONEY_FIELDS:
        row[f] = to_decimal(o.get(f))
    for f in ORDER_JSON_FIELDS:
        row[f"{f}_json"] = json_safe(o.get(f))
    for f in ORDER_PASSTHROUGH_FIELDS:
        row[f] = o.get(f)
    # payment aggregation fields are filled in by attach_payments()
    row["payment_count"] = 0
    row["payment_type_single"] = None
    row["payment_types_json"] = None
    row["payment_records_json"] = None
    row["any_payment_refunded"] = False
    # filled by attach_items()
    row["has_voided_items"] = False
    row["item_count_in_file_b"] = 0
    # set only if this order_id appears more than once in the raw archive —
    # kept as an explicit column (not a conditionally-present key) so every
    # row has the same schema for Parquet
    row["duplicate_order_id_flag"] = False
    return row


PAYMENT_RECORD_FIELDS = [
    "id", "payment_type", "other_payment_type", "amount", "tip", "gratuity",
    "refunded", "refund_transaction_id", "transaction_status", "transaction_id",
    "card_type", "payment_date", "created_date", "station", "online",
]


def attach_payments(orders_by_id, payments):
    grouped = defaultdict(list)
    for p in payments:
        oid = extract_id(p.get("order"))
        if oid is None:
            continue
        rec = {}
        for f in PAYMENT_RECORD_FIELDS:
            v = p.get(f)
            if f in ("amount", "tip", "gratuity"):
                v = to_decimal(v)
            rec[f] = v
        grouped[oid].append(rec)

    for oid, recs in grouped.items():
        order = orders_by_id.get(oid)
        if order is None:
            continue  # orphan payment — reported separately, order not in File A
        order["payment_count"] = len(recs)
        types = [r["payment_type"] for r in recs]
        order["payment_type_single"] = types[0] if len(recs) == 1 else None
        order["payment_types_json"] = json_safe(types)
        order["payment_records_json"] = json_safe(recs)
        order["any_payment_refunded"] = any(bool(r.get("refunded")) for r in recs)
    return grouped


def attach_items_flags(orders_by_id, items):
    counts = Counter()
    voided = defaultdict(bool)
    for it in items:
        oid = extract_id(it.get("order"))
        if oid is None:
            continue
        counts[oid] += 1
        if it.get("is_voided"):
            voided[oid] = True
    for oid, order in orders_by_id.items():
        order["item_count_in_file_b"] = counts.get(oid, 0)
        order["has_voided_items"] = voided.get(oid, False)


# ─── OrderItem record shaping ────────────────────────────────────────────────
ITEM_MONEY_FIELDS = [
    "price", "pure_sales", "tax_amount", "modifier_amount", "initial_price",
    "cost", "modifier_cost", "commission_amount", "discount_amount",
    "discount_rule_amount", "discount_tax_amount_included", "service_fee_tax",
    "service_fee_taxed", "service_fee_untaxed", "combo_saving_amount",
    "wholesale_saving_amount", "price_to_display",
]

ITEM_JSON_FIELDS = ["applied_taxes", "applied_discounts", "modifieritems",
                     "ingredientitems", "commissions", "reference_discounts"]

ITEM_PASSTHROUGH_FIELDS = [
    "uuid", "deleted", "deleted_date", "is_voided", "voided_date", "voided_by",
    "voided_reason", "void_ref_uuid", "discount_reason", "discount_code",
    "combo_uuid", "combo_type", "combo_used", "parent_combo_uuid", "parent_uuid",
    "dining_option", "seat_number", "course_number", "printed", "sent",
    "on_hold", "special_request", "gift_card_number", "item_type",
    "sold_by_weight", "uom", "created_by", "updated_by", "station",
    "start_time", "kitchen_completed", "order_local_id",
]


def build_item_row(item, product_ref):
    created_raw = item.get("created_date")
    updated_raw = item.get("updated_date")
    order_id = extract_id(item.get("order"))
    product_uri = item.get("product")
    product_id = extract_id(product_uri)
    ref = product_ref.get(product_id, {})
    row = {
        "order_item_id": item.get("id"),
        "order_id": order_id,
        "establishment_id": EST_ID,
        "product_id": product_id,
        "product_name_revel_raw": item.get("product_name_override"),
        "product_name_v2_reference": ref.get("name"),
        "category_id_v2_reference": ref.get("category_id"),
        "category_name_v2_reference": ref.get("category_name"),
        "quantity": to_decimal(item.get("quantity"), scale=SCALE3),
        "unit_price": to_decimal(item.get("price")),
        "line_total": to_decimal(item.get("pure_sales")),
        "is_modifier_row": False,  # File B = OrderItem rows only; modifiers are nested, see File C
        "created_date_revel_raw": created_raw,
        "created_at_chicago": parse_chicago(created_raw),
        "updated_date_revel_raw": updated_raw,
        "updated_at_chicago": parse_chicago(updated_raw),
        "created_by_user_id": extract_id(item.get("created_by")),
        "updated_by_user_id": extract_id(item.get("updated_by")),
        "voided_by_user_id": extract_id(item.get("voided_by")),
        "product_uri": product_uri,
        "modifier_row_count": len(item.get("modifieritems") or []),
    }
    for f in ITEM_MONEY_FIELDS:
        row[f] = to_decimal(item.get(f))
    for f in ITEM_JSON_FIELDS:
        row[f"{f}_json"] = json_safe(item.get(f))
    for f in ITEM_PASSTHROUGH_FIELDS:
        row[f] = item.get(f)
    return row


def build_modifier_rows(item):
    out = []
    order_id = extract_id(item.get("order"))
    for mod in (item.get("modifieritems") or []):
        out.append({
            "modifier_row_id": mod.get("id"),
            "order_item_id": item.get("id"),
            "order_id": order_id,
            "establishment_id": EST_ID,
            "modifier_id": extract_id(mod.get("modifier")),
            "modifier_uri": mod.get("modifier"),
            "qty": to_decimal(mod.get("qty"), scale=SCALE3),
            "modifier_price": to_decimal(mod.get("modifier_price")),
            "modifier_cost": to_decimal(mod.get("modifier_cost")),
            "mod_type": mod.get("mod_type"),
            "is_discounted": mod.get("is_discounted"),
            "is_substituted": mod.get("is_substituted"),
            "substitution_of": mod.get("substitution_of"),
            "conditional_of": mod.get("conditional_of"),
            "uuid": mod.get("uuid"),
            "split_combo_data_json": json_safe(mod.get("split_combo_data")),
        })
    return out


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    t0 = time.time()
    report = {"timing": {}, "archive": {}, "counts": {}}

    print("Connecting read-only to Postgres for reference joins...")
    conn = db_connect_readonly()
    est_name = load_establishment_name(conn, EST_ID)
    product_ref = load_product_reference(conn)
    conn.close()
    assert est_name is not None, "establishment 26 not found in establishments table"
    print(f"  establishment {EST_ID} = {est_name!r}, {len(product_ref)} products in reference")

    t_read0 = time.time()
    print("\nReading archived Order pages...")
    raw_orders, orders_meta = read_archive_pages(ARCHIVE_RUNS["orders"]["dir"], "orders")
    print(f"  {orders_meta}")

    print("Reading archived OrderItem pages...")
    raw_items, items_meta = read_archive_pages(ARCHIVE_RUNS["order_items"]["dir"], "order_items")
    print(f"  {items_meta}")

    print("Reading archived Payment pages...")
    raw_payments, payments_meta = read_archive_pages(ARCHIVE_RUNS["payments"]["dir"], "payments")
    print(f"  {payments_meta}")
    report["timing"]["current_export_archive_read_seconds"] = round(time.time() - t_read0, 2)
    report["archive"] = {"orders": orders_meta, "order_items": items_meta, "payments": payments_meta}

    # ── Rawness / duplicate-ID QA on the FULL archive (pre-filter) ──────────
    raw_order_ids = [o.get("id") for o in raw_orders]
    dup_order_ids = {k: v for k, v in Counter(raw_order_ids).items() if v > 1}
    raw_item_ids = [i.get("id") for i in raw_items]
    dup_item_ids = {k: v for k, v in Counter(raw_item_ids).items() if v > 1}
    report["counts"]["raw_order_rows_full_archive"] = len(raw_orders)
    report["counts"]["distinct_order_ids_full_archive"] = len(set(raw_order_ids))
    report["counts"]["duplicate_order_ids_full_archive"] = dup_order_ids
    report["counts"]["raw_orderitem_rows_full_archive"] = len(raw_items)
    report["counts"]["distinct_orderitem_ids_full_archive"] = len(set(raw_item_ids))
    report["counts"]["duplicate_orderitem_ids_full_archive"] = dup_item_ids

    # ── Final June-Chicago selection (independent of the archive's own
    #    fetch window — verifies rather than assumes it's already exact) ──
    print("\nApplying America/Chicago June-2026 boundary filter...")
    orders_in_june = [o for o in raw_orders
                       if (dt := parse_chicago(o.get("created_date"))) is not None
                       and JUNE_START_CHI <= dt < JULY_START_CHI]
    dropped_orders = len(raw_orders) - len(orders_in_june)
    print(f"  orders: {len(raw_orders)} in archive -> {len(orders_in_june)} pass Chicago June filter "
          f"({dropped_orders} dropped)")
    report["counts"]["orders_dropped_by_chicago_boundary_filter"] = dropped_orders

    orders_by_id = {}
    order_row_order = []  # preserve first-seen order for duplicate IDs, no reordering/dedup
    for o in orders_in_june:
        row = build_order_row(o, est_name)
        oid = row["order_id"]
        if oid not in orders_by_id:
            orders_by_id[oid] = row
        else:
            # Duplicate order_id in raw archive: KEEP BOTH per instructions —
            # do not resolve/collapse, do not silently drop the repeat.
            row["duplicate_order_id_flag"] = True
            orders_by_id[oid]["duplicate_order_id_flag"] = True
        order_row_order.append(row)

    june_order_ids = set(o["order_id"] for o in order_row_order)

    # Payments/items are attached to the first-seen row for a given
    # order_id — if duplicates exist (see duplicate_order_id_flag), only
    # that first row carries the aggregated payment/item-flag columns, to
    # avoid double-counting payments across duplicate order rows. Both raw
    # rows are still kept in File A untouched.
    orders_by_id_for_attach = {r["order_id"]: r for r in order_row_order if r["order_id"] in orders_by_id
                                and orders_by_id[r["order_id"]] is r}
    payment_groups = attach_payments(orders_by_id_for_attach, raw_payments)
    orphan_payment_order_ids = [oid for oid in payment_groups if oid not in june_order_ids]
    report["counts"]["payment_records_total_archive"] = len(raw_payments)
    report["counts"]["payments_matched_to_june_order"] = sum(
        len(v) for oid, v in payment_groups.items() if oid in june_order_ids)
    report["counts"]["orphan_payment_order_ids"] = len(orphan_payment_order_ids)
    pay_dist = Counter(len(v) for oid, v in payment_groups.items() if oid in june_order_ids)
    report["counts"]["payment_count_per_order_distribution"] = dict(sorted(pay_dist.items()))

    # Items: filter to items whose order is in the June set (an item's own
    # created_date can differ slightly from its order's; item->order
    # membership, not the item's own timestamp, decides June scope here).
    items_in_scope = [it for it in raw_items if extract_id(it.get("order")) in june_order_ids]
    orphan_items = [it for it in raw_items if extract_id(it.get("order")) not in june_order_ids]
    attach_items_flags(orders_by_id_for_attach, items_in_scope)

    item_rows = [build_item_row(it, product_ref) for it in items_in_scope]
    modifier_rows = []
    for it in items_in_scope:
        modifier_rows.extend(build_modifier_rows(it))

    order_item_ids_in_file_b = {r["order_item_id"] for r in item_rows}
    order_ids_referenced_by_b = {r["order_id"] for r in item_rows}
    orphan_from_b = order_ids_referenced_by_b - june_order_ids
    report["counts"]["orderitems_total_archive"] = len(raw_items)
    report["counts"]["orderitems_in_june_scope"] = len(item_rows)
    report["counts"]["orderitems_orphaned_outside_june_orders"] = len(orphan_items)
    report["counts"]["file_b_orphan_order_ids"] = len(orphan_from_b)
    report["counts"]["modifier_rows"] = len(modifier_rows)

    # ── QA counts (report only — nothing filtered) ──────────────────────────
    n_voided_orders_derived = sum(1 for r in order_row_order if r.get("has_voided_items"))
    n_refunded_orders = sum(1 for r in order_row_order if r.get("any_payment_refunded"))
    n_zero_dollar_orders = sum(1 for r in order_row_order
                                if r.get("final_total") is not None and r["final_total"] == 0)
    n_voided_items = sum(1 for r in item_rows if r.get("is_voided"))
    n_deleted_orders = sum(1 for r in order_row_order if r.get("deleted"))
    n_unpaid_orders = sum(1 for r in order_row_order if r.get("is_unpaid"))
    report["counts"]["final_orders_in_file_a"] = len(order_row_order)
    report["counts"]["distinct_order_ids_in_file_a"] = len(set(r["order_id"] for r in order_row_order))
    report["counts"]["orders_with_voided_items_derived"] = n_voided_orders_derived
    report["counts"]["orders_with_any_refunded_payment"] = n_refunded_orders
    report["counts"]["orders_zero_dollar_final_total"] = n_zero_dollar_orders
    report["counts"]["orders_deleted_flag_true"] = n_deleted_orders
    report["counts"]["orders_is_unpaid_true"] = n_unpaid_orders
    report["counts"]["orderitems_is_voided_true"] = n_voided_items
    report["counts"]["orderitems_in_file_b"] = len(item_rows)
    report["counts"]["distinct_orderitem_ids_in_file_b"] = len(order_item_ids_in_file_b)

    print(f"\nFile A (orders): {len(order_row_order)} rows")
    print(f"File B (order items): {len(item_rows)} rows")
    print(f"Modifier rows (nested, File C): {len(modifier_rows)} rows")

    return order_row_order, item_rows, modifier_rows, report, est_name


if __name__ == "__main__":
    main()
