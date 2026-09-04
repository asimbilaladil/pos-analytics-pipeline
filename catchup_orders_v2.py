"""
catchup_orders_v2.py
=====================
Dedicated, bounded pre-cutover catch-up for orders_v2/order_items_v2 ONLY.

Purpose: close the gap between orders_v2/order_items_v2's Task 09/10 backfill
snapshot (~2026-08-11) and current live data, so both tables are a complete,
current superset of production before Task 14 (feature recompute), Task 15
(reconciliation), and Task 16 (cutover).

This is NOT the production entrypoint. pipeline.py's REVEL_SYNC_MODE=updated
branch (sync_updated.run_establishment_updated) is hardwired to
target="production" with no passthrough, and unconditionally also syncs
payments and order_history to production tables -- both out of scope here
and unsafe to invoke for this purpose. This script instead calls the
lower-level Task 07.4 shadow-mode building blocks directly:
  - sync_updated.sync_orders(..., target="shadow_v2")
  - sync_updated.sync_order_items_and_modifiers(..., target="shadow_v2",
    skip_modifiers=True)

Writes ONLY to orders_v2 and order_items_v2:
  - sync_payments is NEVER called (payments_v2/payments untouched)
  - sync_order_history is NEVER called (order_history_v2/order_history untouched)
  - skip_modifiers=True means P.upsert_modifier_items is NEVER called (its
    only call site inside sync_order_items_and_modifiers has no target=
    passthrough and defaults to production modifier_items -- skip_modifiers
    short-circuits before that call happens at all, so it cannot leak)
  - production orders/order_items are never touched (target="shadow_v2" only)

Watermark isolation (verified against source before writing this script):
sync_orders(target="shadow_v2") already tracks its watermark under
sync_state resource="orders_v2" -- a namespace distinct from resource=
"orders"/"payments"/"order_history" (what the production updated-mode path
would use). The live nightly cron (run.sh -> pipeline.py) runs with
REVEL_SYNC_MODE unset (confirmed: not set in .env), which takes pipeline.py's
default "created" branch -- that branch never reads or writes sync_state at
all. So there is no shared watermark this script could disturb: "orders_v2"
is already dedicated, and the only thing that could ever read "orders"/
"payments"/"order_history" watermarks (the dormant updated-mode branch) is
never invoked by this script.

Bounded window: no prior "orders_v2" watermark exists for any establishment
except est=26 (a one-off Task 07.4 smoke test). get_sync_window's existing
BOOTSTRAP_LOOKBACK=48h supplies the requested "48h overlap" automatically
for every other establishment -- no new window logic needed.

Sequential, one establishment at a time, one Playwright context, no
parallelism. Raw archive-before-parse, retries, and the canonical parser/
UPSERT (pipeline.build_order_row/build_item_row, pipeline.upsert_orders/
upsert_order_items) are all inherited unchanged from sync_updated.py /
pipeline.py -- nothing here reimplements fetch, parse, or archive logic.
"""

import os
import sys
import json
import logging
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(".env")

