"""
backfill_order_history_v2.py
==============================
Task 12 — historical OrderHistory backfill, targeting order_history_v2 ONLY.

    orders_v2 (Task 09, already complete) -> order ids for this chunk
    -> API -> raw .json.gz (Task 06) -> sync_updated.build_order_history_row
    (canonical, unchanged parser) -> pipeline.upsert_order_history(target=
    "shadow_v2")

Live-probed before writing this script: /resources/OrderHistory/ has no
usable date-range filter of its own (same situation as OrderItem, Task 10)
-- order__in= batching against known order ids is the only reliable fetch
strategy. Probe confirmed: order__in works, pagination is correct (forced
50-per-page vs single 1000-page fetch collected identical totals), repeated
identical queries return identical ids (stable), cardinality is 0-or-1
OrderHistory rows per order in the sample checked (no >1 case observed,
consistent with Task 02's "exactly one per closed order" finding -- an order
with zero history is legitimately still open, not a fetch failure), and
field shapes exactly match sync_updated.build_order_history_row's existing
mapping (order_opened_at/order_closed_at are PosStation refs, not
timestamps -- opened/closed are the real timestamps).

Order ids come from orders_v2 (already fully backfilled by Task 09), not a
separate live fetch -- same FK-safety-by-construction as Task 10's
OrderItems backfill: every order id this script asks OrderHistory about is
one we already pulled from orders_v2 itself.

Same fresh-process-per-chunk lesson as Tasks 09/10/11: one (establishment,
month) chunk per invocation via --establishments/--start/--end, run by an
external orchestrator loop, never long-lived across multiple chunks.

Checkpoint state lives in backfill_progress (migrations/09_backfill_
progress.sql's generic table) under resource="order_history_v2" -- distinct
from every other Task's resource key. A chunk is marked 'success' only after
fetch + archive + parse + UPSERT + duplicate-check all complete inside one
transaction that commits at the very end.

order_history_v2 (migrations/12_order_history_v2.sql) has PRIMARY KEY(id)
alone, same as production order_history -- order_history never had the
orders/order_items duplicate-row identity problem, so order_history_v2
exists purely for isolation from the concurrently-running updated-mode sync
writing to production order_history, not to fix a schema bug. Never writes
to production order_history. Does not modify orders, orders_v2, order_items,
order_items_v2, payments, or payments_v2.
"""

import os
import sys
import json
import logging
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(".env")

import pipeline as P
import raw_archive
from sync_updated import build_order_history_row, fetch_order_history_for_orders
from playwright.sync_api import sync_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BACKFILL_START = date(2026, 2, 10)
CATCHUP_END_EXCLUSIVE = date(2026, 9, 1)

ESTABLISHMENTS = [6, 7, 14, 15, 20, 25, 26, 32, 36, 40, 48, 54]

RESOURCE = "order_history_v2"


def month_chunks(start_date: date, end_date_exclusive: date):
    """Same as every other Task 09-12 backfill script -- calendar-month-
    aligned (start, end_exclusive) pairs, except the FIRST chunk starts
    exactly at start_date, not month-aligned."""
    cur = start_date
    while cur < end_date_exclusive:
        nxt = date(cur.year + 1, 1, 1) if cur.month == 12 else date(cur.year, cur.month + 1, 1)
        nxt = min(nxt, end_date_exclusive)
        yield cur, nxt
        cur = nxt


def get_chunk_status(cur, est_id: int, window_start, window_end) -> str:
    cur.execute("""
        SELECT status FROM backfill_progress
        WHERE resource = %s AND establishment_id = %s AND window_start = %s AND window_end = %s
    """, (RESOURCE, est_id, window_start, window_end))
    row = cur.fetchone()
    return row[0] if row else None


def mark_in_progress(conn, est_id, window_start, window_end, run_id):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO backfill_progress
                (resource, establishment_id, window_start, window_end, run_id, status, started_at)
            VALUES (%s, %s, %s, %s, %s, 'in_progress', %s)
            ON CONFLICT (resource, establishment_id, window_start, window_end) DO UPDATE SET
                run_id = EXCLUDED.run_id, status = 'in_progress',
                started_at = EXCLUDED.started_at, error = NULL,
                completed_at = NULL
        """, (RESOURCE, est_id, window_start, window_end, run_id, datetime.now(timezone.utc)))
    conn.commit()


def mark_result(conn, est_id, window_start, window_end, status, pages=None,
                 rows_fetched=None, rows_affected=None, error=None):
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE backfill_progress SET
                status = %s, pages = %s, rows_fetched = %s, rows_affected = %s,
                completed_at = %s, error = %s
            WHERE resource = %s AND establishment_id = %s AND window_start = %s AND window_end = %s
        """, (status, pages, rows_fetched, rows_affected, datetime.now(timezone.utc), error,
              RESOURCE, est_id, window_start, window_end))
    conn.commit()


