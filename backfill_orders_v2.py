"""
backfill_orders_v2.py
======================
Task 09 — historical Orders backfill, targeting orders_v2 ONLY.

    API -> raw .json.gz (Task 06) -> pipeline.build_order_row (canonical,
    unchanged parser) -> pipeline.upsert_orders(target="shadow_v2")

Historical discovery uses created_date windows (we need every Order
originally CREATED in each historical period — updated_date windows, used
by the ongoing daily sync, only find later MUTATIONS and would miss an
order nobody has touched since). This is deliberately a different strategy
from sync_updated.py's updated-mode sync, serving a different purpose —
see the module docstring there. Neither replaces the other.

Chunked by establishment x calendar month (first chunk starts exactly at
BACKFILL_START, not month-aligned). Each chunk is independently
restartable: checkpoint state lives in backfill_progress (migrations/
09_backfill_progress.sql), separate from sync_state. A chunk is marked
'success' only after fetch + archive + parse + UPSERT + duplicate-check
all complete inside one transaction that commits at the very end — a
failure at any point rolls the whole chunk back and marks it 'failed',
never partially applied.

orders_v2 has PRIMARY KEY(id) alone (Task 07.3) — created_date is a normal
mutable field, never part of the conflict key. If the same Order id shows
up in a later chunk (or a later run of the SAME chunk) with a different
created_date, pipeline.upsert_orders(target="shadow_v2") UPDATEs the
existing row via ON CONFLICT (id) DO UPDATE. This is the entire point of
using orders_v2 instead of the old orders table for this backfill.

Timezone: window boundaries are constructed as naive America/Chicago
calendar strings directly (e.g. "2026-06-01T00:00:00"), matching Revel's
own filter convention (verified live, Task 02/07) and pipeline.py's
existing default daily created-mode fetch. No fixed UTC offset is ever
used. Storage in backfill_progress converts through pipeline.parse_dt
(the one canonical Chicago-local -> UTC converter) for consistency, not
because the query itself needs UTC.
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
# "current catch-up boundary": today. No orders exist beyond "now", so the
# last (partial) month chunk naturally stops there -- no special-casing
# needed for the in-progress current month.
CATCHUP_END_EXCLUSIVE = date(2026, 9, 1)

ESTABLISHMENTS = [6, 7, 14, 15, 20, 25, 26, 32, 36, 40, 48, 54]

RESOURCE = "orders_v2"


def month_chunks(start_date: date, end_date_exclusive: date):
    """Calendar-month-aligned (start, end_exclusive) date pairs, except the
    FIRST chunk starts exactly at start_date (2026-02-10), not month-aligned."""
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
    log.info("  [est=%s %s..%s] fetching (Central %s -> %s)", est_id, chunk_start, chunk_end, range_from, range_to)

    # Deterministic, derived from this chunk's own window_start/window_end
    # (not a sequential counter) -- see raw_archive.archive_response's
    # window_key docstring for why: every establishment x month chunk in
    # this backfill shares run_id AND archive_date=today() (today's real
    # date has nothing to do with which historical period each chunk
    # covers), so without this, March's page 1 archive collides with
    # February's.
    window_key = f"{chunk_start:%Y%m%d}_{chunk_end:%Y%m%d}"

    try:
        records = P.fetch_all_pages(
            context,
            endpoint=f"{P.BASE_URL}/resources/Order/",
            params={"establishment": est_id, "created_date__gte": range_from, "created_date__lt": range_to},
            label=f"orders backfill est={est_id} {chunk_start}",
            resource="orders", run_id=run_id, establishment_id=est_id,
            date_window=(range_from, range_to), archive_date=date.today(),
            window_key=window_key,
        )

        today = date.today()
        rows = []
        for o in records:
            if not o.get("id"):
                continue
            if P.parse_dt(o.get("created_date")) is None:
                continue
            rows.append(P.build_order_row(o, est_id, today))

        with conn.cursor() as cur:
            stats = P.upsert_orders(cur, rows, target="shadow_v2")
            # Duplicate check requested explicitly (Task 09 req. 5) -- orders_v2's
            # PRIMARY KEY(id) already makes this structurally impossible, but we
            # verify and report it per chunk anyway, in the same transaction as
            # the UPSERT so a violation (which should never happen) would abort
            # the commit rather than silently pass.
            cur.execute("SELECT COUNT(*), COUNT(DISTINCT id) FROM orders_v2")
            total, distinct = cur.fetchone()
            if total != distinct:
                raise RuntimeError(f"orders_v2 duplicate ids after chunk: total={total} distinct={distinct}")

        conn.commit()

        pages = (len(records) + 999) // 1000 if records else 0  # matches fetch_all_pages' limit=1000/page
        mark_result(conn, est_id, window_start, window_end, "success",
                    pages=pages, rows_fetched=len(records), rows_affected=stats.get("affected", 0))
        log.info("  [est=%s %s..%s] OK — fetched=%d affected=%d",
                 est_id, chunk_start, chunk_end, len(records), stats.get("affected", 0))
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
    log.info("=== Task 09 Orders backfill — run_id=%s — %d establishment(s) x %d chunk(s) ===",
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
