#!/usr/bin/env python3
"""R365 FINANCIAL in chat: validator boundary, laynes_ro security, safe views,
domain routing, trust gate, missing-vs-zero, fiscal periods, cross-source
joins and source guidance.

Run:  venv/bin/python tests/test_r365_financial_chat.py     (exit 0 = pass)

Read-only. Database checks connect as the LLM role (laynes_ro) wherever the
assistant would, so they prove what chat can actually see. No source API call
is made anywhere in this file.
"""
import os
import sys
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
SAFE = ("v_r365_financial_period_llm", "v_r365_financial_coverage_llm",
        "v_r365_financial_account_llm")
RAW = ("r365_financial_period_expense", "r365_financial_candidate_expense",
       "r365_financial_account_period", "r365_financial_account",
       "r365_financial_ingest_run")
P9 = ("2026-08-13", "2026-09-10")
P10 = ("2026-09-10", "2026-10-08")


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
print("1. SQL validator allowlist")
for v in SAFE:
    check(f"{v} is allowed", not rejected(f"SELECT * FROM {v}"))
for t in RAW:
    check(f"raw {t} is rejected", rejected(f"SELECT * FROM {t}"))
check("exactly three financial relations are allowlisted",
      {r for r in C._ALLOWED_RELATIONS if "financial" in r} == set(SAFE))
check("raw table in a CTE is rejected",
      rejected("WITH x AS (SELECT * FROM r365_financial_candidate_expense) SELECT * FROM x"))
check("raw table in an implicit join is rejected",
      rejected("SELECT 1 FROM v_r365_financial_period_llm f, r365_financial_account_period a"))
check("raw table via subquery is rejected",
      rejected("SELECT * FROM v_r365_financial_period_llm WHERE establishment_id IN "
               "(SELECT establishment_id FROM r365_financial_period_expense)"))
check("existing R365 operational allowlist unchanged",
      {r for r in C._ALLOWED_RELATIONS if r.startswith("v_r365_store") or r == "v_r365_completeness_llm"}
      == {"v_r365_store_daily_llm", "v_r365_store_weekly_llm", "v_r365_store_period_llm",
          "v_r365_completeness_llm"})
check("existing raw R365 / HME tables still rejected",
      all(rejected(f"SELECT * FROM {t}") for t in
          ("r365_store_daily", "r365_ingest_run", "hme_store_daily", "hme_ingest_run")))

# ---------------------------------------------------------------------------
print("2. laynes_ro security")
conn, cur = ro()
for v in SAFE:
    try:
        cur.execute(f"SELECT * FROM {v} LIMIT 1")
        check(f"laynes_ro can read {v}", True)
    except psycopg2.Error as e:
        check(f"laynes_ro can read {v}", False, str(e).strip())
for t in RAW:
    try:
        cur.execute(f"SELECT 1 FROM {t} LIMIT 1")
        check(f"laynes_ro denied {t}", False, "readable!")
    except psycopg2.errors.InsufficientPrivilege:
        check(f"laynes_ro denied {t}", True)
cur.execute("""SELECT table_name, string_agg(privilege_type, ',' ORDER BY privilege_type) p
                 FROM information_schema.role_table_grants
                WHERE grantee = 'laynes_ro' AND table_name LIKE '%%financial%%'
                GROUP BY 1 ORDER BY 1""")
grants = {r["table_name"]: r["p"] for r in cur.fetchall()}
check("laynes_ro holds SELECT only, on exactly the three safe views",
      grants == {v: "SELECT" for v in SAFE}, str(grants))

# ---------------------------------------------------------------------------
print("3. safe views")
cur.execute("SELECT * FROM v_r365_financial_period_llm LIMIT 1")
cols = {d.name for d in cur.description}
expected = {"establishment_id", "store_name", "fiscal_year", "period_number", "period_label",
            "period_start", "period_end", "period_status", "analytics_category",
            "expense_amount", "transaction_count", "account_count", "data_status",
            "has_posting", "source_quality"}
check("period view exposes exactly the approved fields", cols == expected,
      str(sorted(cols ^ expected)))
forbidden = {"r365_location_id", "location_id", "ingest_run_id", "error_message",
             "raw_archive_path", "payload_sha256", "account_id", "gl_account_id"}
for v in SAFE:
    cur.execute(f"SELECT * FROM {v} LIMIT 1")
    leaked = {d.name for d in cur.description} & forbidden
    check(f"{v} leaks no ids / run metadata", not leaked, str(leaked))
