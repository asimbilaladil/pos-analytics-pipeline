"""
sync_updated.py
================
Task 07 — update-aware daily sync.

Opt-in only via REVEL_SYNC_MODE=updated. pipeline.py's default flow
(REVEL_SYNC_MODE unset or "created") is completely unchanged — this module
is a separate, additive sync path, not a replacement.

Processing order per establishment (Section 9):
    1. Orders            (updated_date window)
    2. Payments          (updated_date window)
    3. OrderItems         (Strategy A: order__in for Orders touched in step 1)
    4. nested ModifierItems (parsed from the same OrderItem responses)
    5. OrderHistory       (order__in for the same touched Order IDs)
    6. Products/ProductCategories (separate, establishment-independent, called
       once per run — see sync_reference_data())

Strategy A rationale (Section 5): 8/8 live-tested examples (void via
ervc_type 3/5/7, order-level comp, and a normal item in an order updated for
other reasons) showed Order.updated_date always >= the triggering
OrderItem.updated_date, propagation delay 0s-11min, well inside the 48h
overlap window. Strategy B (direct OrderItem updated_date fetch, resolving
establishment via parent order) is intentionally NOT implemented — kept as
a documented fallback only, per instruction. check_orderitem_ahead_of_order()
below is the ongoing monitoring check for whether that fallback is ever
actually needed.

Sync watermark: durable in Postgres (sync_state table, migrations/
07_sync_state.sql), one row per (resource, establishment_id — NULL for
establishment-independent resources). A resource/establishment's watermark
(window_start/window_end/last_success_at) advances ONLY after every page for
that resource/window was fetched, archived, parsed, and committed to the DB
— see record_sync_success()/record_sync_failure(). Failure never advances
the watermark; the next run's window naturally re-covers the gap.
"""

import os
import sys
import json
import logging
from datetime import datetime, date, timedelta, timezone
from typing import Optional

import psycopg2
from psycopg2.extras import execute_values

import pipeline as P
import raw_archive
import dirty_date_queue

log = logging.getLogger(__name__)

SAFETY_DELAY = timedelta(minutes=2)
OVERLAP = timedelta(hours=48)
BOOTSTRAP_LOOKBACK = timedelta(hours=48)  # first-ever run for a resource/establishment: no watermark yet

SYNC_MODE = os.getenv("REVEL_SYNC_MODE", "created")  # "created" (default, unchanged) or "updated"


# ─── Timezone-safe query formatting ────────────────────────────────────────
def format_revel_dt(utc_dt: datetime) -> str:
    """
    Inverse of pipeline.parse_dt: UTC-aware datetime -> naive America/Chicago
    string, matching Revel's own datetime filter convention. Verified live
    (Task 07 Section 5/2 investigation): updated_date__gte/__lt on the Order
    endpoint accepts the same naive-local format as created_date__gte/__lte.
    No fixed -05/-06 offset anywhere — always resolved through zoneinfo,
    which handles DST transitions correctly.
    """
    local = utc_dt.astimezone(P._REVEL_TZ)
    return local.strftime("%Y-%m-%dT%H:%M:%S")


# ─── Sync window / watermark (Postgres-durable, per resource+establishment) ─
def get_sync_window(cur, resource: str, establishment_id: Optional[int]) -> tuple:
    """
    start = last successful window_end - 48h overlap
    end   = now - small safety delay

    If no prior successful sync exists for this (resource, establishment),
    bootstrap with a 48h lookback rather than defaulting to "yesterday" —
    matches the "if down for days, extend back to the last successful run"
    requirement in spirit for a first-ever run (there is no "last successful
    run" yet, so a conservative 48h window is the safest starting point).
    """
    cur.execute(
        "SELECT window_end FROM sync_state WHERE resource=%s AND establishment_id IS NOT DISTINCT FROM %s",
        (resource, establishment_id),
    )
    row = cur.fetchone()
    now = datetime.now(timezone.utc)
    end = now - SAFETY_DELAY
    if row and row[0]:
        start = row[0] - OVERLAP
    else:
        start = now - BOOTSTRAP_LOOKBACK
        log.info("  [%s/est=%s] no prior watermark — bootstrapping with %s lookback",
                 resource, establishment_id, BOOTSTRAP_LOOKBACK)
    return start, end


