"""
reprocess_dirty_features_v2.py
================================
Task 16 prerequisite — drains feature_recompute_queue, recomputing exactly
the (establishment_id, business_date) pairs that changed, scoped via
aggregate_features_v2.aggregate_day(establishment_id=...) so a single dirty
date never triggers a network-wide recompute. Uses the corrected
order_agg/item_agg no-fan-out HOURLY_SQL and the unchanged
PRODUCT_DAILY_SQL/DAILY_SUMMARY_SQL — same formulas, same functions, no
duplicated SQL.

Race safety (the correction to the original Task 15 design): a dirty event
for (establishment_id, business_date) that arrives WHILE this worker is
mid-recompute for that exact row is never lost. dirty_date_queue.
enqueue_dirty_date() always advances dirty_at, even when status='processing'
(leaving status alone so the in-flight claim isn't disturbed). This worker
captures processing_started_at at claim time and, after a successful
recompute, re-reads the row's CURRENT dirty_at: if it's still <=
processing_started_at, nothing changed during the recompute and the row is
marked done; if a newer dirty_at exists, something changed mid-flight and
the row is put back to 'pending' (not 'done', not lost) so the next poll
picks it up and recomputes again with the now-current data.

Idempotent: every recompute reuses aggregate_day()'s existing
ON CONFLICT ... DO UPDATE feature-table UPSERTs — rerunning any
(establishment, date) converges to the same row.

Retries: capped at MAX_ATTEMPTS; beyond the cap the row is left
status='failed' (visible for operator review, never silently dropped).

This does NOT touch or replace the normal scheduled "yesterday" run
(aggregate_features_v2.py --start <yesterday> --end <yesterday> for the
full network, establishment_id=None) — this worker is purely additive, for
dates OTHER than the ones the scheduled run already covers each night.
"""
import os
import sys
import logging
import argparse
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(".env")

import pipeline as P
from aggregate_features_v2 import aggregate_day

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3


def claim_one(conn):
    """Atomically claim exactly one pending row. FOR UPDATE SKIP LOCKED is
    defensive (this project always runs these workers sequentially, never
    concurrently) but costs nothing and makes the claim correct even if
    that ever changes."""
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE feature_recompute_queue
            SET status = 'processing', processing_started_at = NOW()
            WHERE id = (
                SELECT id FROM feature_recompute_queue
                WHERE status = 'pending'
                ORDER BY dirty_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING id, establishment_id, business_date, attempts
        """)
        row = cur.fetchone()
    conn.commit()
    if row is None:
        return None
    return {"id": row[0], "establishment_id": row[1], "business_date": row[2], "attempts": row[3]}


def process_one(conn, claimed: dict) -> str:
    processing_started_at = datetime.now(timezone.utc)
    try:
        stats = aggregate_day(
            conn, claimed["business_date"],
            sections=("hourly", "product_daily", "daily_summary"),
            establishment_id=claimed["establishment_id"],
        )
        with conn.cursor() as cur:
            # Re-read dirty_at as it stands NOW, after the recompute
            # finished -- if a new dirty event landed while we were
            # computing, dirty_at will be newer than our own start time.
            cur.execute("SELECT dirty_at FROM feature_recompute_queue WHERE id=%s", (claimed["id"],))
            current_dirty_at = cur.fetchone()[0]
            if current_dirty_at <= processing_started_at:
                cur.execute("""
                    UPDATE feature_recompute_queue
                    SET status='done', processed_at=NOW()
                    WHERE id=%s
                """, (claimed["id"],))
                result = f"done stats={stats}"
            else:
                cur.execute("""
                    UPDATE feature_recompute_queue
                    SET status='pending'
                    WHERE id=%s
                """, (claimed["id"],))
                result = f"re-dirtied during processing -> pending stats={stats}"
        conn.commit()
        return result
    except Exception as exc:
        conn.rollback()
        new_attempts = claimed["attempts"] + 1
        status = "failed" if new_attempts >= MAX_ATTEMPTS else "pending"
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE feature_recompute_queue
                SET status=%s, attempts=%s, last_error=%s
                WHERE id=%s
            """, (status, new_attempts, str(exc)[:2000], claimed["id"]))
        conn.commit()
        return f"ERROR -> {status} (attempts={new_attempts}): {exc}"


def main():
    ap = argparse.ArgumentParser(description="Drain feature_recompute_queue")
    ap.add_argument("--limit", type=int, default=None, help="max rows to process this run (default: drain until empty)")
    args = ap.parse_args()

    conn = P.db_connect()
    n = 0
    log.info("=== reprocess_dirty_features_v2 start ===")
    while args.limit is None or n < args.limit:
        claimed = claim_one(conn)
        if claimed is None:
            break
        n += 1
        log.info("[%d] claimed est=%s date=%s attempts=%d",
                  n, claimed["establishment_id"], claimed["business_date"], claimed["attempts"])
        result = process_one(conn, claimed)
        log.info("[%d] est=%s date=%s -> %s", n, claimed["establishment_id"], claimed["business_date"], result)

    conn.close()
    log.info("=== reprocess_dirty_features_v2 complete — %d rows processed ===", n)


if __name__ == "__main__":
    main()
