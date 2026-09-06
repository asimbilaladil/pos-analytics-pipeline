"""
sync_loyalty_labor.py
=====================
Daily incremental sync for order_loyalty_v2 and timesheet_entries_v2.

DISCOVERY AXIS -- updated_date, NOT created_date
------------------------------------------------
The historical backfills use created_date/clock_in, because an updated-date
window only finds later mutations and would miss a record nobody has touched
since creation. The daily job has the opposite requirement: it must catch
mutations to records it has already seen, so it uses updated_date.

WHY A 48-HOUR OVERLAP
---------------------
The existing cron's `TZ=America/Chicago` directive is NOT honoured on this host
(the 0 9 * * * entry runs at 09:00 UTC = 04:00 Chicago), and a DST shift moves
the effective boundary again. A 24h window with a drifting boundary can gap; a
48h window absorbs both. Overlap is free because every write is
ON CONFLICT ... DO UPDATE, so re-seeing a record rewrites identical values.

For labour the overlap matters twice over: clock_out is NULL while a shift is
open, so an overnight shift is first stored with NULL hours and only corrected
once it closes and its updated_date moves.

LOYALTY PII RULE CARRIES OVER
-----------------------------
Order.gift_reward_data embeds plaintext PII inside the same JSON value as the
loyalty fields. This path passes resource=None to fetch_all_pages, disabling
raw archiving entirely, exactly as the backfill does. Timesheets DO archive --
`remarks` is excluded at the request layer so the body carries no free text.

CHECKPOINTS
-----------
Daily runs record under their own resource names (order_loyalty_v2_daily,
timesheet_entries_v2_daily) so a daily failure never marks a historical chunk
failed, and --force on a backfill never replays daily windows.
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(".env")

import pipeline as P
import raw_archive
import backfill_loyalty_v2 as BL
import backfill_timesheets_v2 as BT
from playwright.sync_api import sync_playwright
from psycopg2.extras import execute_values

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

CHICAGO = ZoneInfo("America/Chicago")
OVERLAP_HOURS = 48
ESTABLISHMENTS = BL.ESTABLISHMENTS


def _window(overlap_hours: int):
    """(since, until) as Chicago-naive strings, which is what Revel expects."""
    now_local = datetime.now(CHICAGO)
    since = now_local - timedelta(hours=overlap_hours)
    return (since.strftime("%Y-%m-%dT%H:%M:%S"),
            now_local.strftime("%Y-%m-%dT%H:%M:%S"))


def _mark(conn, resource, est_id, since, until, status, fetched=None,
          affected=None, error=None):
    ws = P.parse_dt(since)
    we = P.parse_dt(until)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO backfill_progress
                (resource, establishment_id, window_start, window_end, run_id,
                 status, started_at, completed_at, rows_fetched, rows_affected, error)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (resource, establishment_id, window_start, window_end) DO UPDATE SET
                status=EXCLUDED.status, completed_at=EXCLUDED.completed_at,
                rows_fetched=EXCLUDED.rows_fetched, rows_affected=EXCLUDED.rows_affected,
                error=EXCLUDED.error
        """, (resource, est_id, ws, we, _RUN_ID, status,
              datetime.now(timezone.utc), datetime.now(timezone.utc),
              fetched, affected, error))
    conn.commit()


_RUN_ID = None


