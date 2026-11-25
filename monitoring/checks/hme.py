"""HME drive-thru: daily ingestion, network reconciliation, the scheduled retry,
and per-store trust for every VERIFIED-mapped location.

Timeline for business day D (America/Chicago), from the production timers:
  D+1 05:30  hme-daily-ingest (morning run)
  D+1 10:15  hme-reconciliation-retry (re-extracts D once sources settle)

Before morning_ready: no verdict (PENDING).
Between morning_ready and retry_ready:
  * no run at all, or a failed run               -> HIGH (ingestion problem)
  * unreconciled on settling checks only          -> PENDING "settling" (HME
    Trend/Outliers lag PA for hours after close; the retry normally fixes it)
  * unreconciled on any other check (goal D, row sums), or a store with
    NO_ACCESS                                     -> WARNING (visible, not pushed)
After retry_ready, anything still unreconciled or unverified -> HIGH, which is
what pushes an alert. A morning mismatch that the retry fixes therefore never
alerts, and a persistent one alerts once, after the retry.

Sources of truth: hme_ingest_run (network, store_scope IS NULL), the trust view
v_hme_store_day_trust_llm (per store-day: exactly what AI Chat and MCP serve),
and hme_store_ingest_result (the reason a store-day is not VERIFIED).
"""
from __future__ import annotations

from .. import clock
from ..model import (ACCEPTED, CRITICAL, FAIL, HIGH, OK, PENDING, SRC_HME, WARNING,
                     CheckResult)
from ..pg import rows

CHECK_DAY = "day_reconciliation"
CHECK_STORE = "store_day_trust"

_RUNS_SQL = """
SELECT run_id, business_date, attempt, status, reconciliation_ok, reconciliation_detail,
       store_count, verified_store_count, started_at, finished_at, loader_version
FROM hme_ingest_run
WHERE business_date BETWEEN %(lo)s AND %(hi)s AND store_scope IS NULL
ORDER BY business_date, run_id
"""

_MAPPED_SQL = """
SELECT m.hme_store_number, m.revel_establishment_id AS establishment_id, e.name AS store_name
FROM hme_store_mapping m JOIN establishments e ON e.id = m.revel_establishment_id
WHERE m.active AND m.mapping_status = 'VERIFIED'
ORDER BY m.revel_establishment_id
"""

_TRUST_SQL = """
SELECT establishment_id, business_date, trust_status, store_result_status, store_result_reason
FROM v_hme_store_day_trust_llm
WHERE business_date BETWEEN %(lo)s AND %(hi)s
"""

# The reason recorded by the LATEST network run per date (a scoped backfill may
# have fixed a store later; the trust view already accounts for that).
_RESULTS_SQL = """
SELECT DISTINCT ON (r.business_date, r.revel_establishment_id)
       r.business_date, r.revel_establishment_id AS establishment_id, r.hme_store_number,
       r.status, r.reason_code, r.detail, r.run_id
FROM hme_store_ingest_result r
JOIN hme_ingest_run h ON h.run_id = r.run_id AND h.store_scope IS NULL
WHERE r.business_date BETWEEN %(lo)s AND %(hi)s AND r.revel_establishment_id IS NOT NULL
ORDER BY r.business_date, r.revel_establishment_id, r.run_id DESC
"""


def _failed_checks(run) -> list[str]:
    det = run.get("reconciliation_detail") or {}
    return [c.get("check") for c in det.get("checks", []) if c.get("ok") is False]


def _goal_stores(run) -> list[str]:
    for c in (run.get("reconciliation_detail") or {}).get("checks", []):
        if c.get("check") == "goal_d_matches_pa_threshold" and c.get("ok") is False:
            return list(c.get("stores") or [])
    return []