cur.execute("SELECT * FROM v_r365_financial_account_llm LIMIT 1")
check("account view carries no amounts",
      not any("amount" in d.name for d in cur.description))
cur.execute("SELECT DISTINCT analytics_category FROM v_r365_financial_period_llm")
cats = {r["analytics_category"] for r in cur.fetchall()}
check("VERIFIED category filtering: only ELECTRICITY exposed", cats == {"ELECTRICITY"}, str(cats))
cur.execute("SELECT count(*) n FROM v_r365_financial_period_llm WHERE establishment_id IS NULL")
check("only VERIFIED-mapped stores exposed (no non-store entities)", cur.fetchone()["n"] == 0)
cur.execute("""SELECT count(DISTINCT establishment_id) n FROM v_r365_financial_period_llm
                WHERE fiscal_year = 2026 AND period_number = 10""")
check("all 12 VERIFIED stores represented in P10", cur.fetchone()["n"] == 12)
cur.execute("""SELECT mapping_status, count(*) n FROM v_r365_financial_account_llm
                WHERE account_name ILIKE 'Rent%%' OR account_name IN ('Gas', 'Water')
                GROUP BY 1""")
st = {r["mapping_status"]: r["n"] for r in cur.fetchall()}
check("rent / gas / water accounts visible as metadata and NOT VERIFIED",
      st and "VERIFIED" not in st, str(st))

# ---------------------------------------------------------------------------
print("4. electricity P9 / P10 (actual API values loaded)")


def period_total(pn):
    cur.execute("""SELECT sum(expense_amount) total, count(expense_amount) posting, count(*) n
                     FROM v_r365_financial_period_llm
                    WHERE analytics_category = 'ELECTRICITY' AND fiscal_year = 2026
                      AND period_number = %s""", (pn,))
    return cur.fetchone()


t9, t10 = period_total(9), period_total(10)
check("P10 electricity = $20,329.87 across 9 of 12 stores",
      t10["total"] == Decimal("20329.87") and t10["posting"] == 9 and t10["n"] == 12, str(t10))
check("P9 electricity = $20,789.68 across 9 of 12 stores",
      t9["total"] == Decimal("20789.68") and t9["posting"] == 9 and t9["n"] == 12, str(t9))
delta = t10["total"] - t9["total"]
check("P9 -> P10 change = -$459.81 (-2.2%)",
      delta == Decimal("-459.81") and round(delta / t9["total"] * 100, 1) == Decimal("-2.2"))
cur.execute("""SELECT store_name, expense_amount FROM v_r365_financial_period_llm
                WHERE analytics_category = 'ELECTRICITY' AND fiscal_year = 2026
                  AND period_number = 10 ORDER BY expense_amount DESC NULLS LAST LIMIT 1""")
top = cur.fetchone()
check("P10 top store is Missouri City at $4,222.32",
      top["store_name"] == "LCF Missouri City" and top["expense_amount"] == Decimal("4222.32"))

print("5. missing != zero")
cur.execute("""SELECT store_name, expense_amount, data_status, transaction_count
                 FROM v_r365_financial_period_llm
                WHERE analytics_category = 'ELECTRICITY' AND fiscal_year = 2026
                  AND period_number = 10 AND NOT has_posting ORDER BY store_name""")
none = cur.fetchall()
check("P10 no-posting stores = Downtown Houston, Nederland, Shepherd",
      [r["store_name"] for r in none] == ["LCF Downtown Houston", "LCF Nederland", "LCF Shepherd"],
      str([r["store_name"] for r in none]))
check("no-posting rows carry NULL amount and NO_ACTIVITY status, never 0",
      all(r["expense_amount"] is None and r["transaction_count"] is None
          and r["data_status"] == "NO_ACTIVITY" for r in none))
cur.execute("SELECT count(*) n FROM v_r365_financial_period_llm WHERE expense_amount = 0 "
            "AND NOT has_posting")
check("no zero-amount stand-ins exist anywhere", cur.fetchone()["n"] == 0)
cur.execute("""SELECT * FROM v_r365_financial_coverage_llm
                WHERE fiscal_year = 2026 AND period_number IN (10, 11) ORDER BY period_number""")
cv = cur.fetchall()
check("coverage: P10 COMPLETE with 9 posting / 3 without",
      cv[0]["source_data_status"] == "COMPLETE" and cv[0]["stores_with_posting"] == 9
      and cv[0]["stores_without_posting"] == 3 and cv[0]["expected_store_count"] == 12)
