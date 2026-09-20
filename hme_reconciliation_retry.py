#!/usr/bin/env python3
"""Delayed HME reconciliation retry (Chicago-yesterday only).

    venv/bin/python hme_reconciliation_retry.py                   # Chicago yesterday
    venv/bin/python hme_reconciliation_retry.py --date 2026-09-19 # explicit
    venv/bin/python hme_reconciliation_retry.py --dry-run

Why this exists
---------------
HME's upstream reports do not all settle at the same time. The 05:30 Chicago
ingest was moved from 03:30 because the Trend dashboard lagged Performance
Analysis for hours after close. On 2026-09-19 a DIFFERENT report lagged: the
Outliers report reported 324 car departures for store 115 (Shepherd) while PA
and Trend both reported 338, a global delta of 14. A read-only re-extract of
the same completed business date 4h34m later returned 338 everywhere and
reconciled exactly; PA and Trend never moved.

Chasing that with the primary schedule would mean pushing ingestion ever later
against an unknown upper bound, and into business hours. Instead the primary
run stays at 05:30 and this job revisits the SAME completed day later, only
when the earlier run is both safe and unreconciled.

Safety posture (identical to the primary orchestrator)
------------------------------------------------------
  * target date comes ONLY from America/Chicago, never the host's UTC date;
  * the same flock as the primary ingest -- the two can never overlap;
  * the normal production extractor is invoked; no self-authored semantic
    query, no request replay, all extractor guards run unchanged;
  * the loader re-checks tenant safety independently before writing;
  * a retry NEVER rewrites a historical hme_ingest_run row -- it creates a
    new run_id, so the original mismatch stays auditable;
  * absent OUT_OF_SCOPE stores are never synthesized; incompleteness survives
    the retry untouched.
"""
import argparse
import datetime
import fcntl
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hme_load            # noqa: E402
import hme_daily_ingest as ingest  # noqa: E402

CHICAGO = ingest.CHICAGO
LOG_DIR = ingest.LOG_DIR
LOCK_PATH = ingest.LOCK_PATH
ENV_FILE = ingest.ENV_FILE
RETRY_VERSION = "hme_reconciliation_retry/1.0.0"

EXIT_OK = ingest.EXIT_OK
EXIT_HARD_FAILURE = ingest.EXIT_HARD_FAILURE
EXIT_RECONCILIATION_FAILED = ingest.EXIT_RECONCILIATION_FAILED
EXIT_ACCEPTED_PARTIAL = ingest.EXIT_ACCEPTED_PARTIAL
EXIT_NOOP = ingest.EXIT_NOOP


def latest_run(conn, business_date):
    """The most recent ingest-run record for a business date, or None.

    hme_ingest_run stores OBSERVED counts but not a completeness blob, so
    coverage is derived here from the authoritative tables: what was actually
    loaded for the date (hme_store_daily) against the current active allowlist
    (hme_store_mapping). That is deliberately the same ground truth the loader
    and the views use, rather than parsing a warnings string.
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT run_id, status, reconciliation_ok, tenant_scope_ok,
               store_count, verified_store_count, out_of_scope_store_count
          FROM hme_ingest_run
         WHERE business_date = %s
         ORDER BY run_id DESC
         LIMIT 1""", (business_date,))
    row = cur.fetchone()
    if not row:
        return None
    keys = ("run_id", "status", "reconciliation_ok", "tenant_scope_ok",
            "store_count", "verified_store_count", "out_of_scope_store_count")
    rec = dict(zip(keys, row))

    cur.execute("""
        SELECT count(*) FILTER (WHERE m.mapping_status = 'VERIFIED'),
               count(*)
          FROM hme_store_mapping m
         WHERE m.active""")
    v_exp, total_exp = cur.fetchone()

    cur.execute("""
        SELECT count(*) FILTER (WHERE m.mapping_status = 'VERIFIED'),
               count(*)
          FROM hme_store_daily d
          JOIN hme_store_mapping m USING (hme_store_number)
         WHERE d.business_date = %s AND m.active""", (business_date,))
    v_obs, total_obs = cur.fetchone()

    cur.execute("""
        SELECT coalesce(array_agg(DISTINCT m.mapping_status), '{}')
          FROM hme_store_mapping m
         WHERE m.active
           AND NOT EXISTS (SELECT 1 FROM hme_store_daily d
                            WHERE d.hme_store_number = m.hme_store_number
                              AND d.business_date = %s)""", (business_date,))
    missing_statuses = list(cur.fetchone()[0] or [])

    cur.execute("""
        SELECT count(*) FROM hme_store_daily d
         WHERE d.business_date = %s
           AND NOT EXISTS (SELECT 1 FROM hme_store_mapping m
                            WHERE m.hme_store_number = d.hme_store_number
                              AND m.active)""", (business_date,))
    unexpected = cur.fetchone()[0]

    rec["completeness_detail"] = {
        "verified_observed_count": v_obs,
        "verified_expected_count": v_exp,
        "observed_store_count": total_obs,
        "expected_store_count": total_exp,
        "missing_mapping_statuses": missing_statuses,
        "unexpected_store_count": unexpected,
    }
    return rec


