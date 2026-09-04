"""
catchup_shadow_v2.py
=====================
Task 15 — ONE coordinated final shadow freshness pass, superseding the
narrower catchup_orders_v2.py. Per establishment, sequentially:

    A. Orders            -- sync_orders(target="shadow_v2")
    B. OrderItems + ModifierItems -- sync_order_items_and_modifiers(
                             target="shadow_v2", skip_modifiers=False)
    C. OrderHistory       -- sync_order_history(target="shadow_v2"),
                             same touched_order_ids as B
    D. Payments           -- sync_payments(target="shadow_v2"),
                             its own updated_date window/watermark

All four write ONLY to orders_v2/order_items_v2/modifier_items_v2/
order_history_v2/payments_v2 -- verified by the isolation tests in
test_shadow_isolation.py (Phase 1). Never calls the production entrypoint
(run_establishment_updated) or any function without target= explicitly
passed. Sequential, one establishment at a time, one Playwright context, no
parallelism -- same pattern as catchup_orders_v2.py and every Task 09-13
script.

Affected business dates (America/Chicago, matching aggregate_features.py's
own conversion) are collected and reported per establishment for visibility
-- NOT queued or acted on. The dirty-date recompute queue is designed
(Task 15 remediation plan) but explicitly not implemented yet; this script
does not create, write to, or depend on it.
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
    with conn.cursor() as cur:
        out = {}
        for t in ["orders_v2", "order_items_v2", "payments_v2", "order_history_v2", "modifier_items_v2"]:
            cur.execute(f"SELECT count(*), count(DISTINCT id) FROM {t}")
            total, distinct = cur.fetchone()
            out[t] = {"total": total, "distinct": distinct, "dupes": total - distinct}

        cur.execute("""
            SELECT count(*) FROM order_items_v2 oi
            LEFT JOIN orders_v2 o ON o.id = oi.order_id WHERE o.id IS NULL
        """)
        out["order_items_v2_orphans"] = cur.fetchone()[0]

        cur.execute("""
            SELECT count(*) FROM order_history_v2 h
            LEFT JOIN orders_v2 o ON o.id = h.order_id WHERE o.id IS NULL
        """)
        out["order_history_v2_orphans"] = cur.fetchone()[0]

        cur.execute("""
            SELECT count(*) FROM modifier_items_v2 m
            LEFT JOIN order_items_v2 oi ON oi.id = m.order_item_id WHERE oi.id IS NULL
        """)
        out["modifier_items_v2_item_orphans"] = cur.fetchone()[0]

        cur.execute("""
            SELECT count(*) FROM modifier_items_v2 m
            LEFT JOIN orders_v2 o ON o.id = m.order_id WHERE o.id IS NULL
        """)
        out["modifier_items_v2_order_orphans"] = cur.fetchone()[0]

        cur.execute("""
            SELECT count(*) FROM payments_v2 p
            LEFT JOIN orders_v2 o ON o.id = p.order_id WHERE o.id IS NULL
        """)
        out["payments_v2_orphans"] = cur.fetchone()[0]

        cur.execute("SELECT id FROM orders_v2 WHERE establishment_id=%s", (est_id,))
        out["_order_ids_before"] = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT id FROM order_items_v2 WHERE establishment_id=%s", (est_id,))
        out["_item_ids_before"] = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT id FROM payments_v2 WHERE establishment_id=%s", (est_id,))
        out["_payment_ids_before"] = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT h.id FROM order_history_v2 h JOIN orders_v2 o ON o.id=h.order_id WHERE o.establishment_id=%s", (est_id,))
        out["_history_ids_before"] = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT id FROM modifier_items_v2 WHERE establishment_id=%s", (est_id,))
        out["_modifier_ids_before"] = {r[0] for r in cur.fetchall()}
    return out


def affected_business_dates(conn, order_ids: list) -> list:
    """Report-only: which (Central) business dates the touched orders belong
    to, for visibility -- not queued anywhere."""
    if not order_ids:
        return []
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT DATE(created_date AT TIME ZONE 'America/Chicago')
            FROM orders_v2 WHERE id = ANY(%s) ORDER BY 1
        """, (order_ids,))
        return [r[0] for r in cur.fetchall()]