def sync_loyalty(context, conn, since, until):
    total_fetched = total_affected = total_payload = failures = 0
    for est in ESTABLISHMENTS:
        try:
            records = P.fetch_all_pages(
                context,
                endpoint=f"{P.BASE_URL}/resources/Order/",
                params={"establishment": est,
                        "updated_date__gte": since, "updated_date__lt": until,
                        "fields": BL.ORDER_FIELDS},
                label=f"loyalty sync est={est}",
                resource=None,   # DELIBERATE: no raw archive, payload is PII-bearing
            )
            rows = BL.build_rows(records, est)
            del records
            with conn.cursor() as cur:
                execute_values(cur, BL.UPSERT, rows, page_size=1000)
                affected = cur.rowcount
            conn.commit()
            payload = sum(1 for r in rows if r[4])
            total_fetched += len(rows); total_affected += affected; total_payload += payload
            _mark(conn, "order_loyalty_v2_daily", est, since, until, "success",
                  len(rows), affected)
            log.info("  loyalty est=%s orders=%d with_payload=%d", est, len(rows), payload)
        except Exception as exc:
            conn.rollback(); failures += 1
            _mark(conn, "order_loyalty_v2_daily", est, since, until, "failed",
                  error=str(exc)[:2000])
            log.error("  loyalty est=%s FAILED: %s", est, exc)
    return {"fetched": total_fetched, "affected": total_affected,
            "with_payload": total_payload, "failures": failures}


def sync_labor(context, conn, since, until, run_id):
    total_fetched = total_affected = failures = 0
    for est in ESTABLISHMENTS:
        try:
            records = P.fetch_all_pages(
                context,
                endpoint=f"{P.BASE_URL}/resources/TimeSheetEntry/",
                params={"establishment": est,
                        "updated_date__gte": since, "updated_date__lt": until,
                        "fields": BT.TS_FIELDS},
                label=f"labor sync est={est}",
                # Safe to archive: `remarks` is excluded at the request layer.
                resource="timesheets", run_id=run_id, establishment_id=est,
                date_window=(since, until),
                archive_date=datetime.now(CHICAGO).date(),
                window_key=f"daily_{since[:10].replace('-','')}",
            )
            rows = [BT.build_row(r, est) for r in records if r.get("id")]
            with conn.cursor() as cur:
                execute_values(cur, BT.UPSERT, rows, page_size=1000)
                affected = cur.rowcount
            conn.commit()
            total_fetched += len(rows); total_affected += affected
            _mark(conn, "timesheet_entries_v2_daily", est, since, until, "success",
                  len(rows), affected)
            log.info("  labor est=%s entries=%d", est, len(rows))
        except Exception as exc:
            conn.rollback(); failures += 1
            _mark(conn, "timesheet_entries_v2_daily", est, since, until, "failed",
                  error=str(exc)[:2000])
            log.error("  labor est=%s FAILED: %s", est, exc)
    return {"fetched": total_fetched, "affected": total_affected, "failures": failures}


def main():
    global _RUN_ID
    ap = argparse.ArgumentParser(description="Daily loyalty + labour incremental sync")
    ap.add_argument("--overlap-hours", type=int, default=OVERLAP_HOURS)
    ap.add_argument("--only", choices=["loyalty", "labor"], default=None)
    a = ap.parse_args()

    since, until = _window(a.overlap_hours)
    _RUN_ID = raw_archive.new_run_id()
    log.info("=== loyalty/labour incremental sync run_id=%s ===", _RUN_ID)
    log.info("    updated_date window (Chicago): %s .. %s (%dh overlap)",
             since, until, a.overlap_hours)

    conn = P.db_connect()
    started = datetime.now(timezone.utc)
    loy = lab = None
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state="/tmp/revel_session.json")
        try:
            if a.only in (None, "labor"):
                log.info("--- labour ---")
                lab = sync_labor(context, conn, since, until, _RUN_ID)
            if a.only in (None, "loyalty"):
                log.info("--- loyalty ---")
                loy = sync_loyalty(context, conn, since, until)
        finally:
            browser.close()
    conn.close()

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    if lab:
        log.info("labour : entries=%d affected=%d failures=%d",
                 lab["fetched"], lab["affected"], lab["failures"])
    if loy:
        log.info("loyalty: orders=%d with_payload=%d affected=%d failures=%d",
                 loy["fetched"], loy["with_payload"], loy["affected"], loy["failures"])
    log.info("DONE in %.1fs", elapsed)

    # A failed establishment must alert rather than quietly retry tomorrow: a
    # 48h window will not reach back far enough to self-heal after two
    # consecutive failures.
    failures = (lab["failures"] if lab else 0) + (loy["failures"] if loy else 0)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
