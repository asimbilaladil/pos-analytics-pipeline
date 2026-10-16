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
TXN = ("v_r365_financial_transaction_llm", "v_r365_financial_unapproved_transaction_llm")
SAFE = ("v_r365_financial_period_llm", "v_r365_financial_coverage_llm",
        "v_r365_financial_account_llm") + TXN
RAW = ("r365_financial_period_expense", "r365_financial_candidate_expense",
       "r365_financial_account_period", "r365_financial_account",
       "r365_financial_ingest_run", "r365_financial_transaction",
       "r365_financial_local_category_mapping", "r365_financial_effective_mapping")
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
check("exactly the five safe financial relations are allowlisted",
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
                  AND table_schema = current_schema()
                GROUP BY 1 ORDER BY 1""")
grants = {r["table_name"]: r["p"] for r in cur.fetchall()}
check("laynes_ro holds SELECT only, on exactly the five safe views",
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
check("VERIFIED category filtering: exactly the five VERIFIED categories exposed",
      cats == {"ELECTRICITY", "GAS", "WATER", "RENT", "REPAIRS_MAINTENANCE"}, str(cats))
cur.execute("SELECT count(*) n FROM v_r365_financial_period_llm WHERE establishment_id IS NULL")
check("only VERIFIED-mapped stores exposed (no non-store entities)", cur.fetchone()["n"] == 0)
cur.execute("""SELECT count(DISTINCT establishment_id) n FROM v_r365_financial_period_llm
                WHERE fiscal_year = 2026 AND period_number = 10""")
check("all 12 VERIFIED stores represented in P10", cur.fetchone()["n"] == 12)
cur.execute("""SELECT account_number, mapping_status FROM v_r365_financial_account_llm
                WHERE account_name ILIKE 'Rent%%' OR account_name IN ('Gas', 'Water')""")
st = {r["account_number"]: r["mapping_status"] for r in cur.fetchall()}
check("rent / gas / water accounts: only 6200-01, 6200-02 and 7500-11 VERIFIED",
      {a for a, v in st.items() if v == "VERIFIED"} == {"6200-01", "6200-02", "7500-11"}
      and st.get("7500-09") == "CANDIDATE" and st.get("7500-13") == "CANDIDATE", str(st))

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
                WHERE fiscal_year = 2026 AND period_number IN (10, 11)
                  AND analytics_category = 'ELECTRICITY' ORDER BY period_number""")
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
check("P10 gate warns about the 3 no-posting stores, as never $0",
      any("NO POSTING" in w and "never $0" in w and "LCF Shepherd" in w for w in m["warnings"]))
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
for v in SAFE[:3]:            # summary-grain views: no date grain at all
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
        ("never \"$0\", \"zero expense\"", "missing is not zero"),
        ("NO HOURS, NO USAGE TIMES", "no hourly financial data"),
        ("never add them to a VERIFIED category", "candidate accounts never folded in"),
        ("no VERIFIED <category>", "unverified category answer (rent)"),
        ("never to Revel unless Revel is explicitly requested", "no Revel in cross-source"),
        ("R365 OPERATIONAL", "R365 operational authority"),
        ("REVEL: POS", "Revel authority"), ("HME: drive-thru", "HME authority"),
        ("establishment_id AND fiscal_year AND period_number", "cross-source join keys")]:
    check(f"guidance: {what}", needle in G)

# ---------------------------------------------------------------------------
print("12. transaction drill-down")
conn, cur = ro()
cur.execute("SELECT * FROM v_r365_financial_transaction_llm LIMIT 1")
tcols = {d.name for d in cur.description}
texp = {"establishment_id", "store_name", "accounting_date", "fiscal_year", "period_number",
        "period_label", "gl_account_number", "gl_account_name", "analytics_category",
        "mapping_status", "transaction_type", "transaction_status", "transaction_number",
        "debit_amount", "credit_amount", "expense_amount", "source_quality"}
check("transaction view exposes exactly the reviewed fields", tcols == texp,
      str(sorted(tcols ^ texp)))
cur.execute("SELECT * FROM v_r365_financial_unapproved_transaction_llm LIMIT 1")
check("unapproved view has the same fields", {d.name for d in cur.description} == texp)
cur.execute("SELECT DISTINCT transaction_status FROM v_r365_financial_transaction_llm")
check("default transaction view is Approved only",
      [r["transaction_status"] for r in cur.fetchall()] == ["Approved"])
cur.execute("SELECT DISTINCT transaction_status FROM v_r365_financial_unapproved_transaction_llm")
check("unapproved view holds only Unapproved lines (explicit requests)",
      [r["transaction_status"] for r in cur.fetchall()] == ["Unapproved"])
cur.execute("SELECT count(*) n FROM v_r365_financial_transaction_llm "
            "WHERE mapping_status <> 'VERIFIED' AND analytics_category IS NOT NULL")
