"""
backfill_timesheets_v2.py
=========================
Populates timesheet_entries_v2 (migration 36) from /resources/TimeSheetEntry/.

WHAT IS DELIBERATELY NOT INGESTED
---------------------------------
`remarks` is a free-text field that staff may have typed anything into,
including names or incident detail. It is excluded from the `fields=` request
so it never crosses the wire, and there is no column for it. Employee identity
is reduced to the numeric employee id parsed from the URI -- no name, email or
phone is fetched, because the resource does not expose them and this script
does not go looking for them elsewhere.

Raw archiving IS enabled here (resource="timesheets"), unlike the loyalty
backfill: with `remarks` excluded the response body carries no PII, so it gets
the same archive-then-parse durability the other backfills have.

BREAKS
------
The resource exposes no break-duration field at all -- only break_type, NULL
on every row observed. worked_seconds is clock_out - clock_in and therefore
INCLUDES any unpaid break actually taken. Every row is written with
break_data_status='unavailable' so no consumer can mistake it for verified
paid time.

OPEN SHIFTS
-----------
clock_out is NULL for a shift still in progress. Those rows are stored with
NULL worked_seconds and NULL estimated_labor_cost rather than being skipped or
zero-filled, and are corrected on a later run by the UPSERT once the shift
closes.

Discovery uses clock_in windows -- the business-meaningful axis for labour, and
the one a shift is filed under. Chunked by establishment x calendar month,
checkpointed in backfill_progress (resource="timesheet_entries_v2"), idempotent
via UPSERT on id, resumable. Writes ONLY to timesheet_entries_v2.
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
from playwright.sync_api import sync_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BACKFILL_START = date(2026, 1, 1)
ESTABLISHMENTS = [6, 7, 14, 15, 20, 25, 26, 32, 36, 40, 48, 54]
RESOURCE = "timesheet_entries_v2"

# `remarks` is deliberately absent -- see module docstring.
TS_FIELDS = ("id,employee,establishment,clock_in,clock_out,role_name,role_wage,"
             "department_name,exempt_salaried,is_auto_clock_out,parent,"
             "created_date,updated_date")

# Only spellings proven equivalent by inspection are folded. No taxonomy is invented.
ROLE_ALIASES = {"shift mgr": "Shift Manager"}


def normalize_role(raw):
    if not raw or not str(raw).strip():
        return None
    text = str(raw).strip()
    return ROLE_ALIASES.get(text.lower(), text)


def month_chunks(start_date, end_date_exclusive):
    cur = start_date
    while cur < end_date_exclusive:
        nxt = date(cur.year + 1, 1, 1) if cur.month == 12 else date(cur.year, cur.month + 1, 1)
        nxt = min(nxt, end_date_exclusive)
        yield cur, nxt
        cur = nxt


def _uri_id(uri):
    if isinstance(uri, int):
        return uri
    if isinstance(uri, str):
        tail = uri.rstrip("/").rsplit("/", 1)[-1]
        if tail.isdigit():
            return int(tail)
    return None


def build_row(rec, est_id):
    ci = P.parse_dt(rec["clock_in"]) if rec.get("clock_in") else None
    co = P.parse_dt(rec["clock_out"]) if rec.get("clock_out") else None

    worked = None
    if ci and co:
        worked = int((co - ci).total_seconds())
        if worked < 0:
            # Never store a negative shift; flag by leaving it unknown.
            worked = None

    wage = rec.get("role_wage")
    cost = round(worked / 3600.0 * float(wage), 4) if (worked is not None and wage) else None

    raw_role = rec.get("role_name")
    return (
        rec["id"],
        _uri_id(rec.get("employee")),
        _uri_id(rec.get("establishment")) or est_id,
        ci, co, worked,
        None,            # break_seconds: no upstream source exists
        "unavailable",   # break_data_status
        rec.get("department_name"),
        raw_role,
        normalize_role(raw_role),
        wage,
        cost,
        rec.get("exempt_salaried"),
        rec.get("is_auto_clock_out"),
        _uri_id(rec.get("parent")),
        P.parse_dt(rec["created_date"]) if rec.get("created_date") else None,
        P.parse_dt(rec["updated_date"]) if rec.get("updated_date") else None,
    )


UPSERT = """
INSERT INTO timesheet_entries_v2 (
    id, employee_id, establishment_id, clock_in, clock_out, worked_seconds,
    break_seconds, break_data_status, department_name, role_name_raw,
    role_name_normalized, role_wage, estimated_labor_cost, exempt_salaried,
    is_auto_clock_out, parent_id, source_created_date, source_updated_date)