check("coverage: current P11 is PARTIAL / CURRENT",
      cv[1]["source_data_status"] == "PARTIAL" and cv[1]["period_status"] == "CURRENT")

print("6. fiscal periods")
cur.execute("""SELECT DISTINCT period_start, period_end FROM v_r365_financial_period_llm
                WHERE fiscal_year = 2026 AND period_number IN (9, 10, 11) ORDER BY 1""")
check("P9/P10/P11 dates match the official calendar",
      [(r["period_start"], r["period_end"]) for r in cur.fetchall()]
      == [(date(2026, 8, 13), date(2026, 9, 9)), (date(2026, 9, 10), date(2026, 10, 7)),
          (date(2026, 10, 8), date(2026, 11, 4))])
cur.execute("SELECT min(fiscal_year) fy FROM v_r365_financial_period_llm")
check("no period before FY2026 is exposed (none invented)", cur.fetchone()["fy"] == 2026)
conn.close()

# ---------------------------------------------------------------------------
print("7. domain routing")
D = C.domains_for_relations
check("financial view -> {r365_financial}", D({"v_r365_financial_period_llm"}) == {"r365_financial"})
check("financial + calendar + establishments -> {r365_financial}",
      D({"v_r365_financial_period_llm", "v_ayg_business_periods_llm", "establishments"})
      == {"r365_financial"})
check("financial + R365 operational period -> {r365_financial, r365} (no Revel/HME)",
      D({"v_r365_financial_period_llm", "v_r365_store_period_llm"}) == {"r365_financial", "r365"})
check("financial + Revel -> Revel gate added",
      D({"v_r365_financial_period_llm", "v_orders_classified"}) == {"r365_financial", "revel"})
check("coverage and account views are reference (ungated)",
      D({"v_r365_financial_coverage_llm"}) == set() and D({"v_r365_financial_account_llm"}) == set()
      and {"v_r365_financial_coverage_llm", "v_r365_financial_account_llm"} <= C._REFERENCE_RELATIONS)
check("r365_financial is opt-in (not in the strict default set)",
      "r365_financial" not in C.ALL_DOMAINS and "r365_financial" in C.KNOWN_DOMAINS)
check("check_data accepts 'r365_financial'",
      "r365_financial" in C.CHECK_DATA_TOOL["input_schema"]["properties"]["domains"]["items"]["enum"])
check("unknown domain name never drops r365_financial",
      "r365_financial" in C.normalise_domains(["r365_financial", "bogus"]))

print("8. trust gate")
m = C.meta_profile(None, *P10, domains=["r365_financial"])
check("P10 scope passes the financial gate", m["analysis_permitted"], str(m["blocking_reasons"]))
check("P10 gate warns about the 3 no-posting stores, as NOT $0",
      any("NO POSTING" in w and "NOT $0" in w and "LCF Shepherd" in w for w in m["warnings"]))
check("financial-only scope did not run Revel reconciliation",
      "reconciliation" not in m and m["domains"] == ["r365_financial"])
m = C.meta_profile(None, "2026-09-10", "2026-09-17", domains=["r365_financial"])
check("a one-week scope is refused: period grain only",
      not m["analysis_permitted"] and "per AYG fiscal period only" in m["blocking_reasons"][0])
m = C.meta_profile(None, "2025-09-11", "2025-10-09", domains=["r365_financial"])
check("FY2025 scope is refused: no official calendar, nothing invented",
      not m["analysis_permitted"] and "FY2026 and FY2027 only" in m["blocking_reasons"][0])
m = C.meta_profile(None, "2026-10-08", "2026-11-05", domains=["r365_financial"])
check("current P11 is permitted but flagged PARTIAL",
      m["analysis_permitted"] and any("PARTIAL" in w for w in m["warnings"]))
g = C._r365_fin_gate({"available": True, "scope_is_whole_periods": True,
                      "calendar_periods_in_scope": 1, "periods_in_scope": [],
                      "refresh_age_hours": 50, "refresh_schedule": "daily"})
check("a stale refresh warns (does not silently pass)",
      any("last refreshed 50" in w for w in g[1]))
ctx = C.blocked_domain_context({"blocking_reasons": ["R365 FINANCIAL has no loaded period"],
                                "r365": {"available": True}, "r365_financial": {}},
                               ["r365", "r365_financial"])