check("unverified lines never carry an analytics category (no silent GAS/RENT)",
      cur.fetchone()["n"] == 0)
cur.execute("SELECT DISTINCT analytics_category FROM v_r365_financial_transaction_llm "
            "WHERE analytics_category IS NOT NULL")
check("only the five VERIFIED categories appear on lines",
      sorted(r["analytics_category"] for r in cur.fetchall())
      == ["ELECTRICITY", "GAS", "RENT", "REPAIRS_MAINTENANCE", "WATER"])
cur.execute("SELECT count(*) n FROM v_r365_financial_transaction_llm "
            "WHERE expense_amount <> debit_amount - credit_amount")
check("every visible line satisfies expense = debit - credit", cur.fetchone()["n"] == 0)
cur.execute("SELECT count(*) n FROM v_r365_financial_transaction_llm WHERE establishment_id IS NULL")
check("only VERIFIED-store lines visible", cur.fetchone()["n"] == 0)
cur.execute("SELECT count(*) n FROM v_r365_financial_transaction_llm "
            "WHERE accounting_date < '2026-01-01' AND fiscal_year IS NOT NULL")
check("pre-FY2026 lines carry no invented fiscal period", cur.fetchone()["n"] == 0)
cur.execute("SELECT DISTINCT source_quality FROM v_r365_financial_transaction_llm "
            "WHERE accounting_date >= '2026-03-26'")
check("native TRANSACTION_DETAIL from P4 onward",
      [r["source_quality"] for r in cur.fetchall()] == ["TRANSACTION_DETAIL"])

for pn, total in ((9, Decimal("20789.68")), (10, Decimal("20329.87"))):
    cur.execute("""SELECT p.store_name, p.expense_amount AS summ, t.s AS txn
                     FROM v_r365_financial_period_llm p
                     LEFT JOIN (SELECT establishment_id, sum(expense_amount) s
                                  FROM v_r365_financial_transaction_llm
                                 WHERE analytics_category = 'ELECTRICITY' AND fiscal_year = 2026
                                   AND period_number = %s GROUP BY 1) t USING (establishment_id)
                    WHERE p.analytics_category = 'ELECTRICITY' AND p.fiscal_year = 2026
                      AND p.period_number = %s""", (pn, pn))
    rows = cur.fetchall()
    bad = [r["store_name"] for r in rows if r["summ"] != r["txn"]]
    check(f"P{pn} electricity: every store's lines equal its summary (no-posting stores have none)",
          not bad, str(bad))
    cur.execute("SELECT sum(expense_amount) s FROM v_r365_financial_transaction_llm "
                "WHERE analytics_category = 'ELECTRICITY' AND fiscal_year = 2026 "
                "AND period_number = %s", (pn,))
    check(f"P{pn} electricity transaction total = summary {total}, difference 0",
          cur.fetchone()["s"] == total)
cur.execute("""WITH t AS (SELECT establishment_id, fiscal_year, period_number, analytics_category,
                                 sum(expense_amount) s
                            FROM v_r365_financial_transaction_llm
                           WHERE analytics_category IS NOT NULL AND fiscal_year IS NOT NULL
                           GROUP BY 1, 2, 3, 4),
                    p AS (SELECT establishment_id, fiscal_year, period_number, analytics_category,
                                 expense_amount s FROM v_r365_financial_period_llm WHERE has_posting)
               SELECT count(*) pairs, count(*) FILTER (WHERE t.s IS DISTINCT FROM p.s) diffs
                 FROM p FULL JOIN t USING (establishment_id, fiscal_year, period_number,
                                           analytics_category)""")
rc = cur.fetchone()
check("ALL VERIFIED store/period/category values reconcile summary <-> lines (0 differences)",
      rc["diffs"] == 0 and rc["pairs"] > 100, str(dict(rc)))
cur.execute("""SELECT store_name, expense_amount FROM v_r365_financial_transaction_llm
                WHERE analytics_category = 'ELECTRICITY' AND fiscal_year = 2026
                  AND period_number = 10 ORDER BY expense_amount DESC LIMIT 1""")
top = cur.fetchone()
check("largest P10 electricity posting is a single Missouri City line of $3,856.01",
      top["store_name"] == "LCF Missouri City" and top["expense_amount"] == Decimal("3856.01"))
cur.execute("""SELECT count(*) n, sum(expense_amount) s FROM v_r365_financial_transaction_llm
                WHERE analytics_category = 'ELECTRICITY' AND fiscal_year = 2026
                  AND period_number = 10 AND store_name = 'LCF Missouri City'""")
mc = cur.fetchone()
check("Missouri City P10: 3 lines adding to $4,222.32", mc["n"] == 3 and mc["s"] == Decimal("4222.32"))
cur.execute("SELECT count(*) n FROM v_r365_financial_unapproved_transaction_llm "
            "WHERE gl_account_number = '6200-03'")
