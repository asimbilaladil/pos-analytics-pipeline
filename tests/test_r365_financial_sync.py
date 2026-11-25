#!/usr/bin/env python3
"""Permanent R365 FINANCIAL synchronisation: period targeting on the AYG calendar,
the shared refresh-state rule, the loader's deletion safety, failure handling,
overlap prevention, and the AI Chat / MCP labels. PURE: no database, no network.

Run:  venv/bin/python tests/test_r365_financial_sync.py      (exit 0 = pass)
"""
import datetime as dt
import fcntl
import os
import sys
import tempfile
from zoneinfo import ZoneInfo

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_HOST", "unused")      # nothing here connects
import r365_financial_freshness as F  # noqa: E402
import r365_financial_ingest as I  # noqa: E402
import r365_financial_load as L  # noqa: E402

CHI = ZoneInfo("America/Chicago")
D = dt.date
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {str(detail)[:300]}" if not ok and detail else ""))


# AYG fiscal calendar: 4-week periods (NOT calendar months), FY2026 P9..P13 + FY2027 P1.
CAL = {(2026, 9): (D(2026, 8, 13), D(2026, 9, 9)), (2026, 10): (D(2026, 9, 10), D(2026, 10, 7)),
       (2026, 11): (D(2026, 10, 8), D(2026, 11, 4)), (2026, 12): (D(2026, 11, 5), D(2026, 12, 2)),
       (2026, 13): (D(2026, 12, 3), D(2026, 12, 30)), (2027, 1): (D(2026, 12, 31), D(2027, 1, 27))}

print("1. targeting on the AYG accounting calendar (daily / weekly revalidation)")
wed = D(2026, 11, 25)                                   # a Wednesday
check("daily run on 11-25: current P12 + recently closed P11 (P10 closed 49 days ago: Monday revalidation only)",
      I.scheduled_periods(CAL, wed) == [(2026, 11), (2026, 12)], I.scheduled_periods(CAL, wed))
check("period rollover: the first day of FY2027 P1 targets the new period and the just-closed P13",
      (2027, 1) in I.scheduled_periods(CAL, D(2026, 12, 31)) and (2026, 13) in I.scheduled_periods(CAL, D(2026, 12, 31)))
check("period close: the day after P11 closes it is still re-pulled (post-close refresh is automatic)",
      (2026, 11) in I.scheduled_periods(CAL, D(2026, 11, 5)))
check("a closed period keeps being re-pulled daily through the 42-day window (P10 ends 10-07: still on 11-18)",
      (2026, 10) in I.scheduled_periods(CAL, D(2026, 11, 18)))
check("...and drops out of the DAILY set after the window (11-19, a Thursday)",
      (2026, 10) not in I.scheduled_periods(CAL, D(2026, 11, 19)))
mon = D(2026, 11, 23)                                   # a Monday
check("weekly revalidation (Monday): every elapsed period of the current and previous FY",
      I.scheduled_periods(CAL, mon) == [(2026, 9), (2026, 10), (2026, 11), (2026, 12)])
check("future periods are never targeted", (2026, 13) not in I.scheduled_periods(CAL, wed))

print("2. shared refresh state (what AI Chat, MCP and the monitor all report)")
now = dt.datetime(2026, 11, 25, 10, 0, tzinfo=CHI)


def st(end, loaded, *, today=now.date(), at=now, open_=False, complete=True, recon=True, failed=False):
    return F.period_refresh_state(period_end=end, today=today, now=at, loaded_at=loaded,
                                  period_open=open_, source_complete=complete,
                                  reconciliation_ok=recon, refresh_failed=failed)


oct16 = dt.datetime(2026, 10, 16, 12, 35, tzinfo=CHI)
check("LATE POSTINGS: P10 loaded once after close (10-16), window ended 11-18 -> STALE",
      st(D(2026, 10, 7), oct16)[:2] == ("STALE", "not_refreshed_after_window"))