def days(ctx) -> list[CheckResult]:
    cfg = ctx.cfg["hme"]
    pg = ctx.pg()
    lookback = clock.closed_business_days(ctx.now, cfg["lookback_days"])
    lo, hi = lookback[-1], lookback[0]
    p = {"lo": lo, "hi": hi}
    runs: dict[str, list[dict]] = {}
    for r in rows(pg, _RUNS_SQL, p):
        runs.setdefault(r["business_date"].isoformat(), []).append(r)
    mapped = rows(pg, _MAPPED_SQL)
    trust = {(r["establishment_id"], r["business_date"].isoformat()): r
             for r in rows(pg, _TRUST_SQL, p)}
    results = {(r["establishment_id"], r["business_date"].isoformat()): r
               for r in rows(pg, _RESULTS_SQL, p)}
    accepted_dates = cfg["accepted_dates"]
    accepted_stores = {int(k): v for k, v in cfg["accepted_stores"].items()}
    settling = set(cfg["settling_checks"])

    out: list[CheckResult] = []
    last_ok = None
    for day in lookback:
        d = day.isoformat()
        morning = clock.ready_at(day, cfg["morning_ready_local"])
        retry = clock.ready_at(day, cfg["retry_ready_local"])
        after_retry = ctx.now >= retry
        day_runs = runs.get(d, [])
        good = [r for r in day_runs if r["status"] in ("succeeded", "partial")]
        latest = good[-1] if good else None
        if latest and latest["reconciliation_ok"]:
            last_ok = max(last_ok or latest["finished_at"], latest["finished_at"])
        net = dict(source=SRC_HME, check=CHECK_DAY, business_date=d,
                   expected="network reconciled (all checks exact)")
        ev = {"runs": [{"run_id": r["run_id"], "status": r["status"],
                        "reconciliation_ok": r["reconciliation_ok"],
                        "finished_at": r["finished_at"].isoformat() if r["finished_at"] else None,
                        "failed_checks": _failed_checks(r)} for r in day_runs],
              "morning_ready": morning.isoformat(), "retry_ready": retry.isoformat()}

        if ctx.now < morning:
            out.append(CheckResult(state=PENDING, title="Awaiting HME morning ingestion", **net))
            out += [_store_pending(m, d, "Awaiting HME morning ingestion") for m in mapped]
            continue
        if d in accepted_dates:
            out.append(CheckResult(state=ACCEPTED, title=f"Accepted: {accepted_dates[d]}",
                                   evidence=ev, **net))
            out += [CheckResult(source=SRC_HME, check=CHECK_STORE, state=ACCEPTED,
                                store_id=m["establishment_id"], store_name=m["store_name"],
                                business_date=d, title=f"Accepted: {accepted_dates[d]}")
                    for m in mapped]
            continue

        failed = _failed_checks(latest) if latest else []
        settling_only = bool(failed) and set(failed) <= settling
        if latest is None:
            failed_runs = [r for r in day_runs if r["status"] not in ("succeeded", "partial")]
            out.append(CheckResult(
                state=FAIL, severity=CRITICAL if after_retry else HIGH,
                category="ingestion_failed" if failed_runs else "missing_ingestion",
                title=("HME ingestion failed for the day" if failed_runs
                       else "No HME ingestion run for the day"),
                actual=(f"{len(failed_runs)} failed run(s)" if failed_runs else "no run"),
                evidence=ev, actions=_actions("missing"), **net))
        elif latest["reconciliation_ok"]:
            out.append(CheckResult(state=OK, title="HME network reconciled", evidence=ev,
                                   actual=f"run {latest['run_id']} reconciled", **net))
        elif not after_retry and settling_only:
            out.append(CheckResult(state=PENDING, category="settling",
                                   title="HME source settling; scheduled retry pending",
                                   evidence=ev, actual=", ".join(failed), **net))
        else:
            goal = "goal_d_matches_pa_threshold" in failed
            cat = ("goal_threshold_mismatch" if goal else
                   "settling_persisted" if settling_only else "reconciliation_failed")
            ev["goal_mismatch_hme_stores"] = _goal_stores(latest)
            out.append(CheckResult(
                state=FAIL, severity=HIGH if after_retry else WARNING, category=cat,
                title=("HME day unreconciled after scheduled retry" if after_retry
                       else "HME day unreconciled (retry pending)"),
                actual=f"failed: {', '.join(failed)}",
                difference=_diff(latest), evidence=ev, actions=_actions(cat), **net))

        for m in mapped:
            out.append(_store_result(m, d, trust, results, after_retry, accepted_stores,
                                     settling_only, latest))
    ctx.source_status(SRC_HME, last_ok, {"mapped_stores": len(mapped)})
    return out


def _diff(run) -> str | None:
    parts = []
    for c in (run.get("reconciliation_detail") or {}).get("checks", []):
        if c.get("ok") is False:
            parts.append(f"{c.get('check')}: observed {c.get('observed')} vs expected "
                         f"{c.get('expected')}")
    return "; ".join(parts) or None