import pipeline as P
import raw_archive
import sync_updated as S
from playwright.sync_api import sync_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def snapshot_state(conn, est_id: int) -> dict:
    """Reconciliation snapshot -- safe to call before AND after the catch-up."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*), count(DISTINCT id) FROM orders_v2")
        ov2_total, ov2_distinct = cur.fetchone()

        cur.execute("SELECT count(*), count(DISTINCT id) FROM order_items_v2")
        oiv2_total, oiv2_distinct = cur.fetchone()

        cur.execute("""
            SELECT count(*) FROM order_items_v2 oi
            LEFT JOIN orders_v2 o ON o.id = oi.order_id WHERE o.id IS NULL
        """)
        item_orphans = cur.fetchone()[0]

        cur.execute("""
            SELECT count(*) FROM payments_v2 p
            LEFT JOIN orders_v2 o ON o.id = p.order_id WHERE o.id IS NULL
        """)
        payment_orphans_total = cur.fetchone()[0]

        cur.execute("""
            SELECT count(*) FROM payments_v2 p
            LEFT JOIN orders_v2 o ON o.id = p.order_id
            WHERE o.id IS NULL AND p.establishment_id = %s
        """, (est_id,))
        payment_orphans_est = cur.fetchone()[0]

        cur.execute("SELECT max(created_date), max(updated_date) FROM orders_v2 WHERE establishment_id=%s", (est_id,))
        ov2_max_created, ov2_max_updated = cur.fetchone()

        cur.execute("SELECT max(created_date) FROM order_items_v2 WHERE establishment_id=%s", (est_id,))
        oiv2_max_created = cur.fetchone()[0]

        cur.execute("SELECT id FROM orders_v2 WHERE establishment_id=%s", (est_id,))
        order_ids_before = {r[0] for r in cur.fetchall()}

        cur.execute("SELECT id FROM order_items_v2 WHERE establishment_id=%s", (est_id,))
        item_ids_before = {r[0] for r in cur.fetchall()}

    return {
        "orders_v2_total": ov2_total, "orders_v2_distinct": ov2_distinct,
        "orders_v2_dupes": ov2_total - ov2_distinct,
        "order_items_v2_total": oiv2_total, "order_items_v2_distinct": oiv2_distinct,
        "order_items_v2_dupes": oiv2_total - oiv2_distinct,
        "order_items_v2_orphans": item_orphans,
        "payments_v2_orphans_total": payment_orphans_total,
        "payments_v2_orphans_est": payment_orphans_est,
        "orders_v2_max_created_est": str(ov2_max_created), "orders_v2_max_updated_est": str(ov2_max_updated),
        "order_items_v2_max_created_est": str(oiv2_max_created),
        "_order_ids_before": order_ids_before,
        "_item_ids_before": item_ids_before,
    }


def run_pilot(est_id: int) -> dict:
    run_id = raw_archive.new_run_id()
    conn = P.db_connect()

    log.info("=== orders_v2/order_items_v2 catch-up pilot — est=%s run_id=%s ===", est_id, run_id)

    before = snapshot_state(conn, est_id)
    order_ids_before = before.pop("_order_ids_before")
    item_ids_before = before.pop("_item_ids_before")
    log.info("BEFORE: %s", json.dumps(before, default=str))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state="/tmp/revel_session.json")

        # 1. Orders — updated_date window, target=shadow_v2, watermark
        #    resource="orders_v2" only. Never touches "orders"/"payments"/
        #    "order_history" sync_state rows.
        touched_order_ids, orders_stats = S.sync_orders(
            context, conn, est_id, run_id, target="shadow_v2"
        )
        log.info("orders sync done: %s touched order ids, stats=%s", len(touched_order_ids), orders_stats)

        # 2. OrderItems — Strategy A (order__in for touched_order_ids),
        #    target=shadow_v2, skip_modifiers=True so P.upsert_modifier_items
        #    (whose only call site has no target= passthrough and defaults
        #    to production modifier_items) is never invoked.
        item_stats, modifier_stats = S.sync_order_items_and_modifiers(
            context, conn, est_id, run_id, touched_order_ids, modifier_cache={},
            target="shadow_v2", skip_modifiers=True,
        )
        log.info("order_items sync done: stats=%s modifier_stats=%s (skipped, never written)", item_stats, modifier_stats)

        browser.close()

    after = snapshot_state(conn, est_id)
    order_ids_after = after.pop("_order_ids_before")  # same query, post-run state
    item_ids_after = after.pop("_item_ids_before")
    log.info("AFTER: %s", json.dumps(after, default=str))

    new_orders = order_ids_after - order_ids_before
    updated_orders = (set(touched_order_ids) & order_ids_before)
    new_items = item_ids_after - item_ids_before
    touched_item_ids_after = item_ids_after  # includes both new+updated for touched orders
    updated_items_count = item_stats.get("rows_fetched", 0) - len(new_items)

    conn.close()

    report = {
        "run_id": run_id,
        "establishment_id": est_id,
        "before": before,
        "after": after,
        "orders_fetched": orders_stats.get("rows_fetched", 0),
        "orders_new_inserted": len(new_orders),
        "orders_existing_updated": len(updated_orders),
        "order_items_fetched": item_stats.get("rows_fetched", 0),
        "order_items_new_inserted": len(new_items),
        "order_items_existing_updated": max(updated_items_count, 0),
        "modifier_items_written": 0,
        "modifier_items_skipped": modifier_stats.get("rows_fetched", 0),
    }
    return report


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--establishments", type=str, required=True, help="comma-separated establishment ids")
    args = ap.parse_args()

    ests = [int(x) for x in args.establishments.split(",")]
    all_reports = []
    for est in ests:
        all_reports.append(run_pilot(est))

    print(json.dumps(all_reports, indent=2, default=str))
