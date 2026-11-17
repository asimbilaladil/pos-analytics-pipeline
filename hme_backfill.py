#!/usr/bin/env python3
"""Bounded, resumable HME backfill over a range of Chicago business dates.

    venv/bin/python hme_backfill.py --start 2026-10-14 --end 2026-11-11
    venv/bin/python hme_backfill.py --start 2026-10-14 --end 2026-11-11 --status
    venv/bin/python hme_backfill.py --start 2026-10-14 --end 2026-11-11 --dry-run

STORE-SCOPED (migration 64): load ONE store's missing dates without touching
any other store or any day's network verdict:
    venv/bin/python hme_backfill.py --only-store 1158614 --expect-establishment 25 \
        --start 2026-10-14 --end 2026-11-15 --plan        # read-only plan, no lock
    venv/bin/python hme_backfill.py --only-store 1158614 --expect-establishment 25 \
        --start 2026-10-14 --end 2026-11-15                # the backfill
The store must be an active VERIFIED mapping to exactly that establishment.
Dates on which the store already has a canonical row are skipped (existing
history is never re-written) unless --include-loaded. Its own checkpoint:
backfill-store-<number>-<start>-<end>.json.

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
BACKFILL_VERSION = "hme_backfill/1.1.0"
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


class ScopeError(ValueError):
    pass


def verify_store_scope(conn, store, establishment):
    """The scoped store must be ONE active VERIFIED mapping to exactly the
    expected establishment -- checked against the database, never assumed."""
    cur = conn.cursor()
    cur.execute("""SELECT mapping_status, revel_establishment_id, active
                     FROM hme_store_mapping WHERE hme_store_number = %s""", (str(store),))
    rows = cur.fetchall()
    if len(rows) != 1:
        raise ScopeError(f"HME store {store}: {len(rows)} mapping rows (need exactly 1)")
    status, est, active = rows[0]
    if not active or (status or "").upper() != "VERIFIED":
        raise ScopeError(f"HME store {store} is not an active VERIFIED mapping "
                         f"(status={status}, active={active})")
    if est is None or int(est) != int(establishment):
        raise ScopeError(f"HME store {store} maps to establishment {est}, "
                         f"not the expected {establishment}")


def store_plan(conn, store, start, end, ck, today=None, include_loaded=False):
    """Read-only per-date plan for a store-scoped backfill.

    Returns (rows, todo). A date is to do only when ALL hold:
      * it is older than the daily/retry cycle's date (Chicago yesterday):
        yesterday belongs to hme-daily-ingest and hme-reconciliation-retry,
        which load every store anyway -- a scoped run never lands on the one
        date the retry examines, even if the code is later rolled back;
      * the day already has a network-wide run: a store is only ever filled
        INTO a day the network ingestion has judged, never ahead of it;
      * it is not 'loaded' in this store's checkpoint;
      * the store has no canonical row for it (unless include_loaded).
    `today` = first excluded date (default: Chicago yesterday).
    """
    today = today or ingest.chicago_target_date()
    cur = conn.cursor()
    cur.execute("""SELECT business_date FROM hme_store_daily
                    WHERE hme_store_number = %s AND business_date BETWEEN %s AND %s""",
                (str(store), start, end))
    have = {r[0] for r in cur.fetchall()}
    cur.execute("""SELECT DISTINCT ON (r.business_date) r.business_date, r.status, r.reason_code
                     FROM hme_store_ingest_result r
                    WHERE r.hme_store_number = %s AND r.business_date BETWEEN %s AND %s
                    ORDER BY r.business_date, r.run_id DESC""", (str(store), start, end))
    last = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    cur.execute("""SELECT business_date, status, reconciliation_ok
                     FROM v_hme_day_completeness_llm WHERE business_date BETWEEN %s AND %s""",
                (start, end))
    net = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    rows, todo = [], []
    for d in date_range(start, end):
        e = ck["dates"].get(d.isoformat()) or {}
        if d >= today:
            action = "skip: belongs to the daily/retry cycle (or not complete)"
        elif d not in net:
            action = "skip: no network-wide run for this date yet"
        elif e.get("state") == "loaded":
            action = "skip: loaded (checkpoint)"
        elif d in have and not include_loaded:
            action = "skip: store already has a row (history preserved)"
        else:
            action = "backfill"
            todo.append(d)
        lr = last.get(d)
        ns = net.get(d)
        rows.append({"date": d.isoformat(), "store_row_exists": d in have,
                     "store_latest_result": f"{lr[0]}/{lr[1]}" if lr else None,
                     "network_day_status": ns[0] if ns else None,
                     "network_reconciliation_ok": ns[1] if ns else None,
                     "checkpoint": e.get("state"), "action": action})
    return rows, todo


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
    ap.add_argument("--only-store", default=None,
                    help="HME store number: load ONLY this store (migration 64)")
    ap.add_argument("--expect-establishment", type=int, default=None,
                    help="required with --only-store: the establishment it must map to")
    ap.add_argument("--plan", action="store_true",
                    help="with --only-store: print the read-only per-date plan and exit")
    ap.add_argument("--include-loaded", action="store_true",
                    help="with --only-store: also reload dates the store already has")
    a = ap.parse_args(argv)

    start, end = datetime.date.fromisoformat(a.start), datetime.date.fromisoformat(a.end)
    if end < start:
        ap.error("--end before --start")
    if (a.plan or a.include_loaded or a.expect_establishment is not None) and not a.only_store:
        ap.error("--plan / --include-loaded / --expect-establishment need --only-store")
    if a.only_store and a.expect_establishment is None:
        ap.error("--only-store needs --expect-establishment")
    if a.only_store:
        return main_store(a, start, end)
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


def main_store(a, start, end):
    """Store-scoped backfill (migration 64). Same lock, same extractor, same
    loader checks; only the scoped store is written."""
    store = str(a.only_store)
    ck_path = a.checkpoint or os.path.join(
        STATE_DIR, f"backfill-store-{store}-{a.start}-{a.end}.json")
    ck = load_checkpoint(ck_path)
    ck.setdefault("store_scope", [store])
    if ck["store_scope"] != [store]:
        print(f"checkpoint {ck_path} belongs to store scope {ck['store_scope']}")
        return ingest.EXIT_HARD_FAILURE
    conn = hme_load.connect()
    try:
        try:
            verify_store_scope(conn, store, a.expect_establishment)
        except ScopeError as e:
            print(f"refusing: {e}")
            return ingest.EXIT_HARD_FAILURE
        if a.status:
            for d in date_range(start, end):
                print(d, json.dumps(ck["dates"].get(d.isoformat()), default=str))
            return 0
        if a.plan:                       # read-only: no lock, no extractor
            rows, todo = store_plan(conn, store, start, end, ck,
                                    include_loaded=a.include_loaded)
            for r in rows:
                print(json.dumps(r, default=str))
            print(json.dumps({"store": store, "establishment": a.expect_establishment,
                              "dates_in_range": len(rows), "to_backfill": len(todo),
                              "checkpoint": ck_path}))
            return 0
    finally:
        conn.rollback()
        conn.close()

    os.makedirs(ingest.LOG_DIR, mode=0o700, exist_ok=True)
    log_path = os.path.join(ingest.LOG_DIR, f"backfill-store-{store}-{a.start}-{a.end}.log")
    with open(log_path, "a") as fh:
        os.chmod(log_path, 0o600)
        ingest.log(f"=== HME store-scoped backfill {BACKFILL_VERSION} store={store} "
                   f"est={a.expect_establishment} {a.start}..{a.end} dry_run={a.dry_run} ===", fh)
        lock = ingest.acquire_lock(fh)
        if lock is None:
            return ingest.EXIT_NOOP
        try:
            env = ingest.load_env_file(ingest.ENV_FILE)
            conn = hme_load.connect()
            try:
                verify_store_scope(conn, store, a.expect_establishment)   # again, under the lock
                ingest.refresh_tenant_inventory(conn, fh)
                rows, todo = store_plan(conn, store, start, end, ck,
                                        include_loaded=a.include_loaded)
            finally:
                conn.rollback()
                conn.close()
            ingest.log(f"{len(todo)} date(s) to backfill for store {store}", fh)
            run(todo, ck=ck, ck_path=None if a.dry_run else ck_path, pause=a.pause, fh=fh,
                ingest_fn=lambda d: ingest.ingest_date(
                    d, env=env, fh=fh, max_attempts=a.max_attempts,
                    dry_run=a.dry_run, backoff=[0, 120], only_stores=[store]))
        except ScopeError as e:
            ingest.log(f"refusing: {e}", fh)
            return ingest.EXIT_HARD_FAILURE
        finally:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
        code = overall_exit(ck, todo)
        ingest.log(f"store-scoped backfill finished: exit {code}; checkpoint {ck_path}", fh)
        return code


if __name__ == "__main__":
    sys.exit(main())
