#!/usr/bin/env python3
"""Production HME daily ingest orchestrator (extract -> guard -> reconcile -> load).

    venv/bin/python hme_daily_ingest.py                   # Chicago yesterday
    venv/bin/python hme_daily_ingest.py --date 2026-09-14 # explicit backfill of one day
    venv/bin/python hme_daily_ingest.py --dry-run

One process, two applications: the extractor lives in the separate downloader
repo (/opt/hme-report-downloader) and is invoked as a subprocess; the load and
the run record live here. A single orchestration service is simpler and more
reliable than two units with ordering, because a failed extract must prevent the
load rather than merely be sequenced before it.

Canonical source is Power BI explore/querydata. PDFs are never parsed.

Safety posture:
  * target date is computed ONLY from America/Chicago (zoneinfo), never from the
    host's UTC date, a filename, a folder name, or a PDF's printed range;
  * flock prevents overlapping runs;
  * extraction failure, tenant-scope failure, or a stale/incomplete source all
    end the run as failed/partial -- never SUCCESS;
  * the loader independently re-checks tenant safety before writing;
  * bounded backoff retries a transient failure a few times, then gives up.
"""
import argparse
import datetime
import fcntl
import json
import os
import subprocess
import sys
import time
import zoneinfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hme_load  # noqa: E402

CHICAGO = zoneinfo.ZoneInfo("America/Chicago")
DOWNLOADER = os.environ.get("HME_DOWNLOADER_DIR", "/opt/hme-report-downloader")
RAW_DIR = os.environ.get("HME_RAW_DIR", "/var/lib/laynes/hme/raw")
LOG_DIR = os.environ.get("HME_LOG_DIR", "/var/lib/laynes/hme/logs")
LOCK_PATH = os.environ.get("HME_LOCK_PATH", "/var/lib/laynes/hme/state/ingest.lock")
ENV_FILE = os.environ.get("HME_ENV_FILE", "/etc/laynes/hme-downloader.env")
ORCHESTRATOR_VERSION = "hme_daily_ingest/1.1.0"

MAX_ATTEMPTS = int(os.environ.get("HME_MAX_ATTEMPTS", "3"))
BACKOFF_SECONDS = [0, 300, 900]   # bounded: immediate, +5 min, +15 min


def chicago_target_date(now=None):
    """The most recent COMPLETED America/Chicago calendar date.

    Calendar arithmetic in the Chicago zone, so it stays correct across both DST
    transitions: this is a daily-calendar job, not a fixed UTC offset.
    """
    now = now or datetime.datetime.now(CHICAGO)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(CHICAGO).date() - datetime.timedelta(days=1)


def log(msg, fh=None):
    line = f"{datetime.datetime.now(CHICAGO).isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    if fh:
        fh.write(line + "\n")
        fh.flush()


def load_env_file(path):
    """Read HME credentials for the child process only. Values are never logged."""
    if not os.path.exists(path):
        raise SystemExit(f"env file not found: {path}")
    env = {}
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    for required in ("HME_USERNAME", "HME_PASSWORD"):
        if not env.get(required):
            raise SystemExit(f"{required} missing or empty in {path}")
    return env