def run_pass(est_id: int, modifier_cache: dict) -> dict:
    run_id = raw_archive.new_run_id()
    conn = P.db_connect()

    log.info("=== coordinated shadow freshness pass — est=%s run_id=%s ===", est_id, run_id)

    before = snapshot_state(conn, est_id)
    order_ids_before = before.pop("_order_ids_before")
    item_ids_before = before.pop("_item_ids_before")
    payment_ids_before = before.pop("_payment_ids_before")
    history_ids_before = before.pop("_history_ids_before")
    modifier_ids_before = before.pop("_modifier_ids_before")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state="/tmp/revel_session.json")

        # A. Orders
        touched_order_ids, orders_stats = S.sync_orders(context, conn, est_id, run_id, target="shadow_v2")
        log.info("A. orders: touched=%d stats=%s", len(touched_order_ids), orders_stats)

        dates = affected_business_dates(conn, touched_order_ids)
        log.info("   affected business dates (Central), report-only: %s", dates)

        # B. OrderItems + ModifierItems (skip_modifiers=False this run)
        item_stats, modifier_stats = S.sync_order_items_and_modifiers(
            context, conn, est_id, run_id, touched_order_ids, modifier_cache,
            target="shadow_v2", skip_modifiers=False,
        )
        log.info("B. order_items: %s  modifier_items: %s", item_stats, modifier_stats)

        # C. OrderHistory -- same touched_order_ids as B
        history_stats = S.sync_order_history(context, conn, est_id, run_id, touched_order_ids, target="shadow_v2")
        log.info("C. order_history: %s", history_stats)

        # D. Payments -- its own updated_date window/watermark
        payment_stats = S.sync_payments(context, conn, est_id, run_id, target="shadow_v2")
        log.info("D. payments: %s", payment_stats)

        browser.close()

    after = snapshot_state(conn, est_id)
    order_ids_after = after.pop("_order_ids_before")
    item_ids_after = after.pop("_item_ids_before")
    payment_ids_after = after.pop("_payment_ids_before")
    history_ids_after = after.pop("_history_ids_before")
    modifier_ids_after = after.pop("_modifier_ids_before")

    conn.close()

    # NOTE: "existing_updated" for every resource is fetched-minus-new, not a
    # before/after snapshot intersection -- the latter (order_history_ids_after
    # & order_history_ids_before, etc.) counts almost the ENTIRE pre-existing
    # table (nothing gets deleted between snapshots), not what this specific
    # run touched. Caught during the est=40 pilot (payments/order_history
    # reported ~101K/~104K "updated" against a ~2K fetch). new_inserted is
    # still a genuine before/after set-difference (correct either way); only
    # existing_updated needed the fetched-minus-new fix.
    new_orders = order_ids_after - order_ids_before
    updated_orders = set(touched_order_ids) & order_ids_before
    new_items = item_ids_after - item_ids_before
    new_payments = payment_ids_after - payment_ids_before
    new_history = history_ids_after - history_ids_before
    new_modifiers = modifier_ids_after - modifier_ids_before

    return {
        "run_id": run_id,
        "establishment_id": est_id,
        "before": before,
        "after": after,
        "affected_business_dates": [str(d) for d in dates],
        "orders": {
            "fetched": orders_stats.get("rows_fetched", 0),
            "new_inserted": len(new_orders),
            "existing_updated": len(updated_orders),
        },
        "order_items": {
            "fetched": item_stats.get("rows_fetched", 0),
            "new_inserted": len(new_items),
            "existing_updated": item_stats.get("rows_fetched", 0) - len(new_items),
        },
        "modifier_items": {
            "extracted": modifier_stats.get("rows_fetched", 0),
            "new_inserted": len(new_modifiers),
            "no_op_or_duplicate": modifier_stats.get("rows_fetched", 0) - len(new_modifiers),
        },
        "order_history": {
            "fetched": history_stats.get("rows_fetched", 0),
            "new_inserted": len(new_history),
            "existing_updated": history_stats.get("rows_fetched", 0) - len(new_history),
        },
        "payments": {
            "fetched": payment_stats.get("rows_fetched", 0),
            "new_inserted": len(new_payments),
            "existing_updated": payment_stats.get("rows_fetched", 0) - len(new_payments),
        },
    }


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--establishments", type=str, required=True, help="comma-separated establishment ids")
    args = ap.parse_args()

    ests = [int(x) for x in args.establishments.split(",")]
    conn0 = P.db_connect()
    with conn0.cursor() as cur:
        cur.execute("SELECT id, name FROM modifiers")
        modifier_cache = dict(cur.fetchall())
    conn0.close()
    log.info("Loaded %d modifier names", len(modifier_cache))

    all_reports = []
    for est in ests:
        all_reports.append(run_pass(est, modifier_cache))

    print(json.dumps(all_reports, indent=2, default=str))
