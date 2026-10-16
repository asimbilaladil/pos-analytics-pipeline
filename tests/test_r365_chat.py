#!/usr/bin/env python3
"""R365 in chat: validator boundary, domain routing, trust gate, metadata,
ratio/forecast semantics, fiscal periods and source guidance.

Run:  venv/bin/python tests/test_r365_chat.py     (exit 0 = pass)

Read-only. Database checks connect as the LLM role (laynes_ro) wherever the
assistant would, so they prove what chat can actually see. No R365 API call is
made anywhere in this file.
"""
import os
import sys
import time
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from dotenv import load_dotenv

load_dotenv(".env", override=True)
import chat_sql as C  # noqa: E402
import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402

results = []
CHI = ZoneInfo("America/Chicago")
SAFE = ("v_r365_store_daily_llm", "v_r365_store_weekly_llm",
        "v_r365_store_period_llm", "v_r365_completeness_llm")
RAW = ("r365_store_daily", "r365_store_weekly", "r365_store_mapping", "r365_ingest_run")


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


def rejected(sql):
    try:
        C._validate(sql)
        return False
    except C.SqlError:
        return True


def ro():
    conn = C._ro_conn()
    conn.set_session(readonly=True, autocommit=True)
    return conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


# ---------------------------------------------------------------------------
print("1. SQL validator")
for v in SAFE:
    check(f"{v} is allowed", not rejected(f"SELECT * FROM {v}"))
for t in RAW:
    check(f"raw {t} is rejected", rejected(f"SELECT * FROM {t}"))
check("exactly four R365 relations are allowlisted",
      {r for r in C._ALLOWED_RELATIONS if "r365" in r} == set(SAFE))
check("raw table hidden in a CTE is rejected",
      rejected("WITH x AS (SELECT * FROM r365_ingest_run) SELECT * FROM x"))
check("raw table in an implicit join is rejected",
      rejected("SELECT 1 FROM v_r365_store_weekly_llm w, r365_store_mapping m"))
check("raw table via a subquery is rejected",
      rejected("SELECT * FROM v_r365_store_daily_llm WHERE establishment_id IN "
               "(SELECT establishment_id FROM r365_store_daily)"))

# ---------------------------------------------------------------------------
print("2. domain routing")
D = C.domains_for_relations
check("R365-only -> {r365}", D({"v_r365_store_weekly_llm"}) == {"r365"})
check("R365 + establishments -> {r365} (shared dimension adds nothing)",
      D({"v_r365_store_daily_llm", "establishments"}) == {"r365"})
check("R365 + HME -> {r365, hme}", D({"v_r365_store_weekly_llm", "v_hme_store_daily_llm"}) == {"r365", "hme"})
check("R365 + Revel -> {r365, revel}", D({"v_r365_store_weekly_llm", "v_orders_classified"}) == {"r365", "revel"})
check("R365 + HME + Revel -> all three",
      D({"v_r365_store_daily_llm", "v_hme_store_daily_llm", "v_orders_classified"}) == {"r365", "hme", "revel"})
check("completeness view alone carries no gate (reference)",
      D({"v_r365_completeness_llm"}) == set() and "v_r365_completeness_llm" in C._REFERENCE_RELATIONS)
check("period calendar + R365 period view -> {r365}",
      D({"v_ayg_business_periods_llm", "v_r365_store_period_llm"}) == {"r365"})
check("a Revel-only query is NOT routed to R365", "r365" not in D({"v_orders_classified"}))
check("an HME-only query is NOT routed to R365", "r365" not in D({"v_hme_store_daily_llm"}))
check("omitted domains keep the existing strict set (no R365 added)",
      C.normalise_domains(None) == set(C.ALL_DOMAINS) and "r365" not in C.normalise_domains(None))
check("['r365'] stays {r365}", C.normalise_domains(["r365"]) == {"r365"})
check("unknown domain stays fail-closed and keeps r365 if named",
      C.normalise_domains(["r365", "bogus"]) == set(C.ALL_DOMAINS) | {"r365"})
check("existing behaviour unchanged: ['hme','bogus'] -> ALL_DOMAINS",
      C.normalise_domains(["hme", "bogus"]) == set(C.ALL_DOMAINS))
check("check_data advertises the r365 domain",
      "r365" in C.CHECK_DATA_TOOL["input_schema"]["properties"]["domains"]["items"]["enum"])

