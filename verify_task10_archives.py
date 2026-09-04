"""
verify_task10_archives.py
===========================
Task 13 Phase 1 — read-only verification that all 84 successful Task 10
order_items_v2 chunks have usable raw archives before backfill_modifier_
items_v2.py is ever allowed to replay them. Never touches the DB except to
read backfill_progress. Never calls Revel. Never writes anything except the
report.

For each chunk (est, window_start, window_end, run_id from backfill_progress):
  1. Locate the archive directory: order_items/{Y}/{M}/establishment_{est}/
     run_{run_id}/window_{chunk_start}_{chunk_end}/ -- Y/M derived from
     started_at (the date the fetch actually ran), with a fallback search
     across the whole archive tree by run_id if not found there.
  2. For every page number present, pick the highest-attempt file (mirrors
     what the original live fetch would have actually used -- an earlier
     failed/malformed attempt at the same page doesn't invalidate a later
     successful one).
  3. Decompress + json.loads each selected file. Confirm "objects" key
     exists and each object looks like an OrderItem (has "id"); check
     whether any object has a non-empty "modifieritems" list.
  4. Sum objects across all pages, compare against backfill_progress's
     recorded rows_fetched for that chunk -- an exact match is strong
     evidence of no missing/corrupt pages.

Classifies each chunk as COMPLETE / MISSING / CORRUPT and reports by
establishment/month. Does not attempt any live re-fetch.
"""
import gzip
import json
import os
import re
from collections import defaultdict
from datetime import date, datetime, timezone

import psycopg2
from dotenv import load_dotenv

load_dotenv("/root/pos-analytics-pipeline/.env")

ARCHIVE_ROOT = "/var/lib/laynes/raw_revel/order_items"

PAGE_RE = re.compile(r"^page_(\d+)_attempt_(\d+)\.json\.gz$")


def month_chunks(start_date, end_date_exclusive):
    cur = start_date
    while cur < end_date_exclusive:
        nxt = date(cur.year + 1, 1, 1) if cur.month == 12 else date(cur.year, cur.month + 1, 1)
        nxt = min(nxt, end_date_exclusive)
        yield cur, nxt
        cur = nxt


BACKFILL_START = date(2026, 2, 10)
CATCHUP_END_EXCLUSIVE = date(2026, 9, 1)
CHUNKS = list(month_chunks(BACKFILL_START, CATCHUP_END_EXCLUSIVE))


def db_connect():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"), port=int(os.getenv("DB_PORT", 5432)),
        dbname=os.getenv("DB_NAME"), user=os.getenv("DB_USER"), password=os.getenv("DB_PASS"),
    )


def find_archive_dir(est_id, run_id, window_key, started_at):
    candidates = []
    y, m = started_at.year, started_at.month
    candidates.append(os.path.join(ARCHIVE_ROOT, f"{y:04d}", f"{m:02d}",
                                    f"establishment_{est_id}", f"run_{run_id}", f"window_{window_key}"))
    # fallback: search whole tree for this exact run_id/window_key combo,
    # in case the fetch crossed a day boundary between started_at and the
    # actual archive_date used.
    for yroot in sorted(os.listdir(ARCHIVE_ROOT)) if os.path.isdir(ARCHIVE_ROOT) else []:
        ypath = os.path.join(ARCHIVE_ROOT, yroot)
        if not os.path.isdir(ypath):
            continue
        for mroot in sorted(os.listdir(ypath)):
            mpath = os.path.join(ypath, mroot)
            cand = os.path.join(mpath, f"establishment_{est_id}", f"run_{run_id}", f"window_{window_key}")
            if os.path.isdir(cand) and cand not in candidates:
                candidates.append(cand)
    for c in candidates:
        if os.path.isdir(c):
            return c
    return None