check("P11 loaded mid-period (10-16), closed 11-04 -> STALE loaded_before_close",
      st(D(2026, 11, 4), oct16)[:2] == ("STALE", "loaded_before_close"))
check("current period refreshed 3 h ago -> PROVISIONAL",
      st(D(2026, 12, 2), now - dt.timedelta(hours=3), open_=True, complete=False)[0] == "PROVISIONAL")
check("current period last loaded 40 h ago (missed daily run) -> STALE current_stale",
      st(D(2026, 12, 2), now - dt.timedelta(hours=40), open_=True, complete=False)[:2] == ("STALE", "current_stale"))
check("closed period inside the window, refreshed today -> PROVISIONAL (can still change)",
      st(D(2026, 11, 4), now - dt.timedelta(hours=2))[:2] == ("PROVISIONAL", "window_ok"))
check("closed period inside the window, last refresh 3 days ago -> STALE window_overdue",
      st(D(2026, 11, 4), now - dt.timedelta(days=3))[:2] == ("STALE", "window_overdue"))
check("closed period re-pulled after the window -> FINAL",
      st(D(2026, 10, 7), dt.datetime(2026, 11, 20, 7, 5, tzinfo=CHI))[:2] == ("FINAL", "final"))
check("never loaded -> UNVERIFIED", st(D(2026, 10, 7), None)[0] == "UNVERIFIED")
check("last load did not reconcile -> UNVERIFIED (wins over everything)",
      st(D(2026, 10, 7), dt.datetime(2026, 11, 20, tzinfo=CHI), recon=False)[:2] == ("UNVERIFIED", "not_reconciled"))
check("SOURCE FAILURE: latest refresh failed -> UNVERIFIED (figures are from an earlier load)",
      st(D(2026, 11, 4), now - dt.timedelta(hours=2), failed=True)[:2] == ("UNVERIFIED", "refresh_failed"))
check("INCOMPLETE SOURCE: closed period loaded after close but source PARTIAL -> UNVERIFIED",
      st(D(2026, 11, 4), now - dt.timedelta(hours=2), complete=False)[:2] == ("UNVERIFIED", "source_incomplete"))
print("2b. review fixes: failed refresh and unknown verification fail closed")
check("CURRENT period: good load 3 h ago, then the latest refresh FAILED -> UNVERIFIED refresh_failed (not PROVISIONAL)",
      st(D(2026, 12, 2), now - dt.timedelta(hours=3), open_=True, complete=False, failed=True)[:2] == ("UNVERIFIED", "refresh_failed"))
check("closed period inside the window, latest refresh FAILED -> UNVERIFIED refresh_failed",
      st(D(2026, 11, 4), now - dt.timedelta(hours=3), failed=True)[:2] == ("UNVERIFIED", "refresh_failed"))
check("closed FINAL-eligible period, latest refresh FAILED -> UNVERIFIED (never FINAL over a failure)",
      st(D(2026, 10, 7), dt.datetime(2026, 11, 20, 7, 5, tzinfo=CHI), failed=True)[0] == "UNVERIFIED")
check("reconciliation_ok=None (unknown) on a closed period -> UNVERIFIED reconciliation_unknown",
      st(D(2026, 10, 7), dt.datetime(2026, 11, 20, 7, 5, tzinfo=CHI), recon=None)[:2] == ("UNVERIFIED", "reconciliation_unknown"))
check("reconciliation_ok=None on the CURRENT period -> UNVERIFIED (not PROVISIONAL)",
      st(D(2026, 12, 2), now - dt.timedelta(hours=2), open_=True, complete=False, recon=None)[0] == "UNVERIFIED")
check("source_complete=None (unknown) on a closed period past the window -> UNVERIFIED, never FINAL",
      st(D(2026, 10, 7), dt.datetime(2026, 11, 20, 7, 5, tzinfo=CHI), complete=None)[:2] == ("UNVERIFIED", "source_status_unknown"))
