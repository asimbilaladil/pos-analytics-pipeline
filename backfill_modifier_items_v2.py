"""
backfill_modifier_items_v2.py
===============================
Task 13 — historical ModifierItems backfill, targeting modifier_items_v2
ONLY. Unlike every other Task 09-12 backfill script, this one makes NO
Revel API calls at all -- it replays Task 10's already-archived raw
OrderItem responses (Task 13 Phase 1: all 84 order_items_v2 chunks
verified complete/uncorrupted, except 6 legitimate zero-order chunks that
never made an API call in the first place and are handled the same way
here) and extracts the nested "modifieritems" field, using the exact same
canonical logic pipeline.py's own daily pipeline uses (insert_
establishment_data): item_date is the item's own created_date converted to
America/Chicago, and pipeline.build_modifier_row builds each row.

Task 13 Phase 2 found zero mutations across 32 sampled OrderItems / 65
ModifierItems (4 establishments, up to ~6 months old) comparing archived
vs. live Revel data, so UPSERT semantics remain the existing
INSERT ... ON CONFLICT(id) DO NOTHING -- unchanged, not newly invented here.

modifier_name is resolved from the existing `modifiers` reference table via
a plain SQL SELECT (loaded once per process) -- not a live fetch, since that
reference data is already populated by the regular daily pipeline and
doesn't need re-fetching for a replay.

Chunked by establishment x calendar month, same convention as every other
Task 09-12 script. Checkpoint state lives in backfill_progress under
resource="modifier_items_v2". A chunk is marked 'success' only after
read + parse + UPSERT + duplicate-check all complete inside one transaction
that commits at the very end.

Archive pages within a chunk are streamed one at a time (decompress, parse,
extract, UPSERT, discard) rather than materializing the whole month's
OrderItems/ModifierItems in memory -- a large chunk (est=40/2026-03: 188
pages, 130,618 order items) OOM-killed the old whole-month-at-once version.
Streaming does not change transaction semantics: nothing commits until every
page in the chunk has been read and the final item-count/duplicate checks
pass, so a crash or mismatch mid-chunk still rolls back the whole chunk, not
just the pages already upserted.

modifier_items_v2 (migrations/13_modifier_items_v2.sql) has PRIMARY KEY(id)
alone, same as production modifier_items. Never writes to production
modifier_items. Does not modify orders, orders_v2, order_items,
order_items_v2, payments, payments_v2, order_history, or order_history_v2.
"""

import gzip
import json
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(".env")

import pipeline as P

import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BACKFILL_START = date(2026, 2, 10)
CATCHUP_END_EXCLUSIVE = date(2026, 9, 1)
ESTABLISHMENTS = [6, 7, 14, 15, 20, 25, 26, 32, 36, 40, 48, 54]

RESOURCE = "modifier_items_v2"
ARCHIVE_ROOT = "/var/lib/laynes/raw_revel/order_items"
PAGE_RE = re.compile(r"^page_(\d+)_attempt_(\d+)\.json\.gz$")


def month_chunks(start_date: date, end_date_exclusive: date):
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


def find_archive_dir(est_id, run_id, window_key, started_at):
    """Same lookup logic as verify_task10_archives.py."""
    y, m = started_at.year, started_at.month
    primary = os.path.join(ARCHIVE_ROOT, f"{y:04d}", f"{m:02d}",
                            f"establishment_{est_id}", f"run_{run_id}", f"window_{window_key}")
    if os.path.isdir(primary):
        return primary
    if os.path.isdir(ARCHIVE_ROOT):
        for yroot in sorted(os.listdir(ARCHIVE_ROOT)):
            ypath = os.path.join(ARCHIVE_ROOT, yroot)
            if not os.path.isdir(ypath):
                continue
            for mroot in sorted(os.listdir(ypath)):
                cand = os.path.join(ypath, mroot, f"establishment_{est_id}",
                                     f"run_{run_id}", f"window_{window_key}")
                if os.path.isdir(cand):
                    return cand
    return None


def iter_archive_pages(archive_dir):
    """Highest-attempt-per-page selection, same as verify_task10_archives.py --
    but yields one page's items at a time instead of accumulating the whole
    month in memory. Caller must not hold onto more than one page's `items`
    list at once."""
    files = os.listdir(archive_dir)
    pages = defaultdict(dict)
    for fn in files:
        mo = PAGE_RE.match(fn)
        if mo:
            page_num, attempt_num = int(mo.group(1)), int(mo.group(2))
            pages[page_num][attempt_num] = fn

    for page_num in sorted(pages.keys()):
        attempts = pages[page_num]
        best = attempts[max(attempts.keys())]
        fpath = os.path.join(archive_dir, best)
        with gzip.open(fpath, "rb") as f:
            data = json.loads(f.read())
        yield page_num, data.get("objects", [])