def verify_chunk(est_id, chunk_start, chunk_end, run_id, expected_rows, started_at):
    window_key = f"{chunk_start:%Y%m%d}_{chunk_end:%Y%m%d}"
    d = find_archive_dir(est_id, run_id, window_key, started_at)
    result = {"est": est_id, "start": str(chunk_start), "end": str(chunk_end),
              "run_id": run_id, "expected_rows": expected_rows, "dir": d}

    if d is None:
        result["classification"] = "MISSING"
        result["detail"] = f"no archive directory found for run_id={run_id} window={window_key}"
        return result

    files = os.listdir(d)
    pages = defaultdict(dict)  # page_num -> {attempt_num: filename}
    for fn in files:
        mo = PAGE_RE.match(fn)
        if mo:
            page_num, attempt_num = int(mo.group(1)), int(mo.group(2))
            pages[page_num][attempt_num] = fn

    if not pages:
        result["classification"] = "CORRUPT"
        result["detail"] = f"directory exists but no page_*.json.gz files found ({len(files)} other files)"
        return result

    total_objects = 0
    items_with_modifiers = 0
    read_errors = []
    page_numbers = sorted(pages.keys())
    # gap check: page numbers should be a contiguous 1..N run
    expected_page_seq = list(range(1, max(page_numbers) + 1))
    missing_pages = sorted(set(expected_page_seq) - set(page_numbers))

    for page_num in page_numbers:
        attempts = pages[page_num]
        best_attempt = max(attempts.keys())
        fname = attempts[best_attempt]
        fpath = os.path.join(d, fname)
        try:
            with gzip.open(fpath, "rb") as f:
                raw = f.read()
            data = json.loads(raw)
            objs = data.get("objects")
            if objs is None:
                read_errors.append(f"page {page_num}: no 'objects' key")
                continue
            for o in objs:
                if "id" not in o:
                    read_errors.append(f"page {page_num}: object missing 'id'")
                    continue
                total_objects += 1
                if o.get("modifieritems"):
                    items_with_modifiers += 1
        except (OSError, gzip.BadGzipFile, EOFError) as exc:
            read_errors.append(f"page {page_num} ({fname}): decompress error: {exc}")
        except json.JSONDecodeError as exc:
            read_errors.append(f"page {page_num} ({fname}): JSON parse error: {exc}")

    result["pages_found"] = len(page_numbers)
    result["missing_page_numbers"] = missing_pages
    result["total_objects"] = total_objects
    result["items_with_modifiers"] = items_with_modifiers
    result["read_errors"] = read_errors

    if read_errors or missing_pages:
        result["classification"] = "CORRUPT"
        result["detail"] = f"{len(read_errors)} read errors, missing pages: {missing_pages}"
    elif expected_rows is not None and total_objects != expected_rows:
        result["classification"] = "CORRUPT"
        result["detail"] = f"row count mismatch: archive has {total_objects}, backfill_progress recorded {expected_rows}"
    else:
        result["classification"] = "COMPLETE"
        result["detail"] = f"{total_objects} objects across {len(page_numbers)} pages, matches recorded rows_fetched"

    return result


def main():
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""SELECT establishment_id, window_start, window_end, run_id, rows_fetched, started_at
                   FROM backfill_progress WHERE resource='order_items_v2' AND status='success'
                   ORDER BY establishment_id, window_start""")
    db_rows = cur.fetchall()
    conn.close()

    # match db rows to (chunk_start, chunk_end) via the same month_chunks list
    import pipeline as P
    results = []
    for est_id, window_start, window_end, run_id, rows_fetched, started_at in db_rows:
        matched = None
        for cs, ce in CHUNKS:
            ws = P.parse_dt(cs.strftime("%Y-%m-%dT00:00:00"))
            we = P.parse_dt(ce.strftime("%Y-%m-%dT00:00:00"))
            if ws == window_start and we == window_end:
                matched = (cs, ce)
                break
        if matched is None:
            results.append({"est": est_id, "classification": "UNMATCHED_WINDOW",
                             "detail": f"couldn't map window_start={window_start} window_end={window_end} to a chunk"})
            continue
        cs, ce = matched
        r = verify_chunk(est_id, cs, ce, run_id, rows_fetched, started_at)
        results.append(r)
        print(f"[{est_id} {cs}..{ce}] {r['classification']} — {r.get('detail')}")

    print("\n=== SUMMARY ===")
    by_class = defaultdict(list)
    for r in results:
        by_class[r["classification"]].append(r)
    for cls, items in sorted(by_class.items()):
        print(f"{cls}: {len(items)}")
        if cls != "COMPLETE":
            for it in items:
                print(f"   est={it['est']} {it.get('start')}..{it.get('end')} — {it.get('detail')}")

    with open("/root/pos-analytics-pipeline/logs/task13_archive_verification.json", "w") as f:
        json.dump(results, f, indent=2, default=str)
    print("\nFull results written to logs/task13_archive_verification.json")


if __name__ == "__main__":
    main()
