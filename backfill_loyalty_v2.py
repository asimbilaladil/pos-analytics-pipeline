"""
backfill_loyalty_v2.py
======================
Populates order_loyalty_v2 (migration 35) from Order.gift_reward_data.

PII HANDLING -- THE CENTRAL CONSTRAINT
--------------------------------------
gift_reward_data carries plaintext customerName, firstName, lastName,
phoneNumber and birthday in the SAME JSON value as the loyalty fields, so
Revel's `fields=` parameter cannot strip them: the PII is inside the value,
not in sibling fields.

This script therefore does three things differently from every other backfill
in this repo:

  1. It passes resource=None to fetch_all_pages, which DISABLES raw archiving.
     No response body touches disk. The other backfills archive every raw
     body by design; doing that here would write PII to /var/lib/laynes and
     recreate the exposure this work exists to stop.
  2. It never logs a record. Log lines carry counts only.
  3. The payload is handed straight to loyalty_extract.extract(), which
     returns only safe scalars, and the parsed structure goes out of scope
     immediately. Nothing but those scalars reaches the database.

Discovery uses created_date windows (not updated_date) for the same reason as
the other historical backfills: an updated-date window only finds later
mutations and would miss an order nobody has touched since creation.

Chunked by establishment x calendar month, checkpointed in backfill_progress
(resource="order_loyalty_v2"), idempotent via UPSERT on order_id, and
resumable -- a chunk already marked 'success' is skipped unless --force.

Writes ONLY to order_loyalty_v2. Never touches orders_v2, order_items_v2,
payments_v2 or any other existing table.
"""

import os
import sys
import argparse
import logging
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(".env")

import pipeline as P
import raw_archive
import loyalty_extract
from playwright.sync_api import sync_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BACKFILL_START = date(2026, 1, 1)
ESTABLISHMENTS = [6, 7, 14, 15, 20, 25, 26, 32, 36, 40, 48, 54]
RESOURCE = "order_loyalty_v2"

# Only these leave the API. gift_reward_data still arrives whole -- see above --
# but nothing else unnecessary does.
ORDER_FIELDS = "id,establishment,created_date,updated_date,gift_reward_data"


def month_chunks(start_date: date, end_date_exclusive: date):
    cur = start_date
    while cur < end_date_exclusive:
        nxt = date(cur.year + 1, 1, 1) if cur.month == 12 else date(cur.year, cur.month + 1, 1)
        nxt = min(nxt, end_date_exclusive)
        yield cur, nxt
        cur = nxt


def _est_id(uri):
    if isinstance(uri, str) and uri.rstrip("/").rsplit("/", 1)[-1].isdigit():
        return int(uri.rstrip("/").rsplit("/", 1)[-1])
    return uri if isinstance(uri, int) else None


def build_rows(records, est_id):
    """Reduce raw Order records to safe loyalty rows. The raw payload is read
    exactly once, here, and never returned."""
    rows = []
    for rec in records:
        oid = rec.get("id")
        if not oid:
            continue
        safe = loyalty_extract.extract(rec.get("gift_reward_data"))
        rows.append((
            oid,
            _est_id(rec.get("establishment")) or est_id,
            P.parse_dt(rec.get("created_date")) if rec.get("created_date") else None,
            P.parse_dt(rec.get("updated_date")) if rec.get("updated_date") else None,
            safe["has_loyalty_payload"], safe["loyalty_registered"],
            safe["has_applied_reward"], safe["applied_rewards_count"],
            safe["total_points_snapshot"], safe["has_reward_card"],
            safe["loyalty_key_hash"],
        ))
    return rows


UPSERT = """
INSERT INTO order_loyalty_v2 (
    order_id, establishment_id, order_created_date, source_updated_date,
    has_loyalty_payload, loyalty_registered, has_applied_reward,
    applied_rewards_count, total_points_snapshot, has_reward_card,
    loyalty_key_hash)
VALUES %s
ON CONFLICT (order_id) DO UPDATE SET
    establishment_id      = EXCLUDED.establishment_id,
    order_created_date    = EXCLUDED.order_created_date,
    source_updated_date   = EXCLUDED.source_updated_date,
    has_loyalty_payload   = EXCLUDED.has_loyalty_payload,
    loyalty_registered    = EXCLUDED.loyalty_registered,
    has_applied_reward    = EXCLUDED.has_applied_reward,
    applied_rewards_count = EXCLUDED.applied_rewards_count,
    total_points_snapshot = EXCLUDED.total_points_snapshot,
    has_reward_card       = EXCLUDED.has_reward_card,
    loyalty_key_hash      = EXCLUDED.loyalty_key_hash,
    extracted_at          = now()
"""


def get_chunk_status(cur, est_id, window_start, window_end):
    cur.execute("""SELECT status FROM backfill_progress
                   WHERE resource=%s AND establishment_id=%s
                     AND window_start=%s AND window_end=%s""",
                (RESOURCE, est_id, window_start, window_end))
    row = cur.fetchone()
    return row[0] if row else None