# Isolation: R365-only must not run Revel meta or HME; Revel/HME must not run R365.
calls = []
orig = (C.meta_extract, C._hme_block, C._r365_block)
C.meta_extract = lambda *a, **k: calls.append("revel") or orig[0](*a, **k)
C._hme_block = lambda *a, **k: calls.append("hme") or orig[1](*a, **k)
C._r365_block = lambda *a, **k: calls.append("r365") or orig[2](*a, **k)
try:
    calls.clear()
    t = time.perf_counter()
    m = C.enforce_scope("SELECT sum(net_sales) FROM v_r365_store_weekly_llm "
                        "WHERE week_start = '2026-10-08'", None, "2026-10-08", "2026-10-15")
    r365_only_s = time.perf_counter() - t
    check("R365-only query runs ONLY the R365 profile (no Revel meta, no HME)",
          calls == ["r365"] and m["analysis_permitted"], str(calls))
    calls.clear()
    try:   # HME's own verdict for these dates is irrelevant here (it may fail)
        C.enforce_scope("SELECT sum(drive_thru_cars) FROM v_hme_store_daily_llm "
                        "WHERE business_date >= '2026-10-08' AND business_date < '2026-10-15'",
                        None, "2026-10-08", "2026-10-15")
    except C.ScopeError as e:
        check("an HME failure is reported as HME, never as R365", "R365" not in str(e))
    check("HME-only query does not build the R365 profile",
          "r365" not in calls and "hme" in calls, str(calls))
    calls.clear()
    C.meta_profile(None, "2026-10-08", "2026-10-15", domains=["r365", "hme"])
    check("R365 + HME builds both profiles and not Revel's",
          sorted(calls) == ["hme", "r365"], str(calls))
finally:
    C.meta_extract, C._hme_block, C._r365_block = orig

# ---------------------------------------------------------------------------
print("3. trust gate (live, as laynes_ro)")
P = lambda *a, **k: C.meta_profile(*a, domains=["r365"], **k)  # noqa: E731
m = P(None, "2026-10-08", "2026-10-15")
check("latest complete week passes the R365 gate", m["analysis_permitted"] and not m["warnings"])
m = P(None, "2026-10-15", "2026-10-22")
check("a not-yet-ingested week FAILS (missing != zero)",
      not m["analysis_permitted"] and "next window" in m["blocking_reasons"][0])
m = P(None, "2026-08-01", "2026-08-10")
check("a pre-boundary range FAILS and names the 2026-08-13 boundary",
      not m["analysis_permitted"] and "2026-08-13" in m["blocking_reasons"][0])
m = P(None, "2026-10-08", "2026-11-05")
check("a partly loaded range (current period P11) passes with a coverage warning",
      m["analysis_permitted"] and any("covers only 7 of the 28" in w for w in m["warnings"]))
m = P(14, "2026-09-03", "2026-09-04")
check("Beaumont 2026-09-03 surfaces as CLOSED, not as a normal $0 day",
      any("CLOSED" in w for w in m["warnings"]) and m["r365"]["special_status_days_in_scope"][0]["day_status"] == "CLOSED")
check("all R365 blocking reasons start with 'R365' (attributable)",
      all(r.startswith("R365") for r in P(None, "2026-08-01", "2026-08-10")["blocking_reasons"]))
m = C.meta_profile(None, "2026-08-01", "2026-08-10", domains=["r365"])
b = C.blocked_domain_context(m, ["r365"])
check("blocked verdict reports R365 untrusted and invents no Revel failure",
      b["r365"]["trusted"] is False and "revel" not in b)
m["blocking_reasons"] = m["blocking_reasons"] + ["revel sales reconciliation FAIL"]
b = C.blocked_domain_context(m, ["r365", "revel"])
check("R365 reasons are not attributed to Revel",
      b["revel"]["reason"] == "revel sales reconciliation FAIL")

tf = C.r365_trusted_figures(None, "2026-10-08", "2026-10-15")
nt = tf["network_total"]
check("blocked-path R365 figures: official weekly rows, network from SQL",
      "official R365 weekly rows" in tf["basis"] and len(tf["by_store"]) == 12
      and nt["net_sales"] == 517643.02 and nt["labor_pct"] == 19.85,
      f"{nt['net_sales']} / {nt['labor_pct']}")