def record_sync_success(cur, resource: str, establishment_id: Optional[int],
                         window_start: datetime, window_end: datetime, run_id: str) -> None:
    """
    Call ONLY after: every page for this resource/window was fetched,
    archived, parsed, and the DB write for it committed. Advances
    window_start/window_end/last_success_at.
    """
    cur.execute("""
        INSERT INTO sync_state (resource, establishment_id, last_success_at, window_start, window_end, last_run_id, status, updated_at)
        VALUES (%s, %s, NOW(), %s, %s, %s, 'success', NOW())
        ON CONFLICT (resource, establishment_id) DO UPDATE SET
            last_success_at = NOW(),
            window_start     = EXCLUDED.window_start,
            window_end       = EXCLUDED.window_end,
            last_run_id      = EXCLUDED.last_run_id,
            status           = 'success',
            updated_at       = NOW()
    """, (resource, establishment_id, window_start, window_end, run_id))


def record_sync_failure(cur, resource: str, establishment_id: Optional[int], run_id: str) -> None:
    """
    Marks status='failed' for visibility, but deliberately does NOT touch
    window_start/window_end/last_success_at — the next run's window is
    computed from the last value that actually succeeded, so a failure here
    can never advance the watermark past unprocessed data.
    """
    cur.execute("""
        INSERT INTO sync_state (resource, establishment_id, last_run_id, status, updated_at)
        VALUES (%s, %s, %s, 'failed', NOW())
        ON CONFLICT (resource, establishment_id) DO UPDATE SET
            last_run_id = EXCLUDED.last_run_id,
            status      = 'failed',
            updated_at  = NOW()
    """, (resource, establishment_id, run_id))


# ─── Row stats ──────────────────────────────────────────────────────────────
def _empty_stats():
    return {"rows_fetched": 0, "rows_affected": 0, "rows_failed": 0, "api_pages": 0}


# ─── Orders ─────────────────────────────────────────────────────────────────
def sync_orders(context, conn, est_id: int, run_id: str, target: str = "production") -> tuple:
    """Returns (touched_order_ids, stats). Raises on any failure (caller
    decides whether to record_sync_failure and continue to the next
    establishment, matching pipeline.py's existing per-establishment
    resilience pattern).

    target="shadow_v2" (Task 07.4): writes go to orders_v2 via
    pipeline.upsert_orders' target= passthrough, and the watermark is
    tracked under a SEPARATE sync_state resource ("orders_v2", not
    "orders") — critical so a shadow test run can never advance the real
    production "orders" watermark for this establishment. get_sync_window/
    record_sync_success are otherwise used completely unchanged.
    """
    watermark_resource = "orders" if target == "production" else "orders_v2"
    with conn.cursor() as cur:
        start, end = get_sync_window(cur, watermark_resource, est_id)
    range_from, range_to = format_revel_dt(start), format_revel_dt(end)
    log.info("  [orders/%s] est=%s window %s -> %s (Central)", target, est_id, range_from, range_to)

    records = P.fetch_all_pages(
        context,
        endpoint=f"{P.BASE_URL}/resources/Order/",
        params={"establishment": est_id, "updated_date__gte": range_from, "updated_date__lt": range_to},
        label="orders (updated)",
        resource="orders", run_id=run_id, establishment_id=est_id,
        date_window=(range_from, range_to), archive_date=date.today(),
    )

    today = date.today()
    rows, order_ids = [], []
    dirty_dates = set()
    for o in records:
        if not o.get("id"):
            continue
        created = P.parse_dt(o.get("created_date"))
        if created is None:
            continue
        rows.append(P.build_order_row(o, est_id, today))
        order_ids.append(o["id"])
        dirty_dates.add(dirty_date_queue.business_date(created))

    with conn.cursor() as cur:
        stats = P.upsert_orders(cur, rows, target=target)
        # Task 16 prerequisite: only queue recomputes for dates whose source
        # data actually landed in orders_v2 -- never for target="production"
        # writes, since aggregate_features_v2.py never reads production
        # orders/order_items.
        if target == "shadow_v2":
            dirty_date_queue.enqueue_dirty_dates(cur, est_id, dirty_dates, source_run_id=run_id)
    conn.commit()

    with conn.cursor() as cur:
        record_sync_success(cur, watermark_resource, est_id, start, end, run_id)
    conn.commit()

    return order_ids, {
        "rows_fetched": len(records), "rows_affected": stats.get("affected", 0),
        "rows_failed": 0, "api_pages": None,
    }