VALUES %s
ON CONFLICT (id) DO UPDATE SET
    employee_id          = EXCLUDED.employee_id,
    establishment_id     = EXCLUDED.establishment_id,
    clock_in             = EXCLUDED.clock_in,
    clock_out            = EXCLUDED.clock_out,
    worked_seconds       = EXCLUDED.worked_seconds,
    break_seconds        = EXCLUDED.break_seconds,
    break_data_status    = EXCLUDED.break_data_status,
    department_name      = EXCLUDED.department_name,
    role_name_raw        = EXCLUDED.role_name_raw,
    role_name_normalized = EXCLUDED.role_name_normalized,
    role_wage            = EXCLUDED.role_wage,
    estimated_labor_cost = EXCLUDED.estimated_labor_cost,
    exempt_salaried      = EXCLUDED.exempt_salaried,
    is_auto_clock_out    = EXCLUDED.is_auto_clock_out,
    parent_id            = EXCLUDED.parent_id,
    source_created_date  = EXCLUDED.source_created_date,
    source_updated_date  = EXCLUDED.source_updated_date,
    extracted_at         = now()
"""


def get_chunk_status(cur, est_id, ws, we):
    cur.execute("""SELECT status FROM backfill_progress
                   WHERE resource=%s AND establishment_id=%s
                     AND window_start=%s AND window_end=%s""",
                (RESOURCE, est_id, ws, we))
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
    log.info("  [est=%s %s..%s] fetching", est_id, chunk_start, chunk_end)

    try:
        records = P.fetch_all_pages(
            context,
            endpoint=f"{P.BASE_URL}/resources/TimeSheetEntry/",
            params={"establishment": est_id, "clock_in__gte": range_from,
                    "clock_in__lt": range_to, "fields": TS_FIELDS},
            label=f"timesheets est={est_id} {chunk_start}",
            resource="timesheets", run_id=run_id, establishment_id=est_id,
            date_window=(range_from, range_to), archive_date=date.today(),
            window_key=f"{chunk_start:%Y%m%d}_{chunk_end:%Y%m%d}",
        )

        rows = [build_row(r, est_id) for r in records if r.get("id")]

        with conn.cursor() as cur:
            execute_values(cur, UPSERT, rows, page_size=1000)
            affected = cur.rowcount
            cur.execute("SELECT COUNT(*), COUNT(DISTINCT id) FROM timesheet_entries_v2")
            total, distinct = cur.fetchone()
            if total != distinct:
                raise RuntimeError(f"timesheet_entries_v2 duplicate ids: {total} != {distinct}")
        conn.commit()

        open_shifts = sum(1 for r in rows if r[4] is None)
        mark_result(conn, est_id, ws, we, "success",
                    pages=(len(records) + 999) // 1000 if records else 0,
                    rows_fetched=len(records), rows_affected=affected)
        log.info("  [est=%s %s..%s] OK - entries=%d open_shifts=%d",
                 est_id, chunk_start, chunk_end, len(rows), open_shifts)
        return {"skipped": False, "status": "success", "rows_fetched": len(rows),
                "rows_affected": affected, "open_shifts": open_shifts}

    except raw_archive.ArchiveError as exc:
        conn.rollback()
        mark_result(conn, est_id, ws, we, "failed", error=f"ArchiveError: {exc}")
        log.error("  [est=%s %s..%s] ARCHIVE FAILURE: %s", est_id, chunk_start, chunk_end, exc)
        raise
    except Exception as exc:
        conn.rollback()
        mark_result(conn, est_id, ws, we, "failed", error=str(exc)[:2000])
        log.error("  [est=%s %s..%s] FAILED: %s", est_id, chunk_start, chunk_end, exc)
        raise


def run_backfill(establishments, start_date, end_date_exclusive, force=False):
    conn = P.db_connect()
    run_id = raw_archive.new_run_id()
    chunks = list(month_chunks(start_date, end_date_exclusive))
    log.info("=== Timesheets backfill run_id=%s - %d est x %d chunk(s) ===",
             run_id, len(establishments), len(chunks))

    report = {"run_id": run_id, "by_establishment": {}}
    started = datetime.now(timezone.utc)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state="/tmp/revel_session.json")
        try:
            for est_id in establishments:
                est = {"rows_fetched": 0, "rows_affected": 0, "open_shifts": 0,
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
                        est["open_shifts"] += r["open_shifts"]
                report["by_establishment"][est_id] = est
                log.info("[est=%s] entries=%d open=%d failed=%d skipped=%d",
                         est_id, est["rows_fetched"], est["open_shifts"],
                         est["failed"], est["skipped"])
        finally:
            browser.close()

    report["elapsed_seconds"] = (datetime.now(timezone.utc) - started).total_seconds()
    conn.close()
    return report


def main():
    ap = argparse.ArgumentParser(description="Backfill timesheet_entries_v2 (no employee PII)")
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