tf = C.r365_trusted_figures(14, "2026-09-02", "2026-09-05")
check("blocked-path figures for a non-week range sum daily rows, one store, no network row",
      "daily rows" in tf["basis"] and len(tf["by_store"]) == 1 and tf["network_total"] is None)

base = {"available": True, "coverage_start": "2026-08-13", "latest_daily_date": "2026-10-14",
        "uncovered_dates_in_scope": {}, "store_missing_dates_in_scope": [],
        "special_status_days_in_scope": [], "covered_dates_in_scope": 7, "requested_dates": 7}
f, w = C._r365_gate({**base, "weeks_in_scope": [{"week_start": "2026-10-08"}],
                     "partial_weeks_in_scope": ["2026-10-08"]})
check("a single PARTIAL week FAILS weekly analysis", f and "PARTIAL" in f[0])
f, w = C._r365_gate({**base, "weeks_in_scope": [{"week_start": "2026-10-01"}, {"week_start": "2026-10-08"}],
                     "partial_weeks_in_scope": ["2026-10-08"]})
check("a PARTIAL week among COMPLETE ones warns (answer the trusted part, label it)",
      not f and any("PARTIAL" in x for x in w))
f, w = C._r365_gate({**base, "weeks_in_scope": [], "partial_weeks_in_scope": [],
                     "store_missing_dates_in_scope": ["2026-10-09"]})
check("a missing store-day warns as unknown, not zero", any("not zero" in x for x in w))
f, w = C._r365_gate({**base, "weeks_in_scope": [{"week_start": "2026-10-08", "latest_refresh_failed": True}],
                     "partial_weeks_in_scope": []})
check("a failed later refresh is surfaced", any("refresh failed" in x for x in w))
check("undeterminable availability fails closed", C._r365_gate({})[0])

# ---------------------------------------------------------------------------
print("4. metadata")
m = P(None, "2026-10-08", "2026-10-15")["r365"]
check("metadata carries the agreed fields",
      {"available", "coverage_start", "latest_daily_date", "latest_complete_week_start",
       "latest_complete_week_end", "mapped_store_count", "expected_store_count",
       "week_definition", "timezone", "partial_weeks", "metric_availability",
       "freshness_status", "next_ingest_window", "semantics"} <= m.keys())
check("coverage 2026-08-13, latest week 2026-10-08..2026-10-14, 12/12 stores",
      m["coverage_start"] == "2026-08-13" and m["latest_complete_week_start"] == "2026-10-08"
      and m["latest_complete_week_end"] == "2026-10-14" and m["latest_daily_date"] == "2026-10-14"
      and m["mapped_store_count"] == 12 and m["expected_store_count"] == 12)
check("week definition and timezone are explicit",
      m["week_definition"] == "Thursday-Wednesday" and m["timezone"] == "America/Chicago")
ma = m["metric_availability"]
check("metric availability: R365 metrics true; items/hourly/employees false",
      all(ma[k] for k in ("sales", "labor", "cash", "discounts", "gift_cards", "tips", "voids", "forecast"))
      and not any(ma[k] for k in ("menu_items", "hourly", "individual_employees")))
sem = " ".join(m["semantics"])
check("metadata documents points, NULL, missing!=zero, CLOSED/PRE_OPENING, forecast base",
      all(s in sem for s in ("PERCENTAGE POINTS", "NULL", "never zero", "CLOSED",
                             "forecast_sales_base")))
C._r365_summary_cache.clear()
conn, cur = ro()
t = time.perf_counter()
C._r365_summary(cur)
cold = time.perf_counter() - t
t = time.perf_counter()
C._r365_summary(cur)
warm = time.perf_counter() - t
check("network summary is cached (second build does no SQL)", warm < cold and warm < 0.001,
      f"cold {cold*1000:.1f} ms, warm {warm*1000:.3f} ms")
conn.close()
tz = CHI
check("freshness: Thursday 19:00 still expects the PREVIOUS week",
      C.r365_expected_week_start(datetime(2026, 10, 15, 19, 0, tzinfo=tz)) == date(2026, 10, 1))
check("freshness: Thursday 21:30 expects the week that closed Wednesday",
      C.r365_expected_week_start(datetime(2026, 10, 15, 21, 30, tzinfo=tz)) == date(2026, 10, 8))
check("freshness: CURRENT on Friday with 10-08 loaded; LATE next Thursday night without 10-15",
      C.r365_freshness(date(2026, 10, 8), datetime(2026, 10, 16, 10, tzinfo=tz))["freshness_status"] == "CURRENT"
      and C.r365_freshness(date(2026, 10, 8), datetime(2026, 10, 22, 21, 30, tzinfo=tz))["freshness_status"] == "LATE")

