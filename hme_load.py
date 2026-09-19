#!/usr/bin/env python3
"""Load canonical HME facts produced by the separate downloader application.

    venv/bin/python hme_load.py --date 2026-09-14
    venv/bin/python hme_load.py --date 2026-09-14 --dry-run

Reads $HME_RAW_DIR/<date>/querydata-facts.json (written by
/opt/hme-report-downloader/hme_querydata.py) and upserts it into the canonical
HME tables. Idempotent: re-running the same day overwrites that day's rows and
leaves other days untouched.

Fail-closed rules enforced here as well as in the extractor, because the loader
is the last gate before data becomes queryable:

  * every store number in the payload must exist in hme_store_mapping
  * the payload's tenant_scope_ok must be true
  * business_date must equal requested_date
  * store count must match the active mapper inventory

Nothing token-shaped is ever read from the payload or written to the database.
"""
import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=False)
import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402

LOADER_VERSION = "hme_load/1.3.0"
RAW_DIR = os.environ.get("HME_RAW_DIR", "/var/lib/laynes/hme/raw")
FORBIDDEN_KEYS = ("token", "id_token", "ctx_token", "cookie", "password", "authorization")


class LoadAborted(Exception):
    pass


def connect():
    return psycopg2.connect(
        host=os.environ["DB_HOST"], port=os.environ["DB_PORT"],
        dbname=os.environ["DB_NAME"], user=os.environ["DB_USER"],
        password=os.environ["DB_PASS"])


def assert_no_secrets(doc):
    """Defence in depth: refuse to persist a payload carrying credential-shaped keys."""
    def walk(node, path=""):
        if isinstance(node, dict):
            for k, v in node.items():
                if any(f in str(k).lower() for f in FORBIDDEN_KEYS):
                    raise LoadAborted(f"payload contains forbidden key at {path}.{k}")
                walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
    walk(doc)


def reconcile(doc):
    """Cross-check independent HME measures for the same day.

    Deliberately does NOT force regular + disastrous == total: those are
    independent DAX buckets and a small residual is a real source property.
    """
    sd = {r["hme_store_number"]: r for r in doc["facts"].get("store_daily", [])}
    od = {r["hme_store_number"]: r for r in doc["facts"].get("outliers_daily", [])}
    td = {r["hme_store_number"]: r for r in doc["facts"].get("trend_store_daily", [])}
    checks, warnings = [], []

    def add(name, ok, got, exp, note=None):
        checks.append({"check": name, "ok": bool(ok), "observed": got,
                       "expected": exp, "note": note})

    tot = (doc.get("source_totals") or {}).get("store_daily") or {}
    otot = (doc.get("source_totals") or {}).get("outliers_daily") or {}

    # 1. Performance Analysis total vs Outliers car departures (independent queries)
    add("pa_total_orders_vs_outliers_car_departures",
        tot.get("total_orders") == otot.get("car_departures"),
        otot.get("car_departures"), tot.get("total_orders"))

    # 2. Sum of store rows vs the source's own grand total
    ssum = sum(r["total_orders"] or 0 for r in sd.values())
    add("store_rows_sum_vs_source_total", ssum == (tot.get("total_orders") or -1),
        ssum, tot.get("total_orders"))

    # 3. Trend total_cars agrees with PA total_orders per store
    mismatch = [s for s in sd if s in td
                and sd[s]["total_orders"] is not None
                and td[s]["total_cars"] is not None
                and sd[s]["total_orders"] != td[s]["total_cars"]]
    add("trend_total_cars_matches_pa_total_orders_per_store",
        not mismatch, len(mismatch), 0,
        None if not mismatch else f"stores differing: {mismatch[:5]}")

    # 4. Regular + Disastrous vs Total -- recorded, never corrected
    delta = (tot.get("total_orders") or 0) - ((tot.get("regular_orders") or 0)
                                              + (tot.get("disastrous_orders") or 0))
    checks.append({"check": "regular_plus_disastrous_vs_total",
                   "ok": True, "observed": delta, "expected": "not enforced",
                   "note": ("independent DAX buckets; residual recorded as a source "
                            f"property, not corrected (delta={delta})")})
    if delta:
        warnings.append(f"regular+disastrous differs from total by {delta} "
                        f"(source bucket distinction, not corrected)")

    # 5. Goals observed for the day agree with the PA threshold (Goal D)
    goals = {r["hme_store_number"]: r for r in doc["facts"].get("goal_observations", [])}
    gmis = [s for s in sd if s in goals
            and sd[s].get("lane_total_goal_d_seconds") is not None
            and goals[s].get("goal_d_seconds") is not None
            and sd[s]["lane_total_goal_d_seconds"] != goals[s]["goal_d_seconds"]]
    add("goal_d_matches_pa_threshold", not gmis, len(gmis), 0,
        None if not gmis else f"stores differing: {gmis[:5]}")

    ok = all(c["ok"] for c in checks)
    return ok, {"checks": checks, "warnings": warnings}


