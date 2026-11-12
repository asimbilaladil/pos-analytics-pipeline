#!/usr/bin/env python3
"""Bounded, resumable HME backfill over a range of Chicago business dates.

    venv/bin/python hme_backfill.py --start 2026-10-14 --end 2026-11-11
    venv/bin/python hme_backfill.py --start 2026-10-14 --end 2026-11-11 --status
    venv/bin/python hme_backfill.py --start 2026-10-14 --end 2026-11-11 --dry-run

One date at a time, oldest first, through the SAME path as the daily job
(hme_daily_ingest.ingest_date): same extractor, same guards, same loader,
same per-store grading and reconciliation. Nothing is special-cased here.

Safety:
  * holds the shared HME ingest lock for the whole run, so it can never
    overlap the daily ingest or the reconciliation retry (exit 75 if held);
  * one extractor (one headless browser) at a time -- memory and HME load
    stay bounded -- with a pause between dates;
  * a checkpoint file records every date's outcome as it completes. A date
    is marked "loaded" only after its load committed; a failed date stays
    "failed" and is retried on the next run -- it is never skipped over;
  * loads are idempotent upserts; a store that returns nothing writes
    nothing, so existing valid rows are never overwritten with absence.

Exit: 0 every date loaded and every store complete; 4 every date loaded but
some stores failed/partial; 1 one or more dates failed (see the checkpoint);
75 another HME job holds the lock.
"""
import argparse
import datetime
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hme_daily_ingest as ingest  # noqa: E402
import hme_load  # noqa: E402

STATE_DIR = os.environ.get("HME_STATE_DIR", "/var/lib/laynes/hme/state")
BACKFILL_VERSION = "hme_backfill/1.0.0"
# Codes that mean the day was loaded (committed) -- possibly partial.
LOADED_CODES = (ingest.EXIT_OK, ingest.EXIT_ACCEPTED_PARTIAL,
                ingest.EXIT_PARTIAL_STORES, ingest.EXIT_RECONCILIATION_FAILED)


def date_range(start, end):
    d = start
    while d <= end:
        yield d
        d += datetime.timedelta(days=1)


def load_checkpoint(path):
    if path and os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {"version": BACKFILL_VERSION, "dates": {}}


def save_checkpoint(path, ck):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(ck, f, indent=1, default=str)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def plan(start, end, ck, today=None):
    """Dates still to do: never-run or failed. A completed Chicago date only:
    today and later are refused (the day is not over)."""
    today = today or ingest.chicago_target_date() + datetime.timedelta(days=1)
    todo = []
    for d in date_range(start, end):
        if d >= today:
            break
        e = ck["dates"].get(d.isoformat())
        if e and e.get("state") == "loaded":
            continue
        todo.append(d)
    return todo


def record(ck, d, code, res, reason):
    loaded = code in LOADED_CODES and res is not None
    entry = {"state": "loaded" if loaded else "failed", "exit": code,
             "reason": reason,
             "at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
    if res is not None:
        entry.update({"run_id": res["run_id"], "status": res["status"],
                      "reconciliation_ok": res["reconciliation_ok"],
                      "rows_written": res["rows_written"],
                      "stores": hme_load.summarise_results(res.get("store_results") or [])})
    prev = ck["dates"].get(d.isoformat()) or {}
    entry["attempts"] = int(prev.get("attempts", 0)) + 1
    ck["dates"][d.isoformat()] = entry
    return entry


def run(dates, *, ingest_fn, ck, ck_path, pause, fh, sleep=time.sleep):
    for i, d in enumerate(dates):
        if i and pause:
            sleep(pause)
        ingest.log(f"=== backfill {d} ({i + 1}/{len(dates)}) ===", fh)
        try:
            code, res, reason = ingest_fn(d.isoformat())
        except Exception as e:  # noqa: BLE001 -- one bad date must not stop the range
            code, res, reason = ingest.EXIT_HARD_FAILURE, None, f"{type(e).__name__}: {e}"
        e = record(ck, d, code, res, reason)
        if ck_path:
            save_checkpoint(ck_path, ck)
        ingest.log(f"backfill {d}: {e['state']} exit={code} {e.get('stores', '')}", fh)
    return ck


def overall_exit(ck, dates):
    ents = [ck["dates"].get(d.isoformat()) or {} for d in dates]
    if any(e.get("state") != "loaded" for e in ents):
        return ingest.EXIT_HARD_FAILURE
    if any(e.get("exit") != ingest.EXIT_OK for e in ents):
        return ingest.EXIT_PARTIAL_STORES
    return ingest.EXIT_OK


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True, help="inclusive")
    ap.add_argument("--checkpoint", default=None)
    ap.add_argument("--max-attempts", type=int, default=2,
                    help="extraction attempts per date (default 2)")
    ap.add_argument("--pause", type=int, default=30, help="seconds between dates")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--status", action="store_true", help="print the checkpoint and exit")
    a = ap.parse_args(argv)

    start, end = datetime.date.fromisoformat(a.start), datetime.date.fromisoformat(a.end)
    if end < start:
        ap.error("--end before --start")
    ck_path = a.checkpoint or os.path.join(STATE_DIR, f"backfill-{a.start}-{a.end}.json")
    ck = load_checkpoint(ck_path)
    all_dates = list(date_range(start, end))
    if a.status:
        for d in all_dates:
            print(d, json.dumps(ck["dates"].get(d.isoformat()), default=str))
        return 0

    os.makedirs(ingest.LOG_DIR, mode=0o700, exist_ok=True)
    log_path = os.path.join(ingest.LOG_DIR, f"backfill-{a.start}-{a.end}.log")
    with open(log_path, "a") as fh:
        os.chmod(log_path, 0o600)
        ingest.log(f"=== HME backfill {BACKFILL_VERSION} {a.start}..{a.end} "
                   f"dry_run={a.dry_run} partial_stores={ingest.PARTIAL_STORES} ===", fh)
        lock = ingest.acquire_lock(fh)
        if lock is None:
            return ingest.EXIT_NOOP
        try:
            env = ingest.load_env_file(ingest.ENV_FILE)
            conn = hme_load.connect()
            try:
                ingest.refresh_tenant_inventory(conn, fh)
            finally:
                conn.close()
            todo = plan(start, end, ck)
            ingest.log(f"{len(todo)} date(s) to process; "
                       f"{len(all_dates) - len(todo)} already loaded or not yet complete", fh)
            run(todo, ck=ck, ck_path=None if a.dry_run else ck_path, pause=a.pause, fh=fh,
                ingest_fn=lambda d: ingest.ingest_date(
                    d, env=env, fh=fh, max_attempts=a.max_attempts,
                    dry_run=a.dry_run, backoff=[0, 120]))
        finally:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
        code = overall_exit(ck, [d for d in all_dates
                                 if d < ingest.chicago_target_date() + datetime.timedelta(days=1)])
        ingest.log(f"backfill finished: exit {code}; checkpoint {ck_path}", fh)
        return code


if __name__ == "__main__":
    sys.exit(main())
