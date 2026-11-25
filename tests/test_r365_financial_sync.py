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
check("small tables: up to 50 rows may go even above 25% (5-row candidate table, 1 gone)",
      L.deletion_decision("t", 5, 1, {"allow": True}) == "delete")
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
    check("COMPLETE source -> deletions allowed (bounded)", all(d["allow"] for _, d in calls))
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

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed" + (f"; FAILED: {failed}" if failed else ""))
sys.exit(1 if failed else 0)