def record_failed_run(conn, business_date, *, status, reason, attempt=1,
                      started_at=None, tenant_scope_ok=None, freshness=None,
                      extractor_version=None):
    """Record an attempt that never reached a successful load.

    Committed on its own connection state so a failure is still auditable. Only
    non-sensitive text is stored -- never a token, cookie, or credential.
    """
    if status not in ("failed", "aborted_tenant_scope", "partial"):
        raise ValueError(f"unexpected failure status {status!r}")
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO hme_ingest_run (business_date, requested_date, source,
            source_reports, started_at, finished_at, tenant_scope_ok, store_count,
            rows_received, rows_written, reconciliation_ok, status, warnings,
            attempt, extractor_version, loader_version, freshness_ok, freshness_detail)
        VALUES (%s,%s,'powerbi_querydata',%s,%s,now(),%s,NULL,NULL,0,NULL,%s,%s,
                %s,%s,%s,%s,%s)
        RETURNING run_id
    """, (business_date, business_date, [],
          started_at or datetime.datetime.now(datetime.timezone.utc),
          tenant_scope_ok, status, [reason[:500]], attempt,
          extractor_version, LOADER_VERSION,
          None if freshness is None else bool(freshness.get("ok")),
          None if freshness is None else json.dumps(freshness)))
    run_id = cur.fetchone()[0]
    conn.commit()
    return run_id


GOAL_COLS = ("goal_a_seconds", "goal_b_seconds", "goal_c_seconds", "goal_d_seconds")


def _goal_vals(row):
    return tuple(row.get(c) for c in GOAL_COLS)


def _record_goal_conflict(cur, sn, obs_date, vals, existing, reason, run_id):
    """Case B: never rewrite history; park the disagreement for human review."""
    ex_from = ex_to = None
    ex_vals = (None, None, None, None)
    if existing:
        ex_from, ex_to = existing[0], existing[1]
        ex_vals = tuple(existing[2:6])
    cur.execute("""
        INSERT INTO hme_goal_conflict (hme_store_number, observed_date,
            observed_goal_a, observed_goal_b, observed_goal_c, observed_goal_d,
            existing_from, existing_to,
            existing_goal_a, existing_goal_b, existing_goal_c, existing_goal_d,
            reason, run_id)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (hme_store_number, observed_date, reason) DO NOTHING
    """, (sn, obs_date, *vals, ex_from, ex_to, *ex_vals, reason, run_id))


def _close_and_open(cur, sn, obs_date, vals, run_id):
    """Case D: close the open period the day before, open a new forward period."""
    cur.execute(
        "UPDATE hme_goal_history SET effective_to_date = %s "
        "WHERE hme_store_number = %s AND effective_to_date IS NULL",
        (obs_date - datetime.timedelta(days=1), sn))
    cur.execute(
        "INSERT INTO hme_goal_history (hme_store_number, effective_from_date, "
        "  goal_a_seconds, goal_b_seconds, goal_c_seconds, goal_d_seconds, "
        "  observed_run_id) VALUES (%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (hme_store_number, effective_from_date) DO NOTHING",
        (sn, obs_date, *vals, run_id))
    return "opened"


def apply_goal_observation(cur, obs, obs_date, run_id):
    """Fold one goal observation into hme_goal_history.

    The source exposes no per-date goal, so a goal value is only ever known
    as-of an observation. Load order must not decide history, which is why an
    older observation carrying IDENTICAL values is allowed to widen a period
    backward while an older observation carrying DIFFERENT values is not
    allowed to rewrite anything.

      A older + identical  -> extend effective_from_date backward   ("extended")
      B older + different  -> log a conflict, change nothing         ("conflicts")
      C newer + identical  -> nothing to do                          ("unchanged")
      D newer + changed    -> close the open period, open a new one  ("opened")

    Returns one of: extended | conflicts | unchanged | opened.
    """
    sn = obs["hme_store_number"]
    vals = _goal_vals(obs)

    cur.execute("""SELECT effective_from_date, effective_to_date,
                          goal_a_seconds, goal_b_seconds, goal_c_seconds, goal_d_seconds
                   FROM hme_goal_history
                   WHERE hme_store_number = %s
                   ORDER BY effective_from_date""", (sn,))
    periods = cur.fetchall()

    if not periods:
        cur.execute("""INSERT INTO hme_goal_history (hme_store_number, effective_from_date,
                           goal_a_seconds, goal_b_seconds, goal_c_seconds, goal_d_seconds,
                           observed_run_id)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)""", (sn, obs_date, *vals, run_id))
        return "opened"

    # Does a recorded period already cover this observation date?
    covering = next((p for p in periods
                     if p[0] <= obs_date and (p[1] is None or obs_date <= p[1])), None)
    if covering is not None:
        if tuple(covering[2:6]) == vals:
            return "unchanged"
        # Values differ from the period covering this date. An OPEN period
        # nominally covers every future date, so a LATER observation carrying new
        # values is a legitimate forward change (case D), not a conflict.
        if covering[1] is None and obs_date > covering[0]:
            return _close_and_open(cur, sn, obs_date, vals, run_id)
        # Otherwise the observation genuinely contradicts recorded history:
        # either a CLOSED period covering this date, or a disagreement about the
        # period's own start date. Neither is ever rewritten automatically.
        _record_goal_conflict(cur, sn, obs_date, vals, covering,
                              "observation_contradicts_recorded_period", run_id)
        return "conflicts"

    earliest = periods[0]
    if obs_date < earliest[0]:
        # --- older than anything recorded ---
        if tuple(earliest[2:6]) == vals:
            # Case A: same values seen earlier -> the stable period started earlier.
            cur.execute("""UPDATE hme_goal_history SET effective_from_date = %s
                           WHERE hme_store_number = %s AND effective_from_date = %s""",
                        (obs_date, sn, earliest[0]))
            return "extended"
        # Case B: different values before our earliest record. Opening a period
        # here would imply a change date we never observed, so refuse.
        _record_goal_conflict(cur, sn, obs_date, vals, earliest,
                              "older_observation_differs_from_earliest_period", run_id)
        return "conflicts"

    # --- newer than everything recorded (gap after the last closed period, or
    #     after an open period that somehow ended) ---
    latest = periods[-1]
    if tuple(latest[2:6]) == vals:
        if latest[1] is not None:
            # Same values resume after a closed period: reopen by extending it.
            cur.execute("""UPDATE hme_goal_history SET effective_to_date = NULL
                           WHERE hme_store_number = %s AND effective_from_date = %s""",
                        (sn, latest[0]))
            return "extended"
        return "unchanged"
    # Case D: values changed after everything recorded.
    return _close_and_open(cur, sn, obs_date, vals, run_id)


def load(doc, conn, dry_run=False):
    date = doc["business_date"]
    if doc["business_date"] != doc["requested_date"]:
        raise LoadAborted("business_date != requested_date; date provenance broken")
    if not doc.get("tenant_scope_ok"):
        raise LoadAborted("payload tenant_scope_ok is not true; refusing to load")
    assert_no_secrets(doc)

    cur = conn.cursor()
    cur.execute("""SELECT hme_store_number, hme_store_name, mapping_status
                   FROM hme_store_mapping WHERE active""")
    inv = {r[0]: {"hme_store_name": r[1], "mapping_status": r[2]} for r in cur.fetchall()}
    known = set(inv)

    payload_stores = set()
    for rows in doc["facts"].values():
        payload_stores |= {str(r["hme_store_number"]) for r in rows}

    # --- SECURITY: an unexpected store is widening and is always fatal ------
    unknown = sorted(payload_stores - known)
    if unknown:
        raise LoadAborted(
            f"{len(unknown)} store(s) not in hme_store_mapping -> {unknown[:10]}; "
            f"tenant scope unproven, refusing to load")

    # --- COMPLETENESS: absence is graded, never silently accepted -----------
    # A missing VERIFIED store means the day cannot represent Laynes coverage
    # and is refused. Missing non-VERIFIED stores are allowed through but make
    # the run PARTIAL. No zero rows are ever synthesised for an absent store.
    missing = sorted(known - payload_stores)
    missing_verified = [n for n in missing
                        if (inv[n]["mapping_status"] or "").upper() == "VERIFIED"]
    if missing_verified:
        raise LoadAborted(
            f"{len(missing_verified)} VERIFIED store(s) absent from the extract "
            f"-> {missing_verified}; refusing to load a day that cannot represent "
            f"mapped Laynes coverage")
    completeness = {
        "expected_store_count": len(known),
        "observed_store_count": len(payload_stores),
        "missing_store_numbers": missing,
        "missing_store_names": [inv[n]["hme_store_name"] for n in missing],
        "missing_mapping_statuses": [inv[n]["mapping_status"] for n in missing],
        "verified_expected_count": sum(
            1 for v in inv.values() if (v["mapping_status"] or "").upper() == "VERIFIED"),
        "verified_observed_count": sum(
            1 for n in payload_stores
            if (inv[n]["mapping_status"] or "").upper() == "VERIFIED"),
        "complete": not missing,
    }

    # The extractor's own freshness verdict is honoured here too: an incomplete
    # day must never be written as if it were final.
    if doc.get("freshness_ok") is False:
        raise LoadAborted(
            f"extract for {date} is marked not fresh "
            f"({doc.get('freshness_detail')}); refusing to load an incomplete day")

    recon_ok, recon = reconcile(doc)

    cur.execute("""SELECT count(*) FILTER (WHERE mapping_status='VERIFIED'),
                          count(*) FILTER (WHERE mapping_status='OUT_OF_SCOPE')
                   FROM hme_store_mapping WHERE active""")
    verified_n, oos_n = cur.fetchone()

    rows_received = sum(len(v) for v in doc["facts"].values())
    cur.execute("""
        INSERT INTO hme_ingest_run (business_date, requested_date, source, source_reports,
            query_hash, started_at, finished_at, tenant_scope_ok, tenant_scope_detail,
            store_count, verified_store_count, out_of_scope_store_count,
            rows_received, reconciliation_ok, reconciliation_detail,
            status, warnings, extractor_version, loader_version, attempt,
            freshness_ok, freshness_detail)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'running',%s,%s,%s,%s,%s,%s)
        RETURNING run_id
    """, (date, doc["requested_date"], doc["source"], doc["source_reports"],
          doc.get("query_hash"), doc["started_at"], doc["finished_at"],
          True, json.dumps(doc.get("tenant_scope_detail")), len(payload_stores),
          verified_n, oos_n,
          rows_received, recon_ok, json.dumps(recon), recon["warnings"],
          doc.get("extractor_version"), LOADER_VERSION, int(doc.get("attempt", 1)),
          doc.get("freshness_ok"),
          json.dumps({"freshness": doc.get("freshness_detail"),
                      "completeness": completeness})))
    run_id = cur.fetchone()[0]

    trend = {r["hme_store_number"]: r for r in doc["facts"].get("trend_store_daily", [])}
    written = 0

    sd_rows = [(r["hme_store_number"], date, r["total_orders"], r["regular_orders"],
                r["disastrous_orders"], r["disastrous_pct"], r["lane_total_avg_seconds"],
                r["lane_queue_avg_seconds"], r["lane_total_2_avg_seconds"],
                r["lane_total_goal_d_seconds"],
                (trend.get(r["hme_store_number"]) or {}).get("total_cars"),
                (trend.get(r["hme_store_number"]) or {}).get("avg_time_seconds"),
                r["hme_store_name_seen"], run_id)
               for r in doc["facts"].get("store_daily", [])]
    psycopg2.extras.execute_batch(cur, """
        INSERT INTO hme_store_daily (hme_store_number, business_date, total_orders,
            regular_orders, disastrous_orders, disastrous_pct, lane_total_avg_seconds,
            lane_queue_avg_seconds, lane_total_2_avg_seconds, lane_total_goal_d_seconds,
            total_cars, trend_avg_time_seconds, hme_store_name_seen, run_id)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (hme_store_number, business_date) DO UPDATE SET
            total_orders=EXCLUDED.total_orders, regular_orders=EXCLUDED.regular_orders,
            disastrous_orders=EXCLUDED.disastrous_orders,
            disastrous_pct=EXCLUDED.disastrous_pct,
            lane_total_avg_seconds=EXCLUDED.lane_total_avg_seconds,
            lane_queue_avg_seconds=EXCLUDED.lane_queue_avg_seconds,
            lane_total_2_avg_seconds=EXCLUDED.lane_total_2_avg_seconds,
            lane_total_goal_d_seconds=EXCLUDED.lane_total_goal_d_seconds,
            total_cars=EXCLUDED.total_cars,
            trend_avg_time_seconds=EXCLUDED.trend_avg_time_seconds,
            hme_store_name_seen=EXCLUDED.hme_store_name_seen,
            run_id=EXCLUDED.run_id, ingested_at=now()
    """, sd_rows)
    written += len(sd_rows)

    od_rows = [(r["hme_store_number"], date, r["all_car_records"], r["car_departures"],
                r["total_outliers"], r["outlier_pct"], r["pull_in_pct"], r["pull_out_pct"],
                r["max_over_delete_pct"], r["discard_pct"], r["manual_delete_pct"],
                r["hme_store_name_seen"], run_id)
               for r in doc["facts"].get("outliers_daily", [])]
    psycopg2.extras.execute_batch(cur, """
        INSERT INTO hme_outliers_daily (hme_store_number, business_date, all_car_records,
            car_departures, total_outliers, outlier_pct, pull_in_pct, pull_out_pct,
            max_over_delete_pct, discard_pct, manual_delete_pct, hme_store_name_seen, run_id)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (hme_store_number, business_date) DO UPDATE SET
            all_car_records=EXCLUDED.all_car_records,
            car_departures=EXCLUDED.car_departures,
            total_outliers=EXCLUDED.total_outliers, outlier_pct=EXCLUDED.outlier_pct,
            pull_in_pct=EXCLUDED.pull_in_pct, pull_out_pct=EXCLUDED.pull_out_pct,
            max_over_delete_pct=EXCLUDED.max_over_delete_pct,
            discard_pct=EXCLUDED.discard_pct, manual_delete_pct=EXCLUDED.manual_delete_pct,
            hme_store_name_seen=EXCLUDED.hme_store_name_seen,
            run_id=EXCLUDED.run_id, ingested_at=now()
    """, od_rows)
    written += len(od_rows)

    # Goal history, four explicit cases (see apply_goal_observation).
    goal_stats = {"extended": 0, "opened": 0, "closed": 0,
                  "unchanged": 0, "conflicts": 0}
    obs_date = datetime.date.fromisoformat(date)
    for r in doc["facts"].get("goal_observations", []):
        outcome = apply_goal_observation(cur, r, obs_date, run_id)
        goal_stats[outcome] = goal_stats.get(outcome, 0) + 1
    goal_written = goal_stats["extended"] + goal_stats["opened"]
    written += goal_written
    if goal_stats["conflicts"]:
        recon["warnings"].append(
            f"{goal_stats['conflicts']} goal observation(s) conflict with recorded "
            f"history; see hme_goal_conflict (history was NOT rewritten)")

    # Run status must reflect reconciliation. A day whose cross-source checks
    # disagree is NOT a successful production ingestion: the facts are loaded and
    # remain idempotently correctable, but the run is recorded 'partial' so no
    # consumer or monitor can later read it as reconciled.
    #   reconciliation_ok = true  -> 'succeeded'
    #   reconciliation_ok = false -> 'partial'
    # (Previously both branches returned 'succeeded' -- a no-op conditional that
    # let the 2026-09-16 Trend/PA divergence be recorded as a clean success.)
    final_status = "succeeded" if (recon_ok and completeness["complete"]) else "partial"
    if not recon_ok:
        failed_checks = [c["check"] for c in recon["checks"] if not c["ok"]]
        recon["warnings"].append(
            "run marked PARTIAL: reconciliation failed -> " + ", ".join(failed_checks))
    if not completeness["complete"]:
        # Tenant scope did NOT widen -- the source simply did not report these
        # stores. Recorded as absence, never as zero.
        recon["warnings"].append(
            "run marked PARTIAL: source incomplete; "
            f"missing expected HME stores: {completeness['missing_store_numbers']}; "
            f"missing store names: {completeness['missing_store_names']}; "
            f"mapping statuses: {completeness['missing_mapping_statuses']}; "
            f"observed_store_count: {completeness['observed_store_count']}; "
            f"expected_store_count: {completeness['expected_store_count']}; "
            f"verified {completeness['verified_observed_count']}"
            f"/{completeness['verified_expected_count']} present")
    cur.execute("""UPDATE hme_ingest_run SET rows_written=%s, status=%s, warnings=%s
                   WHERE run_id=%s""",
                (written, final_status, recon["warnings"], run_id))

    if dry_run:
        conn.rollback()
        print("DRY RUN -- rolled back")
    else:
        conn.commit()
    return {"run_id": run_id, "rows_received": rows_received, "rows_written": written,
            "goal_rows": goal_written, "goal_stats": goal_stats,
            "reconciliation_ok": recon_ok, "reconciliation": recon,
            "status": final_status, "completeness": completeness,
            "store_count": len(payload_stores)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--file", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    path = args.file or os.path.join(RAW_DIR, args.date, "querydata-facts.json")
    with open(path) as fh:
        doc = json.load(fh)
    if doc["business_date"] != args.date:
        raise LoadAborted(f"payload business_date {doc['business_date']} != --date {args.date}")

    conn = connect()
    try:
        res = load(doc, conn, dry_run=args.dry_run)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print(f"run_id={res['run_id']}  status={res['status'].upper()}  "
          f"stores={res['store_count']}  received={res['rows_received']}  "
          f"written={res['rows_written']} (goal rows {res['goal_rows']})")
    c = res["completeness"]
    print(f"completeness: {'COMPLETE' if c['complete'] else 'INCOMPLETE'} "
          f"{c['observed_store_count']}/{c['expected_store_count']} stores; "
          f"VERIFIED {c['verified_observed_count']}/{c['verified_expected_count']}"
          + (f"; missing {c['missing_store_numbers']} {c['missing_store_names']} "
             f"{c['missing_mapping_statuses']}" if not c['complete'] else ""))
    print(f"goals: {res['goal_stats']}")
    print(f"reconciliation_ok={res['reconciliation_ok']}")
    for c in res["reconciliation"]["checks"]:
        flag = "ok " if c["ok"] else "FAIL"
        print(f"  [{flag}] {c['check']}: observed={c['observed']} expected={c['expected']}"
              + (f"  -- {c['note']}" if c.get("note") else ""))
    for w in res["reconciliation"]["warnings"]:
        print(f"  warning: {w}")
    if res["status"] != "succeeded":
        # Exit non-zero so systemd and the orchestrator show a PARTIAL day as a
        # problem rather than a clean production ingestion. Name the ACTUAL
        # cause: reconciliation and completeness are independent reasons.
        causes = []
        if not res["reconciliation_ok"]:
            causes.append("reconciliation did not pass")
        if not res["completeness"]["complete"]:
            causes.append("source incomplete "
                          f"({res['completeness']['observed_store_count']}"
                          f"/{res['completeness']['expected_store_count']} stores, "
                          f"missing {res['completeness']['missing_store_numbers']})")
        print(f"LOAD COMPLETED AS {res['status'].upper()}: " + "; ".join(causes))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
