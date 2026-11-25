"""R365: weekly operational ingestion (per store) and financial-period coverage.

Weekly (r365-weekly-ingest, Thu 20:00 Chicago): weeks run Thursday-Wednesday.
The week ending Wednesday W is expected after week_ready_local on W+1. For each
of the last `lookback_weeks` expected weeks the verdict comes from
v_r365_completeness_llm (the view AI Chat's R365 gate reads), and each VERIFIED
store gets its own result from r365_store_daily (7 days; CLOSED / PRE_OPENING
days count as present -- they are source facts, not gaps).

Financial (GL expenses): r365-financial-ingest has no installed timer, so a
fiscal period that has CLOSED but was last loaded before its close is reported
(WARNING -- the figures are not wrong, they are incomplete for the period).
A failed reconciliation is HIGH.
"""
from __future__ import annotations

import os
import sys

from datetime import date, timedelta

from .. import clock
from ..model import (ACCEPTED, FAIL, HIGH, OK, PENDING, SRC_R365, SRC_R365_FIN, WARNING,
                     CheckResult)
from ..pg import rows

CHECK_WEEK = "weekly_coverage"
CHECK_STORE_WEEK = "store_week_coverage"
CHECK_PERIOD = "period_coverage"
CHECK_PERIOD_SOURCE = "period_source_sync"

_WEEKS_SQL = """
SELECT week_start, week_end, week_status, source_complete, reconciliation_ok,
       expected_store_count, observed_store_count, stores_with_full_week, missing_store_days,
       closed_store_days, pre_opening_store_days, latest_refresh_failed, loaded_at
FROM v_r365_completeness_llm
WHERE week_start = ANY(%(starts)s)
"""

_STORE_DAYS_SQL = """
SELECT m.establishment_id, e.name AS store_name, m.opening_date,
       COUNT(d.business_date) AS days_present
FROM r365_store_mapping m
JOIN establishments e ON e.id = m.establishment_id
LEFT JOIN r365_store_daily d ON d.r365_store_id = m.r365_store_id
                            AND d.week_start = %(ws)s
WHERE m.status = 'VERIFIED' AND m.active
GROUP BY m.establishment_id, e.name, m.opening_date
ORDER BY m.establishment_id
"""

_LAST_RUN_SQL = """
SELECT id, status, error_code, completed_at, requested_week_start
FROM r365_ingest_run ORDER BY id DESC LIMIT 1
"""


def expected_weeks(now, ready_hhmm: str, n: int) -> list[date]:
    """Week starts (Thursdays) of the n most recent weeks that should be loaded."""
    today = clock.chicago(now).date()
    # Most recent Wednesday strictly before today.
    w = today - timedelta(days=(today.weekday() - 2) % 7 or 7)
    if now < clock.at_local(w + timedelta(days=1), ready_hhmm):
        w -= timedelta(days=7)
    return [w - timedelta(days=6 + 7 * i) for i in range(n)]