check("no Unapproved electricity lines exist (a real, reportable finding)",
      cur.fetchone()["n"] == 0)
cur.execute("""SELECT gl_account_number, mapping_status, count(*) n
                 FROM v_r365_financial_transaction_llm
                WHERE fiscal_year = 2026 AND period_number = 10
                  AND gl_account_name LIKE 'Rent%%' GROUP BY 1, 2""")
rent = {r["gl_account_number"]: r["mapping_status"] for r in cur.fetchall()}
check("rent lines in P10: 7500-11 VERIFIED, other rent accounts not",
      rent.get("7500-11") == "VERIFIED"
      and all(v != "VERIFIED" for a, v in rent.items() if a != "7500-11"), str(rent))
cur.execute("SELECT count(*) n FROM v_r365_financial_transaction_llm WHERE gl_account_number = '7500-01'")
check("account 7500-01 lines are queryable by account number", cur.fetchone()["n"] >= 0)
conn.close()

D2 = C.domains_for_relations
check("transaction views route to r365_financial_detail",
      D2({"v_r365_financial_transaction_llm"}) == {"r365_financial_detail"}
      and D2({"v_r365_financial_unapproved_transaction_llm"}) == {"r365_financial_detail"})
check("lines + summary in one query -> both financial gates",
      D2({"v_r365_financial_transaction_llm", "v_r365_financial_period_llm"})
      == {"r365_financial", "r365_financial_detail"})
check("r365_financial_detail is opt-in and accepted by check_data",
      "r365_financial_detail" not in C.ALL_DOMAINS
      and "r365_financial_detail" in C.CHECK_DATA_TOOL["input_schema"]["properties"]["domains"]["items"]["enum"])
qd = ("SELECT store_name, expense_amount FROM v_r365_financial_transaction_llm "
      "WHERE analytics_category = 'ELECTRICITY' AND fiscal_year = 2026 AND period_number = 10 "
      "AND accounting_date BETWEEN '2026-09-10' AND '2026-10-07'")
try:
    C.enforce_scope(qd, None, None, None)
    check("drill-down without a scope is rejected", False)
except C.ScopeError:
    check("drill-down without a scope is rejected", True)
md = C.enforce_scope(qd, None, *P10)
check("P10 drill-down passes its gate with 0 reconciliation differences",
      md["analysis_permitted"] and md["r365_financial_detail"]["reconciliation"]["differences"] == 0)
m = C.meta_profile(None, "2023-01-01", "2023-02-01", domains=["r365_financial_detail"])
check("drill-down before loaded history is refused",
      not m["analysis_permitted"] and "no GL lines loaded" in m["blocking_reasons"][0])
m = C.meta_profile(None, "2026-01-01", "2026-03-26", domains=["r365_financial_detail"])
check("P1-P3 drill-down warns about MIGRATED_SUMMARY reduced detail",
      any("MIGRATED_SUMMARY" in w for w in m["warnings"]))
g = C._r365_fin_detail_gate({"available": True, "approved_lines_in_scope": 5,
                             "reconciliation": {"differences": 2}})
check("a reconciliation difference BLOCKS the drill-down as incomplete",
      g[0] and "do not reconcile" in g[0][0])
ctx = C.blocked_domain_context({"blocking_reasons": ["R365 FINANCIAL DETAIL lines do not reconcile"],
                                "r365_financial": {"available": True},
                                "r365_financial_detail": {"available": True}},
                               ["r365_financial", "r365_financial_detail"])
check("a detail failure is not attributed to the period summary",
      ctx["r365_financial"]["trusted"] is True)
check("a detail failure marks the detail domain untrusted",
      ctx["r365_financial_detail"]["trusted"] is False)
conn, cur = ro()
for v in TXN:
    cur.execute(f"SELECT * FROM {v} LIMIT 1")
    names = {d.name for d in cur.description}
    check(f"{v} has no hour / time-of-day column",
          not names & {"hour", "local_hour", "accounting_time", "posted_at", "timestamp"})
conn.close()
G2 = C.R365_FINANCIAL_GUIDANCE
for needle, what in [
        ("DRILL-DOWN one row per GL detail line", "two grains named"),
        ("never reconstruct lines from", "no reconstruction from summaries"),
        ("APPROVED BY DEFAULT", "approved default"),
        ("ONLY when the user\n    explicitly asks", "unapproved only on request"),
        ("NOT a\n    VERIFIED <category> figure", "account-level != verified category"),
        ("never infer hourly usage", "no hourly inference"),
        ("MIGRATED_SUMMARY lines", "migrated history fidelity")]:
    check(f"guidance: {what}", " ".join(needle.split()) in " ".join(G2.split()))