def assess_retry_eligibility(run, *, verified_expected=None):
    """Decide whether a delayed retry is warranted. Fail-closed.

    Returns (eligible: bool, reason: str). The ONLY eligible shape is a day
    that is already proven tenant-safe and VERIFIED-complete but whose
    cross-source checks disagree -- i.e. a report that had not settled yet.
    Everything else is a no-op, including every security or coverage problem,
    which must be escalated by a human rather than silently re-extracted.
    """
    if run is None:
        return False, "no ingest run exists for this business date"

    status = (run.get("status") or "").lower()
    if status in ("failed", "aborted_tenant_scope"):
        return False, f"previous run status={status}: hard failure, needs a human"
    if not run.get("tenant_scope_ok"):
        return False, "tenant_scope_ok is not true: security failure, never retried"
    if status == "succeeded":
        return False, "previous run already succeeded"
    if status != "partial":
        return False, f"unrecognised previous run status={status!r}: fail closed"
    if run.get("reconciliation_ok"):
        return False, ("previous run is partial but already reconciled "
                       "(safe source incompleteness) -- nothing to retry")

    c = run.get("completeness_detail") or {}
    v_obs = c.get("verified_observed_count", run.get("verified_store_count"))
    v_exp = c.get("verified_expected_count", verified_expected)
    if v_obs is None or v_exp is None:
        return False, "VERIFIED coverage unknown: fail closed"
    if v_obs != v_exp:
        return False, (f"VERIFIED coverage {v_obs}/{v_exp} is incomplete: "
                       "a missing VERIFIED store needs attention, not a retry")

    if c.get("unexpected_store_count"):
        return False, (f"{c['unexpected_store_count']} store(s) outside the active "
                       "allowlist are present: security concern, never retried")

    missing = set(c.get("missing_mapping_statuses") or [])
    if missing - {"OUT_OF_SCOPE"}:
        return False, (f"missing stores include non-OUT_OF_SCOPE {sorted(missing)}: "
                       "fail closed")

    return True, (f"partial and unreconciled with VERIFIED {v_obs}/{v_exp} present "
                  "and tenant scope OK: a source report may not have settled")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None,
                    help="YYYY-MM-DD; default = America/Chicago yesterday")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check-only", action="store_true",
                    help="report eligibility and exit without extracting")
    args = ap.parse_args()

    target = args.date or ingest.chicago_target_date().isoformat()
    datetime.date.fromisoformat(target)

    os.makedirs(LOG_DIR, mode=0o700, exist_ok=True)
    os.makedirs(os.path.dirname(LOCK_PATH), mode=0o700, exist_ok=True)
    log_path = os.path.join(LOG_DIR, f"reconciliation-retry-{target}.log")

    with open(log_path, "a") as fh:
        os.chmod(log_path, 0o600)
        def say(msg):
            ingest.log(msg, fh)

        say(f"=== HME reconciliation retry {RETRY_VERSION} ===")
        say(f"chicago now      : {datetime.datetime.now(CHICAGO).isoformat(timespec='seconds')}")
        say(f"target business_date (Chicago yesterday): {target}")

        # Same lock as the primary ingest: the two must never run together.
        lock = open(LOCK_PATH, "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            say("primary HME ingest holds the lock; retry exits without action")
            return EXIT_NOOP
        os.chmod(LOCK_PATH, 0o600)
        lock.write(f"{os.getpid()} retry {target}\n")
        lock.flush()

        try:
            conn = hme_load.connect()
            try:
                run = latest_run(conn, target)
                eligible, reason = assess_retry_eligibility(run)
            finally:
                conn.close()

            prev_id = run.get("run_id") if run else None
            say(f"latest run for {target}: run_id={prev_id} "
                f"status={(run or {}).get('status')} "
                f"reconciliation_ok={(run or {}).get('reconciliation_ok')}")
            if not eligible:
                say(f"NO-OP: {reason}")
                return EXIT_NOOP
            say(f"ELIGIBLE: {reason}")
            if args.check_only:
                say("check-only: not extracting")
                return EXIT_NOOP

            env = ingest.load_env_file(ENV_FILE)
            conn = hme_load.connect()
            try:
                inv_n = ingest.refresh_tenant_inventory(conn, fh)
            finally:
                conn.close()

            started = datetime.datetime.now(datetime.timezone.utc)
            rc, facts_path = ingest.run_extractor(target, env, fh)
            if rc != 0 or not facts_path:
                say(f"retry extraction failed: rc={rc}"
                    + ("" if facts_path else "; no facts file produced"))
                # A failed retry leaves the earlier PARTIAL record standing;
                # nothing is written and nothing is made worse.
                return EXIT_RECONCILIATION_FAILED

            with open(facts_path) as f:
                doc = json.load(f)
            if doc.get("business_date") != target:
                say(f"refusing to load: facts business_date "
                    f"{doc.get('business_date')} != target {target}")
                return EXIT_HARD_FAILURE
            doc["retry_of_run_id"] = prev_id

            conn = hme_load.connect()
            try:
                res = hme_load.load(doc, conn, dry_run=args.dry_run)
            except hme_load.LoadAborted as e:
                conn.rollback()
                say(f"retry load aborted: {e}")
                rid = hme_load.record_failed_run(
                    conn, target,
                    status=("aborted_tenant_scope"
                            if "tenant" in str(e).lower() or "store" in str(e).lower()
                            else "partial"),
                    reason=f"reconciliation retry: {e}", attempt=1,
                    started_at=started, tenant_scope_ok=doc.get("tenant_scope_ok"),
                    freshness=doc.get("freshness_detail"),
                    extractor_version=doc.get("extractor_version"))
                say(f"recorded failed run_id={rid}")
                return EXIT_HARD_FAILURE
            finally:
                if not conn.closed:
                    conn.close()

            say(f"retry run_id={res['run_id']} (previous run_id={prev_id} left "
                f"untouched as the audit record) status={res['status']} "
                f"stores={res['store_count']} recon_ok={res['reconciliation_ok']}")
            for c in res["reconciliation"]["checks"]:
                say(f"  recon [{'ok' if c['ok'] else 'FAIL'}] {c['check']}: "
                    f"observed={c['observed']} expected={c['expected']}")
            say(f"tenant inventory used: {inv_n} stores")

            if res["status"] == "succeeded":
                say("retry CORRECTED the day: complete and reconciled")
                return EXIT_OK
            if not res["reconciliation_ok"]:
                say("retry did NOT reconcile; mismatches above. Status stays partial.")
                return EXIT_RECONCILIATION_FAILED
            return ingest.classify_partial_exit(res, log=say)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()


if __name__ == "__main__":
    sys.exit(main())