check("source_complete=None on a closed period inside the window -> UNVERIFIED, not PROVISIONAL",
      st(D(2026, 11, 4), now - dt.timedelta(hours=2), complete=None)[0] == "UNVERIFIED")
check("an open period's PARTIAL source is EXPECTED -> PROVISIONAL (distinguished from an unverified closed period)",
      st(D(2026, 12, 2), now - dt.timedelta(hours=2), open_=True, complete=False)[0] == "PROVISIONAL"
      and st(D(2026, 11, 4), now - dt.timedelta(hours=2), complete=False)[0] == "UNVERIFIED")
check("an open period with unknown source status is still PROVISIONAL (completeness not expected yet)",
      st(D(2026, 12, 2), now - dt.timedelta(hours=2), open_=True, complete=None)[0] == "PROVISIONAL")
check("FINAL requires BOTH confirmed reconciliation and a COMPLETE source",
      st(D(2026, 10, 7), dt.datetime(2026, 11, 20, 7, 5, tzinfo=CHI), complete=True, recon=True)[0] == "FINAL")
try:
    st(D(2026, 10, 7), dt.datetime(2026, 11, 20, 7, 5)); naive = False
except ValueError:
    naive = True
check("a naive load timestamp is refused, not guessed", naive)

check("boundary: the last DAILY pull (on the window's last day, 11-18) makes the period FINAL",
      st(D(2026, 10, 7), dt.datetime(2026, 11, 18, 7, 5, tzinfo=CHI))[0] == "FINAL")
check("boundary: a load the day before the window ends is not final",
      st(D(2026, 10, 7), dt.datetime(2026, 11, 17, 7, 5, tzinfo=CHI))[0] == "STALE")
check("consistency: a period leaves the daily set exactly when its window ends (no gap, no overlap)",
      all(((2026, 10) in I.scheduled_periods(CAL, d)) == (d <= D(2026, 10, 7) + dt.timedelta(days=F.REPAIR_WINDOW_DAYS))
          for d in (D(2026, 11, 17), D(2026, 11, 18), D(2026, 11, 19), D(2026, 11, 20)) if d.isoweekday() != I.REPAIR_WEEKDAY))
check("REPAIR_WINDOW_DAYS == the orchestrator's OVERLAP_DAYS", F.REPAIR_WINDOW_DAYS == I.OVERLAP_DAYS)

print("3. deletion safety (deleted / moved transactions, incomplete or truncated responses)")
check("COMPLETE source, 449 of 4,279 lines gone (10.5%, P11 on 2026-11-25) -> delete",
      L.deletion_decision("t", 4279, 449, {"allow": True}) == "delete")
check("PARTIAL source -> deletions DEFERRED (upsert-only), never deleted",
      L.deletion_decision("t", 4279, 449, {"allow": False}) == "defer")
try:
    L.deletion_decision("t", 4279, 3000, {"allow": True}); refused = False
except L.ReconciliationError as e:
    refused = "refusing to delete 3000 of 4279" in str(e)
check("TRUNCATED response: 3,000 of 4,279 gone (70%) -> target FAILS (whole period rolled back)", refused)
check("P11 candidate table: 1 of 5 gone, 50 returned -> delete", L.deletion_decision("t", 5, 1, {"allow": True}, new_rows=50) == "delete")
check("P11 account_period: 1 of 413 gone -> delete", L.deletion_decision("t", 413, 1, {"allow": True}, new_rows=774) == "delete")


def refused(*a, **k):
    try:
        L.deletion_decision(*a, **k); return None
    except L.ReconciliationError as e:
        return str(e)


check("EMPTY scope (0 rows before): nothing to delete -> no-op", L.deletion_decision("t", 0, 0, {"allow": True}, new_rows=0) == "delete")
check("TOTAL deletion of a SMALL table (50 of 50 gone, nothing returned) -> refused (was allowed by max(50,25%))",
      "EMPTY" in (refused("t", 50, 50, {"allow": True}, new_rows=0) or ""))
