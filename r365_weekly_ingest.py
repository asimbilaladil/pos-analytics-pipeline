#!/usr/bin/env python3
"""R365 weekly ingest orchestrator (fetch -> archive -> validate -> load).

    venv/bin/python r365_weekly_ingest.py                     # scheduled: latest TWO completed weeks
    venv/bin/python r365_weekly_ingest.py --week 2026-10-08   # one week
    venv/bin/python r365_weekly_ingest.py --backfill          # every week from the 2026-08-13 boundary
    venv/bin/python r365_weekly_ingest.py --dry-run           # fetch + validate, write nothing

Weeks run Thursday -> Wednesday on the America/Chicago calendar. The target is
computed ONLY from Chicago local time (zoneinfo), never from the host's UTC
date. A week is eligible once its Wednesday has fully passed in Chicago.

The scheduled run re-fetches the latest TWO completed weeks: R365 values can
be adjusted after first publication, and the overlap gives a one-week repair
window without reloading history. Older weeks are never touched by it.

Safety posture:
  * the token is read from /etc/laynes/r365.env (root, 0600) and only ever
    sent as a request header -- never on a command line, in a log line, in the
    raw archive or in the database;
  * flock prevents overlapping runs;
  * every response is archived verbatim BEFORE it is parsed, with its sha256;
  * a schema surprise, wrong week, or empty source FAILS the week and loads
    nothing; missing stores/days make it PARTIAL, never zero rows.

Exit contract:
   0  every requested week COMPLETE
   1  at least one week FAILED (or a configuration error)
   2  no failure, but at least one week PARTIAL (incomplete or unreconciled)
  75  EX_TEMPFAIL: another run holds the lock
"""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
import sys
import time
import uuid
import zoneinfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import r365_load as L  # noqa: E402

CHICAGO = zoneinfo.ZoneInfo("America/Chicago")
ENV_FILE = os.environ.get("R365_ENV_FILE", "/etc/laynes/r365.env")
RAW_DIR = os.environ.get("R365_RAW_DIR", "/var/lib/laynes/r365/raw")
LOCK_PATH = os.environ.get("R365_LOCK_PATH", "/var/lib/laynes/r365/state/ingest.lock")
ORCHESTRATOR_VERSION = "r365_weekly_ingest/1.0.0"
ENDPOINT = "/weekly-data"
MAX_ATTEMPTS = int(os.environ.get("R365_MAX_ATTEMPTS", "3"))
BACKOFF_SECONDS = [0, 30, 120]
CONNECT_TIMEOUT, READ_TIMEOUT = 15, 300   # recent weeks are fetched live from R365

EXIT_OK, EXIT_FAILED, EXIT_PARTIAL, EXIT_LOCKED = 0, 1, 2, 75


class FetchError(Exception):
    def __init__(self, code, message, http_status=None, retryable=False):
        super().__init__(message)
        self.code, self.http_status, self.retryable = code, http_status, retryable


# ---------------------------------------------------------------------------
# Business-week arithmetic (pure)
# ---------------------------------------------------------------------------
def chicago_today(now=None):
    now = now or datetime.datetime.now(CHICAGO)
    return now.astimezone(CHICAGO).date()


def latest_completed_week_start(today):
    """Thursday opening the most recent week whose Wednesday is before today.

    Thursday 2026-10-15 -> 2026-10-08 (the week that ended yesterday).
    Wednesday 2026-10-14 -> 2026-10-01 (10-08's week is still in progress).
    """
    return L.week_start_for(today) - datetime.timedelta(days=7)


def scheduled_targets(today):
    """The latest two completed weeks, oldest first, inside the boundary."""
    last = latest_completed_week_start(today)
    return [w for w in (last - datetime.timedelta(days=7), last) if w >= L.BOUNDARY]


def backfill_targets(today, start=None, end=None):
    start = start or L.BOUNDARY
    end = end or latest_completed_week_start(today)
    out, w = [], start
    while w <= end:
        out.append(w)
        w += datetime.timedelta(days=7)
    return out