# ---------------------------------------------------------------------------
print("13. utility posting semantics (no posting != $0, billing cadence)")
G3 = " ".join(C.R365_FINANCIAL_GUIDANCE.split())
for needle, what in [
        ('"NO POSTING" -- never "$0", "zero expense" or "nothing spent"', "no posting is never $0"),
        ('NOT "missing source data" either', "no posting is not missing source data"),
        ("source_data_status PARTIAL or latest_refresh_failed", "incomplete only when coverage says so"),
        ("Never call it consumption, usage, meter reading", "postings, not consumption"),
        ("13 fiscal periods of 28 days", "13-period vs monthly billing caveat"),
        ("store-level swings can be posting timing", "period comparison timing caveat"),
        ("Its prior bill posted Sep 9, the final day of P9, and the next bill posted "
         "Oct 9 in P11, so P10 fell between two monthly postings", "Shepherd P10 explanation"),
        ("The August bill posted Aug 12 in P8 and the next bill Sep 17 in P10, so the "
         "apparent P9 gap is posting timing rather than evidence of zero usage",
         "Beaumont P9 explanation"),
        ('"<Month> electricity posting not yet present in R365"', "not-yet-present wording"),
        ("never predict its date or amount", "no bill prediction"),
        ('"No identifiable electricity posting was found in R365 for <store>."',
         "no identifiable posting wording"),
        ("bundled into rent, paid by the landlord, included in CAM",
         "no landlord / rent / CAM speculation"),
        ("Do not offer those as possible explanations either", "no speculative alternatives"),
        ('never "$0 (no posting)"', "no '$0 (no posting)' phrasing"),
        ('latest posting Sep 8 -> "October electricity posting not yet present in R365"',
         "Nederland October wording"),
        ('not "late", "overdue" or "missed bill"', "no late/overdue claims"),
        ('not even as an "e.g." of what you cannot say', "no example speculation"),
        ('never "likely" / "very likely" on no evidence', "no unsupported 'likely' timing claims"),
        ("Never apply the timing caveat to a store with NO IDENTIFIABLE POSTING",
         "timing caveat not applied to Downtown"),
        ("do NOT rank on one 28-day fiscal period", "rankings use a longer window"),
        ("then add the posting-timing caveat", "explicit-period ranking keeps the caveat"),
        ("never use them to predict a bill, to estimate an expense for a no-posting period",
         "cadence is descriptive only")]:
    check(f"guidance: {what}", " ".join(needle.split()) in G3, needle[:60])
check("guidance no longer says no-posting stores are 'unknown'",
      "It is unknown, NOT $0" not in G3)

# every SQL pattern in the new section validates, passes the scope gate and runs as laynes_ro
blocks, cur_block = [], None
for line in C.R365_FINANCIAL_GUIDANCE.splitlines():
    if cur_block is None and line.startswith("        ") and \
            not line.startswith("         ") and line.strip().split(" ")[0] in ("SELECT", "WITH"):
        cur_block = [line]
        blocks.append(cur_block)
    elif cur_block is not None and line.startswith("        "):
        cur_block.append(line)
    else:
        cur_block = None
patterns = {"WHERE analytics_category = 'ELECTRICITY' AND establishment_id = 15":
                ("posting dates", 15, "2026-08-13", "2026-10-17"),
            "total_6m": ("6-month ranking", None, "2026-04-01", "2026-10-01"),
            "gap_days": ("cadence", None, "2026-03-26", "2026-10-17")}
found = {}
conn, cur = ro()
for b in blocks:
    sql = "\n".join(b)
    for key, (name, est, ps, pe) in patterns.items():
        if key in sql:
            try:
                C._validate(sql)
                meta = C.enforce_scope(sql, est, ps, pe)
                cur.execute(sql)
                found[name] = cur.fetchall()
                check(f"{name} pattern validates, passes the scope gate and runs",
                      meta and meta["analysis_permitted"], str(meta and meta["blocking_reasons"]))
            except Exception as e:  # noqa: BLE001
                check(f"{name} pattern validates, passes the scope gate and runs", False,
                      f"{type(e).__name__}: {e}")
check("all three new SQL patterns found in guidance", set(found) == {
    "posting dates", "6-month ranking", "cadence"}, str(sorted(found)))

def elec_lines(est, lo, hi):
    cur.execute("SELECT accounting_date, period_number, expense_amount FROM "
                "v_r365_financial_transaction_llm WHERE analytics_category = 'ELECTRICITY' "
                "AND establishment_id = %s AND accounting_date BETWEEN %s AND %s "
                "ORDER BY accounting_date", (est, lo, hi))
    return [(str(r["accounting_date"]), r["period_number"], r["expense_amount"])
            for r in cur.fetchall()]

def period_row(est, pn):
    cur.execute("SELECT has_posting, expense_amount, data_status FROM v_r365_financial_period_llm "
                "WHERE analytics_category = 'ELECTRICITY' AND fiscal_year = 2026 "
                "AND period_number = %s AND establishment_id = %s", (pn, est))
    return cur.fetchone()

