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
       reconciliation_ok, source_sync_stale, source_last_sync_at, error_code
FROM r365_financial_ingest_run
WHERE target_kind = 'PERIOD' AND status IN ('COMPLETE', 'PARTIAL')
ORDER BY requested_fiscal_year, requested_period_number, id DESC
"""


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
    """Per closed fiscal period, three separate questions:

      period_coverage   is OUR copy current?  never loaded / loaded before close /
                        refresh overdue inside the source's repair window / never
                        re-pulled after that window (late postings may be missing) /
                        reconciliation failed
      period_source_sync is the SOURCE fresh?  (its own sync flag, as recorded by the
                        last load) -- reported separately from reconciliation

    The source changes closed periods for ~5 weeks (late postings, approvals,
    re-classification): 2026-11-25, FY2026 P10 -- loaded after close, so it passed
    the old check -- had since gained new and re-approved lines at the source.
    """
    cfg = ctx.cfg["r365"]
    if not cfg["financial_enabled"]:
        return []
    pg = ctx.pg()
    today = clock.chicago(ctx.now).date()
    periods = rows(pg, _PERIODS_SQL, {"today": today, "n": cfg["financial_lookback_periods"] + 1})
    runs = {(r["requested_fiscal_year"], r["requested_period_number"]): r
            for r in rows(pg, _PERIOD_RUN_SQL)}
    timer = _timer_state(ctx, cfg["financial_timer"])
    window = timedelta(days=cfg["financial_repair_window_days"])
    max_age = timedelta(hours=cfg["financial_refresh_max_age_hours"])
    out: list[CheckResult] = []
    newest = None
    for p in periods:
        label = f"FY{p['fiscal_year']} {p['period_label'] or 'P' + str(p['period_number'])}"
        base = dict(source=SRC_R365_FIN, check=CHECK_PERIOD, qualifier=label,
                    business_date=p["end_date"].isoformat(),
                    expected="loaded after close, kept current through the source's repair "
                             "window, and reconciled")
        sbase = dict(source=SRC_R365_FIN, check=CHECK_PERIOD_SOURCE, qualifier=label,
                     business_date=p["end_date"].isoformat(),
                     expected="source reports its own data sync as current")
        run = runs.get((p["fiscal_year"], p["period_number"]))
        if run and run["completed_at"]:
            newest = max(newest or run["completed_at"], run["completed_at"])
        window_end = p["end_date"] + window
        ev = {"period_start": p["start_date"].isoformat(), "period_end": p["end_date"].isoformat(),
              "repair_window_end": window_end.isoformat(),
              "scheduled_ingest": timer,
              "last_load": None if not run else {
                  "run_id": run["id"], "status": run["status"],
                  "completed_at": run["completed_at"].isoformat() if run["completed_at"] else None},
              # kept apart on purpose: reconciliation of OUR load vs freshness of the SOURCE
              "reconciliation": None if not run else {"reconciliation_ok": run["reconciliation_ok"]},
              "source_freshness": None if not run else {
                  "source_sync_stale": run["source_sync_stale"],
                  "source_last_sync_at": run["source_last_sync_at"].isoformat()
                  if hasattr(run["source_last_sync_at"], "isoformat") else run["source_last_sync_at"]}}
        if p["end_date"] >= today:
            out.append(CheckResult(state=PENDING, title="Fiscal period still open", evidence=ev,
                                   **base))
            continue
        # -- source freshness: its own result, independent of our load ----------
        if run is None:
            out.append(CheckResult(state=PENDING, title="No load yet: source freshness unknown",
                                   evidence=ev, **sbase))
        elif run["source_sync_stale"]:
            out.append(CheckResult(state=FAIL, severity=WARNING, category="source_stale",
                                   title="R365 reported its own data sync as stale",
                                   actual=f"source_last_sync_at {run['source_last_sync_at']}",
                                   evidence=ev, actions=_actions("fin"), **sbase))
        else:
            out.append(CheckResult(state=OK, title="Source sync reported current at last load",
                                   evidence=ev, **sbase))
        # -- our copy ---------------------------------------------------------------
        due = p["end_date"] + timedelta(days=cfg["financial_refresh_grace_days"])
        if run is None:
            out.append(CheckResult(
                state=FAIL, severity=WARNING if today <= due else HIGH,
                category="missing_ingestion", title="R365 financial period never loaded",
                actual="no load", evidence=ev, actions=_actions("fin", timer), **base))
            continue
        if run["reconciliation_ok"] is False:
            out.append(CheckResult(state=FAIL, severity=HIGH, category="reconciliation_failed",
                                   title="R365 financial period did not reconcile",
                                   actual=f"run {run['id']} reconciliation_ok=false",
                                   evidence=ev, actions=_actions("fin"), **base))
            continue
        loaded = clock.chicago(run["completed_at"]) if run["completed_at"] else None
        loaded_after_close = bool(loaded) and loaded.date() > p["end_date"]
        last = f"last load {loaded:%Y-%m-%d} ({run['status']})" if loaded else "no completed load"
        if not loaded_after_close:
            if today > due:
                out.append(CheckResult(
                    state=FAIL, severity=WARNING, category="stale_after_close",
                    title="R365 financial period closed but last loaded before close",
                    actual=f"{last}; period ended {p['end_date']}",
                    evidence=ev, actions=_actions("fin", timer), **base))
            else:
                out.append(CheckResult(state=PENDING, title="Awaiting post-close refresh",
                                       evidence=ev, **base))
        elif today <= window_end:
            if ctx.now - run["completed_at"] > max_age:
                out.append(CheckResult(
                    state=FAIL, severity=WARNING, category="refresh_overdue",
                    title="Closed period inside the source's repair window not refreshed recently",
                    actual=f"{last}; repair window open until {window_end}",
                    evidence=ev, actions=_actions("fin", timer), **base))
            else:
                out.append(CheckResult(state=PENDING,
                                       title="Within the source's repair window; refreshed recently",
                                       evidence=ev, **base))
        elif loaded.date() <= window_end:
            out.append(CheckResult(
                state=FAIL, severity=WARNING, category="not_refreshed_after_repair_window",
                title="Closed period not re-pulled after the source's repair window",
                actual=f"{last}; repair window ended {window_end}",
                evidence=ev, actions=_actions("fin", timer), **base))
        else:
            out.append(CheckResult(state=OK, title="Financial period final (loaded after the "
                                                   "repair window)", evidence=ev, **base))
    ctx.source_status(SRC_R365_FIN, newest, {"timer_installed": timer.get("installed"),
                                             "scheduled_ingest": timer})
    return out


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