check("TOTAL deletion of a 5-row table -> refused", "EMPTY" in (refused("t", 5, 5, {"allow": True}, new_rows=0) or ""))
check("single-row scope emptied -> refused", "EMPTY" in (refused("t", 1, 1, {"allow": True}, new_rows=0) or ""))
check("NEAR-TOTAL: 5 of 6 gone (83%) -> refused even though 5 <= the 5-row floor",
      "near-total" in (refused("t", 6, 5, {"allow": True}, new_rows=6) or ""))
check("NEAR-TOTAL: every row re-keyed (5 of 5 gone, 5 new) -> refused", "near-total" in (refused("t", 5, 5, {"allow": True}, new_rows=5) or ""))
check("small legitimate change: 1 of 2 gone (50%) -> delete", L.deletion_decision("t", 2, 1, {"allow": True}, new_rows=2) == "delete")
check("single row re-keyed (1 gone, 1 new) -> delete", L.deletion_decision("t", 1, 1, {"allow": True}, new_rows=1) == "delete")
check("LARGE scope over the 25% bound (1,100 of 4,279) -> refused",
      "exceeds the bound" in (refused("t", 4279, 1100, {"allow": True}, new_rows=3500) or ""))
check("unknown returned count is treated as possibly empty (fail closed)",
      "EMPTY" in (refused("t", 3, 3, {"allow": True}) or ""))
check("INTENTIONAL removal of all rows: explicit --allow-mass-delete override on a COMPLETE source -> delete",
      L.deletion_decision("t", 50, 50, {"allow": True, "override": True}, new_rows=0) == "delete")
check("...the override never applies to a PARTIAL source (still deferred)",
      L.deletion_decision("t", 50, 50, {"allow": False, "override": True}, new_rows=0) == "defer")
try:
    I.main(["--allow-mass-delete"]); cli = False
except SystemExit as e:
    cli = e.code == 2
check("--allow-mass-delete without --period FY P is rejected by the CLI", cli)
check("idempotent re-run: nothing gone -> no deletion work", L.deletion_decision("t", 100, 0, {"allow": False}) == "delete")
check("moved line (now reported under another period): the transaction table key is the line id, so the "
      "other period's upsert MOVES it -- never a duplicate", L._TXN_PK == ["source_transaction_detail_id"])

# write_period wiring: record the deletion policy each table receives
calls, finished = [], {}
orig = (L._replace, L._finish)


def fake_replace(cur, table, pk, vals, rows, run_id, scope_sql, scope_params, stamp=None, deletion=None):
    calls.append((table, deletion))
    if deletion is not None and not deletion["allow"]:
        deletion.setdefault("deferred", {})[table] = 7
    return len(rows), 0, 0


class Cur:
    def __enter__(self): return self
    def __exit__(self, *a): return False


class Conn:
    def cursor(self): return Cur()


def plan(status):
    return {"fiscal_year": 2026, "period_number": 12, "source_last_sync_at": None, "status": status,
            "expense_rows": [], "candidate_rows": [], "account_period_rows": [], "transaction_rows": [],
            "rows_received": 0, "no_activity_rows": 0, "detail_lines": 0, "unmapped_locations": [],
            "unaccounted_stores": {}, "reconciliation": {"buckets_checked": 1, "mismatches": 0},
            "source_data_status": {}, "source_status_reasons": {}, "source_sync_stale": False,
            "source_min_date": None, "source_max_date": None, "schema_warnings": [], "transaction_stats": {}}