def check_target(week_start, today):
    """Reason the week may not be ingested, or None."""
    if week_start.isoweekday() != 4:
        return f"{week_start} is not a Thursday"
    if week_start < L.BOUNDARY:
        return (f"{week_start} is before the approved R365 boundary {L.BOUNDARY}; "
                "pre-boundary weeks are out of scope")
    if L.week_end_for(week_start) >= today:
        return (f"week {week_start}..{L.week_end_for(week_start)} is not complete "
                f"in America/Chicago (today is {today})")
    return None


# ---------------------------------------------------------------------------
# Source access
# ---------------------------------------------------------------------------
def read_env_file(path=ENV_FILE):
    vals = {}
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip().strip('"').strip("'")
    base, token = vals.get("R365_API_BASE"), vals.get("R365_API_TOKEN")
    if not base or not token or token == "REPLACE_ME":
        raise FetchError("CONFIG", f"R365_API_BASE / R365_API_TOKEN not set in {path}")
    return base.rstrip("/"), token


def fetch_week(base, token, week_start, session=None, sleep=time.sleep):
    """GET /weekly-data for one week with bounded retries. Returns raw bytes."""
    import requests
    session = session or requests.Session()
    last = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if attempt > 1:
            sleep(BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)])
        t0 = time.monotonic()
        try:
            r = session.get(base + ENDPOINT, params={"week": week_start.isoformat()},
                            headers={"Authorization": f"Bearer {token}"},
                            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
        except (requests.Timeout, requests.ConnectionError) as e:
            last = FetchError("NETWORK", f"{type(e).__name__} on attempt {attempt}",
                              retryable=True)
            continue
        secs = round(time.monotonic() - t0, 3)
        if r.status_code == 200:
            return {"raw": r.content, "http_status": 200, "fetch_seconds": secs,
                    "attempts": attempt}
        if r.status_code in (401, 403):
            raise FetchError("AUTH", f"HTTP {r.status_code}: token rejected", r.status_code)
        if r.status_code == 400:
            raise FetchError("BAD_REQUEST", f"HTTP 400: {r.text[:300]}", 400)
        last = FetchError("HTTP", f"HTTP {r.status_code} on attempt {attempt}",
                          r.status_code, retryable=r.status_code >= 500 or r.status_code == 429)
        if not last.retryable:
            raise last
    raise last


def archive(raw, run_id, week_start, fetched_at, http_status):
    """Write the response verbatim inside a provenance envelope. 0700/0600.

    The payload bytes are embedded unparsed, so the archived payload is
    byte-identical to what R365 returned and payload_sha256 verifies it.
    """
    digest = hashlib.sha256(raw).hexdigest()
    d = os.path.join(RAW_DIR, week_start.isoformat())
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    stamp = fetched_at.astimezone(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(d, f"run-{run_id}-{stamp}.json")
    meta = json.dumps({"run_id": run_id, "fetched_at": fetched_at.isoformat(),
                       "target_week_start": week_start.isoformat(),
                       "target_week_end": L.week_end_for(week_start).isoformat(),
                       "request": {"endpoint": ENDPOINT,
                                   "params": {"week": week_start.isoformat()}},
                       "http_status": http_status, "payload_sha256": digest,
                       "orchestrator_version": ORCHESTRATOR_VERSION})
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(b'{"meta": ' + meta.encode() + b', "payload": ' + raw + b"}\n")
    return path, digest


# ---------------------------------------------------------------------------
# One week
# ---------------------------------------------------------------------------
def ingest_week(conn, base, token, week_start, trigger, batch_id, dry_run=False,
                log=print):
    if dry_run:
        f = fetch_week(base, token, week_start)
        with conn.cursor() as cur:
            mapping = L.load_mapping(cur)
        conn.rollback()
        plan = L.plan_week(L.parse_payload(f["raw"]), week_start, mapping)
        log(f"[dry-run] {week_start} {plan['status']} daily={len(plan['daily_rows'])} "
            f"weekly={len(plan['weekly_rows'])} recon_ok={plan['reconciliation_ok']} "
            f"fetch={f['fetch_seconds']}s")
        return plan["status"]

    run_id = L.start_run(conn, week_start, trigger, batch_id)
    fetch = {}
    try:
        f = fetch_week(base, token, week_start)
        fetched_at = datetime.datetime.now(datetime.timezone.utc)
        path, digest = archive(f["raw"], run_id, week_start, fetched_at, f["http_status"])
        fetch = {"http_status": f["http_status"], "fetch_seconds": f["fetch_seconds"],
                 "payload_sha256": digest, "raw_archive_path": path,
                 "fetched_at": fetched_at}
        try:
            payload = L.parse_payload(f["raw"])
        except ValueError as e:
            raise L.SourceSchemaError(f"response is not JSON: {e}") from None
        with conn.cursor() as cur:
            mapping = L.load_mapping(cur)
        plan = L.plan_week(payload, week_start, mapping)
        counts = L.write_plan(conn, run_id, plan, fetch)
        conn.commit()
    except FetchError as e:
        L.fail_run(conn, run_id, e.code, str(e), {**fetch, "http_status": e.http_status})
        log(f"run {run_id} week {week_start} FAILED {e.code}: {e}")
        return L.STATUS_FAILED
    except L.SourceSchemaError as e:
        L.fail_run(conn, run_id, "SOURCE_SCHEMA", str(e), fetch)
        log(f"run {run_id} week {week_start} FAILED SOURCE_SCHEMA: {e}")
        return L.STATUS_FAILED
    except Exception as e:  # noqa: BLE001 -- the run row must never stay RUNNING
        L.fail_run(conn, run_id, "LOAD_ERROR", f"{type(e).__name__}: {e}", fetch)
        log(f"run {run_id} week {week_start} FAILED LOAD_ERROR: {type(e).__name__}: {e}")
        return L.STATUS_FAILED

    log(f"run {run_id} week {week_start}..{plan['week_end']} {plan['status']} "
        f"stores={plan['mapped_observed_count']}/{plan['expected_store_count']} "
        f"full_week={plan['stores_with_full_week']} "
        f"daily={counts['daily_upserted']} (changed {counts['daily_changed']}) "
        f"weekly={counts['weekly_upserted']} (changed {counts['weekly_changed']}) "
        f"recon_ok={plan['reconciliation_ok']} fetch={fetch['fetch_seconds']}s"
        + (f" unmapped={[u['r365_store_name'] for u in plan['unmapped_stores']]}"
           if plan["unmapped_stores"] else "")
        + (f" missing_stores={[m['r365_store_name'] for m in plan['missing_stores']]}"
           if plan["missing_stores"] else ""))
    for w in plan["schema_warnings"]:
        log(f"  warning: {w}")
    return plan["status"]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--week", type=datetime.date.fromisoformat,
                   help="ingest one Thursday-start week")
    g.add_argument("--backfill", action="store_true",
                   help="ingest every completed week from --from (default: boundary)")
    ap.add_argument("--from", dest="start", type=datetime.date.fromisoformat)
    ap.add_argument("--to", dest="end", type=datetime.date.fromisoformat)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    today = chicago_today()
    if args.week:
        targets, trigger = [args.week], "manual"
    elif args.backfill:
        targets, trigger = backfill_targets(today, args.start, args.end), "backfill"
    else:
        targets, trigger = scheduled_targets(today), "scheduled"
    bad = [r for r in (check_target(w, today) for w in targets) if r]
    if bad:
        print("refusing: " + "; ".join(bad))
        return EXIT_FAILED
    if not targets:
        print("no eligible weeks")
        return EXIT_OK

    os.makedirs(os.path.dirname(LOCK_PATH), mode=0o700, exist_ok=True)
    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another R365 ingest holds the lock; exiting")
        return EXIT_LOCKED

    try:
        base, token = read_env_file()
    except (OSError, FetchError) as e:
        print(f"configuration error: {e}")
        return EXIT_FAILED

    batch_id = uuid.uuid4().hex
    print(f"{ORCHESTRATOR_VERSION} batch={batch_id} trigger={trigger} "
          f"chicago_today={today} weeks={[w.isoformat() for w in targets]}")
    conn = L.connect()
    try:
        results = [ingest_week(conn, base, token, w, trigger, batch_id, args.dry_run)
                   for w in targets]
    finally:
        conn.close()
    if L.STATUS_FAILED in results:
        return EXIT_FAILED
    if L.STATUS_PARTIAL in results:
        return EXIT_PARTIAL
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