# Shepherd (15): P10 sits between the Sep 9 (P9) and Oct 9 (P11) bills
r = period_row(15, 10)
check("Shepherd P10 is a NO_ACTIVITY row with NULL amount (not $0)",
      r and r["has_posting"] is False and r["expense_amount"] is None
      and r["data_status"] == "NO_ACTIVITY", str(r))
check("Shepherd: prior bill Sep 9 in P9, next Oct 9 in P11, nothing in P10",
      elec_lines(15, "2026-09-01", "2026-10-16") ==
      [("2026-09-09", 9, Decimal("2221.32")), ("2026-10-09", 11, Decimal("1955.84"))])
# Beaumont (14): P9 sits between Aug 12 (P8) and Sep 17 (P10)
r = period_row(14, 9)
check("Beaumont P9 is NO POSTING (NULL, not $0)",
      r and r["has_posting"] is False and r["expense_amount"] is None, str(r))
check("Beaumont: Aug 12 in P8, Sep 17 in P10 ($2,238.88), nothing in P9",
      elec_lines(14, "2026-08-01", "2026-10-07") ==
      [("2026-08-12", 8, Decimal("2404.12")), ("2026-09-17", 10, Decimal("2238.88"))])
# Rosenberg (40): same shape as Beaumont
check("Rosenberg: Aug 12 in P8, Sep 21 in P10, nothing in P9",
      elec_lines(40, "2026-08-01", "2026-10-07") ==
      [("2026-08-12", 8, Decimal("2169.87")), ("2026-09-21", 10, Decimal("2174.85"))])
# Nederland (26): as of 2026-10-16 the October posting is not yet present, and
# its own cadence says it normally would be (days since latest > longest gap)
cur.execute("""WITH d AS (SELECT accounting_date FROM v_r365_financial_transaction_llm
                 WHERE analytics_category = 'ELECTRICITY' AND establishment_id = 26
                   AND source_quality = 'TRANSACTION_DETAIL'
                   AND accounting_date BETWEEN '2026-03-26' AND '2026-10-16'
                 GROUP BY accounting_date),
               g AS (SELECT accounting_date, accounting_date - lag(accounting_date)
                        OVER (ORDER BY accounting_date) AS gap FROM d)
               SELECT max(accounting_date) AS latest, max(gap) AS longest,
                      DATE '2026-10-16' - max(accounting_date) AS since FROM g""")
r = cur.fetchone()
check("Nederland: latest posting Sep 8, none since (as of 2026-10-16)",
      str(r["latest"]) == "2026-09-08", str(r))
check("Nederland: days since latest exceeds its longest gap -> 'not yet present' wording applies",
      r["since"] > r["longest"], f"since={r['since']} longest={r['longest']}")
# Downtown Houston (48): no electricity line in the entire loaded history
cur.execute("SELECT count(*) AS n FROM v_r365_financial_transaction_llm "
            "WHERE analytics_category = 'ELECTRICITY' AND establishment_id = 48 "
            "AND accounting_date BETWEEN '2024-04-07' AND '2026-10-16'")
check("Downtown Houston has no identifiable electricity posting in loaded history",
      cur.fetchone()["n"] == 0)
cur.execute("SELECT count(*) AS n FROM v_r365_financial_period_llm WHERE establishment_id = 48 "
            "AND analytics_category = 'ELECTRICITY' AND expense_amount IS NOT NULL")
check("Downtown Houston never shows a $ figure (incl. $0) in the period summary",
      cur.fetchone()["n"] == 0)
cur.execute("SELECT count(*) AS n FROM v_r365_financial_period_llm WHERE "
            "analytics_category = 'ELECTRICITY' AND expense_amount = 0")
check("no electricity period row anywhere is stored as $0", cur.fetchone()["n"] == 0)
rank = {x["store_name"]: x for x in found.get("6-month ranking", [])}
check("6-month ranking excludes Downtown Houston (no posting) rather than ranking it $0",
      "LCF Downtown Houston" not in rank and len(rank) == 11, str(sorted(rank)))
cad = {x["store_name"]: x for x in found.get("cadence", [])}
check("cadence: Shepherd postings roughly monthly (typical 25-35 days)",
      "LCF Shepherd" in cad and 25 <= cad["LCF Shepherd"]["typical_days_between"] <= 35)
conn.close()

m = C.meta_profile(None, *P10, domains=["r365_financial"])
w = " ".join(m["warnings"])
check("P10 gate warning names the no-posting stores as NO POSTING",
      "NO POSTING" in w and "LCF Shepherd" in w and "LCF Downtown Houston" in w, w[:200])
check("P10 gate warning says not $0 and not missing source data",
      "never $0" in w and "not missing source data" in w)