def weekly(ctx) -> list[CheckResult]:
    cfg = ctx.cfg["r365"]
    pg = ctx.pg()
    starts = expected_weeks(ctx.now, cfg["week_ready_local"], cfg["lookback_weeks"])
    weeks = {r["week_start"]: r for r in rows(pg, _WEEKS_SQL, {"starts": starts})}
    out: list[CheckResult] = []
    newest_loaded = None
    covered: dict[int, str] | None = None   # VERIFIED active r365_store_mapping, when read
    for ws in starts:
        we = ws + timedelta(days=6)
        q = f"{ws.isoformat()}..{we.isoformat()}"
        base = dict(source=SRC_R365, check=CHECK_WEEK, qualifier=q, business_date=we.isoformat(),
                    expected="week COMPLETE, source complete, reconciled")
        w = weeks.get(ws)
        if w is None:
            out.append(CheckResult(
                state=FAIL, severity=HIGH, category="missing_ingestion",
                title="R365 week not loaded", actual="no COMPLETE/PARTIAL run for the week",
                actions=_actions("missing"), **base))
            continue
        if w["loaded_at"]:
            newest_loaded = max(newest_loaded or w["loaded_at"], w["loaded_at"])
        ev = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in w.items()}
        problems = []
        if w["week_status"] != "COMPLETE":
            problems.append(f"status {w['week_status']}")
        if w["source_complete"] is False:
            problems.append("source incomplete")
        if w["reconciliation_ok"] is False:
            problems.append("reconciliation failed")
        if w["latest_refresh_failed"]:
            problems.append("latest refresh FAILED")
        if (w["stores_with_full_week"] or 0) < (w["expected_store_count"] or 0):
            problems.append(f"{w['stores_with_full_week']}/{w['expected_store_count']} stores "
                            "with a full week")
        if problems:
            out.append(CheckResult(state=FAIL, severity=HIGH, category="incomplete",
                                   title="R365 week incomplete or unreconciled",
                                   actual="; ".join(problems), evidence=ev,
                                   actions=_actions("incomplete"), **base))
        else:
            out.append(CheckResult(state=OK, title="R365 week complete", evidence=ev, **base))
        for s in rows(pg, _STORE_DAYS_SQL, {"ws": ws}):
            covered = covered if covered is not None else {}
            covered[s["establishment_id"]] = s["store_name"]
            sb = dict(source=SRC_R365, check=CHECK_STORE_WEEK, store_id=s["establishment_id"],
                      store_name=s["store_name"], qualifier=q, business_date=we.isoformat(),
                      expected="7 days")
            if s["opening_date"] and s["opening_date"] > we:
                out.append(CheckResult(state=ACCEPTED, title="Store not open yet", **sb))
            elif int(s["days_present"]) >= 7:
                out.append(CheckResult(state=OK, title="R365 store week complete",
                                       actual="7 days", **sb))
            else:
                out.append(CheckResult(
                    state=FAIL, severity=HIGH, category="coverage_gap",
                    title="R365 store week has missing days",
                    actual=f"{s['days_present']} of 7 days", evidence={"week_start": ws.isoformat()},
                    actions=_actions("incomplete"), **sb))
    last = rows(pg, _LAST_RUN_SQL)
    detail = {"last_run": {k: (v.isoformat() if hasattr(v, "isoformat") else v)
                           for k, v in (last[0] if last else {}).items()}}
    if covered is not None:     # omitted (coverage unknown) when no expected week was loaded
        detail["covered_stores"] = [{"id": k, "name": v} for k, v in sorted(covered.items())]
    ctx.source_status(SRC_R365, newest_loaded, detail)
    return out


_PERIODS_SQL = """
SELECT fiscal_year, period_number, period_label, start_date, end_date
FROM ayg_business_periods
WHERE start_date <= %(today)s
ORDER BY start_date DESC
LIMIT %(n)s
"""

_PERIOD_RUN_SQL = """
SELECT DISTINCT ON (requested_fiscal_year, requested_period_number)
       requested_fiscal_year, requested_period_number, id, status, completed_at,
       reconciliation_ok, source_sync_stale, source_last_sync_at, error_code, trigger,
       rows_changed, rows_deleted, transaction_rows_changed, transaction_rows_deleted
FROM r365_financial_ingest_run
WHERE target_kind = 'PERIOD' AND status IN ('COMPLETE', 'PARTIAL')
ORDER BY requested_fiscal_year, requested_period_number, id DESC
"""

_PERIOD_ATTEMPT_SQL = """ /* r365_fin_last_attempt */
SELECT DISTINCT ON (requested_fiscal_year, requested_period_number)
       requested_fiscal_year, requested_period_number, id, status, error_code, started_at
FROM r365_financial_ingest_run
WHERE target_kind = 'PERIOD' AND status <> 'RUNNING'
ORDER BY requested_fiscal_year, requested_period_number, id DESC
"""

_SCHEDULED_SQL = """ /* r365_fin_scheduled */
SELECT max(started_at) AS last_started,
       max(completed_at) FILTER (WHERE status IN ('COMPLETE', 'PARTIAL')) AS last_ok,
       count(*) FILTER (WHERE status = 'FAILED' AND started_at > now() - interval '36 hours')
           AS failed_36h
FROM r365_financial_ingest_run
WHERE trigger = 'scheduled'
"""


CHECK_SCHEDULE = "scheduled_ingest"
SCHEDULE_MAX_GAP = timedelta(hours=36)    # daily 07:00 Chicago run; one slip tolerated