L._replace, L._finish = fake_replace, lambda cur, run_id, status, **f: finished.update(f, status=status)
try:
    L.write_period(Conn(), 1, plan("PARTIAL"), {})
    check("write_period passes the SAME no-delete policy to all four period tables on a PARTIAL source",
          len(calls) == 4 and all(d is not None and d["allow"] is False for _, d in calls), calls)
    import json
    pol = json.loads(finished["reconciliation"])["deletion_policy"]
    check("deferred deletions are recorded in the run's reconciliation evidence",
          pol["deletions_allowed"] is False and len(pol["deferred_deletions"]) == 4, pol)
    calls.clear()
    L.write_period(Conn(), 2, plan("COMPLETE"), {})
    check("COMPLETE source -> deletions allowed (bounded), no override by default",
          all(d["allow"] and not d["override"] for _, d in calls))
    calls.clear()
    L.write_period(Conn(), 3, plan("COMPLETE"), {}, allow_mass_delete=True)
    pol = json.loads(finished["reconciliation"])["deletion_policy"]
    check("explicit override is passed to every table and recorded in the run",
          all(d["override"] for _, d in calls) and pol["mass_delete_override"] is True and pol["bound"]["never_empty_a_scope"])
    calls.clear()
    L.write_period(Conn(), 4, plan("PARTIAL"), {}, allow_mass_delete=True)
    check("override requested on a PARTIAL source is NOT granted", all(not d["override"] and not d["allow"] for _, d in calls))
finally:
    L._replace, L._finish = orig

print("4. failure handling: bounded, transactional, recorded")
failed_runs, commits = [], []


class C2:
    def commit(self): commits.append(1)
    def rollback(self): pass


L_fail = L.fail_run
L.fail_run = lambda conn, run_id, code, msg, fetch: failed_runs.append(code)
try:
    def boom_fetch(fetch):
        raise I.FetchError("HTTP_503", "source unavailable", http_status=503, retryable=True)
    r = I._run_target(C2(), 9, boom_fetch, lambda m: None, "FY2026-P12")
    check("SOURCE FAILURE (HTTP 503): target FAILED, recorded, nothing committed",
          r == L.STATUS_FAILED and failed_runs[-1] == "HTTP_503" and not commits)

    def bound(fetch):
        L.deletion_decision("t", 4279, 3000, {"allow": True})
    r = I._run_target(C2(), 10, bound, lambda m: None, "FY2026-P11")
    check("deletion bound exceeded -> FAILED RECONCILIATION, nothing committed",
          r == L.STATUS_FAILED and failed_runs[-1] == "RECONCILIATION" and not commits)

    def schema(fetch):
        raise L.SourceSchemaError("detail[3]: duplicate detailId across pages")
    r = I._run_target(C2(), 11, schema, lambda m: None, "FY2026-P11")
    check("malformed source (duplicate line id) -> FAILED SOURCE_SCHEMA", failed_runs[-1] == "SOURCE_SCHEMA")
    r = I._run_target(C2(), 12, lambda fetch: ("COMPLETE", "ok"), lambda m: None, "FY2026-P12")
    check("successful target commits exactly once", r == "COMPLETE" and commits == [1])
finally:
    L.fail_run = L_fail

print("5. overlapping runs are prevented")
tmp = tempfile.mkdtemp(prefix="r365lock-")
lock_path = os.path.join(tmp, "ingest.lock")
orig_lock = I.LOCK_PATH
I.LOCK_PATH = lock_path
try:
    holder = open(lock_path, "w")
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    check("a second run while one holds the lock exits 75 (EX_TEMPFAIL) without touching anything",
          I.main([]) == I.EXIT_LOCKED)
    fcntl.flock(holder, fcntl.LOCK_UN)
    holder.close()
finally:
    I.LOCK_PATH = orig_lock