# ─── Payments ───────────────────────────────────────────────────────────────
def build_payment_row(p: dict, ingestion_date: date) -> tuple:
    """
    Maps Revel's Payment fields to payments.PAYMENT_COLS (pipeline.py).
    Deliberately excludes cardholder PII never in our schema to begin with
    (cc_first_name, cc_last_name, signature_img_url, receipt_email,
    payer_id, first/last_4_cc_digits, transaction_data) — see Task 03.

    refunded/deleted are bool()-coerced with a False default (not passed
    through raw) specifically because upsert_payments' UPSERT uses monotonic
    `payments.col OR EXCLUDED.col` — SQL's three-valued logic means
    `FALSE OR NULL` evaluates to NULL, which would silently un-set a
    previously-correct FALSE. Every live Payment sample observed a real
    boolean for both fields (never null), so this default is a safety net,
    not an expected case.
    """
    return (
        p["id"],
        p.get("uuid"),
        P.extract_id(p.get("order")),
        P.extract_id(p.get("establishment")),
        p.get("payment_type"),
        p.get("other_payment_type"),
        P.to_float(p.get("amount")),
        P.to_float(p.get("amount_authorized")),
        P.to_float(p.get("tip")),
        P.to_float(p.get("gratuity")),
        P.to_float(p.get("change")),
        bool(p.get("refunded", False)),
        p.get("refund_transaction_id"),
        p.get("transaction_id"),
        p.get("transaction_status"),
        p.get("transaction_captured"),
        p.get("processor_accepted"),
        p.get("card_type"),
        bool(p.get("online", False)),
        p.get("source_type"),
        p.get("executed"),
        bool(p.get("deleted", False)),
        p.get("exchanged"),
        P.extract_id(p.get("station")),
        P.parse_dt(p.get("payment_date")),
        P.parse_dt(p.get("created_date")),
        P.parse_dt(p.get("updated_date")),
        P.extract_id(p.get("created_by")),
        P.extract_id(p.get("updated_by")),
        ingestion_date,
    )


def sync_payments(context, conn, est_id: int, run_id: str, target: str = "production") -> dict:
    """target="shadow_v2" (Task 15 coordinated freshness pass): writes go to
    payments_v2 via pipeline.upsert_payments' existing target= passthrough,
    and the watermark is tracked under a SEPARATE sync_state resource
    ("payments_v2", not "payments") -- same pattern already proven for
    sync_orders (Task 07.4). Default "production" is byte-for-byte the
    pre-existing behavior; no existing caller passes target= today, so
    nothing about the live path changes."""
    watermark_resource = "payments" if target == "production" else "payments_v2"
    with conn.cursor() as cur:
        start, end = get_sync_window(cur, watermark_resource, est_id)
    range_from, range_to = format_revel_dt(start), format_revel_dt(end)
    log.info("  [payments/%s] est=%s window %s -> %s (Central)", target, est_id, range_from, range_to)

    records = P.fetch_all_pages(
        context,
        endpoint=f"{P.BASE_URL}/resources/Payment/",
        params={"establishment": est_id, "updated_date__gte": range_from, "updated_date__lt": range_to},
        label="payments",
        resource="payments", run_id=run_id, establishment_id=est_id,
        date_window=(range_from, range_to), archive_date=date.today(),
    )

    today = date.today()
    rows = [build_payment_row(p, today) for p in records if p.get("id")]

    with conn.cursor() as cur:
        stats = P.upsert_payments(cur, rows, target=target)
    conn.commit()

    with conn.cursor() as cur:
        record_sync_success(cur, watermark_resource, est_id, start, end, run_id)
    conn.commit()

    return {"rows_fetched": len(records), "rows_affected": stats.get("affected", 0),
            "rows_failed": 0, "api_pages": None}