# shared rule (also used by AI Chat / MCP), imported from the release root
try:
    import r365_financial_freshness as FR          # noqa: E402
except ImportError:                               # pragma: no cover - run outside the repo root
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    import r365_financial_freshness as FR          # noqa: E402

_COVERAGE = {   # refresh-state code -> (category, severity, title); severity None = decided by grace
    "never_loaded": ("missing_ingestion", None, "R365 financial period never loaded"),
    "not_reconciled": ("reconciliation_failed", HIGH, "R365 financial period did not reconcile"),
    "reconciliation_unknown": ("reconciliation_unknown", HIGH,
                               "R365 financial load has no confirmed reconciliation result"),
    "source_status_unknown": ("source_status_unknown", WARNING,
                              "R365 source status unknown for this closed period"),
    "refresh_failed": ("refresh_failed", WARNING,
                       "Latest R365 financial refresh failed; figures are from an earlier load"),
    "loaded_before_close": ("stale_after_close", WARNING,
                            "R365 financial period closed but last loaded before close"),
    "source_incomplete": ("source_incomplete", WARNING,
                          "R365 source reports this closed period incomplete"),
    "window_overdue": ("refresh_overdue", WARNING,
                       "Closed period inside the source's correction window not refreshed recently"),
    "not_refreshed_after_window": ("not_refreshed_after_repair_window", WARNING,
                                   "Closed period not re-pulled after the source's correction window"),
    "current_stale": ("current_period_stale", WARNING, "Current fiscal period data is stale"),
}


def _timer_state(ctx, unit: str) -> dict:
    """Is the scheduled financial ingest actually installed and enabled? Never raises."""
    try:
        st = ctx.systemctl(unit) or {}
    except Exception as e:  # noqa: BLE001
        return {"unit": unit, "installed": None, "error": type(e).__name__}
    loaded = st.get("LoadState") == "loaded"
    return {"unit": unit, "installed": loaded,
            "enabled": loaded and st.get("UnitFileState") in ("enabled", "enabled-runtime"),
            "active": st.get("ActiveState") == "active"}