print("6. AI Chat / MCP labels (shared gate)")
import chat_sql as C  # noqa: E402
f = {"available": True, "calendar_periods_in_scope": 3, "scope_is_whole_periods": True,
     "periods_in_scope": [
         {"fiscal_year": 2026, "period_label": "P10", "period_status": "COMPLETED",
          "source_data_status": "COMPLETE", "latest_refresh_failed": False, "refresh_state": "STALE",
          "refresh_state_reason": "last loaded 2026-10-16, before the source's correction window ended",
          "loaded_at": "2026-10-16T17:35:57+00:00"},
         {"fiscal_year": 2026, "period_label": "P12", "period_status": "CURRENT",
          "source_data_status": "PARTIAL", "latest_refresh_failed": False, "refresh_state": "PROVISIONAL",
          "refresh_state_reason": "current period", "loaded_at": "2026-11-25T13:00:00+00:00"},
         {"fiscal_year": 2026, "period_label": "P9", "period_status": "COMPLETED",
          "source_data_status": "COMPLETE", "latest_refresh_failed": False, "refresh_state": "FINAL",
          "refresh_state_reason": "final", "loaded_at": "2026-10-20T12:00:00+00:00"}],
     "no_posting_in_scope": [], "no_identifiable_posting": []}
fails, warns = C._r365_fin_gate(f)
check("STALE period -> explicit 'NOT current' warning naming the period", any("P10 is STALE" in w and "NOT current" in w for w in warns), warns)
check("PROVISIONAL period -> 'label the figures provisional'", any("P12 is PROVISIONAL" in w for w in warns))
check("FINAL period -> no freshness warning", not any("P9 is" in w for w in warns))
check("freshness never blocks an answer (warnings only; trust gate unchanged)", fails == [])
note = C.r365_fin_result_note({"r365_financial": f}, "SELECT 1")
check("the result note puts STALE / PROVISIONAL next to the rows, with the last load date",
      note and "FY2026 P10 figures are STALE -- last loaded 2026-10-16" in note and "P12 figures are PROVISIONAL" in note, note)
check("no note when every period in scope is FINAL and nothing else applies",
      C.r365_fin_freshness_note({"periods_in_scope": [f["periods_in_scope"][2]]}) is None)

print("7. AI Chat / MCP and the monitor agree on the same period")


class Cov:
    """Fake cursor for chat_sql._r365_fin_block: calendar, coverage, no-posting."""
    def __init__(self, cov, cal):
        self.cov, self.cal, self._r = cov, cal, []
    def execute(self, sql, params=None):
        self._r = self.cov if "v_r365_financial_coverage_llm" in sql else (
            self.cal if "v_ayg_business_periods_llm" in sql else [])
    def fetchall(self):
        return [dict(r) for r in self._r]


UTC = dt.timezone.utc
good = dt.datetime(2026, 11, 25, 13, 0, tzinfo=UTC)
cov = [{"fiscal_year": 2026, "period_number": 12, "period_label": "P12", "period_start": D(2026, 11, 5),
        "period_end": D(2026, 12, 2), "period_status": "CURRENT", "analytics_category": "ELECTRICITY",
        "source_data_status": "PARTIAL", "expected_store_count": 12, "stores_with_posting": 3,
        "stores_without_posting": 9, "source_last_sync_at": good, "source_sync_stale": False,
        "latest_refresh_failed": True, "loaded_at": good, "reconciliation_ok": True}]
cal = [{"fiscal_year": 2026, "period_number": 12, "period_label": "P12", "start_date": D(2026, 11, 5), "end_date": D(2026, 12, 2)}]
at = dt.datetime(2026, 11, 25, 16, 0, tzinfo=CHI)
blk = C._r365_fin_block(Cov(cov, cal), None, "2026-11-05", "2026-12-03", now=at)
chat_state = blk["periods_in_scope"][0]["refresh_state"]
from monitoring import config as mcfg, engine  # noqa: E402
from monitoring.checks import r365 as MR  # noqa: E402
from monitoring.store import Store  # noqa: E402


class MCur:
    def __init__(self, routes): self.routes, self._r = routes, []
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None):
        for k, v in self.routes:
            if k in sql:
                self._r = [dict(x) for x in v]; return
        raise AssertionError(sql[:60])
    def fetchall(self): return self._r


class MPG:
    def __init__(self, routes): self.routes = routes
    def cursor(self, cursor_factory=None): return MCur(self.routes)
    def rollback(self): pass
    def close(self): pass


