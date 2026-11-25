#!/usr/bin/env python3
"""Overview additions for the dashboard (freshness, coverage) and API access.

Run:  venv/bin/python tests/test_monitoring_dashboard_api.py      (exit 0 = pass)
Pure: in-memory SQLite store, no PostgreSQL, no network.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("LAYNES_MONITOR_CONFIG", "/nonexistent")

from monitoring import config as mcfg  # noqa: E402
from monitoring import engine, pg, summary  # noqa: E402
from monitoring.model import FAIL, HIGH, OK, PENDING, WARNING, CheckResult  # noqa: E402
from monitoring.store import Store  # noqa: E402

results: list[tuple[str, bool]] = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {str(detail)[:300]}" if not ok and detail else ""))


NOW = datetime(2026, 11, 25, 9, 0, tzinfo=timezone.utc)
CFG = mcfg._merge(mcfg.DEFAULTS, {"monitor": {"state_db": ":memory:"}})


def r(source, check_, state, store=None, date=None, sev=WARNING, cat=None, q=None, title="t"):
    return CheckResult(source=source, check=check_, state=state, severity=sev, store_id=store,
                       store_name=f"S{store}" if store else None, business_date=date, qualifier=q,
                       category=cat, title=title)


st = Store(":memory:")
rs = []
# HME: 3 mapped stores (1, 2, 3); store 4 is not mapped (never evaluated).
for s in (1, 2, 3):
    rs.append(r("hme", "store_day_trust", OK, s, "2026-11-21"))
    rs.append(r("hme", "store_day_trust", OK, s, "2026-11-22"))
    rs.append(r("hme", "store_day_trust", FAIL if s == 2 else OK, s, "2026-11-23", sev=HIGH,
                cat="store_reconciliation_mismatch" if s == 2 else None))
    rs.append(r("hme", "store_day_trust", PENDING, s, "2026-11-24", cat="settling" if s == 1 else None,
                title="HME store figures settling; retry pending" if s == 1 else "Awaiting HME morning ingestion"))
rs.append(r("hme", "day_reconciliation", OK, None, "2026-11-22"))
# Revel: 4 stores, every retained day has one warning -> nothing fully verified.
for d in ("2026-11-22", "2026-11-23"):
    for s in (1, 2, 3, 4):
        rs.append(r("revel", "sales_reconciliation", FAIL if s == 4 else OK, s, d, cat="payment_timing" if s == 4 else None))
# R365 financial: period labels.
rs.append(r("r365_financial", "period_coverage", OK, None, "2026-10-07", q="FY2026 P10"))
rs.append(r("r365_financial", "period_coverage", FAIL, None, "2026-11-04", q="FY2026 P11", cat="stale_after_close"))
rs.append(r("r365_financial", "period_coverage", PENDING, None, "2026-12-02", q="FY2026 P12"))
st.begin()
engine.apply_results(st, CFG, rs, NOW)
st.commit()

# No PostgreSQL may be touched while building the overview.
orig = pg.connect
pg.connect = lambda *a, **k: (_ for _ in ()).throw(AssertionError("overview touched PostgreSQL"))
try:
    ov = summary.overview(st, NOW, CFG)
    touched = False
except AssertionError:
    touched = True
finally:
    pg.connect = orig
check("overview builds from the monitor store only (no PostgreSQL)", not touched)

print("freshness")
fr = {f["source"]: f for f in ov["freshness"]}
h = fr["hme"]
check("HME latest expected = newest evaluated date (Nov 24)", h["latest_expected_date"] == "2026-11-24")
check("HME latest checked = newest date with no PENDING (Nov 23) + counts",
      h["latest_checked_date"] == "2026-11-23" and h["latest_checked_counts"] == {"OK": 2, "FAIL": 1},
      h["latest_checked_counts"])
check("HME latest verified skips the failing day -> Nov 22", h["latest_verified_date"] == "2026-11-22")
check("HME retry pending detected from a 'settling' PENDING result", h["retry_pending"] is True)
check("pending titles listed (deduplicated)", sorted(h["pending"]) ==
      ["Awaiting HME morning ingestion", "HME store figures settling; retry pending"], h["pending"])
v = fr["revel"]
check("Revel: every retained day has a failure -> latest verified None (not faked)",
      v["latest_verified_date"] is None and v["latest_checked_date"] == "2026-11-23")
check("Revel: no retry pending", v["retry_pending"] is False)
f = fr["r365_financial"]
check("R365 financial: expected P12 (open), checked P11, verified P10 -- with period labels",
      (f["latest_expected_label"], f["latest_checked_label"], f["latest_verified_label"])
      == ("FY2026 P12", "FY2026 P11", "FY2026 P10"), f)
r365 = fr["r365"]
check("a source with no results -> all dates None (never 'verified')",
      r365["latest_expected_date"] is None and r365["latest_verified_date"] is None)
check("freshness carries the monitor's check time and monitoring state",
      all("last_check_at" in x and "monitoring_state" in x for x in ov["freshness"]))

print("coverage")
check("HME coverage = the 3 evaluated stores; unmapped store 4 absent (shown N/A, not healthy)",
      ov["coverage"]["hme"] == [1, 2, 3], ov["coverage"])
check("Revel coverage = 4 stores", ov["coverage"]["revel"] == [1, 2, 3, 4])
check("network-level results never add a store", None not in ov["coverage"]["hme"])
check("existing overview keys unchanged", all(k in ov for k in (
    "generated_at", "sources", "stores", "open_incidents", "open_counts", "reconciliation_differences",
    "last_runs", "checks", "monitor", "states")))
check("open incidents still all listed (4)", len(ov["open_incidents"]) == 1 + 2 + 1, len(ov["open_incidents"]))

print("API access")
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
check("every monitoring route (incl. overview) requires the admin dependency",
      deps and all(any(getattr(d, "__name__", "") == "monitoring_admin" for d in ds) for ds in deps.values()), list(deps))
check("no new routes were added", sorted(deps) == sorted([
    "/api/admin/monitoring/overview", "/api/admin/monitoring/incidents",
    "/api/admin/monitoring/incidents/{incident_id}", "/api/admin/monitoring/incidents/{incident_id}/ack",
    "/api/admin/monitoring/notifications"]), sorted(deps))

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed" + (f"; FAILED: {failed}" if failed else ""))
sys.exit(1 if failed else 0)