def financial(ctx) -> list[CheckResult]:
    """R365 FINANCIAL, three separate questions (none masks another):

      scheduled_ingest    is the automatic sync installed, enabled, running on time?
      period_coverage     is OUR copy of each period current? -- the shared refresh-state
                          rule (r365_financial_freshness): FINAL / PROVISIONAL / STALE /
                          UNVERIFIED, exactly what AI Chat and MCP are told
      period_source_sync  is the SOURCE fresh? (its own sync flag at the last load)

    A closed period keeps changing at the source for ~5 weeks; one loaded once after
    close passed the old "loaded after close?" check while missing late postings.
    """
    cfg = ctx.cfg["r365"]
    if not cfg["financial_enabled"]:
        return []
    pg = ctx.pg()
    today = clock.chicago(ctx.now).date()
    periods = rows(pg, _PERIODS_SQL, {"today": today, "n": cfg["financial_lookback_periods"] + 1})
    runs = {(r["requested_fiscal_year"], r["requested_period_number"]): r
            for r in rows(pg, _PERIOD_RUN_SQL)}
    attempts = {(r["requested_fiscal_year"], r["requested_period_number"]): r
                for r in rows(pg, _PERIOD_ATTEMPT_SQL)}
    sched = (rows(pg, _SCHEDULED_SQL) or [{}])[0]
    timer = _timer_state(ctx, cfg["financial_timer"])
    out: list[CheckResult] = []

    # -- 1. the automatic sync itself -------------------------------------------------
    sbase = dict(source=SRC_R365_FIN, check=CHECK_SCHEDULE, qualifier=timer["unit"],
                 expected="scheduled financial ingest installed, enabled, and run within 36 h")
    sev = {"timer": timer, "last_scheduled_start": _iso(sched.get("last_started")),
           "last_scheduled_success": _iso(sched.get("last_ok")),
           "scheduled_failures_36h": sched.get("failed_36h")}
    if timer.get("installed") is None:
        pass   # systemd unreadable: no verdict (services checks report systemd itself)
    elif not timer["installed"] or not timer["enabled"]:
        out.append(CheckResult(state=FAIL, severity=HIGH, category="scheduled_ingest_missing",
                               title="Scheduled R365 financial ingest is not installed/enabled",
                               actual=f"{timer['unit']}: installed={timer['installed']} "
                                      f"enabled={timer['enabled']}",
                               evidence=sev, actions=_actions("fin", timer), **sbase))
    elif not timer["active"]:
        out.append(CheckResult(state=FAIL, severity=HIGH, category="scheduled_ingest_inactive",
                               title="Scheduled R365 financial ingest timer is not active",
                               actual=f"{timer['unit']} enabled but inactive", evidence=sev,
                               actions=_actions("fin", timer), **sbase))
    elif not sched.get("last_ok") or ctx.now - sched["last_ok"] > SCHEDULE_MAX_GAP:
        out.append(CheckResult(state=FAIL, severity=HIGH, category="scheduled_run_missed",
                               title="No successful scheduled R365 financial run in 36 h",
                               actual=f"last scheduled success {_iso(sched.get('last_ok')) or 'never'}",
                               evidence=sev, actions=_actions("fin", timer), **sbase))
    elif sched.get("failed_36h"):
        out.append(CheckResult(state=FAIL, severity=HIGH, category="scheduled_run_failed",
                               title="A scheduled R365 financial run failed a target in the last 36 h",
                               actual=f"{sched['failed_36h']} failed target(s)", evidence=sev,
                               actions=_actions("fin", timer), **sbase))
    else:
        out.append(CheckResult(state=OK, title="Scheduled R365 financial ingest running",
                               evidence=sev, **sbase))

    # -- 2/3. per period ------------------------------------------------------------
    newest = None
    for p in periods:
        label = f"FY{p['fiscal_year']} {p['period_label'] or 'P' + str(p['period_number'])}"
        key = (p["fiscal_year"], p["period_number"])
        base = dict(source=SRC_R365_FIN, check=CHECK_PERIOD, qualifier=label,
                    business_date=p["end_date"].isoformat(),
                    expected="current figures: refreshed on schedule, reconciled, and final "
                             "once the source's correction window has passed")
        srcbase = dict(source=SRC_R365_FIN, check=CHECK_PERIOD_SOURCE, qualifier=label,
                       business_date=p["end_date"].isoformat(),
                       expected="source reports its own data sync as current")
        run, att = runs.get(key), attempts.get(key)
        if run and run["completed_at"]:
            newest = max(newest or run["completed_at"], run["completed_at"])
        period_open = p["end_date"] >= today
        state, code, reason = FR.period_refresh_state(
            period_end=p["end_date"], today=today, now=ctx.now,
            loaded_at=run["completed_at"] if run else None, period_open=period_open,
            source_complete=None if not run else run["status"] == "COMPLETE",
            reconciliation_ok=None if not run else run["reconciliation_ok"],
            refresh_failed=bool(att and run and att["id"] != run["id"] and att["status"] == "FAILED"),
            repair_window_days=cfg["financial_repair_window_days"],
            max_age_hours=cfg["financial_refresh_max_age_hours"])
        changed = None if not run else sum(int(run.get(k) or 0) for k in (
            "rows_changed", "rows_deleted", "transaction_rows_changed", "transaction_rows_deleted"))
        ev = {"period_start": p["start_date"].isoformat(), "period_end": p["end_date"].isoformat(),
              "repair_window_end": (p["end_date"] + timedelta(
                  days=cfg["financial_repair_window_days"])).isoformat(),
              "refresh_state": state, "refresh_state_code": code, "refresh_state_reason": reason,
              "scheduled_ingest": timer,
              # our latest successful LOCAL load ...
              "last_load": None if not run else {
                  "run_id": run["id"], "status": run["status"], "trigger": run.get("trigger"),
                  "completed_at": _iso(run["completed_at"]),
                  "rows_changed_by_that_refresh": changed},
              "latest_attempt": None if not att else {"run_id": att["id"], "status": att["status"],
                                                      "error_code": att.get("error_code")},
              "reconciliation": None if not run else {"reconciliation_ok": run["reconciliation_ok"]},
              # ... kept apart from the SOURCE server's own freshness
              "source_freshness": None if not run else {
                  "source_sync_stale": run["source_sync_stale"],
                  "source_last_sync_at": _iso(run["source_last_sync_at"])}}
        # source freshness: its own result
        if period_open or run is None:
            out.append(CheckResult(state=PENDING, title="Source freshness: no closed-period load yet"
                                   if run is None else "Fiscal period still open", evidence=ev, **srcbase))
        elif run["source_sync_stale"]:
            out.append(CheckResult(state=FAIL, severity=WARNING, category="source_stale",
                                   title="R365 reported its own data sync as stale",
                                   actual=f"source_last_sync_at {run['source_last_sync_at']}",
                                   evidence=ev, actions=_actions("fin"), **srcbase))
        else:
            out.append(CheckResult(state=OK, title="Source sync reported current at last load",
                                   evidence=ev, **srcbase))
        # our copy
        due = p["end_date"] + timedelta(days=cfg["financial_refresh_grace_days"])
        if state == FR.FINAL:
            if changed and run.get("trigger") == "scheduled":
                out.append(CheckResult(
                    state=FAIL, severity=WARNING, category="source_changed_final_period",
                    title="Source changed a period after its correction window",
                    actual=f"refresh run {run['id']} changed/deleted {changed} rows",
                    evidence=ev, actions=_actions("fin", timer), **base))
            else:
                out.append(CheckResult(state=OK, title="Financial period final", evidence=ev, **base))
            continue
        if state == FR.PROVISIONAL:
            # Our copy is as current as it can be; the figures may still change at the
            # source. OK (not PENDING) so an earlier stale incident resolves.
            out.append(CheckResult(state=OK, title="Current period refreshed on schedule"
                                   if period_open else "Closed period refreshed; source may "
                                   "still correct it until the window ends",
                                   evidence=ev, **base))
            continue
        if code == "never_loaded" and period_open and today - p["start_date"] < timedelta(days=2):
            out.append(CheckResult(state=PENDING, title="New fiscal period: first scheduled load "
                                   "not due yet", evidence=ev, **base))
            continue
        cat, sev_, title = _COVERAGE[code]
        if code == "never_loaded":
            sev_ = WARNING if today <= due else HIGH
        if code == "loaded_before_close" and today <= due:
            out.append(CheckResult(state=PENDING, title="Awaiting post-close refresh",
                                   evidence=ev, **base))
            continue
        out.append(CheckResult(state=FAIL, severity=sev_, category=cat, title=title,
                               actual=reason, evidence=ev, actions=_actions("fin", timer), **base))
    ctx.source_status(SRC_R365_FIN, newest, {"timer_installed": timer.get("installed"),
                                             "scheduled_ingest": timer,
                                             "last_scheduled_success": _iso(sched.get("last_ok"))})
    return out


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def _actions(kind: str, timer: dict | None = None) -> list[str]:
    fin = []
    if timer is not None:
        if timer.get("installed") is False or timer.get("enabled") is False:
            fin.append(f"Scheduled financial ingest ({timer.get('unit')}) is not installed/enabled: "
                       "closed periods are only refreshed manually, so late source postings are "
                       "missed. Installing it is a separate, approved change.")
        elif timer.get("installed") is None:
            fin.append("Could not read the scheduled financial ingest timer state.")
    fin += ["Compare the period with the source read-only first "
            "(`r365_financial_ingest.py --period <FY> <P> --dry-run` writes nothing).",
            "Then refresh it with approval (`--period <FY> <P>`, after a backup); the refresh "
            "replaces the period's rows with the source's current, reconciled state.",
            "AI Chat serves VERIFIED categories only and labels source status; it never invents "
            "figures, but an old load can under-report late postings."]
    return {
        "missing": ["Check `systemctl status r365-weekly-ingest` and "
                    "`journalctl -u r365-weekly-ingest`.",
                    "Check r365_ingest_run for the week (status, error_code).",
                    "Re-run manually only after the cause is known."],
        "incomplete": ["Read r365_ingest_run.missing_stores / source_missing_days for the week.",
                       "If R365 itself has not finished posting, the next weekly run refreshes it.",
                       "AI Chat's R365 gate refuses incomplete weeks; nothing wrong is served."],
        "fin": fin,
    }[kind]