# ---------------------------------------------------------------------------
print("5. ratio, forecast and day-status semantics (views, as laynes_ro)")
conn, cur = ro()
cur.execute("""SELECT sum(net_sales) n, sum(check_count) c, sum(guest_count) g,
                      sum(labor_cost) lc, sum(labor_hours) lh, sum(total_discounts) td,
                      avg(labor_pct) naive_avg
                 FROM v_r365_store_weekly_llm WHERE week_start = '2026-10-08'""")
r = cur.fetchone()
weighted = r["lc"] / r["n"] * 100
check("weighted network labour % differs from the naive average of store %",
      abs(weighted - r["naive_avg"]) > Decimal("0.01"),
      f"weighted {weighted:.4f} vs naive {r['naive_avg']:.4f}")
cur.execute("""SELECT net_sales, check_count, guest_count, labor_cost, labor_hours,
                      total_discounts, labor_pct, avg_check, discount_pct, sales_per_labor_hour
                 FROM v_r365_store_period_llm WHERE fiscal_year = 2026 AND period_number = 10""")
rows = cur.fetchall()
ok = all(abs(x["labor_pct"] - x["labor_cost"] / x["net_sales"] * 100) < Decimal("1e-9")
         and abs(x["avg_check"] - x["net_sales"] / x["check_count"]) < Decimal("1e-9")
         and abs(x["discount_pct"] - x["total_discounts"] / x["net_sales"] * 100) < Decimal("1e-9")
         and abs(x["sales_per_labor_hour"] - x["net_sales"] / x["labor_hours"]) < Decimal("1e-9")
         for x in rows)
check("period view ratios are recomputed from summed parts (12 stores, P10)", ok and len(rows) == 12)
cur.execute("""SELECT net_sales, labor_cost, labor_pct, discount_pct, avg_check, avg_per_guest
                 FROM v_r365_store_daily_llm
                WHERE establishment_id = 14 AND business_date = '2026-09-03'""")
z = cur.fetchone()
check("zero-denominator day: ratios are NULL, not 0% (Beaumont 09-03)",
      z["net_sales"] == 0 and z["labor_cost"] > 0 and z["labor_pct"] is None
      and z["discount_pct"] is None and z["avg_check"] is None and z["avg_per_guest"] is None)
cur.execute("""SELECT count(*) FILTER (WHERE forecasted_sales IS NULL) no_fc,
                      count(*) FILTER (WHERE forecasted_sales IS NULL
                                       AND (sales_vs_forecast_amount IS NOT NULL
                                            OR sales_vs_forecast_pct IS NOT NULL
                                            OR forecast_sales_base IS NOT NULL)) leaked,
                      count(*) FILTER (WHERE forecasted_sales = 0) zero_fc
                 FROM v_r365_store_daily_llm""")
f = cur.fetchone()
check("no-forecast days are NULL with no variance; no 0 forecast reaches chat",
      f["no_fc"] == 76 and f["leaked"] == 0 and f["zero_fc"] == 0, str(dict(f)))
cur.execute("""SELECT sum(forecast_sales_base) - sum(forecasted_sales) covered,
                      sum(net_sales) - sum(forecasted_sales) naive
                 FROM v_r365_store_weekly_llm WHERE week_start = '2026-10-08'""")
f = cur.fetchone()
check("forecast-covered variance differs from the naive all-sales variance",
      f["covered"] != f["naive"], f"covered {f['covered']:.2f} vs naive {f['naive']:.2f}")
cur.execute("""SELECT count(*) n FROM v_r365_store_weekly_llm
                WHERE week_start = '2026-10-08' AND forecasted_sales IS NULL""")
check("stores with no forecast in a week are identifiable (neither met nor missed)",
      cur.fetchone()["n"] >= 1)
cur.execute("""SELECT count(*) n FROM v_r365_store_daily_llm
                WHERE business_date BETWEEN '2026-08-13' AND '2026-10-14'""")
check("daily view: 756 rows, no zero rows synthesised", cur.fetchone()["n"] == 756)