check("a financial failure is not attributed to R365 operational",
      ctx["r365"]["trusted"] is True and ctx["r365_financial"]["trusted"] is False)
ctx = C.blocked_domain_context({"blocking_reasons": ["R365 FINANCIAL has no loaded period"]},
                               ["revel", "r365_financial"])
check("a financial failure is not attributed to Revel", ctx["revel"]["trusted"] is True)

print("9. scope enforcement + cross-source")
q10 = ("SELECT store_name, expense_amount FROM v_r365_financial_period_llm "
       "WHERE analytics_category = 'ELECTRICITY' AND fiscal_year = 2026 AND period_number = 10 "
       "AND period_start = '2026-09-10'")
try:
    C.enforce_scope(q10, None, None, None)
    check("financial query without a scope is rejected (raises)", False)
except C.ScopeError:
    check("financial query without a scope is rejected (raises)", True)
meta = C.enforce_scope(q10, None, *P10)
check("scoped P10 query passes enforce_scope on the financial domain only",
      meta["domains"] == ["r365_financial"])
xs = ("SELECT f.store_name, f.expense_amount, s.net_sales, s.labor_pct, "
      "f.expense_amount / NULLIF(s.net_sales, 0) * 100 AS electricity_pct_of_sales "
      "FROM v_r365_financial_period_llm f JOIN v_r365_store_period_llm s "
      "ON s.establishment_id = f.establishment_id AND s.fiscal_year = f.fiscal_year "
      "AND s.period_number = f.period_number "
      "WHERE f.analytics_category = 'ELECTRICITY' AND f.fiscal_year = 2026 "
      "AND f.period_number = 10 AND f.period_start = '2026-09-10'")
meta = C.enforce_scope(xs, None, *P10)
check("financial + R365 operational join gated by both, not Revel/HME",
      meta["domains"] == ["r365", "r365_financial"] and meta["analysis_permitted"])
rows = C.run_sql(xs)["rows"]
withp = [r for r in rows if r["expense_amount"] is not None]
pct = sum(r["expense_amount"] for r in withp) / sum(r["net_sales"] for r in withp) * 100
check("electricity % of R365 net sales computes over stores with a posting",
      len(rows) == 12 and len(withp) == 9 and 0 < pct < 10, f"{pct:.2f}%")
tot = C.run_sql("SELECT sum(f.expense_amount) / NULLIF(sum(s.net_sales), 0) * 100 AS pct "
                "FROM v_r365_financial_period_llm f JOIN v_r365_store_period_llm s "
                "ON s.establishment_id = f.establishment_id AND s.fiscal_year = f.fiscal_year "
                "AND s.period_number = f.period_number WHERE f.has_posting "
                "AND f.analytics_category = 'ELECTRICITY' AND f.fiscal_year = 2026 "
                "AND f.period_number = 10 AND f.period_start = '2026-09-10'")["rows"][0]["pct"]
check("SQL network % equals the per-store recomputation", abs(tot - pct) < 1e-9, f"{tot:.4f}%")

print("10. no hourly financial fabrication")
conn, cur = ro()
for v in SAFE:
    cur.execute(f"SELECT * FROM {v} LIMIT 1")
    names = {d.name for d in cur.description}
    check(f"{v} has no hour/day/week column",
          not names & {"hour", "local_hour", "business_date", "week_start", "accounting_date"})
conn.close()
check("metric availability: hourly/daily/weekly false",
      not any(C.R365_FIN_METRIC_AVAILABILITY[k] for k in ("hourly", "daily", "weekly")))

print("11. source guidance")
s = C._system_static()
G = C.R365_FINANCIAL_GUIDANCE
check("financial guidance is in the system prompt", G in s)
for needle, what in [
        ("NEVER derive or estimate an expense from sales", "never derive expense from sales"),
        ("It is unknown, NOT $0", "missing is not zero"),
        ("PERIOD GRAIN ONLY", "no hourly/daily financial data"),
        ("are NOT electricity", "gas/water/etc. not electricity"),
        ("no VERIFIED <category>", "unverified category answer (rent)"),
        ("never to Revel unless Revel is explicitly requested", "no Revel in cross-source"),
        ("R365 OPERATIONAL", "R365 operational authority"),
        ("REVEL: POS", "Revel authority"), ("HME: drive-thru", "HME authority"),
        ("establishment_id AND fiscal_year AND period_number", "cross-source join keys")]:
    check(f"guidance: {what}", needle in G)

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