okrun = {"requested_fiscal_year": 2026, "requested_period_number": 12, "id": 70, "status": "PARTIAL",
         "completed_at": good, "reconciliation_ok": True, "source_sync_stale": False, "source_last_sync_at": good,
         "error_code": None, "trigger": "scheduled", "rows_changed": 0, "rows_deleted": 0,
         "transaction_rows_changed": 0, "transaction_rows_deleted": 0}
failrun = {"requested_fiscal_year": 2026, "requested_period_number": 12, "id": 71, "status": "FAILED",
           "error_code": "HTTP_503", "started_at": at}
cfg = mcfg._merge(mcfg.DEFAULTS, {"monitor": {"state_db": ":memory:"}})
ctx = engine.Context(cfg, Store(":memory:"), at, "data",
                     pg_connect=lambda: MPG([("FROM ayg_business_periods", [{"fiscal_year": 2026, "period_number": 12,
                                              "period_label": "P12", "start_date": D(2026, 11, 5), "end_date": D(2026, 12, 2)}]),
                                             ("r365_fin_last_attempt", [failrun]),
                                             ("r365_fin_scheduled", [{"last_started": at, "last_ok": good, "failed_36h": 1}]),
                                             ("FROM r365_financial_ingest_run", [okrun])]),
                     systemctl=lambda u: {"LoadState": "loaded", "UnitFileState": "enabled", "ActiveState": "active"})
mon = {r.check: r for r in MR.financial(ctx) if r.qualifier in ("FY2026 P12", "r365-financial-ingest.timer")}
check("FAILED refresh of the CURRENT period: AI Chat/MCP gate says UNVERIFIED", chat_state == "UNVERIFIED", blk["periods_in_scope"][0])
check("...the monitor says refresh_failed (same shared rule, same state)",
      mon["period_coverage"].category == "refresh_failed" and mon["period_coverage"].evidence["refresh_state"] == "UNVERIFIED")
check("...and the scheduled-ingest check reports the failed target", mon["scheduled_ingest"].category == "scheduled_run_failed")
gfails, gwarns = C._r365_fin_gate(blk)
check("...the gate warns once (failed-refresh warning, no duplicate UNVERIFIED line)",
      sum("refresh failed" in w for w in gwarns) == 1 and not any("is UNVERIFIED" in w for w in gwarns), gwarns)

closed = [{**cov[0], "period_number": 11, "period_label": "P11", "period_start": D(2026, 10, 8), "period_end": D(2026, 11, 4),
           "period_status": "CLOSED", "latest_refresh_failed": False, "source_data_status": None}]
cal11 = [{"fiscal_year": 2026, "period_number": 11, "period_label": "P11", "start_date": D(2026, 10, 8), "end_date": D(2026, 11, 4)}]
b = C._r365_fin_block(Cov(closed, cal11), None, "2026-10-08", "2026-11-05", now=at)["periods_in_scope"][0]
check("AI Chat: closed period with UNKNOWN source status -> UNVERIFIED (not PROVISIONAL/FINAL)",
      b["refresh_state"] == "UNVERIFIED" and "unknown" in b["refresh_state_reason"], b)
closed[0].update(source_data_status="COMPLETE", reconciliation_ok=None)
b = C._r365_fin_block(Cov(closed, cal11), None, "2026-10-08", "2026-11-05", now=at)["periods_in_scope"][0]
check("AI Chat: UNKNOWN reconciliation -> UNVERIFIED", b["refresh_state"] == "UNVERIFIED", b)
closed[0].update(reconciliation_ok=True)
b = C._r365_fin_block(Cov(closed, cal11), None, "2026-10-08", "2026-11-05", now=at)["periods_in_scope"][0]
check("AI Chat: confirmed reconciliation + COMPLETE inside the window -> PROVISIONAL", b["refresh_state"] == "PROVISIONAL", b)

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed" + (f"; FAILED: {failed}" if failed else ""))
sys.exit(1 if failed else 0)