# ─── OrderItems (Strategy A) + nested ModifierItems ────────────────────────
def _update_item_updated_dates(cur, items: list, target: str = "production") -> None:
    """
    order_items.updated_date is populated ONLY by this (opt-in) sync path —
    pipeline.py's default created-mode flow is unchanged and leaves it NULL.
    This is genuinely separate infrastructure (Task 07's own migration,
    07_2_order_items_updated_date.sql — not part of the original Task 04
    field set), so it stays a small supplementary UPDATE here.

    The Task 04 fields themselves (ervc_type, item_type, initial_price,
    discount_amount, discount_reason, discounted_by_user_id, cost,
    exchanged, void_ref_uuid) are NOT handled here — they're populated
    through pipeline.build_item_row / ITEM_COLS / upsert_order_items, the
    same canonical parser and UPSERT the default created-mode path uses, so
    there is exactly one place that knows how to turn a Revel OrderItem
    into a DB row. See pipeline.py ITEM_COLS for why they're appended, not
    inserted mid-tuple.

    target="shadow_v2" (Task 07.4): targets order_items_v2 instead, via the
    same pipeline.UPSERT_TARGETS allowlist upsert_order_items uses.
    """
    table = P.UPSERT_TARGETS[target]["order_items"]
    rows = []
    for i in items:
        if not i.get("id"):
            continue
        created = P.parse_dt(i.get("created_date"))
        if created is None:
            continue
        rows.append((i["id"], created, P.parse_dt(i.get("updated_date"))))
    if not rows:
        return
    execute_values(cur, f"""
        UPDATE {table} AS oi SET updated_date = v.updated_date
        FROM (VALUES %s) AS v(id, created_date, updated_date)
        WHERE oi.id = v.id AND oi.created_date = v.created_date
    """, rows, page_size=500)


def sync_order_items_and_modifiers(context, conn, est_id: int, run_id: str,
                                    order_ids: list, modifier_cache: dict,
                                    target: str = "production",
                                    skip_modifiers: bool = False) -> tuple:
    """Strategy A: OrderItems for the Order IDs touched by sync_orders() this
    run, via the existing verified order__in batching (Task 06). No
    watermark of its own — driven entirely by sync_orders' output, so there
    is nothing to bootstrap or advance independently.

    target="shadow_v2" (Task 07.4): items go to order_items_v2 (pipeline.
    upsert_order_items' target= passthrough) and updated_date backfill goes
    to the same table (_update_item_updated_dates' target= passthrough).
    skip_modifiers=True: lets a shadow-mode caller skip the modifier_items
    UPSERT entirely (Task 07.4's original use case, before modifier_items_v2
    existed). Default False keeps every existing (production) caller
    unchanged.

    Task 15 fix: when skip_modifiers=False, the modifier UPSERT now passes
    target=target through to P.upsert_modifier_items — previously this call
    had no target= at all and always defaulted to production modifier_items
    regardless of what target the caller passed for orders/items, which
    would have silently leaked shadow-mode writes into the real production
    table the moment skip_modifiers=False was ever used with target=
    "shadow_v2". Production callers (target="production", the default) are
    unaffected: target="production" was always upsert_modifier_items's
    implicit default already."""
    if not order_ids:
        return _empty_stats(), _empty_stats()

    today = date.today()
    items = P.fetch_items_for_orders(context, order_ids, run_id=run_id,
                                      establishment_id=est_id, archive_date=today)

    item_rows, modifier_rows = [], []
    dirty_dates = set()
    for item in items:
        if not item.get("id"):
            continue
        order_id = P.extract_id(item.get("order"))
        created = P.parse_dt(item.get("created_date"))
        item_tuple = P.build_item_row(item, est_id, today, created or datetime.now(timezone.utc))
        item_rows.append(item_tuple)

        item_created_dt = item_tuple[15]  # see pipeline.insert_establishment_data for this index
        item_date = item_created_dt.astimezone(P._REVEL_TZ).date() if item_created_dt else today
        dirty_dates.add(item_date)

        for mod in item.get("modifieritems", []):
            if not mod.get("id"):
                continue
            modifier_rows.append(P.build_modifier_row(mod, item["id"], order_id, est_id, item_date, modifier_cache))

    with conn.cursor() as cur:
        item_stats = P.upsert_order_items(cur, item_rows, target=target)
        _update_item_updated_dates(cur, items, target=target)
        if skip_modifiers:
            modifier_stats = {"fetched": len(modifier_rows), "affected": 0, "skipped": True}
        else:
            modifier_stats = P.upsert_modifier_items(cur, modifier_rows, target=target)
        # Task 16 prerequisite: same reasoning as sync_orders -- only queue
        # for target="shadow_v2", since that's the only source
        # aggregate_features_v2.py reads from.
        if target == "shadow_v2":
            dirty_date_queue.enqueue_dirty_dates(cur, est_id, dirty_dates, source_run_id=run_id)
    conn.commit()

    return (
        {"rows_fetched": len(items), "rows_affected": item_stats.get("affected", 0), "rows_failed": 0, "api_pages": None},
        {"rows_fetched": len(modifier_rows), "rows_affected": modifier_stats.get("affected", 0) if skip_modifiers else (modifier_rows and len(modifier_rows) or 0),
         "rows_failed": 0, "api_pages": None, "skipped": skip_modifiers},
    )