def load_modifier_cache(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT id, name FROM modifiers")
        return dict(cur.fetchall())


def process_chunk(conn, modifier_cache, est_id: int, chunk_start: date, chunk_end: date,
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

    # Look up the exact successful Task 10 order_items_v2 chunk this replay is based on.
    with conn.cursor() as cur:
        cur.execute("""
            SELECT run_id, rows_fetched, started_at FROM backfill_progress
            WHERE resource='order_items_v2' AND establishment_id=%s
              AND window_start=%s AND window_end=%s AND status='success'
        """, (est_id, window_start, window_end))
        row = cur.fetchone()

    if row is None:
        mark_result(conn, est_id, window_start, window_end, "failed",
                    error="no successful order_items_v2 chunk found for this window — cannot replay")
        raise RuntimeError(f"no source order_items_v2 chunk for est={est_id} {chunk_start}..{chunk_end}")

    src_run_id, src_rows_fetched, src_started_at = row

    if src_rows_fetched == 0:
        # One of the 6 known zero-order chunks (Task 10) -- no archive was
        # ever created because no fetch ever happened. Record as success
        # with 0 modifier rows, matching Task 10's own treatment.
        mark_result(conn, est_id, window_start, window_end, "success",
                    pages=0, rows_fetched=0, rows_affected=0)
        log.info("  [est=%s %s..%s] OK — source chunk had 0 orders, 0 modifiers (no archive expected)",
                 est_id, chunk_start, chunk_end)
        return {"skipped": False, "status": "success", "rows_fetched": 0, "rows_affected": 0}

    window_key = f"{chunk_start:%Y%m%d}_{chunk_end:%Y%m%d}"

    try:
        archive_dir = find_archive_dir(est_id, src_run_id, window_key, src_started_at)
        if archive_dir is None:
            raise RuntimeError(f"archive directory not found for run_id={src_run_id} window={window_key} "
                                f"(expected {src_rows_fetched} order items) — not live-refetching, stopping")

        # Stream one archive page at a time: decompress, parse, extract
        # modifiers, UPSERT, then let the page's items/rows be garbage
        # collected before the next page is opened. Never holds more than
        # one page's worth of OrderItems/ModifierItems in memory at once --
        # this is the fix for the OOM that killed est=40/2026-03 mid-chunk.
        n_pages = 0
        total_items = 0
        total_rows = 0
        with conn.cursor() as cur:
            for page_num, items in iter_archive_pages(archive_dir):
                n_pages += 1
                total_items += len(items)

                page_rows = []
                for item in items:
                    if not item.get("id"):
                        continue
                    mods = item.get("modifieritems")
                    if not mods:
                        continue
                    created = P.parse_dt(item.get("created_date"))
                    if created is None:
                        log.warning("  skipping modifiers for item=%s: unparseable created_date", item.get("id"))
                        continue
                    item_date = created.astimezone(P._REVEL_TZ).date()
                    order_id = P.extract_id(item.get("order"))
                    for mod in mods:
                        if not mod.get("id"):
                            continue
                        page_rows.append(P.build_modifier_row(mod, item["id"], order_id, est_id, item_date, modifier_cache))

                if page_rows:
                    stats = P.upsert_modifier_items(cur, page_rows, target="shadow_v2")
                    total_rows += stats.get("fetched", len(page_rows))
                # items/page_rows go out of scope on the next loop iteration --
                # nothing beyond this page is retained.

            if total_items != src_rows_fetched:
                raise RuntimeError(f"archive item count mismatch: found {total_items}, "
                                    f"order_items_v2 chunk recorded {src_rows_fetched}")

            cur.execute("SELECT COUNT(*), COUNT(DISTINCT id) FROM modifier_items_v2")
            total, distinct = cur.fetchone()
            if total != distinct:
                raise RuntimeError(f"modifier_items_v2 duplicate ids after chunk: total={total} distinct={distinct}")

        conn.commit()

        mark_result(conn, est_id, window_start, window_end, "success",
                    pages=n_pages, rows_fetched=total_rows, rows_affected=total_rows)
        log.info("  [est=%s %s..%s] OK — order_items=%d modifiers_extracted=%d",
                 est_id, chunk_start, chunk_end, total_items, total_rows)
        return {"skipped": False, "status": "success", "rows_fetched": total_rows, "rows_affected": total_rows}

    except Exception as exc:
        conn.rollback()
        mark_result(conn, est_id, window_start, window_end, "failed", error=str(exc)[:2000])
        log.error("  [est=%s %s..%s] FAILED: %s", est_id, chunk_start, chunk_end, exc)
        raise


def run_backfill(establishments: list, start_date: date, end_date_exclusive: date,
                  force: bool = False) -> dict:
    conn = P.db_connect()
    run_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    chunks = list(month_chunks(start_date, end_date_exclusive))
    log.info("=== Task 13 ModifierItems backfill (archive replay) — run_id=%s — %d establishment(s) x %d chunk(s) ===",
              run_id, len(establishments), len(chunks))

    modifier_cache = load_modifier_cache(conn)
    log.info("Loaded %d modifier names from reference table", len(modifier_cache))

    report = {"run_id": run_id, "by_establishment": {}}
    started = datetime.now(timezone.utc)

    for est_id in establishments:
        est_report = {"chunks": [], "rows_fetched": 0, "rows_affected": 0, "failed": 0, "skipped": 0}
        for chunk_start, chunk_end in chunks:
            try:
                result = process_chunk(conn, modifier_cache, est_id, chunk_start, chunk_end, run_id, force=force)
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