def _store_pending(m, d, title) -> CheckResult:
    return CheckResult(source=SRC_HME, check=CHECK_STORE, state=PENDING,
                       store_id=m["establishment_id"], store_name=m["store_name"],
                       business_date=d, title=title)


def _store_result(m, d, trust, results, after_retry, accepted, settling_only, latest):
    est = m["establishment_id"]
    base = dict(source=SRC_HME, check=CHECK_STORE, store_id=est, store_name=m["store_name"],
                business_date=d, expected="store-day VERIFIED")
    if est in accepted:
        return CheckResult(state=ACCEPTED, title=f"Excluded: {accepted[est]}", **base)
    t = trust.get((est, d))
    if t and t["trust_status"] == "VERIFIED":
        return CheckResult(state=OK, title="HME store-day verified", **base)
    res = results.get((est, d)) or {}
    reason = res.get("reason_code") or (t or {}).get("store_result_reason")
    status = res.get("status") or (t or {}).get("store_result_status")
    detail = res.get("detail") or {}
    ev = {"hme_store_number": m["hme_store_number"], "trust_status": (t or {}).get("trust_status"),
          "store_result_status": status, "store_result_reason": reason, "detail": detail,
          "run_id": res.get("run_id")}
    if latest is None and not t:
        # The day-level incident already covers a missing run; the store has no data yet.
        return CheckResult(state=PENDING if not after_retry else FAIL,
                           severity=HIGH, category="missing_ingestion",
                           title="No HME data for the store-day (no ingestion run)",
                           actual="no data", evidence=ev, actions=_actions("missing"), **base)
    cat = {"RECONCILIATION_MISMATCH": "store_reconciliation_mismatch",
           "NO_ACCESS": "no_access", "NO_DATA": "no_data",
           "INGEST_ERROR": "ingest_error"}.get(reason or "", "missing")
    if not after_retry and cat == "store_reconciliation_mismatch" and settling_only:
        return CheckResult(state=PENDING, category="settling",
                           title="HME store figures settling; retry pending", evidence=ev, **base)
    title = {
        "store_reconciliation_mismatch": "HME store-day did not reconcile",
        "no_access": "HME store not visible to the account (NO_ACCESS)",
        "no_data": "HME returned no data for the store",
        "ingest_error": "HME store ingestion error",
        "missing": "HME store-day missing",
    }[cat]
    return CheckResult(state=FAIL, severity=HIGH if after_retry else WARNING, category=cat,
                       title=title + ("" if after_retry else " (retry pending)"),
                       actual=f"{status or 'no row'} / {reason or 'no trust row'}",
                       evidence=ev, actions=_actions(cat), **base)


def _actions(cat: str) -> list[str]:
    excluded = ("AI Chat and MCP exclude UNVERIFIED HME store-days; nothing is served "
                "from them until a reconciled run exists.")
    return {
        "missing": [
            "Check `systemctl status hme-daily-ingest hme-reconciliation-retry` and the logs "
            "in /var/lib/laynes/hme/logs/.",
            "Do not trigger ingestion from the monitor. Re-run manually only after the cause "
            "is understood (lock free, no concurrent HME job).", excluded],
        "goal_threshold_mismatch": [
            "HME reported goal values that differ from goal history (see hme_goal_conflict "
            "for the business date). Confirm in the HME portal whether goals were changed.",
            "If the goal change is real, record it through the goal-history procedure; if "
            "the observation is wrong, leave the day unreconciled.", excluded],
        "settling_persisted": [
            "HME Trend/Outliers still differ from PA after the 10:15 retry. Compare the "
            "retry log with the raw extract for the date.",
            "A later backfill for the date may reconcile it once HME settles; run it "
            "manually only with approval.", excluded],
        "reconciliation_failed": [
            "A non-settling reconciliation check failed. Read the retry log and "
            "reconciliation_detail for the run before anything else.", excluded],
        "store_reconciliation_mismatch": [
            "This store's figures differ between HME reports. See the day incident and "
            "the store's row in hme_store_ingest_result.", excluded],
        "no_access": [
            "The HME account cannot see this store for the day. Check the store's access "
            "in the HME portal (user/store permissions).", excluded],
        "no_data": ["HME returned no rows for the store: confirm whether it traded.", excluded],
        "ingest_error": ["Read the ingest log for the run; the store row failed to load.",
                         excluded],
    }.get(cat, [excluded])