check("P10 gate warning no longer calls them 'unknown'", "These are unknown" not in w)
check("P10 profile marks Downtown Houston as NO IDENTIFIABLE POSTING (data-driven)",
      m["r365_financial"]["no_identifiable_posting"]
      == ["LCF Downtown Houston ELECTRICITY", "LCF Downtown Houston WATER"],
      str(m["r365_financial"].get("no_identifiable_posting")))
check("P10 warning: Downtown gets the no-identifiable-posting sentence, not the timing caveat",
      any("NO IDENTIFIABLE POSTING" in x and "LCF Downtown Houston" in x
          and "do NOT attribute it to billing timing" in x and "LCF Shepherd" not in x
          for x in m["warnings"]))
m9 = C.meta_profile(None, *P9, domains=["r365_financial"])
check("P9: Beaumont/Rosenberg are timing gaps (posted before), only Downtown is unidentified",
      m9["r365_financial"]["no_identifiable_posting"]
      == ["LCF Downtown Houston ELECTRICITY", "LCF Downtown Houston WATER"]
      and "LCF Beaumont P9 ELECTRICITY" in m9["r365_financial"]["no_posting_in_scope"])
note = C.r365_fin_result_note(C.enforce_scope(
    "SELECT store_name, expense_amount FROM v_r365_financial_period_llm WHERE "
    "analytics_category = 'ELECTRICITY' AND period_start = '2026-09-10'", None, *P10))
check("run_sql result note (no check_data needed) lists P10 no-posting stores as NO POSTING",
      note and "NO POSTING (never $0" in note and "LCF Shepherd P10" in note, str(note)[:160])
check("run_sql result note marks Downtown as no identifiable posting, no timing caveat",
      note and "NO IDENTIFIABLE POSTING in the whole loaded GL history: LCF Downtown Houston"
      in note and "do NOT apply the posting-timing caveat" in note)
check("no note when every store has a posting / non-financial scope",
      C.r365_fin_result_note({"r365_financial": {"no_posting_in_scope": []}}) is None
      and C.r365_fin_result_note(None) is None)
g = C._r365_fin_gate({"available": True, "calendar_periods_in_scope": 1,
                      "scope_is_whole_periods": True, "periods_in_scope": [],
                      "no_posting_in_scope": ["X P10 ELECTRICITY"],
                      "no_identifiable_posting": []})
check("a store that has posted before gets no 'no identifiable posting' warning",
      not any("NO IDENTIFIABLE POSTING" in x for x in g[1]))

# ---------------------------------------------------------------------------
print("14. VERIFIED categories beyond electricity (local mappings, migration 60)")
EXPECT = {
    "ELECTRICITY": {"6200-03"},
    "GAS": {"6200-01"},
    "WATER": {"6200-02"},
    "RENT": {"7500-11"},
    "REPAIRS_MAINTENANCE": {"6000-01", "6000-02", "6000-11", "6000-13", "6000-14",
                            "6000-20", "6000-31"},
}
NEW = ["GAS", "WATER", "RENT", "REPAIRS_MAINTENANCE"]
conn, cur = ro()
cur.execute("""SELECT account_number, account_name, analytics_category, mapping_status,
                      mapping_source, source_mapping_status, category_trusted
                 FROM v_r365_financial_account_llm""")
acct = {r["account_number"]: r for r in cur.fetchall()}
verified = {}
for a, r in acct.items():
    if r["mapping_status"] == "VERIFIED":
        verified.setdefault(r["analytics_category"], set()).add(a)
check("VERIFIED account set per category is exactly the approved one (no leakage)",
      verified == EXPECT, str({k: sorted(v) for k, v in verified.items()}))
check("GAS: 6200-01 Gas VERIFIED locally (source still CANDIDATE)",
      acct["6200-01"]["analytics_category"] == "GAS" and acct["6200-01"]["mapping_source"] == "LOCAL"
      and acct["6200-01"]["source_mapping_status"] == "CANDIDATE" and acct["6200-01"]["category_trusted"])
check("WATER: 6200-02 Water VERIFIED locally",
      acct["6200-02"]["analytics_category"] == "WATER" and acct["6200-02"]["mapping_source"] == "LOCAL")
check("ELECTRICITY still mapped by the SOURCE, unchanged",
      acct["6200-03"]["mapping_source"] == "SOURCE" and acct["6200-03"]["source_mapping_status"] == "VERIFIED")
check("RENT: only 7500-11 Rent - Restaurant; storage / equipment / empty rent accounts CANDIDATE",
      all(acct[a]["mapping_status"] == "CANDIDATE" for a in ("7500-01", "7500-03", "7500-09", "7500-13"))
      and acct["7500-02"]["mapping_status"] == "UNMAPPED")
check("RENT excludes CAM-like / tax / insurance / management-fee accounts",
      all(acct[a]["mapping_status"] != "VERIFIED"
          for a in ("7500-04", "7500-05", "7500-07", "7500-08", "7500-10", "7500-12", "94050",
                    "8900-01", "8500-06")))