def process_chunk(context, conn, est_id: int, chunk_start: date, chunk_end: date,
                   run_id: str, force: bool = False) -> dict:
    range_from = chunk_start.strftime("%Y-%m-%dT00:00:00")
    range_to = chunk_end.strftime("%Y-%m-%dT00:00:00")
    window_start = P.parse_dt(range_from)
    window_end = P.parse_dt(range_to)

    with conn.cursor() as cur:
        existing_status = get_chunk_status(cur, est_id, window_start, window_end)
    if existing_status == "success" and not force:
        log.info("  [est=%s %s..%s] already succeeded — skipping", est_id, chunk_start, chunk_end)
        return {"skipped": True, "status": "success"}

    mark_in_progress(conn, est_id, window_start, window_end, run_id)

    # Order ids come from orders_v2 itself (Task 09, already complete for
    # this chunk) -- not a fresh Revel Order fetch. Guarantees every
    # OrderHistory row's order_id resolves against orders_v2's data.
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id FROM orders_v2
            WHERE establishment_id = %s AND created_date >= %s AND created_date < %s
        """, (est_id, window_start, window_end))
        order_ids = [r[0] for r in cur.fetchall()]

    log.info("  [est=%s %s..%s] %d orders in orders_v2 for this window",
             est_id, chunk_start, chunk_end, len(order_ids))

    if not order_ids:
        mark_result(conn, est_id, window_start, window_end, "success",
                    pages=0, rows_fetched=0, rows_affected=0)
        log.info("  [est=%s %s..%s] OK — no orders, 0 history rows", est_id, chunk_start, chunk_end)
        return {"skipped": False, "status": "success", "rows_fetched": 0, "rows_affected": 0}

    window_key = f"{chunk_start:%Y%m%d}_{chunk_end:%Y%m%d}"

    try:
        records = fetch_order_history_for_orders(
            context, order_ids, run_id, est_id, date.today(), window_key=window_key,
        )

        rows = [build_order_history_row(h) for h in records if h.get("id")]

        with conn.cursor() as cur:
            stats = P.upsert_order_history(cur, rows, target="shadow_v2")
            cur.execute("SELECT COUNT(*), COUNT(DISTINCT id) FROM order_history_v2")
            total, distinct = cur.fetchone()
            if total != distinct:
                raise RuntimeError(f"order_history_v2 duplicate ids after chunk: total={total} distinct={distinct}")

        conn.commit()

        batches = (len(order_ids) + P.BATCH_SIZE - 1) // P.BATCH_SIZE
        mark_result(conn, est_id, window_start, window_end, "success",
                    pages=batches, rows_fetched=len(records), rows_affected=stats.get("affected", 0))
        log.info("  [est=%s %s..%s] OK — orders=%d history_fetched=%d affected=%d",
                 est_id, chunk_start, chunk_end, len(order_ids), len(records), stats.get("affected", 0))
        return {"skipped": False, "status": "success", "rows_fetched": len(records),
                "rows_affected": stats.get("affected", 0)}

    except raw_archive.ArchiveError as exc:
        conn.rollback()
        mark_result(conn, est_id, window_start, window_end, "failed", error=f"ArchiveError: {exc}")
        log.error("  [est=%s %s..%s] ARCHIVE FAILURE — aborting chunk: %s", est_id, chunk_start, chunk_end, exc)
        raise
    except Exception as exc:
        conn.rollback()
        mark_result(conn, est_id, window_start, window_end, "failed", error=str(exc)[:2000])
        log.error("  [est=%s %s..%s] FAILED: %s", est_id, chunk_start, chunk_end, exc)
        raise


def run_backfill(establishments: list, start_date: date, end_date_exclusive: date,
                  force: bool = False) -> dict:
    conn = P.db_connect()
    run_id = raw_archive.new_run_id()
    chunks = list(month_chunks(start_date, end_date_exclusive))
    log.info("=== Task 12 OrderHistory backfill — run_id=%s — %d establishment(s) x %d chunk(s) ===",
              run_id, len(establishments), len(chunks))

    report = {"run_id": run_id, "by_establishment": {}}
    started = datetime.now(timezone.utc)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state="/tmp/revel_session.json")

        for est_id in establishments:
            est_report = {"chunks": [], "rows_fetched": 0, "rows_affected": 0, "failed": 0, "skipped": 0}
            for chunk_start, chunk_end in chunks:
                try:
                    result = process_chunk(context, conn, est_id, chunk_start, chunk_end, run_id, force=force)
                except Exception as exc:
                    est_report["failed"] += 1
                    est_report["chunks"].append({"window": [str(chunk_start), str(chunk_end)], "status": "failed", "error": str(exc)})
                    continue
                if result.get("skipped"):
                    est_report["skipped"] += 1
                else:
                    est_report["rows_fetched"] += result.get("rows_fetched", 0)
                    est_report["rows_affected"] += result.get("rows_affected", 0)
                est_report["chunks"].append({
                    "window": [str(chunk_start), str(chunk_end)],
                    "status": result.get("status"),
                    "rows_fetched": result.get("rows_fetched"),
                    "rows_affected": result.get("rows_affected"),
                })
            report["by_establishment"][est_id] = est_report

        browser.close()

    conn.close()
    report["total_seconds"] = (datetime.now(timezone.utc) - started).total_seconds()
    return report


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--establishments", type=str, default=None, help="comma-separated establishment ids; default all")
    ap.add_argument("--start", type=str, default=None, help="YYYY-MM-DD, default BACKFILL_START")
    ap.add_argument("--end", type=str, default=None, help="YYYY-MM-DD exclusive, default CATCHUP_END_EXCLUSIVE")
    ap.add_argument("--force", action="store_true", help="re-process chunks already marked success")
    args = ap.parse_args()

    ests = [int(x) for x in args.establishments.split(",")] if args.establishments else ESTABLISHMENTS
    start = date.fromisoformat(args.start) if args.start else BACKFILL_START
    end = date.fromisoformat(args.end) if args.end else CATCHUP_END_EXCLUSIVE

    r = run_backfill(ests, start, end, force=args.force)
    print(json.dumps(r, indent=2, default=str))
