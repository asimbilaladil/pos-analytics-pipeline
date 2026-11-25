#!/usr/bin/env python3
"""R365 financial period monitoring: our copy vs the source's repair window, and source
freshness reported separately from reconciliation.

Run:  venv/bin/python tests/test_monitoring_r365_financial.py      (exit 0 = pass)
Pure: fake PostgreSQL rows and a fake systemctl; no database, no network.
"""
import os
import sys
from datetime import date, datetime, timedelta, timezone

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
from monitoring import config as mcfg, engine  # noqa: E402
from monitoring.checks import r365  # noqa: E402
from monitoring.config import CHICAGO  # noqa: E402
from monitoring.model import FAIL, HIGH, OK, PENDING, WARNING  # noqa: E402
from monitoring.store import Store  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {str(detail)[:300]}" if not ok and detail else ""))


class Cur:
    def __init__(self, routes): self.routes, self._r = routes, []
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None):
        for k, v in self.routes:
            if k in sql:
                self._r = [dict(x) for x in v]; return
        raise AssertionError(sql[:80])
    def fetchall(self): return self._r


class PG:
    def __init__(self, routes): self.routes = routes
    def cursor(self, cursor_factory=None): return Cur(self.routes)
    def rollback(self): pass
    def close(self): pass


CFG = mcfg._merge(mcfg.DEFAULTS, {"monitor": {"state_db": ":memory:"}})
P = [{"fiscal_year": 2026, "period_number": 12, "period_label": "P12", "start_date": date(2026, 11, 5), "end_date": date(2026, 12, 2)},
     {"fiscal_year": 2026, "period_number": 11, "period_label": "P11", "start_date": date(2026, 10, 8), "end_date": date(2026, 11, 4)},
     {"fiscal_year": 2026, "period_number": 10, "period_label": "P10", "start_date": date(2026, 9, 10), "end_date": date(2026, 10, 7)}]


def run(pn, completed, *, recon=True, stale=False, status="COMPLETE", rid=1):
    return {"requested_fiscal_year": 2026, "requested_period_number": pn, "id": rid, "status": status,
            "completed_at": completed, "reconciliation_ok": recon, "source_sync_stale": stale,
            "source_last_sync_at": completed, "error_code": None}


TIMER_OFF = {"LoadState": "not-found", "ActiveState": "inactive", "UnitFileState": ""}
TIMER_ON = {"LoadState": "loaded", "ActiveState": "active", "UnitFileState": "enabled"}


def evaluate(runs, now, timer=TIMER_OFF, periods=P):
    ctx = engine.Context(CFG, Store(":memory:"), now, "data",
                         pg_connect=lambda: PG([("FROM ayg_business_periods", periods),
                                                ("FROM r365_financial_ingest_run", runs)]),
                         systemctl=(timer if callable(timer) else (lambda u: timer)))
    res = r365.financial(ctx)
    cov = {r.qualifier: r for r in res if r.check == "period_coverage"}
    src = {r.qualifier: r for r in res if r.check == "period_source_sync"}
    return cov, src, ctx


UTC = timezone.utc
NOV25 = datetime(2026, 11, 25, 10, 0, tzinfo=CHICAGO)

print("1. production shape on 2026-11-25 (P10 and P11 both loaded 2026-10-16, no timer)")
oct16 = datetime(2026, 10, 16, 17, 35, tzinfo=UTC)
cov, src, ctx = evaluate([run(11, oct16, status="PARTIAL", rid=31), run(10, oct16, rid=33)], NOV25)
check("P12 (open) -> PENDING", cov["FY2026 P12"].state == PENDING)
check("P11 loaded before close -> WARNING stale_after_close (unchanged behaviour)",
      cov["FY2026 P11"].category == "stale_after_close" and cov["FY2026 P11"].severity == WARNING)
check("P10 loaded after close but before its repair window ended (11-18) -> WARNING not_refreshed_after_repair_window",
      cov["FY2026 P10"].state == FAIL and cov["FY2026 P10"].category == "not_refreshed_after_repair_window"
      and cov["FY2026 P10"].severity == WARNING, cov["FY2026 P10"])
check("evidence carries repair_window_end and the scheduled-ingest state",
      cov["FY2026 P10"].evidence["repair_window_end"] == "2026-11-18"
      and cov["FY2026 P10"].evidence["scheduled_ingest"]["installed"] is False)
check("actions name the missing timer as the root cause and the read-only dry run first",
      any("not installed/enabled" in a for a in cov["FY2026 P10"].actions)
      and any("--dry-run" in a for a in cov["FY2026 P10"].actions))
check("source_status reports timer_installed False from systemd (no longer hardcoded)",
      ctx.source_updates["r365_financial"][1]["timer_installed"] is False)