check("REPAIRS_MAINTENANCE: R&M children only; 6000-09 (never posted) left CANDIDATE",
      acct["6000-09"]["mapping_status"] == "CANDIDATE"
      and all(acct[a]["mapping_status"] != "VERIFIED"
              for a in ("6100-03", "6100-04", "6100-06", "6175")))
check("candidate utilities (telephone, cable, waste) stay CANDIDATE",
      all(acct[a]["mapping_status"] == "CANDIDATE" for a in ("6200-04", "6200-05", "6200-06")))
check("parent accounts 6150 / 6200 / 7500 are never mapped (no parent/child double count)",
      all(acct[a]["mapping_status"] == "UNMAPPED" for a in ("6150", "6200", "7500")))

for cat in NEW:
    accts = sorted(EXPECT[cat])
    cur.execute("""SELECT count(*) FILTER (WHERE analytics_category = %(c)s) AS in_cat,
                          count(*) FILTER (WHERE gl_account_number = ANY(%(a)s)) AS on_accounts,
                          count(*) FILTER (WHERE analytics_category = %(c)s
                                             AND NOT gl_account_number = ANY(%(a)s)) AS stray,
                          count(*) FILTER (WHERE gl_account_number IN ('6150', '6200', '7500'))
                                                                  AS on_parents
                     FROM v_r365_financial_transaction_llm""", {"c": cat, "a": accts})
    r = cur.fetchone()
    check(f"{cat}: every line of its accounts is in the category exactly once, nothing else",
          r["in_cat"] == r["on_accounts"] > 0 and r["stray"] == 0 and r["on_parents"] == 0, str(dict(r)))
cur.execute("""SELECT count(*) n FROM v_r365_financial_transaction_llm
                WHERE gl_account_number IN ('7500-09', '7500-13', '6200-04', '6200-05', '6200-06',
                                            '6100-03', '6100-06', '6175')
                  AND analytics_category IS NOT NULL""")
check("no category leakage from CANDIDATE / excluded accounts", cur.fetchone()["n"] == 0)

FACTS = {("GAS", 9): ("7161.56", 11), ("GAS", 10): ("4864.82", 7),
         ("WATER", 9): ("3802.84", 8), ("WATER", 10): ("7980.10", 8),
         ("RENT", 9): ("186282.43", 12), ("RENT", 10): ("162090.83", 10),
         ("REPAIRS_MAINTENANCE", 9): ("29488.42", 12), ("REPAIRS_MAINTENANCE", 10): ("33862.08", 12)}
for (cat, pn), (total, n) in FACTS.items():
    cur.execute("""SELECT sum(expense_amount) t, count(expense_amount) n, count(*) stores
                     FROM v_r365_financial_period_llm
                    WHERE analytics_category = %s AND fiscal_year = 2026 AND period_number = %s""",
                (cat, pn))
    r = cur.fetchone()
    check(f"{cat} P{pn} = ${total} across {n} of 12 stores",
          r["t"] == Decimal(total) and r["n"] == n and r["stores"] == 12, str(dict(r)))
    cur.execute("""WITH t AS (SELECT establishment_id, sum(expense_amount) s
                                FROM v_r365_financial_transaction_llm
                               WHERE analytics_category = %(c)s AND fiscal_year = 2026
                                 AND period_number = %(p)s GROUP BY 1),
                        p AS (SELECT establishment_id, expense_amount s
                                FROM v_r365_financial_period_llm
                               WHERE analytics_category = %(c)s AND fiscal_year = 2026
                                 AND period_number = %(p)s AND has_posting)
                   SELECT count(*) pairs, count(*) FILTER (WHERE t.s IS DISTINCT FROM p.s) diffs,
                          (SELECT sum(s) FROM t) - (SELECT sum(s) FROM p) AS network_diff
                     FROM p FULL JOIN t USING (establishment_id)""", {"c": cat, "p": pn})
    r = cur.fetchone()
    check(f"{cat} P{pn}: Approved lines reconcile per store and network (difference 0)",
          r["diffs"] == 0 and r["network_diff"] == 0 and r["pairs"] == n, str(dict(r)))

cur.execute("""SELECT analytics_category, count(*) FILTER (WHERE NOT has_posting) no_post,
                      count(*) FILTER (WHERE NOT has_posting AND (expense_amount IS NOT NULL
                                       OR data_status <> 'NO_ACTIVITY')) bad
                 FROM v_r365_financial_period_llm GROUP BY 1""")
np = {r["analytics_category"]: r for r in cur.fetchall()}
check("missing posting != zero: every no-posting row of every category is NULL / NO_ACTIVITY",
      all(np[c]["bad"] == 0 and np[c]["no_post"] > 0 for c in NEW), str(np))