def refresh_tenant_inventory(conn, fh):
    """Re-export the allowlist so the extractor guards against current truth."""
    path = os.environ.get("HME_TENANT_INVENTORY",
                          "/var/lib/laynes/hme/state/tenant_inventory.json")
    cur = conn.cursor()
    # mapping_status travels with the allowlist so the extractor can tell a
    # missing VERIFIED store (hard stop) from a missing OUT_OF_SCOPE one
    # (incomplete, but loadable).
    cur.execute("""SELECT hme_store_number, hme_store_name, mapping_status
                   FROM hme_store_mapping WHERE active ORDER BY hme_store_number""")
    inv = [{"hme_store_number": a, "hme_store_name": b, "mapping_status": c}
           for a, b, c in cur.fetchall()]
    if not inv:
        raise SystemExit("refusing to run: hme_store_mapping has no active stores")
    tmp = path + ".tmp"
    with open(tmp, "w") as out:
        json.dump(inv, out, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    log(f"tenant inventory refreshed: {len(inv)} active stores", fh)
    return len(inv)


def run_extractor(date, env, fh):
    """Returns (returncode, facts_path_or_None)."""
    cmd = [os.path.join(DOWNLOADER, ".venv/bin/python"),
           os.path.join(DOWNLOADER, "hme_querydata.py"), "--date", date]
    child = dict(os.environ)
    child.update(env)
    child["HME_RAW_DIR"] = RAW_DIR
    proc = subprocess.run(cmd, cwd=DOWNLOADER, env=child,
                          capture_output=True, text=True, timeout=1800)
    for line in (proc.stdout or "").splitlines():
        log(f"  [extract] {line}", fh)
    if proc.returncode != 0:
        # stderr can carry a traceback; it never carries credentials because the
        # extractor only ever reads them from the environment.
        for line in (proc.stderr or "").splitlines()[-15:]:
            log(f"  [extract:err] {line}", fh)
    candidates = sorted(
        (os.path.join(RAW_DIR, date, f) for f in os.listdir(os.path.join(RAW_DIR, date))
         if f.startswith("querydata-facts") and f.endswith(".json")),
        key=os.path.getmtime, reverse=True) if os.path.isdir(os.path.join(RAW_DIR, date)) else []
    return proc.returncode, (candidates[0] if candidates else None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None,
                    help="YYYY-MM-DD; default = America/Chicago yesterday")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-attempts", type=int, default=MAX_ATTEMPTS)
    args = ap.parse_args()

    target = args.date or chicago_target_date().isoformat()
    datetime.date.fromisoformat(target)

    os.makedirs(LOG_DIR, mode=0o700, exist_ok=True)
    os.makedirs(os.path.dirname(LOCK_PATH), mode=0o700, exist_ok=True)
    log_path = os.path.join(LOG_DIR, f"daily-ingest-{target}.log")

    with open(log_path, "a") as fh:
        os.chmod(log_path, 0o600)
        now_chi = datetime.datetime.now(CHICAGO)
        log(f"=== HME daily ingest {ORCHESTRATOR_VERSION} ===", fh)
        log(f"chicago now      : {now_chi.isoformat(timespec='seconds')}", fh)
        log(f"host utc now     : {datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')}", fh)
        log(f"target business_date (Chicago yesterday): {target}", fh)

        # --- single-run lock -------------------------------------------------
        lock = open(LOCK_PATH, "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("another HME ingest run holds the lock; exiting without action", fh)
            return 75  # EX_TEMPFAIL
        os.chmod(LOCK_PATH, 0o600)
        lock.write(f"{os.getpid()} {target}\n")
        lock.flush()

        try:
            env = load_env_file(ENV_FILE)
            conn = hme_load.connect()
            try:
                inv_n = refresh_tenant_inventory(conn, fh)
            finally:
                conn.close()

            started = datetime.datetime.now(datetime.timezone.utc)
            last_reason = "no attempt ran"
            for attempt in range(1, max(1, args.max_attempts) + 1):
                delay = BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)]
                if delay and attempt > 1:
                    log(f"backoff {delay}s before attempt {attempt}", fh)
                    time.sleep(delay)
                log(f"--- attempt {attempt}/{args.max_attempts} ---", fh)

                rc, facts_path = run_extractor(target, env, fh)
                if rc != 0 or not facts_path:
                    last_reason = (f"extractor rc={rc}"
                                   + ("" if facts_path else "; no facts file produced"))
                    log(f"attempt {attempt} failed: {last_reason}", fh)
                    continue

                with open(facts_path) as f:
                    doc = json.load(f)
                doc["attempt"] = attempt
                if doc.get("business_date") != target:
                    last_reason = (f"facts business_date {doc.get('business_date')} "
                                   f"!= target {target}")
                    log(f"attempt {attempt} aborted: {last_reason}", fh)
                    break

                conn = hme_load.connect()
                try:
                    res = hme_load.load(doc, conn, dry_run=args.dry_run)
                except hme_load.LoadAborted as e:
                    conn.rollback()
                    last_reason = f"load aborted: {e}"
                    log(f"attempt {attempt}: {last_reason}", fh)
                    status = ("aborted_tenant_scope"
                              if "tenant" in str(e).lower() or "store" in str(e).lower()
                              else "partial")
                    rid = hme_load.record_failed_run(
                        conn, target, status=status, reason=str(e), attempt=attempt,
                        started_at=started, tenant_scope_ok=doc.get("tenant_scope_ok"),
                        freshness=doc.get("freshness_detail"),
                        extractor_version=doc.get("extractor_version"))
                    log(f"recorded failed run_id={rid} status={status}", fh)
                    conn.close()
                    return 1   # do not retry a safety abort
                finally:
                    if not conn.closed:
                        conn.close()

                # A reconciliation failure is recorded as 'partial' by the
                # loader and must NOT be announced as a successful production
                # ingestion, nor exit 0 -- systemd only treats 0 and 75 as
                # success, so exiting 2 makes a PARTIAL day visibly failed.
                verdict = "SUCCESS" if res["status"] == "succeeded" else "PARTIAL"
                log(f"{verdict} run_id={res['run_id']} status={res['status']} "
                    f"stores={res['store_count']} "
                    f"received={res['rows_received']} written={res['rows_written']} "
                    f"goals={res['goal_stats']} recon_ok={res['reconciliation_ok']}", fh)
                for c in res["reconciliation"]["checks"]:
                    log(f"  recon [{'ok' if c['ok'] else 'FAIL'}] {c['check']}: "
                        f"observed={c['observed']} expected={c['expected']}", fh)
                for w in res["reconciliation"]["warnings"]:
                    log(f"  warning: {w}", fh)
                log(f"tenant inventory used: {inv_n} stores", fh)
                if res["status"] != "succeeded":
                    c = res.get("completeness") or {}
                    why = []
                    if not res["reconciliation_ok"]:
                        why.append("not reconciled")
                    if c and not c.get("complete"):
                        why.append(
                            f"source incomplete {c.get('observed_store_count')}"
                            f"/{c.get('expected_store_count')} stores, missing "
                            f"{c.get('missing_store_numbers')} "
                            f"{c.get('missing_store_names')} "
                            f"{c.get('missing_mapping_statuses')} "
                            f"(VERIFIED {c.get('verified_observed_count')}"
                            f"/{c.get('verified_expected_count')} present)")
                    log(f"run {res['run_id']} is PARTIAL: facts are loaded and remain "
                        f"idempotently correctable, but " + "; ".join(why), fh)
                    return 2
                return 0

            # every attempt exhausted
            log(f"all {args.max_attempts} attempt(s) failed: {last_reason}", fh)
            conn = hme_load.connect()
            try:
                rid = hme_load.record_failed_run(
                    conn, target, status="failed", reason=last_reason,
                    attempt=args.max_attempts, started_at=started)
                log(f"recorded failed run_id={rid} status=failed", fh)
            finally:
                conn.close()
            return 1
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()


if __name__ == "__main__":
    sys.exit(main())