print("2. inside the repair window")
nov10 = datetime(2026, 11, 10, 10, 0, tzinfo=CHICAGO)
cov, _, _ = evaluate([run(10, datetime(2026, 11, 9, 13, 0, tzinfo=UTC))], nov10)
check("closed P10, refreshed 21 h ago, window open until 11-18 -> PENDING (not final yet)",
      cov["FY2026 P10"].state == PENDING, cov["FY2026 P10"])
cov, _, _ = evaluate([run(10, datetime(2026, 11, 5, 13, 0, tzinfo=UTC))], nov10)
check("closed P10, last refreshed 5 days ago, window still open -> WARNING refresh_overdue",
      cov["FY2026 P10"].category == "refresh_overdue" and cov["FY2026 P10"].severity == WARNING)
cov, _, _ = evaluate([run(10, datetime(2026, 11, 7, 17, 0, tzinfo=UTC))], nov10)
check("72 h boundary: 65 h old -> not overdue", cov["FY2026 P10"].state == PENDING)

print("3. after the repair window")
cov, _, _ = evaluate([run(10, datetime(2026, 11, 20, 13, 0, tzinfo=UTC))], NOV25)
check("P10 re-pulled after its window ended -> OK (final)", cov["FY2026 P10"].state == OK, cov["FY2026 P10"])
cov, _, _ = evaluate([run(10, datetime(2026, 11, 18, 23, 0, tzinfo=UTC))], NOV25)
check("load on the window's last day (Chicago date 11-18) is not final -> WARNING",
      cov["FY2026 P10"].category == "not_refreshed_after_repair_window")

print("4. never loaded, reconciliation, source freshness kept separate")
cov, src, _ = evaluate([], datetime(2026, 11, 10, 10, 0, tzinfo=CHICAGO))
check("P11 never loaded within grace (ends 11-04, grace 10 d) -> WARNING missing_ingestion",
      cov["FY2026 P11"].category == "missing_ingestion" and cov["FY2026 P11"].severity == WARNING)
check("no load -> source freshness PENDING (unknown), not OK", src["FY2026 P11"].state == PENDING)
cov, _, _ = evaluate([], NOV25)
check("P11 never loaded past grace -> HIGH", cov["FY2026 P11"].severity == HIGH)
cov, src, _ = evaluate([run(10, datetime(2026, 11, 20, 13, 0, tzinfo=UTC), recon=False)], NOV25)
check("reconciliation failed -> HIGH reconciliation_failed on coverage", cov["FY2026 P10"].category == "reconciliation_failed"
      and cov["FY2026 P10"].severity == HIGH)
check("...while source freshness is reported on its own result (OK here)", src["FY2026 P10"].state == OK)
cov, src, _ = evaluate([run(10, datetime(2026, 11, 20, 13, 0, tzinfo=UTC), stale=True)], NOV25)
check("source reports stale sync -> separate period_source_sync WARNING source_stale",
      src["FY2026 P10"].category == "source_stale" and src["FY2026 P10"].severity == WARNING)
check("...and it no longer masks our copy's verdict (coverage OK: final load)", cov["FY2026 P10"].state == OK)
check("evidence keeps reconciliation and source_freshness as separate fields",
      set(cov["FY2026 P10"].evidence["reconciliation"]) == {"reconciliation_ok"}
      and "source_sync_stale" in cov["FY2026 P10"].evidence["source_freshness"])

print("5. scheduled-ingest timer state")
cov, _, ctx = evaluate([run(10, oct16)], NOV25, timer=TIMER_ON)
check("timer installed+enabled -> reported, and no 'not installed' action",
      ctx.source_updates["r365_financial"][1]["timer_installed"] is True
      and not any("not installed" in a for a in cov["FY2026 P10"].actions))
def boom(u):
    raise OSError("no systemd")
cov, _, ctx = evaluate([run(10, oct16)], NOV25, timer=boom)
check("systemctl failure -> installed None, check still completes", ctx.source_updates["r365_financial"][1]["timer_installed"] is None
      and cov["FY2026 P10"].category == "not_refreshed_after_repair_window")

print("6. config")
for bad in ({"financial_repair_window_days": 5}, {"financial_refresh_max_age_hours": 0}):
    try:
        mcfg.validate(mcfg._merge(CFG, {"r365": bad})); ok = False
    except ValueError:
        ok = True
    check(f"config rejects {bad}", ok)
mcfg.validate(CFG)
check("defaults: 42-day repair window, 72 h max refresh age",
      CFG["r365"]["financial_repair_window_days"] == 42 and CFG["r365"]["financial_refresh_max_age_hours"] == 72)

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed" + (f"; FAILED: {failed}" if failed else ""))
sys.exit(1 if failed else 0)
