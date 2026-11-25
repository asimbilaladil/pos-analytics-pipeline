#!/usr/bin/env python3
"""Overview additions for the dashboard (freshness, coverage) and API access.

Run:  venv/bin/python tests/test_monitoring_dashboard_api.py      (exit 0 = pass)
Pure: in-memory SQLite store and fake PostgreSQL rows; no database, no network.
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta, timezone

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("LAYNES_MONITOR_CONFIG", "/nonexistent")

from monitoring import config as mcfg  # noqa: E402
from monitoring import engine, pg, summary  # noqa: E402
from monitoring.checks import hme, r365, revel  # noqa: E402
from monitoring.config import CHICAGO  # noqa: E402
from monitoring.model import FAIL, HIGH, OK, PENDING, WARNING, CheckResult  # noqa: E402
from monitoring.store import Store  # noqa: E402

results: list[tuple[str, bool]] = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {str(detail)[:300]}" if not ok and detail else ""))


def ct(*a):
    return datetime(*a, tzinfo=CHICAGO)


NOW = ct(2026, 11, 25, 3, 0)          # 03:00 Chicago: Nov 24 not yet due for Revel (09:45) or HME (06:30)
CFG = mcfg._merge(mcfg.DEFAULTS, {"monitor": {"state_db": ":memory:"}})


def r(source, check_, state, store=None, d=None, sev=WARNING, cat=None, q=None, title="t"):
    return CheckResult(source=source, check=check_, state=state, severity=sev, store_id=store,
                       store_name=f"S{store}" if store else None, business_date=d, qualifier=q,
                       category=cat, title=title)


def build(extra_status=None, rs=None):
    st = Store(":memory:")
    st.begin()
    engine.apply_results(st, CFG, rs or [], NOW)
    for src, detail in (extra_status or {}).items():
        st.set_source_status(src, None, detail, NOW)
    st.commit()
    return st


rs = []
for s in (1, 2, 3):
    rs.append(r("hme", "store_day_trust", OK, s, "2026-11-21"))
    rs.append(r("hme", "store_day_trust", OK, s, "2026-11-22"))
    rs.append(r("hme", "store_day_trust", FAIL if s == 2 else OK, s, "2026-11-23", sev=HIGH,
                cat="store_reconciliation_mismatch" if s == 2 else None))
    rs.append(r("hme", "store_day_trust", PENDING, s, "2026-11-24", cat="settling" if s == 1 else None,
                title="HME store figures settling; retry pending" if s == 1 else "Awaiting HME morning ingestion"))
for d in ("2026-11-22", "2026-11-24"):            # Revel: Nov 23 deliberately NOT evaluated
    for s in (1, 2, 3, 4):
        rs.append(r("revel", "sales_reconciliation", FAIL if s == 4 else OK, s, d, cat="payment_timing" if s == 4 else None))
rs.append(r("r365_financial", "period_coverage", OK, None, "2026-10-07", q="FY2026 P10"))
rs.append(r("r365_financial", "period_coverage", FAIL, None, "2026-11-04", q="FY2026 P11", cat="stale_after_close"))
rs.append(r("r365_financial", "period_coverage", PENDING, None, "2026-12-02", q="FY2026 P12"))
cov = lambda ids: {"covered_stores": [{"id": i, "name": f"S{i}"} for i in ids]}  # noqa: E731
st = build({"hme": {"mapped_stores": 4, **cov([1, 2, 3, 9])}, "revel": {"stores": 4, **cov([1, 2, 3, 4])}}, rs)

orig = pg.connect
pg.connect = lambda *a, **k: (_ for _ in ()).throw(AssertionError("overview touched PostgreSQL"))
try:
    ov = summary.overview(st, NOW.astimezone(timezone.utc), CFG)
    touched = False
except AssertionError:
    touched = True
finally:
    pg.connect = orig
check("overview builds from the monitor store only (no PostgreSQL)", not touched)
fr = {f["source"]: f for f in ov["freshness"]}

print("1. expected (schedule) vs evaluated (results)")
check("Revel expected by 03:00 Chicago = Nov 23 (Nov 24 due 09:45), from the readiness rule",
      fr["revel"]["expected_date"] == "2026-11-23" and "09:45" in fr["revel"]["expected_rule"], fr["revel"])
check("Revel: Nov 23 has NO results -> expected_counts empty ('not evaluated yet'), "
      "even though an older and a newer date were evaluated",
      fr["revel"]["expected_counts"] == {} and fr["revel"]["latest_evaluated_date"] == "2026-11-24")
check("HME expected = Nov 23 (morning 06:30 rule); its counts come from Nov 23 only",
      fr["hme"]["expected_date"] == "2026-11-23" and fr["hme"]["expected_counts"] == {"OK": 2, "FAIL": 1}, fr["hme"])
later = summary._freshness([], [], ct(2026, 11, 25, 7, 0).astimezone(timezone.utc), CFG)
check("after 06:30 Chicago HME expects Nov 24", {f["source"]: f for f in later}["hme"]["expected_date"] == "2026-11-24")
check("after 09:45 Chicago Revel expects Nov 24",
      {f["source"]: f for f in summary._freshness([], [], ct(2026, 11, 25, 9, 46).astimezone(timezone.utc), CFG)}
      ["revel"]["expected_date"] == "2026-11-24")
check("R365 expected = week ending Wed Nov 18 (Thu Nov 26 not yet past the ready time)",
      fr["r365"]["expected_date"] == "2026-11-18" and "Wednesday" in fr["r365"]["expected_rule"], fr["r365"])
check("R365 financial has no schedule here -> expected None (never inferred from results)",
      fr["r365_financial"]["expected_date"] is None)
check("field renamed: latest_evaluated_* present, latest_expected_* gone",
      all("latest_evaluated_date" in f and "latest_expected_date" not in f for f in ov["freshness"]))
h = fr["hme"]
check("HME latest evaluated Nov 24 / checked Nov 23 / verified Nov 22",
      (h["latest_evaluated_date"], h["latest_checked_date"], h["latest_verified_date"]) == ("2026-11-24", "2026-11-23", "2026-11-22"))
check("HME retry pending from a 'settling' result", h["retry_pending"] is True)
check("Revel: a failure on every evaluated full day -> verified None (not faked)",
      fr["revel"]["latest_verified_date"] is None)
f = fr["r365_financial"]
check("R365 financial labels: evaluated P12, checked P11, verified P10",
      (f["latest_evaluated_label"], f["latest_checked_label"], f["latest_verified_label"])
      == ("FY2026 P12", "FY2026 P11", "FY2026 P10"))

print("2. coverage from the authoritative mapping, never from results")
check("HME coverage = recorded mapping [1, 2, 3, 9] (store 9 mapped but has no results)",
      ov["coverage"]["hme"]["store_ids"] == [1, 2, 3, 9] and ov["coverage"]["hme"]["basis"] == "mapping", ov["coverage"])
check("R365 mapping not recorded -> coverage null (unknown, so never N/A)", ov["coverage"]["r365"] is None)
check("a mapped store with no results still gets a matrix row (shown UNKNOWN, not dropped)",
      any(s["store_id"] == 9 and s["store_name"] == "S9" for s in ov["stores"]))
check("store 4 (Revel only) is in Revel coverage but not HME's",
      4 in ov["coverage"]["revel"]["store_ids"] and 4 not in ov["coverage"]["hme"]["store_ids"])
check("existing overview keys unchanged", all(k in ov for k in (
    "generated_at", "sources", "stores", "open_incidents", "open_counts", "reconciliation_differences",
    "last_runs", "checks", "monitor", "states")))


class Cur:
    def __init__(self, routes):
        self.routes, self._rows = routes, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        for needle, rows_ in self.routes:
            if needle in sql:
                self._rows = [dict(x) for x in (rows_(params or {}) if callable(rows_) else rows_)]
                return
        raise AssertionError(f"no route: {sql.strip()[:100]}")

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class PG:
    def __init__(self, routes):
        self.routes = routes

    def cursor(self, cursor_factory=None):
        return Cur(self.routes)

    def rollback(self):
        pass

    def close(self):
        pass


print("3. checks record the authoritative store list they already read")
now = ct(2026, 11, 25, 10, 0)
fresh = now - timedelta(hours=1)
ctx = engine.Context(CFG, Store(":memory:"), now, "data", pg_connect=lambda: PG([("FROM establishments e\nLEFT JOIN sync_state", [
    {"establishment_id": e, "store_name": n, "resource": res, "last_success_at": fresh, "window_end": fresh, "status": "success"}
    for e, n in ((6, "LCF Katy"), (61, "LCF Downtown Houston")) for res in ("orders_v2", "payments_v2")])]))
revel.freshness(ctx)
check("revel.freshness records active establishments as covered_stores",
      ctx.source_updates["revel"][1]["covered_stores"] == [{"id": 6, "name": "LCF Katy"}, {"id": 61, "name": "LCF Downtown Houston"}])
mapped = [{"hme_store_number": "108", "establishment_id": 6, "store_name": "LCF Katy"}]
ctx = engine.Context(mcfg._merge(CFG, {"hme": {"lookback_days": 1}}), Store(":memory:"), now, "data",
                     pg_connect=lambda: PG([("FROM hme_ingest_run", []), ("FROM hme_store_mapping m", mapped),
                                            ("FROM v_hme_store_day_trust_llm", []), ("FROM hme_store_ingest_result r", [])]))
hme.days(ctx)
check("hme.days records VERIFIED active mappings as covered_stores (Downtown Houston absent)",
      ctx.source_updates["hme"][1]["covered_stores"] == [{"id": 6, "name": "LCF Katy"}]
      and ctx.source_updates["hme"][1]["mapped_stores"] == 1)
weeks = r365.expected_weeks(now, CFG["r365"]["week_ready_local"], CFG["r365"]["lookback_weeks"])
wrow = lambda ws: {"week_start": ws, "week_end": ws + timedelta(days=6), "week_status": "COMPLETE",  # noqa: E731
                   "source_complete": True, "reconciliation_ok": True, "expected_store_count": 2,
                   "observed_store_count": 2, "stores_with_full_week": 2, "missing_store_days": 0,
                   "closed_store_days": 0, "pre_opening_store_days": 0, "latest_refresh_failed": False,
                   "loaded_at": now}
store_days = [{"establishment_id": e, "store_name": n, "opening_date": date(2020, 1, 1), "days_present": 7}
              for e, n in ((6, "LCF Katy"), (61, "LCF Downtown Houston"))]
last = [{"id": 1, "status": "COMPLETE", "error_code": None, "completed_at": now, "requested_week_start": weeks[0]}]
ctx = engine.Context(CFG, Store(":memory:"), now, "data", pg_connect=lambda: PG([
    ("FROM v_r365_completeness_llm", [wrow(w) for w in weeks]), ("FROM r365_store_mapping m", store_days),
    ("FROM r365_ingest_run", last)]))
r365.weekly(ctx)
check("r365.weekly records VERIFIED active mappings as covered_stores (no extra query)",
      [x["id"] for x in ctx.source_updates["r365"][1]["covered_stores"]] == [6, 61])
ctx = engine.Context(CFG, Store(":memory:"), now, "data", pg_connect=lambda: PG([
    ("FROM v_r365_completeness_llm", []), ("FROM r365_store_mapping m", store_days), ("FROM r365_ingest_run", last)]))
r365.weekly(ctx)
check("no expected R365 week loaded -> mapping not read -> covered_stores omitted (unknown, not empty)",
      "covered_stores" not in ctx.source_updates["r365"][1])

print("4. API access")
from fastapi import HTTPException  # noqa: E402
from backend import auth as AUTH  # noqa: E402
from backend import monitoring_api as MA  # noqa: E402
try:
    MA.monitoring_admin({"role": "user", "email": "u@example.com"})
    code = 200
except HTTPException as e:
    code = e.status_code
check("non-admin -> 403", code == 403)
orig_gsu = AUTH.get_session_user
AUTH.get_session_user = lambda tok: None
try:
    AUTH.current_user(None)
    code = 200
except HTTPException as e:
    code = e.status_code
finally:
    AUTH.get_session_user = orig_gsu
check("no session -> 401", code == 401)
deps = {r_.path: [d.call for d in r_.dependant.dependencies] for r_ in MA.router.routes}
check("every monitoring route requires the admin dependency",
      deps and all(any(getattr(d, "__name__", "") == "monitoring_admin" for d in ds) for ds in deps.values()), list(deps))
check("no new routes were added", sorted(deps) == sorted([
    "/api/admin/monitoring/overview", "/api/admin/monitoring/incidents",
    "/api/admin/monitoring/incidents/{incident_id}", "/api/admin/monitoring/incidents/{incident_id}/ack",
    "/api/admin/monitoring/notifications"]), sorted(deps))
import ast  # noqa: E402
tree = ast.parse(open(os.path.join(ROOT, "monitoring", "summary.py")).read())
sql_strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
               and __import__("re").search(r"\bSELECT\b[\s\S]+\bFROM\b", n.value)]
pg_imports = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
              and any(a.name in ("pg", "psycopg2") for a in n.names) or
              (isinstance(n, ast.ImportFrom) and (n.module or "").endswith("pg"))]
check("summary.py (the dashboard's overview) has no SQL strings and does not import the PostgreSQL module",
      not sql_strings and not pg_imports, (sql_strings, pg_imports))

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed" + (f"; FAILED: {failed}" if failed else ""))
sys.exit(1 if failed else 0)