def mark_in_progress(conn, est_id, ws, we, run_id):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO backfill_progress
                (resource, establishment_id, window_start, window_end, run_id, status, started_at)
            VALUES (%s,%s,%s,%s,%s,'in_progress',%s)
            ON CONFLICT (resource, establishment_id, window_start, window_end) DO UPDATE SET
                run_id=EXCLUDED.run_id, status='in_progress',
                started_at=EXCLUDED.started_at, error=NULL, completed_at=NULL
        """, (RESOURCE, est_id, ws, we, run_id, datetime.now(timezone.utc)))
    conn.commit()


def mark_result(conn, est_id, ws, we, status, pages=None, rows_fetched=None,
                rows_affected=None, error=None):
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE backfill_progress SET status=%s, pages=%s, rows_fetched=%s,
                   rows_affected=%s, completed_at=%s, error=%s
            WHERE resource=%s AND establishment_id=%s AND window_start=%s AND window_end=%s
        """, (status, pages, rows_fetched, rows_affected, datetime.now(timezone.utc),
              error, RESOURCE, est_id, ws, we))
    conn.commit()


def process_chunk(context, conn, est_id, chunk_start, chunk_end, run_id, force=False):
    from psycopg2.extras import execute_values

    range_from = chunk_start.strftime("%Y-%m-%dT00:00:00")
    range_to = chunk_end.strftime("%Y-%m-%dT00:00:00")
    ws, we = P.parse_dt(range_from), P.parse_dt(range_to)

    with conn.cursor() as cur:
        if get_chunk_status(cur, est_id, ws, we) == "success" and not force:
            log.info("  [est=%s %s..%s] already succeeded - skipping", est_id, chunk_start, chunk_end)
            return {"skipped": True, "status": "success"}

    mark_in_progress(conn, est_id, ws, we, run_id)
    log.info("  [est=%s %s..%s] fetching (no raw archive - payload is PII-bearing)",
             est_id, chunk_start, chunk_end)

    try:
        records = P.fetch_all_pages(
            context,
            endpoint=f"{P.BASE_URL}/resources/Order/",
            params={"establishment": est_id, "created_date__gte": range_from,
                    "created_date__lt": range_to, "fields": ORDER_FIELDS},
            label=f"loyalty est={est_id} {chunk_start}",
            resource=None,   # DELIBERATE: disables raw archiving. See module docstring.
        )

        rows = build_rows(records, est_id)
        del records   # drop every raw payload before touching the database

        with conn.cursor() as cur:
            execute_values(cur, UPSERT, rows, page_size=1000)
            affected = cur.rowcount
        conn.commit()

        with_payload = sum(1 for r in rows if r[4])
        mark_result(conn, est_id, ws, we, "success",
                    pages=(len(rows) + 999) // 1000, rows_fetched=len(rows),
                    rows_affected=affected)
        log.info("  [est=%s %s..%s] OK - orders=%d with_loyalty_payload=%d",
                 est_id, chunk_start, chunk_end, len(rows), with_payload)
        return {"skipped": False, "status": "success", "rows_fetched": len(rows),
                "rows_affected": affected, "with_payload": with_payload}

    except Exception as exc:
        conn.rollback()
        # str(exc) only -- never the record that caused it.
        mark_result(conn, est_id, ws, we, "failed", error=str(exc)[:2000])
        log.error("  [est=%s %s..%s] FAILED: %s", est_id, chunk_start, chunk_end, exc)
        raise


def run_backfill(establishments, start_date, end_date_exclusive, force=False):
    conn = P.db_connect()
    run_id = raw_archive.new_run_id()
    chunks = list(month_chunks(start_date, end_date_exclusive))
    log.info("=== Loyalty backfill run_id=%s - %d est x %d chunk(s) ===",
             run_id, len(establishments), len(chunks))

    report = {"run_id": run_id, "by_establishment": {}}
    started = datetime.now(timezone.utc)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state="/tmp/revel_session.json")
        try:
            for est_id in establishments:
                est = {"rows_fetched": 0, "rows_affected": 0, "with_payload": 0,
                       "failed": 0, "skipped": 0}
                for cs, ce in chunks:
                    try:
                        r = process_chunk(context, conn, est_id, cs, ce, run_id, force=force)
                    except Exception:
                        est["failed"] += 1
                        continue
                    if r.get("skipped"):
                        est["skipped"] += 1
                    else:
                        est["rows_fetched"] += r["rows_fetched"]
                        est["rows_affected"] += r["rows_affected"]
                        est["with_payload"] += r["with_payload"]
                report["by_establishment"][est_id] = est
                log.info("[est=%s] orders=%d with_payload=%d failed=%d skipped=%d",
                         est_id, est["rows_fetched"], est["with_payload"],
                         est["failed"], est["skipped"])
        finally:
            browser.close()

    report["elapsed_seconds"] = (datetime.now(timezone.utc) - started).total_seconds()
    conn.close()
    return report


def main():
    ap = argparse.ArgumentParser(description="Backfill order_loyalty_v2 (no PII persisted)")
    ap.add_argument("--establishments", default=",".join(map(str, ESTABLISHMENTS)))
    ap.add_argument("--start", default=str(BACKFILL_START))
    ap.add_argument("--end", default=None, help="exclusive; defaults to tomorrow Chicago")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    ests = [int(x) for x in a.establishments.split(",") if x.strip()]
    start = date.fromisoformat(a.start)
    if a.end:
        end = date.fromisoformat(a.end)
    else:
        from zoneinfo import ZoneInfo
        from datetime import timedelta
        end = datetime.now(ZoneInfo("America/Chicago")).date() + timedelta(days=1)

    rep = run_backfill(ests, start, end, force=a.force)
    log.info("DONE in %.1fs", rep["elapsed_seconds"])


if __name__ == "__main__":
    main()