cur.execute("""SELECT has_posting, expense_amount, data_status FROM v_r365_financial_period_llm
                WHERE analytics_category = 'RENT' AND establishment_id = 20
                  AND fiscal_year = 2026 AND period_number = 10""")
r = cur.fetchone()
check("Pasadena P10 RENT is NO POSTING (its Oct rent is Unapproved), not $0",
      r["has_posting"] is False and r["expense_amount"] is None and r["data_status"] == "NO_ACTIVITY")
cur.execute("""SELECT count(*) n, sum(expense_amount) s FROM v_r365_financial_unapproved_transaction_llm
                WHERE analytics_category = 'RENT' AND establishment_id = 20
                  AND accounting_date BETWEEN '2026-09-10' AND '2026-10-07'""")
r = cur.fetchone()
check("Unapproved excluded by default, visible only in the unapproved view (Pasadena $12,029.10)",
      r["n"] == 1 and r["s"] == Decimal("12029.1"), str(dict(r)))
cur.execute("""SELECT count(*) n FROM v_r365_financial_transaction_llm
                WHERE transaction_status <> 'Approved' AND analytics_category IS NOT NULL""")
check("Approved default: no Unapproved line on any category in the main view", cur.fetchone()["n"] == 0)
cur.execute("""SELECT period_label, analytics_category, source_data_status FROM v_r365_financial_coverage_llm
                WHERE fiscal_year = 2026 AND period_number IN (9, 10, 11)""")
cov = {(r["period_label"], r["analytics_category"]): r["source_data_status"] for r in cur.fetchall()}
check("safe-view exposure: coverage lists the new categories, P9/P10 COMPLETE, P11 PARTIAL",
      all(cov.get(("P9", c)) == cov.get(("P10", c)) == "COMPLETE" and cov.get(("P11", c)) == "PARTIAL"
          for c in NEW), str(cov))
cur.execute("""SELECT source_quality, count(*) n FROM v_r365_financial_period_llm
                WHERE analytics_category = 'REPAIRS_MAINTENANCE' AND has_posting
                  AND fiscal_year = 2026 AND period_number BETWEEN 4 AND 10 GROUP BY 1""")
check("R&M P4-P10 come from native transaction detail only",
      {r["source_quality"] for r in cur.fetchall()} == {"TRANSACTION_DETAIL"})
conn.close()

# drill-down through the real scope gate, as laynes_ro
for cat, sql in [
        ("GAS", "SELECT store_name, accounting_date, expense_amount FROM v_r365_financial_transaction_llm "
                "WHERE analytics_category = 'GAS' AND fiscal_year = 2026 AND period_number = 10 "
                "AND accounting_date BETWEEN '2026-09-10' AND '2026-10-07'"),
        ("REPAIRS_MAINTENANCE",
         "SELECT gl_account_number, sum(expense_amount) FROM v_r365_financial_transaction_llm "
         "WHERE analytics_category = 'REPAIRS_MAINTENANCE' AND establishment_id = 36 "
         "AND accounting_date BETWEEN '2026-09-10' AND '2026-10-07' GROUP BY 1")]:
    est = 36 if "establishment_id = 36" in sql else None
    gm = C.enforce_scope(sql, est, *P10)
    d = gm.get("r365_financial_detail") or {}
    check(f"{cat} drill-down passes the scope gate and reconciles (0 differences)",
          d.get("available") and (d.get("reconciliation") or {}).get("differences") == 0, str(d)[:200])

note = C.r365_fin_result_note(C.enforce_scope(
    "SELECT store_name, expense_amount FROM v_r365_financial_period_llm WHERE "
    "analytics_category = 'WATER' AND period_start = '2026-09-10'", None, *P10),
    "SELECT store_name FROM v_r365_financial_period_llm WHERE analytics_category = 'WATER'")
check("run_sql note for a WATER query lists only WATER no-posting stores",
      note and "WATER" in note and "ELECTRICITY" not in note and "GAS" not in note, str(note)[:200])
check("run_sql note gives Downtown the water-specific no-identifiable sentence",
      note and "No identifiable water posting was found in R365 for LCF Downtown Houston." in note)
G = C.R365_FINANCIAL_GUIDANCE
for needle, what in [
        ("RENT                 7500-11 Rent - Restaurant ONLY", "RENT = 7500-11 only"),
        ("NOT CAM, property\n                       tax, insurance or management fees", "rent excludes CAM/tax/insurance"),
        ("never guess what it is (no CAM", "no speculation on secondary rent lines"),
        ("no\nrepairs & maintenance posting in P10", "R&M no-posting wording"),
        ("never \"$0\" or \"nothing was repaired\"", "R&M missing is not zero"),
        ("Never\nbuild a category from account names yourself", "no fuzzy account guessing"),
        ("NOT vehicle fuel", "gas is not fuel"),
        ("never add them to a VERIFIED category", "candidates never folded in")]:
    check(f"guidance: {what}", needle in G)

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