# ---------------------------------------------------------------------------
print("6. fiscal periods")
cur.execute("""SELECT period_label, period_status, weeks_loaded, weeks_in_period,
                      period_fully_covered, sum(net_sales) OVER (PARTITION BY period_label) pn
                 FROM v_r365_store_period_llm WHERE fiscal_year = 2026 AND establishment_id = 32
                ORDER BY period_number""")
per = {x["period_label"]: x for x in cur.fetchall()}
check("P9 and P10 are fully covered (4/4 COMPLETE weeks)",
      per["P9"]["period_fully_covered"] and per["P10"]["period_fully_covered"]
      and per["P10"]["weeks_loaded"] == per["P10"]["weeks_in_period"] == 4)
check("current P11 is marked partial (1 of 4 weeks)",
      per["P11"]["period_status"] == "CURRENT" and not per["P11"]["period_fully_covered"]
      and per["P11"]["weeks_loaded"] == 1)
cur.execute("""SELECT (SELECT sum(net_sales) FROM v_r365_store_period_llm
                        WHERE fiscal_year = 2026 AND period_number = 10) p,
                      (SELECT sum(net_sales) FROM v_r365_store_weekly_llm
                        WHERE week_start BETWEEN '2026-09-10' AND '2026-10-07') w""")
x = cur.fetchone()
check("P10 period total equals its four official weekly rows", x["p"] == x["w"], f"{x['p']} / {x['w']}")
conn.close()
check("a period query with its start_date literal passes the scope gate",
      C.enforce_scope("SELECT * FROM v_r365_store_period_llm WHERE fiscal_year = 2026 "
                      "AND period_number = 10 AND start_date = '2026-09-10'",
                      None, "2026-09-10", "2026-10-08")["analysis_permitted"])
try:
    C.enforce_scope("SELECT * FROM v_r365_store_period_llm WHERE period_number = 10",
                    None, "2026-09-10", "2026-10-08")
    no_literal = False
except C.ScopeError:
    no_literal = True
check("a period query with NO date literal is rejected (unprovable scope)", no_literal)

# ---------------------------------------------------------------------------
print("7. guidance (no fabrication, correct authority)")
g = " ".join(C._system_static().split()).lower()   # wrap- and case-insensitive
for needle, why in [
    ("R365 DOES NOT HAVE menu items, hours/dayparts, individual orders or individual\nemployees",
     "R365 has no items/hours/employees"),
    ("finest grain is store x DAY", "finest grain stated"),
    ("the latest COMPLETE R365 week", "last week = latest COMPLETE week"),
    ("not a rolling seven days", "not rolling 7 days"),
    ("NEVER average or sum a ratio column", "no averaged ratios"),
    ("SUM(labor_cost) / NULLIF(SUM(net_sales), 0) * 100", "weighted labour %"),
    ("Never use SUM(net_sales) - SUM(forecasted_sales)", "forecast base rule"),
    ("neither met nor missed forecast", "no-forecast is not a miss"),
    ("Do NOT join HME on week_start", "weekly HME aggregation"),
    ("Revel REAL subtotal (pre-tax)", "R365 vs Revel labelling"),
    ("never on store name", "no name joins"),
    ("Correlation is not causation", "causation caveat"),
    ("drive-thru) keep their existing source. Do not switch them to R365 unasked",
     "no silent source switch for plain sales/orders/items/drive-thru"),
    ("CLOSED and PRE_OPENING", "day status"),
    ("R365 DOES report discounts, comps", "Revel comp limitation scoped"),
    ("come from R365 (labour cost and the sales it is measured against",
     "day/week/period labour % comes from R365"),
    ("remain the source for HOURLY staffing", "hourly staffing stays with v_labor_*"),
    ("never mix it with estimated_labor_cost", "R365 vs estimated labour cost kept apart"),
    ("NETWORK TOTALS COME FROM SQL", "network totals computed in SQL, not mentally"),
    ("Do not call them payroll", "R365 labour not described as payroll"),
    ("fetch the R365 side FIRST", "cross-source: trusted R365 side fetched first"),
    ("Quote the NETWORK TOTAL row exactly as returned", "network total row pattern"),
    ("The CAUSE of that gap is not established", "no speculation about R365/Revel gap"),
    ("an R365 week always starts on a THURSDAY", "week dates read, never recalled"),
]:
    check(f"guidance: {why}", " ".join(needle.split()).lower() in g)

failed = [r for r in results if not r[1]]
print(f"\nR365-only enforce_scope (profile + gate): {r365_only_s*1000:.1f} ms")
print(f"{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
