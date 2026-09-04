"""
backfill_order_items_v2.py
===========================
Task 10 — historical OrderItems backfill, targeting order_items_v2 ONLY.

    orders_v2 (Task 09, already complete) -> order ids for this chunk
    -> API -> raw .json.gz (Task 06) -> pipeline.build_item_row (canonical,
    unchanged parser) -> pipeline.upsert_order_items(target="shadow_v2")

OrderItems have no reliable establishment/date filter of their own (the
OrderItem endpoint's establishment= filter silently returns all locations —
Task 06 finding, still true). The only reliable fetch strategy is
order__in= batching against a known list of order ids (pipeline.
fetch_items_for_orders, BATCH_SIZE=200). So instead of an independent
created_date-window fetch against Revel (what backfill_orders_v2.py does for
Orders), this script fetches order ids for the chunk directly from orders_v2
— which Task 09 already backfilled completely for every establishment/month
in scope — then asks Revel for exactly those orders' items. This also makes
order_items_v2's order_id -> orders_v2.id FK impossible to violate: every
item fetched belongs to an order id we already pulled from orders_v2 itself.

Same fresh-process-per-chunk lesson as Task 09: this script is invoked once
per (establishment, month) via --establishments/--start/--end, run by an
external orchestrator loop, never long-lived across multiple chunks. A
month with a large order count (e.g. est=40 March: 37,855 orders, ~135k
items) is exactly the case Task 09's OOM kills warned about — one process,
one chunk, then exit, so the OS reclaims everything before the next chunk.

Checkpoint state lives in the same backfill_progress table as Task 09
(migrations/09_backfill_progress.sql), under resource="order_items_v2" — a
distinct resource key from Task 09's "orders_v2", so the two backfills'
progress never collides or is mistaken for each other. A chunk is marked
'success' only after fetch + archive + parse + UPSERT + duplicate-check all
complete inside one transaction that commits at the very end; a failure at
any point rolls the whole chunk back and marks it 'failed', never partially
applied.

order_items_v2 has PRIMARY KEY(id) alone (Task 07.3), same as orders_v2 —
created_date is a normal mutable field. If the same item id shows up again
(a later chunk, or a rerun of the same chunk), upsert_order_items(target=
"shadow_v2") UPDATEs the existing row via ON CONFLICT (id) DO UPDATE, making
every chunk safely re-runnable (idempotent).

Modifiers are explicitly out of scope — there is no modifier_items_v2 (Task
07.4), so this script never fetches or writes modifier data, only OrderItem
rows. Does NOT modify order_items, orders, orders_v2, or any other table.

Timezone: window boundaries are constructed as naive America/Chicago
calendar strings, matching Task 09's convention, then converted through
pipeline.parse_dt for both the orders_v2 lookup and backfill_progress
storage.
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
from playwright.sync_api import sync_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BACKFILL_START = date(2026, 2, 10)
CATCHUP_END_EXCLUSIVE = date(2026, 9, 1)

ESTABLISHMENTS = [6, 7, 14, 15, 20, 25, 26, 32, 36, 40, 48, 54]

RESOURCE = "order_items_v2"


def month_chunks(start_date: date, end_date_exclusive: date):
    """Same as backfill_orders_v2.month_chunks — calendar-month-aligned
    (start, end_exclusive) pairs, except the FIRST chunk starts exactly at
    start_date, not month-aligned."""
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
    # this chunk) -- not a fresh Revel Order fetch. Guarantees every item's
    # order_id resolves against orders_v2's FK by construction.
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
        log.info("  [est=%s %s..%s] OK — no orders, 0 items", est_id, chunk_start, chunk_end)
        return {"skipped": False, "status": "success", "rows_fetched": 0, "rows_affected": 0}

    window_key = f"{chunk_start:%Y%m%d}_{chunk_end:%Y%m%d}"

    try:
        items = P.fetch_items_for_orders(
            context, order_ids, run_id=run_id, establishment_id=est_id,
            archive_date=date.today(), window_key=window_key,
        )

        today = date.today()
        rows = []
        for item in items:
            if not item.get("id"):
                continue
            created = P.parse_dt(item.get("created_date"))
            row = P.build_item_row(item, est_id, today, created or datetime.now(timezone.utc))
            rows.append(row)

        with conn.cursor() as cur:
            stats = P.upsert_order_items(cur, rows, target="shadow_v2")
            # Duplicate check requested explicitly (Task 09 precedent) --
            # order_items_v2's PRIMARY KEY(id) already makes this structurally
            # impossible, but verify and report per chunk anyway, in the same
            # transaction as the UPSERT so a violation would abort the commit
            # rather than silently pass.
            cur.execute("SELECT COUNT(*), COUNT(DISTINCT id) FROM order_items_v2")
            total, distinct = cur.fetchone()
            if total != distinct:
                raise RuntimeError(f"order_items_v2 duplicate ids after chunk: total={total} distinct={distinct}")

        conn.commit()

        # fetch_items_for_orders paginates 1000/page WITHIN each 200-order
        # batch; page count isn't tracked per-chunk by that function, so
        # report batch count (a meaningful, comparable proxy) instead.
        batches = (len(order_ids) + P.BATCH_SIZE - 1) // P.BATCH_SIZE
        mark_result(conn, est_id, window_start, window_end, "success",
                    pages=batches, rows_fetched=len(items), rows_affected=stats.get("affected", 0))
        log.info("  [est=%s %s..%s] OK — orders=%d items_fetched=%d affected=%d",
                 est_id, chunk_start, chunk_end, len(order_ids), len(items), stats.get("affected", 0))
        return {"skipped": False, "status": "success", "rows_fetched": len(items),
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
    log.info("=== Task 10 OrderItems backfill — run_id=%s — %d establishment(s) x %d chunk(s) ===",
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