# ─── OrderHistory ───────────────────────────────────────────────────────────
def build_order_history_row(h: dict) -> tuple:
    """
    order_opened_at/order_closed_at are PosStation references, not
    timestamps (Task 02 finding) -- opened/closed are the real timestamps.
    Matches pipeline.ORDER_HISTORY_COLS field order.
    """
    return (
        h["id"],
        h.get("uuid"),
        P.extract_id(h.get("order")),
        P.parse_dt(h.get("opened")),
        P.parse_dt(h.get("closed")),
        P.extract_id(h.get("order_opened_by")),
        P.extract_id(h.get("order_closed_by")),
        P.extract_id(h.get("order_opened_at")),
        P.extract_id(h.get("order_closed_at")),
    )


def fetch_order_history_for_orders(context, order_ids: list, run_id: str,
                                    establishment_id: int, archive_date: date,
                                    window_key: str = None) -> list:
    """
    order__in batching, same pattern as pipeline.fetch_items_for_orders
    (verified reliable in Task 02/06). Does NOT assume one record per order
    per page -- reads meta.total_count and paginates exactly like every
    other fetch here; an order with zero history (still open) legitimately
    contributes zero records, an order with more than one history record
    would be fully paginated, not truncated.

    window_key (Task 12): same passthrough as pipeline.fetch_all_pages'/
    fetch_items_for_orders' window_key (Tasks 09/10) -- lets a single script
    invocation fetch multiple independent historical (establishment, month)
    windows without their batch/page counters colliding under one shared
    run_id/archive_date. Existing caller (sync_updated's own updated-mode
    sync) fetches exactly one window per run and leaves this unset -- no
    behavior change for it. Batching, pagination, and retry/give-up behavior
    are otherwise completely unchanged.
    """
    all_history = []
    total_batches = (len(order_ids) + P.BATCH_SIZE - 1) // P.BATCH_SIZE

    for i in range(0, len(order_ids), P.BATCH_SIZE):
        batch_order_ids = order_ids[i: i + P.BATCH_SIZE]
        batch_num = (i // P.BATCH_SIZE) + 1
        ids_str = ",".join(map(str, batch_order_ids))
        offset = 0

        while True:
            page = (offset // 1000) + 1  # unique WITHIN this batch; batch= below makes
                                          # it unique across batches too (own directory)
            attempt_box = [0]

            def _get_page():
                attempt_box[0] += 1
                query_params = {"format": "json", "order__in": ids_str, "limit": 1000, "offset": offset}
                r = context.request.get(f"{P.BASE_URL}/resources/OrderHistory/", params=query_params)
                if r.status != 200:
                    raise RuntimeError(f"HTTP {r.status} at offset {offset}: {r.text()[:200]}")
                raw_text = r.text()
                raw_archive.archive_response(
                    "order_history", run_id, raw_text,
                    endpoint=f"{P.BASE_URL}/resources/OrderHistory/", query_params=query_params,
                    page=page, offset=offset, attempt=attempt_box[0], batch=batch_num,
                    window_key=window_key,
                    archive_date=archive_date, establishment_id=establishment_id,
                    pipeline_version=P.SCRIPT_VERSION,
                )
                return json.loads(raw_text)

            try:
                data = P.with_retries(_get_page, attempts=3, delay=5, label=f"OrderHistory offset {offset}")
            except raw_archive.ArchiveError:
                raise
            except Exception as exc:
                log.error("  OrderHistory batch %d/%d — giving up at offset %d: %s",
                          batch_num, total_batches, offset, exc)
                break

            records = data.get("objects", [])
            total = data.get("meta", {}).get("total_count", 0)
            if not records:
                break
            all_history.extend(records)
            offset += 1000
            if offset >= total:
                break

    return all_history


def sync_order_history(context, conn, est_id: int, run_id: str, order_ids: list,
                        target: str = "production") -> dict:
    """target="shadow_v2" (Task 15 coordinated freshness pass): writes go to
    order_history_v2 via pipeline.upsert_order_history's existing target=
    passthrough. No watermark involved either way -- this resource has none
    of its own (Strategy: driven entirely by the touched_order_ids sync_orders
    produced this run), so there is no production/shared sync_state to
    protect here, only the direct table write. Default "production" is
    byte-for-byte the pre-existing behavior."""
    if not order_ids:
        return _empty_stats()

    today = date.today()
    records = fetch_order_history_for_orders(context, order_ids, run_id, est_id, today)
    rows = [build_order_history_row(h) for h in records if h.get("id")]

    with conn.cursor() as cur:
        stats = P.upsert_order_history(cur, rows, target=target)
    conn.commit()

    return {"rows_fetched": len(records), "rows_affected": stats.get("affected", 0),
            "rows_failed": 0, "api_pages": None}


# ─── Products / ProductCategories (establishment-independent) ─────────────
PRODUCT_CATEGORY_ENDPOINT = f"{P.BASE_URL}/products/ProductCategory/"

def build_product_category_row(c: dict) -> tuple:
    cid = c.get("id")
    return (
        cid,
        (c.get("name") or "").strip() or f"Category #{cid}",
        P.extract_id(c.get("parent")),
        bool(c.get("active", True)),
        c.get("sorting"),
        c.get("description"),
        P.parse_dt(c.get("created_date")),
        P.parse_dt(c.get("updated_date")),
    )


PRODUCT_COLS_WITH_CATEGORY = [
    "id", "revel_uri", "name", "product_class", "is_combo", "is_modifier",
    "base_price", "active", "category_id",
]

def build_product_row_with_category(p: dict) -> tuple:
    """
    Fixed (was flagged in the Task 07 report, resolved before Task 09):
    Revel's raw field is "productclass" (a ProductClass URI reference), not
    "product_class" — confirmed live (e.g. "/products/ProductClass/34/").
    Stored as-is (raw value, products.product_class is VARCHAR, no
    ProductClass reference table exists to resolve it further).
    """
    pid = p.get("id")
    return (
        pid,
        p.get("resource_uri"),
        (p.get("name") or "").strip() or f"Product #{pid}",
        p.get("productclass"),
        bool(p.get("is_combo", False)),
        bool(p.get("is_modifier", False)),
        P.to_float(p.get("price")),
        bool(p.get("active", True)),
        P.extract_id(p.get("category")),
    )


def upsert_products_with_category(cur, rows: list) -> dict:
    """
    Separate from pipeline.fetch_and_upsert_products (which stays exactly
    as-is for the default created-mode path). This is the only place
    products.category_id is populated, and it only runs after
    product_categories has been upserted in the same call (see
    sync_reference_data) — category_id has an FK to product_categories(id),
    and that table starts out empty until this runs.
    """
    if not rows:
        return {"fetched": 0, "affected": 0}
    col_str = ", ".join(PRODUCT_COLS_WITH_CATEGORY)
    sql = f"""
        INSERT INTO products ({col_str}) VALUES %s
        ON CONFLICT (id) DO UPDATE SET
            name          = EXCLUDED.name,
            product_class = EXCLUDED.product_class,
            is_combo      = EXCLUDED.is_combo,
            is_modifier   = EXCLUDED.is_modifier,
            base_price    = EXCLUDED.base_price,
            active        = EXCLUDED.active,
            category_id   = COALESCE(EXCLUDED.category_id, products.category_id),
            updated_at    = NOW()
    """
    execute_values(cur, sql, rows, page_size=500)
    return {"fetched": len(rows), "affected": len(rows)}


# Revel's ProductCategory list endpoint defaults to active=True and has no
# active__in filter (confirmed live: {} and {"active":"True"} both return
# the same 2,595; {"active":"False"} returns a disjoint 763; combined =
# 3,358, 0 overlap, 0 remaining orphan parent refs after combining). "The
# complete resource" is therefore two fetches merged, not one — an
# unfiltered/default-only fetch silently drops every deactivated category,
# which showed up as ~20% of rows (521/2,595) having a parent_id absent
# from the default fetch alone.
CATEGORY_FETCH_SETS = [
    ("active", {}),
    ("inactive", {"active": "False"}),
]


def fetch_all_categories(context, run_id_prefix: str) -> tuple:
    """
    Fetch both the active and inactive ProductCategory sets and return
    (records, run_ids). Each set gets its own run_id (run_id_prefix +
    "_active"/"_inactive") so their page numbering, which restarts at 1 per
    fetch_all_pages call, can never collide in the Task06 archive directory
    — the same reason batch= exists for order__in fetches.
    """
    records = []
    run_ids = {}
    for label, params in CATEGORY_FETCH_SETS:
        set_run_id = f"{run_id_prefix}_{label}"
        run_ids[label] = set_run_id
        set_records = P.fetch_all_pages(
            context, endpoint=PRODUCT_CATEGORY_ENDPOINT, params=params,
            label=f"product_categories({label})",
            resource="product_categories", run_id=set_run_id, archive_date=date.today(),
        )
        log.info("Fetched %d ProductCategory records (%s)", len(set_records), label)
        records.extend(set_records)
    return records, run_ids


def sync_reference_data(context, conn, run_id: str) -> tuple:
    """Products/ProductCategories — establishment-independent, called once
    per run (Section 9 step 6), not per-establishment. ProductCategory
    always upserted first (FK dependency)."""
    with conn.cursor() as cur:
        start, end = get_sync_window(cur, "product_categories", None)
    range_from, range_to = format_revel_dt(start), format_revel_dt(end)

    cat_records, cat_run_ids = fetch_all_categories(context, run_id)
    cat_rows = [build_product_category_row(c) for c in cat_records if c.get("id")]
    with conn.cursor() as cur:
        cat_stats = P.upsert_product_categories(cur, cat_rows)
    conn.commit()
    with conn.cursor() as cur:
        record_sync_success(cur, "product_categories", None, start, end, run_id)
    conn.commit()

    prod_records = P.fetch_all_pages(
        context, endpoint=f"{P.BASE_URL}/resources/Product/", params={},
        label="products",
        resource="products", run_id=run_id, archive_date=date.today(),
    )
    prod_rows = [build_product_row_with_category(p) for p in prod_records if p.get("id")]
    with conn.cursor() as cur:
        prod_stats = upsert_products_with_category(cur, prod_rows)
    conn.commit()
    with conn.cursor() as cur:
        record_sync_success(cur, "products", None, start, end, run_id)
    conn.commit()

    return (
        {"rows_fetched": len(cat_records), "rows_affected": cat_stats.get("affected", 0), "rows_failed": 0, "api_pages": None},
        {"rows_fetched": len(prod_records), "rows_affected": prod_stats.get("affected", 0), "rows_failed": 0, "api_pages": None},
    )


# ─── Strategy A monitoring check ───────────────────────────────────────────
GRACE_PERIOD = timedelta(hours=1)  # monitoring-check only — NOT the 48h sync OVERLAP window


def check_orderitem_ahead_of_order(
    conn, limit: int = 100, grace_period: timedelta = GRACE_PERIOD
) -> list:
    """
    Returns OrderItems whose updated_date is strictly later than their
    parent Order's updated_date -- counterexamples to the assumption
    Strategy A relies on (that fetching Orders by updated_date is
    sufficient to discover every changed OrderItem). Only meaningful for
    rows where order_items.updated_date has actually been populated (i.e.
    touched by this sync path) — NULL on either side is excluded, not
    treated as a violation.

    Live evidence (Section 5 investigation, 8/8 examples) showed Order.
    updated_date propagates AFTER OrderItem.updated_date, with observed
    delays up to ~11 minutes — and separately, a Revel-side batch/closeout
    process appears to touch many orders' updated_date well after the item
    event itself, which briefly makes the gap look reversed from a raw
    snapshot query. Neither is a real Strategy A failure: the 48-hour sync
    OVERLAP (unrelated to and unchanged by this) will still pick up the
    order on the next run regardless. So a bare "oi.updated_date >
    o.updated_date" snapshot comparison conflates that transient lag with
    an actual, persistent counterexample.

    grace_period excludes any OrderItem whose updated_date is too recent to
    have had time to propagate — i.e. we only flag a row once it has aged
    past the grace period while STILL showing the order behind it. Re-
    running this check later (e.g. next monitoring cycle) re-evaluates every
    row against the then-current order.updated_date, so a row that clears
    on a later call was transient lag, not a real violation; one that keeps
    reappearing past the grace period is the persistent case worth escalating.

    Intended for ongoing monitoring and later backfill/reconciliation
    (Task 14/15), not a blocking check in the sync path itself.

    Joins to only the LATEST orders row per id (DISTINCT ON ... ORDER BY
    updated_date DESC), not a plain "o.id = oi.order_id" join — see Task
    07.3: orders.id is not currently a unique key (Revel's created_date can
    be revised while an order stays open, and (id, created_date) is the
    actual conflict/partition key), so a plain join fans out across every
    duplicate id and can match a stale, long-abandoned snapshot row. This
    is exactly what produced the original 14 "violations" on order
    16282265 — its stale duplicate (frozen at its own created_date, never
    updated again) looked older than the items, while the live/current row
    was correctly ahead of them the whole time. This is a query-level
    mitigation only; see Task 07.3 for the underlying orders_v2/
    order_items_v2 canonical-key remediation plan (not executed here).
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT oi.id, oi.order_id, oi.updated_date, o.updated_date
            FROM order_items oi
            JOIN LATERAL (
                SELECT o2.updated_date
                FROM orders o2
                WHERE o2.id = oi.order_id
                ORDER BY o2.updated_date DESC
                LIMIT 1
            ) o ON TRUE
            WHERE oi.updated_date IS NOT NULL
              AND o.updated_date IS NOT NULL
              AND oi.updated_date > o.updated_date
              AND oi.updated_date < NOW() - %s
            ORDER BY (oi.updated_date - o.updated_date) DESC
            LIMIT %s
        """, (grace_period, limit))
        return cur.fetchall()


# ─── Per-establishment orchestration (Section 9 processing order) ─────────
def run_establishment_updated(context, conn, est_id: int, run_id: str, modifier_cache: dict,
                               target: str = "production") -> dict:
    """
    1. Orders  2. Payments  3. OrderItems  4. nested ModifierItems
    5. OrderHistory. Products/ProductCategories are run once per whole
    invocation, not per establishment — see sync_reference_data().

    Each resource's failure is isolated: caught, logged, marked
    status='failed' in sync_state (watermark not advanced for THAT
    resource), and processing continues to the next resource so one
    resource's outage doesn't block the others — mirroring pipeline.py's
    existing per-establishment resilience pattern at the resource level.

    target="shadow_v2" (Task 16 prerequisite): threaded to all four sync_*
    calls, which already each support target= independently (sync_orders/
    sync_payments/sync_order_history: Task 07.4/Task 15;
    sync_order_items_and_modifiers's ModifierItem UPSERT: fixed during the
    coordinated freshness-pass work). Default "production" is byte-for-byte
    the pre-existing behavior -- this function's only existing caller
    (pipeline.py's REVEL_SYNC_MODE=updated branch) doesn't pass target=
    today, so it is unaffected until pipeline.py's call site is changed
    (Task 16 §2, not done here).

    record_sync_failure's resource name is also target-aware here (was
    hardcoded to "orders"/"payments" before this change) -- a failure while
    target="shadow_v2" must mark the "orders_v2"/"payments_v2" watermark
    failed, never the shared "orders"/"payments" one, mirroring
    get_sync_window/record_sync_success's existing watermark_resource
    pattern in sync_orders/sync_payments.
    """
    report = {}
    orders_watermark = "orders" if target == "production" else "orders_v2"
    payments_watermark = "payments" if target == "production" else "payments_v2"

    order_ids = []
    try:
        order_ids, orders_stats = sync_orders(context, conn, est_id, run_id, target=target)
        report["orders"] = orders_stats
    except Exception as exc:
        conn.rollback()
        log.error("  [orders] est=%s FAILED: %s", est_id, exc)
        with conn.cursor() as cur:
            record_sync_failure(cur, orders_watermark, est_id, run_id)
        conn.commit()
        report["orders"] = {**_empty_stats(), "rows_failed": -1, "error": str(exc)}

    try:
        report["payments"] = sync_payments(context, conn, est_id, run_id, target=target)
    except Exception as exc:
        conn.rollback()
        log.error("  [payments] est=%s FAILED: %s", est_id, exc)
        with conn.cursor() as cur:
            record_sync_failure(cur, payments_watermark, est_id, run_id)
        conn.commit()
        report["payments"] = {**_empty_stats(), "rows_failed": -1, "error": str(exc)}

    try:
        item_stats, modifier_stats = sync_order_items_and_modifiers(
            context, conn, est_id, run_id, order_ids, modifier_cache, target=target)
        report["order_items"] = item_stats
        report["modifier_items"] = modifier_stats
    except Exception as exc:
        conn.rollback()
        log.error("  [order_items] est=%s FAILED: %s", est_id, exc)
        report["order_items"] = {**_empty_stats(), "rows_failed": -1, "error": str(exc)}
        report["modifier_items"] = _empty_stats()

    try:
        report["order_history"] = sync_order_history(context, conn, est_id, run_id, order_ids, target=target)
    except Exception as exc:
        conn.rollback()
        log.error("  [order_history] est=%s FAILED: %s", est_id, exc)
        report["order_history"] = {**_empty_stats(), "rows_failed": -1, "error": str(exc)}

    return report
