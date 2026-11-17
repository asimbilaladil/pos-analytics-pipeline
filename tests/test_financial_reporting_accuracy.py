#!/usr/bin/env python3
"""Financial reporting accuracy (2026-11 P0 audit of conversation 688).

Run:  venv/bin/python tests/test_financial_reporting_accuracy.py   (exit 0 = pass)

Conversation 688, "del periodo 11 quienes cumplieron los goles": the answer
put full-period R365 net sales (28 days) beside a forecast and R365 variance
that cover only the 21 forecast days of Airtex, Beaumont and Ella, so
sales - forecast != difference, and called it a full-period comparison. Every
figure was correct on its own. The Revel follow-up called a tax-inclusive total
"sales" and adopted the closest of several figures as the user's "gross".

Pinned here:
  A. financial_guard (pure): the conv-688 table is caught row by row; a
     corrected table passes; coverage disclosure and full-period claims;
     number parsing, rounding tolerance, percent checks; notes in ES/EN.
  B. migration 62 run as session-only TEMP views (production objects are not
     touched): forecast coverage for all 12 stores in P11, R365's variance and
     every existing column unchanged, fiscal-period arithmetic, weekly coverage.
  C. live forecast coverage through the read-only role (pre-migration path).
  D. Revel definitions: gross / discounts / comps / net excl. tax / tax / total
     incl. tax / payments for the audited Cypress week, and network invariants.
  E. conversation 688 replayed with a mocked model: one deterministic rewrite,
     then the corrected answer; a model that repeats the error gets the
     server-computed coverage note appended; non-forecast answers untouched.
  F. allowlist, trust gate, R365 meta coverage, prompt rules, the +/-0.5%
     reconciliation tolerance, store-scoped credentials.
Model calls are faked (no API call). Database access is read-only, except the
TEMP views of section B, which exist only inside one rolled-back session.
"""
import json
import os
import re
import sys
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
os.chdir(os.path.join(HERE, ".."))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(".env", override=True)
import psycopg2.extras  # noqa: E402

import chat_sql as C  # noqa: E402
import financial_guard as G  # noqa: E402
import migration_sandbox  # noqa: E402
import store_scope  # noqa: E402
from backend.db import rw_conn  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


def section(t):
    print(f"\n{t}")


P11 = ("2026-10-08", "2026-11-05")       # fiscal period 11 FY2026, end exclusive

# The P11 answer of conversation 688 (message 1076), tables and the closing
# sentence verbatim.
CONV688_ANSWER = """**Período Fiscal P11 FY2026 (2026-10-08 → 2026-11-04, 28 días, período COMPLETO)**

**Cumplieron o superaron el forecast de ventas (sales_vs_forecast_amount ≥ 0) — 5 tiendas:**

| Tienda | Ventas reales | Forecast | Diferencia $ | Diferencia % |
|---|---|---|---|---|
| **Mission Bend** | $145,005.34 | $117,999.03 | +$27,006.31 | **+22.89%** |
| **Airtex** | $236,892.94 | $163,258.04 | +$11,876.89 | +7.27% |
| **Beaumont** | $103,710.17 | $74,882.00 | +$2,790.63 | +3.73% |
| **Katy** | $122,291.85 | $121,764.00 | +$527.85 | +0.43% |
| **Pasadena** | $146,167.75 | $145,799.78 | +$367.97 | +0.25% |

**No cumplieron el forecast (missed, monto negativo) — 6 tiendas:**

| Tienda | Diferencia $ | Diferencia % |
|---|---|---|
| Missouri City | -$334.14 | -0.22% |
| Rosenberg | -$2,572.93 | -0.93% |
| Ella | -$3,300.19 | -2.06% |
| Nederland | -$2,002.85 | -2.62% |
| Cypress | -$7,936.43 | -3.03% |
| **Shepherd** | **-$12,720.81** | **-5.44%** (la mayor caída) |

**Sin forecast cargado — 1 tienda:**
- **Downtown Houston** — no tiene forecast ingresado en R365 para este período (no cuenta como "cumplió" ni "no cumplió").

Todas las 12 tiendas tienen las 4 semanas del período cargadas (período totalmente cubierto), así que la comparación es sobre el total completo del período, no parcial."""

CORRECTED_ANSWER = """**P11 FY2026 (2026-10-08 → 2026-11-04) — ventas vs forecast en días con forecast**

| Tienda | Días con forecast | Ventas en días con forecast | Forecast | Diferencia $ | Diferencia % | Ventas del período completo (aparte) |
|---|---|---|---|---|---|---|
| Mission Bend | 28 de 28 | $145,005.34 | $117,999.03 | +$27,006.31 | +22.89% | $145,005.34 |
| Airtex | 21 de 28 | $175,134.93 | $163,258.04 | +$11,876.89 | +7.27% | $236,892.94 |
| Beaumont | 21 de 28 | $77,672.63 | $74,882.00 | +$2,790.63 | +3.73% | $103,710.17 |
| Ella | 21 de 28 | $156,913.82 | $160,214.01 | -$3,300.19 | -2.06% | $214,085.92 |
| Shepherd | 28 de 28 | $221,144.20 | $233,865.01 | -$12,720.81 | -5.44% | $221,144.20 |

Airtex y Ella no tienen forecast del 2026-10-29 al 2026-11-04 y Beaumont del 2026-10-15 al 2026-10-21: para ellas la comparación cubre solo 21 de 28 días, no el período completo. Downtown Houston: sin forecast."""

P11_SQL = ("SELECT store_name, net_sales, forecasted_sales, forecast_sales_base,\n"
           "       sales_vs_forecast_amount, sales_vs_forecast_pct, weeks_loaded, weeks_in_period, "
           "period_fully_covered\nFROM v_r365_store_period_llm\n"
           "WHERE fiscal_year = 2026 AND period_number = 11 AND start_date = '2026-10-08'\n"
           "ORDER BY sales_vs_forecast_pct DESC NULLS LAST")

# Live P11 truth (re-derived from the daily rows in the 2026-11-16 audit).
P11_TRUTH = {
    "LCF Airtex": (21, "236892.94", "175134.93", "163258.04", "11876.89"),
    "LCF Beaumont": (21, "103710.17", "77672.63", "74882.00", "2790.63"),
    "LCF Ella": (21, "214085.92", "156913.82", "160214.01", "-3300.19"),
    "LCF Cypress": (28, "254263.54", "254263.54", "262199.97", "-7936.43"),
    "LCF Katy": (28, "122291.85", "122291.85", "121764.00", "527.85"),
    "LCF Mission Bend": (28, "145005.34", "145005.34", "117999.03", "27006.31"),
    "LCF Missouri City": (28, "152402.95", "152402.95", "152737.09", "-334.14"),
    "LCF Nederland": (28, "74470.15", "74470.15", "76473.00", "-2002.85"),
    "LCF Pasadena": (28, "146167.75", "146167.75", "145799.78", "367.97"),
    "LCF Rosenberg": (28, "274616.09", "274616.09", "277189.02", "-2572.93"),
    "LCF Shepherd": (28, "221144.20", "221144.20", "233865.01", "-12720.81"),
    "LCF Downtown Houston": (0, "103897.10", None, None, None),
}


def money(v):
    return None if v is None else Decimal(str(v)).quantize(Decimal("0.01"))


# ═══ A. financial_guard, pure ═══════════════════════════════════════════════
section("A. financial_guard (pure)")
partial_cov = [
    {"store_name": n, "establishment_id": i, "forecast_days": 21, "days_in_scope": 28,
     "coverage_status": "PARTIAL", "days_without_forecast": [(date(2026, 10, 29), date(2026, 11, 4))],
     "sales_on_forecast_days": Decimal("1"), "forecasted_sales": Decimal("1"),
     "sales_vs_forecast_amount": Decimal("0"), "net_sales": Decimal("2")}
    for n, i in (("LCF Airtex", 32), ("LCF Beaumont", 14), ("LCF Ella", 7))]
iss = G.review(CONV688_ANSWER, partial_cov)
arith = {i.row for i in iss if i.kind == "ARITHMETIC"}
check("conv 688: Airtex and Beaumont rows do not add up", arith == {"Airtex", "Beaumont"}, str(arith))
check("conv 688: the Mission Bend / Katy / Pasadena rows add up (not flagged)",
      not arith & {"Mission Bend", "Katy", "Pasadena"})
check("conv 688: Airtex detail shows the 73,634.90 the row implies",
      any("73,634.90" in i.detail for i in iss if i.row == "Airtex"))
check("conv 688: coverage undisclosed for all three PARTIAL stores",
      {i.row for i in iss if i.kind == "COVERAGE_UNDISCLOSED"}
      == {"LCF Airtex", "LCF Beaumont", "LCF Ella"})
check("conv 688: the 'total completo del período, no parcial' sentence is a full-period claim",
      any(i.kind == "FULL_PERIOD_CLAIM" for i in iss))
check("conv 688: percent column consistent (no PERCENT issue)",
      not [i for i in iss if i.kind == "PERCENT"])
check("corrected answer passes every check", G.review(CORRECTED_ANSWER, partial_cov) == [],
      str([(i.kind, i.row) for i in G.review(CORRECTED_ANSWER, partial_cov)]))
check("no coverage facts -> arithmetic still checked", {i.row for i in G.review(CONV688_ANSWER, [])
                                                        if i.kind == "ARITHMETIC"} == {"Airtex", "Beaumont"})

c = G.parse_cell
check("parse: '+$11,876.89'", c("+$11,876.89").value == Decimal("11876.89"))
check("parse: '−$12,720.81' (unicode minus) and '**-$1.00**'",
      c("−$12,720.81").value == Decimal("-12720.81") and c("**-$1.00**").value == Decimal("-1.00"))
check("parse: '($1,234)' negative, '+7.27%' percent",
      c("($1,234)").value == Decimal("-1234") and c("+7.27%").is_pct)
check("parse: ranges, dates and text are not numbers",
      c("2026-10-08").value is None and c("21 de 28").value is None and c("n/a").value is None)

tbl = "| Store | Sales | Forecast | Difference | Diff % |\n|---|---|---|---|---|\n"
check("whole-dollar rounding tolerated",
      G.check_tables(tbl + "| A | $1,001 | $500 | +$500 | +100.20% |") == [])
check("cent-level mismatch caught",
      G.check_tables(tbl + "| A | $1,000.00 | $500.00 | +$500.05 | +100.01% |")[0].kind == "ARITHMETIC")
check("wrong percent caught",
      [i.kind for i in G.check_tables(tbl + "| A | $1,000.00 | $500.00 | +$500.00 | +50.00% |")] == ["PERCENT"])
check("table without forecast column ignored (labor table)",
      G.check_tables("| Store | Net sales | Labor cost | Labor % |\n|---|---|---|---|\n| A | $100.00 | $20.00 | 25.00% |") == [])
check("'sales on forecast days' header is the comparison column",
      G.check_tables("| Store | Full-period sales | Sales on forecast days | Forecast | Variance |\n"
                     "|---|---|---|---|---|\n| A | $236,892.94 | $175,134.93 | $163,258.04 | +$11,876.89 |") == [])
check("claim hedged as partial is not a claim",
      not [i for i in G.check_coverage("The comparison covers only 21 of 28 days, not the full period.",
                                       partial_cov[:1]) if i.kind == "FULL_PERIOD_CLAIM"])
check("'Full-period sales' as a column header is not a claim",
      not [i for i in G.check_coverage("| S | Full-period sales |\n|---|---|\n| A | 1 |\n21 of 28 days.",
                                       partial_cov[:1]) if i.kind == "FULL_PERIOD_CLAIM"])
check("disclosure formats 21/28, 21 of 28, 21 de 28",
      all(G._disclosed(t, 21, 28) for t in ("21/28", "21 of 28 days", "21 de 28 días"))
      and not G._disclosed("21 days", 21, 28))
check("needs_forecast_check: R365 store view + forecast column only",
      G.needs_forecast_check(P11_SQL)
      and not G.needs_forecast_check("SELECT net_sales, labor_pct FROM v_r365_store_period_llm")
      and not G.needs_forecast_check("SELECT forecasted_sales FROM v_orders_classified"))
check("ranges() collapses consecutive dates",
      G.ranges([date(2026, 10, 29), date(2026, 10, 30), date(2026, 11, 2)])
      == [(date(2026, 10, 29), date(2026, 10, 30)), (date(2026, 11, 2), date(2026, 11, 2))])
note_es = G.final_note(G.review(CONV688_ANSWER, partial_cov), partial_cov, CONV688_ANSWER)
check("final note in Spanish for a Spanish answer, states 21 de 28 and not full period",
      "21 de 28" in note_es and "no es una comparación del período completo" in note_es
      and "pero la tabla muestra" in note_es)
note_en = G.final_note([], partial_cov, "Here are the stores that met forecast.")
check("final note in English otherwise", "21 of 28" in note_en and "not a full-period" in note_en)
check("correction prompt names the rule and the facts",
      "SALES ON FORECAST DAYS" in G.correction_prompt(iss, partial_cov)
      and "LCF Airtex" in G.correction_prompt(iss, partial_cov))

# ═══ B. migration 62 as session-only TEMP views ═════════════════════════════
section("B. migration 62 (session TEMP views; production objects untouched)")
MIG = open("migrations/62_financial_reporting_accuracy.sql").read()
conn = rw_conn()
try:
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    names = migration_sandbox.install(cur, MIG)
    check("migration 62 defines the two R365 views and the Revel metrics view",
          names == ["v_r365_store_weekly_llm", "v_r365_store_period_llm",
                    "v_revel_sales_metrics_daily_llm"], str(names))
    for v in ("v_r365_store_weekly_llm", "v_r365_store_period_llm"):
        cur.execute(f"SELECT * FROM public.{v} LIMIT 0")
        old_cols = [d.name for d in cur.description]
        cur.execute(f"SELECT * FROM {v} LIMIT 0")
        new_cols = [d.name for d in cur.description]
        check(f"{v}: existing columns unchanged and in order; new ones appended",
              new_cols[:len(old_cols)] == old_cols and new_cols[len(old_cols):] == [
                  "forecast_days", "days_in_week" if "weekly" in v else "days_in_period",
                  "forecast_coverage_status", "sales_on_forecast_days",
                  "sales_on_days_without_forecast"], str(new_cols[len(old_cols):]))
        keys = ("establishment_id, week_start" if "weekly" in v
                else "establishment_id, fiscal_year, period_number")
        cur.execute(f"""SELECT count(*) AS n FROM (
            (SELECT {', '.join(old_cols)} FROM public.{v} EXCEPT SELECT {', '.join(old_cols)} FROM {v})
            UNION ALL
            (SELECT {', '.join(old_cols)} FROM {v} EXCEPT SELECT {', '.join(old_cols)} FROM public.{v})) x""")
        check(f"{v}: every existing value identical to production (incl. R365 variance)",
              cur.fetchone()["n"] == 0)

    cur.execute("""SELECT store_name, net_sales, forecast_days, days_in_period, forecast_coverage_status,
                          sales_on_forecast_days, forecasted_sales, sales_vs_forecast_amount,
                          sales_on_days_without_forecast, period_fully_covered
                     FROM v_r365_store_period_llm WHERE fiscal_year = 2026 AND period_number = 11""")
    p11 = {r["store_name"]: r for r in cur.fetchall()}
    check("P11: all 12 stores present", set(p11) == set(P11_TRUTH), str(sorted(p11)))
    for store, (fd, net, onfc, fc, var) in sorted(P11_TRUTH.items()):
        r = p11.get(store) or {}
        want_status = "NONE" if fd == 0 else ("FULL" if fd == 28 else "PARTIAL")
        ok = (r.get("forecast_days") == fd and r.get("days_in_period") == 28
              and r.get("forecast_coverage_status") == want_status
              and money(r.get("net_sales")) == money(net)
              and money(r.get("sales_on_forecast_days")) == money(onfc)
              and money(r.get("forecasted_sales")) == money(fc)
              and money(r.get("sales_vs_forecast_amount")) == money(var))
        check(f"P11 {store}: {want_status} {fd}/28, figures exact", ok,
              "" if ok else str({k: str(v) for k, v in r.items()}))
    check("P11: period_fully_covered stays TRUE for the PARTIAL stores (it is week coverage)",
          all(p11[s]["period_fully_covered"] for s in ("LCF Airtex", "LCF Beaumont", "LCF Ella")))
    check("P11 arithmetic: sales_on_forecast_days - forecast = variance; + days without = net",
          all(money(r["sales_on_forecast_days"] - r["forecasted_sales"]) == money(r["sales_vs_forecast_amount"])
              and money(r["sales_on_forecast_days"] + r["sales_on_days_without_forecast"]) == money(r["net_sales"])
              for r in p11.values() if r["forecasted_sales"] is not None))
    cur.execute("""SELECT round(sum(sales_on_forecast_days) - sum(forecasted_sales), 2) AS v,
                          round(sum(net_sales) - sum(forecasted_sales), 2) AS naive
                     FROM v_r365_store_period_llm WHERE fiscal_year = 2026 AND period_number = 11""")
    r = cur.fetchone()
    check("P11 network: forecast-day variance +13,702.30 (not the naive +262,567.05)",
          r["v"] == Decimal("13702.30") and r["naive"] == Decimal("262567.05"), str(r))

    cur.execute("""WITH w AS (SELECT establishment_id, fiscal_year, period_number,
                                     sum(forecast_days) fd, sum(sales_on_forecast_days) sf,
                                     sum(sales_on_days_without_forecast) sw
                                FROM v_r365_store_weekly_llm WHERE fiscal_year IS NOT NULL GROUP BY 1, 2, 3)
                   SELECT count(*) n, count(*) FILTER (WHERE p.forecast_days <> w.fd
                          OR p.sales_on_forecast_days IS DISTINCT FROM w.sf
                          OR abs(p.sales_on_days_without_forecast - w.sw) > 0.005) bad
                     FROM v_r365_store_period_llm p JOIN w USING (establishment_id, fiscal_year, period_number)""")
    r = cur.fetchone()
    check("every store-period: coverage columns = sum of its weeks", r["n"] >= 48 and r["bad"] == 0, str(r))
    cur.execute("""WITH d AS (SELECT establishment_id, week_start, count(forecasted_sales) fd
                                FROM v_r365_store_daily_llm GROUP BY 1, 2)
                   SELECT count(*) n, count(*) FILTER (WHERE w.forecast_days <> d.fd) bad
                     FROM v_r365_store_weekly_llm w JOIN d USING (establishment_id, week_start)""")
    r = cur.fetchone()
    check("every store-week: forecast_days = forecast-bearing daily rows", r["n"] >= 156 and r["bad"] == 0, str(r))
    cur.execute("""SELECT store_name, forecast_days FROM v_r365_store_weekly_llm
                    WHERE forecast_coverage_status = 'PARTIAL' ORDER BY 1""")
    pw = [(r["store_name"], r["forecast_days"]) for r in cur.fetchall()]
    check("weekly PARTIAL rows are the known 5-of-7 weeks of 2026-08-27",
          pw == [("LCF Cypress", 5), ("LCF Ella", 5), ("LCF Missouri City", 5)], str(pw))

    # ═══ D. Revel definitions (same TEMP session) ═══════════════════════════
    section("D. Revel sales definitions")
    cur.execute("""SELECT sum(gross_sales_before_discounts) g, sum(discounts) d, sum(comps) c,
                          sum(net_sales_excluding_tax) n, sum(tax) t, sum(total_including_tax) tot,
                          sum(payments_received) p, sum(identity_residual) r, sum(real_transactions) rt,
                          sum(comp_tickets) ct
                     FROM v_revel_sales_metrics_daily_llm
                    WHERE establishment_id = 54 AND business_date >= '2026-11-05' AND business_date < '2026-11-12'""")
    r = cur.fetchone()
    check("Cypress 11/05-11/11: gross 63,447.58 / disc 1,664.28 / comps 1,364.67",
          (money(r["g"]), money(r["d"]), money(r["c"])) == (money("63447.58"), money("1664.28"), money("1364.67")))
    check("Cypress: net excl. tax 60,418.63 / tax 4,637.89 / total incl. tax 65,056.52",
          (money(r["n"]), money(r["t"]), money(r["tot"])) == (money("60418.63"), money("4637.89"), money("65056.52")))
    check("Cypress: payments received 65,056.55; residual 0.00; 3,023 REAL / 144 COMP",
          money(r["p"]) == money("65056.55") and money(r["r"]) == money("0") and r["rt"] == 3023 and r["ct"] == 144)
    check("Cypress: no definition equals the unverified Revel UI figure 63,482.60",
          money("63482.60") not in {money(r[k]) for k in ("g", "n", "tot", "p")})
    cur.execute("""SELECT count(*) n,
                          count(*) FILTER (WHERE m.total_including_tax <> COALESCE(v.real_final, 0)) bad_total,
                          count(*) FILTER (WHERE abs(m.gross_sales_before_discounts - m.discounts - m.comps
                                                     - m.net_sales_excluding_tax) > 0.001) bad_net,
                          count(*) FILTER (WHERE m.payments_received IS DISTINCT FROM p.payment_amount) bad_pay,
                          sum(abs(m.identity_residual)) resid
                     FROM v_revel_sales_metrics_daily_llm m
                     JOIN (SELECT establishment_id, business_date,
                                  sum(final_total) FILTER (WHERE txn_class = 'REAL') real_final
                             FROM v_orders_classified
                            WHERE business_date >= '2026-10-15' AND business_date < '2026-11-15' GROUP BY 1, 2) v
                       USING (establishment_id, business_date)
                     LEFT JOIN v_payments_daily_v2 p USING (establishment_id, business_date)
                    WHERE m.business_date >= '2026-10-15' AND m.business_date < '2026-11-15'""")
    r = cur.fetchone()
    check("network 30 days: total_including_tax = legacy REAL final_total on every store-day",
          r["n"] >= 300 and r["bad_total"] == 0, str(r))
    check("network 30 days: net = gross - discounts - comps; payments = v_payments_daily_v2",
          r["bad_net"] == 0 and r["bad_pay"] == 0, str(r))
    check("network 30 days: identity residual small and visible (< $200 total)",
          Decimal(r["resid"]) < 200, str(r["resid"]))
    # MCP's inline SQL is the same definition as the view.
    sys.path.insert(0, ".")
    from mcp_server import analytics as MA  # noqa: E402
    cur.execute(MA._REVEL_SQL, {"ids": [54, 15], "start": date(2026, 11, 5), "end": date(2026, 11, 12)})
    inline = {(x["establishment_id"], x["business_date"]): MA.revel_metrics(x, None) for x in cur.fetchall()}
    cur.execute("""SELECT * FROM v_revel_sales_metrics_daily_llm WHERE establishment_id IN (54, 15)
                    AND business_date >= '2026-11-05' AND business_date < '2026-11-12'""")
    view = {(x["establishment_id"], x["business_date"]): x for x in cur.fetchall()}
    check("MCP inline metrics == v_revel_sales_metrics_daily_llm (Cypress + Shepherd week)",
          set(inline) == set(view) and all(
              all(abs(Decimal(str(inline[k][f])) - money(view[k][f])) < Decimal("0.01")
                  for f in ("gross_sales_before_discounts", "discounts", "comps",
                            "net_sales_excluding_tax", "tax", "total_including_tax"))
              for k in view))
finally:
    conn.rollback()
    conn.close()

# ═══ C. live forecast coverage, read-only role ══════════════════════════════
section("C. forecast coverage through laynes_ro (works before migration 62)")
ro = C._ro_conn()
try:
    cur = ro.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cov = {c["store_name"]: c for c in G.forecast_coverage(cur, None, *P11)}
finally:
    ro.rollback()
    ro.close()
check("P11 coverage: 12 stores", len(cov) == 12)
check("P11 coverage: Airtex/Beaumont/Ella PARTIAL 21/28, Downtown NONE, 8 FULL",
      {s for s, c in cov.items() if c["coverage_status"] == "PARTIAL"} == {"LCF Airtex", "LCF Beaumont", "LCF Ella"}
      and cov["LCF Downtown Houston"]["coverage_status"] == "NONE"
      and sum(c["coverage_status"] == "FULL" for c in cov.values()) == 8
      and all(cov[s]["forecast_days"] == 21 for s in ("LCF Airtex", "LCF Beaumont", "LCF Ella")))
ro = C._ro_conn()
try:
    cur = ro.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cov12 = G.forecast_coverage(cur, None, "2026-11-05", "2026-12-03")
finally:
    ro.rollback()
    ro.close()
check("current period (P12, partly loaded): unloaded days are not 'no forecast'",
      cov12 and all(c["days_requested"] == 28 and c["days_not_loaded"] == 28 - c["days_in_scope"]
                    and c["coverage_status"] != "PARTIAL" for c in cov12)
      and not [i for i in G.review("| Store | Sales | Forecast | Difference |\n|---|---|---|---|\n"
                                   "| A | $10.00 | $5.00 | +$5.00 |", cov12) if i.kind == "COVERAGE_UNDISCLOSED"],
      str({c["store_name"]: (c["coverage_status"], c["forecast_days"], c["days_in_scope"]) for c in cov12}))
check("P11 coverage: the exact uncovered weeks",
      cov["LCF Airtex"]["days_without_forecast"] == [(date(2026, 10, 29), date(2026, 11, 4))]
      and cov["LCF Ella"]["days_without_forecast"] == [(date(2026, 10, 29), date(2026, 11, 4))]
      and cov["LCF Beaumont"]["days_without_forecast"] == [(date(2026, 10, 15), date(2026, 10, 21))])
check("P11 coverage: figures equal R365's own variance for every store",
      all(money(c["sales_vs_forecast_amount"]) == money(P11_TRUTH[s][4])
          and money(c["net_sales"]) == money(P11_TRUTH[s][1]) for s, c in cov.items()))
note = G.final_note(G.review(CONV688_ANSWER, list(cov.values())), list(cov.values()), CONV688_ANSWER)
check("live note for conv 688 carries 175,134.93 vs 163,258.04 = +11,876.89 and 236,892.94 apart",
      "$175,134.93 vs forecast $163,258.04 = +$11,876.89" in note and "$236,892.94" in note)

# ═══ E. conversation 688 replayed with a mocked model ══════════════════════
section("E. conversation 688 replay (mocked model)")


def text(t):
    return SimpleNamespace(type="text", text=t)


def resp(stop, *blocks):
    return SimpleNamespace(stop_reason=stop, content=list(blocks),
                           usage=SimpleNamespace(input_tokens=1, output_tokens=1))


_n = [0]


def tool(name, **inp):
    _n[0] += 1
    return SimpleNamespace(type="tool_use", id=f"t{_n[0]}", name=name, input=inp)


class FakeClient:
    def __init__(self, script):
        self.script, self.calls = list(script), []
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kw):
        self.calls.append(json.loads(json.dumps(kw, default=lambda o: getattr(o, "__dict__", str(o)))))
        return self.script.pop(0) if self.script else resp("end_turn", text("done"))


def replay(script, question="del periodo 11 quienes cumplieron los goles"):
    fake = FakeClient(script)
    orig = C.anthropic.Anthropic
    C.anthropic.Anthropic = lambda **kw: fake
    try:
        return C.answer_question([], question, model="claude-sonnet-5"), fake
    finally:
        C.anthropic.Anthropic = orig


def p11_call():
    return tool("run_sql", sql=P11_SQL, establishment_id=None,
                period_start=P11[0], period_end_exclusive=P11[1])


res, fake = replay([resp("tool_use", p11_call()), resp("end_turn", text(CONV688_ANSWER)),
                    resp("end_turn", text(CORRECTED_ANSWER))])
guard_steps = [s["financial_guard"] for s in res.steps if "financial_guard" in s]
check("replay: the original P11 query passes the R365 gate and runs",
      any(s.get("sql") == P11_SQL and s.get("row_count") == 12 and s.get("gate") == "PASS" for s in res.steps),
      str([(s.get("gate"), s.get("row_count"), s.get("error")) for s in res.steps if s.get("sql")]))
check("replay: the conv-688 draft is sent back once for a rewrite",
      guard_steps and guard_steps[0]["action"] == "rewrite_requested"
      and "ARITHMETIC: Airtex" in guard_steps[0]["issues"], str(guard_steps))
last_user = fake.calls[-1]["messages"][-1]
check("replay: the rewrite request carries the server-computed coverage facts",
      last_user["role"] == "user" and "FINANCIAL CONSISTENCY CHECK" in str(last_user["content"])
      and "LCF Airtex: forecast on 21 of 28 days" in str(last_user["content"]))
check("replay: the corrected answer is what the user gets, with no extra note",
      res.answer == CORRECTED_ANSWER.strip(), res.answer[-200:])
check("replay: final pass recorded as passed", guard_steps[-1]["action"] == "passed", str(guard_steps))
check("replay: exactly three model calls (no loop)", len(fake.calls) == 3)

res2, fake2 = replay([resp("tool_use", p11_call()), resp("end_turn", text(CONV688_ANSWER)),
                      resp("end_turn", text(CONV688_ANSWER))])
check("replay, model repeats the error: deterministic Spanish note appended",
      res2.answer.startswith(CONV688_ANSWER[:60]) and "Cobertura del forecast (verificación automática)" in res2.answer
      and "forecast en 21 de 28 días" in res2.answer and "pero la tabla muestra" in res2.answer)
check("replay, model repeats the error: only one rewrite was requested",
      sum(1 for s in res2.steps if s.get("financial_guard", {}).get("action") == "rewrite_requested") == 1
      and len(fake2.calls) == 3)

labor_sql = ("SELECT store_name, net_sales, labor_cost, labor_pct, weeks_loaded, weeks_complete, "
             "period_fully_covered\nFROM v_r365_store_period_llm\nWHERE fiscal_year = 2026 AND "
             "period_number = 11 AND start_date = '2026-10-08'\nORDER BY labor_pct DESC")
labor_answer = "| Tienda | Net sales | Labor cost | Labor % |\n|---|---|---|---|\n| Nederland | $74,470.15 | $18,837.81 | 25.30% |"
res3, fake3 = replay([resp("tool_use", tool("run_sql", sql=labor_sql, establishment_id=None,
                                            period_start=P11[0], period_end_exclusive=P11[1])),
                      resp("end_turn", text(labor_answer))], question="y el labor?")
check("replay: a non-forecast (labor) answer is untouched and needs no extra call",
      res3.answer == labor_answer and len(fake3.calls) == 2
      and not any("financial_guard" in s for s in res3.steps))

res4, fake4 = replay([resp("end_turn", text(
    "| Store | Sales | Forecast | Difference |\n|---|---|---|---|\n| X | $1,000.00 | $900.00 | +$50.00 |"))],
    question="sales vs forecast?")
check("replay: a broken sales/forecast table without any SQL is still caught",
      any(s.get("financial_guard", {}).get("action") == "rewrite_requested" for s in res4.steps))

# ═══ F. allowlist, gates, meta, prompt, tolerance, store scope ══════════════
section("F. allowlist, gates, prompt, tolerance, store scope")
check("v_revel_sales_metrics_daily_llm allowlisted", "v_revel_sales_metrics_daily_llm" in C._ALLOWED_RELATIONS)
check("... and gated as Revel (not reference data)",
      C.domains_for_relations({"v_revel_sales_metrics_daily_llm"}) == {"revel"}
      and "v_revel_sales_metrics_daily_llm" not in C._REFERENCE_RELATIONS)
check("raw payments_v2 / r365_store_daily stay denied",
      "payments_v2" not in C._ALLOWED_RELATIONS and "r365_store_daily" not in C._ALLOWED_RELATIONS)
rejected = False
try:
    C._validate("SELECT * FROM v_revel_sales_metrics_daily_llm; DELETE FROM orders_v2")
except Exception:
    rejected = True
check("SQL safety: multi-statement / write still rejected", rejected)
check("+/-0.5% reconciliation tolerance unchanged", C.RECON_TOLERANCE_PCT == 0.5)
sp = C._system_static()
check("prompt: FORECAST COVERAGE rule present", "FORECAST COVERAGE" in sp and "sales_on_forecast_days" in sp
      and "NEVER put net_sales in the same row" in sp)
check("prompt: default 'sales' mapping unchanged but labelled incl. tax",
      '"sales" / "revenue" / "net sales"  -> features_daily_summary_v2.total_revenue' in sp
      and "THIS FIGURE INCLUDES SALES TAX" in sp)
check("prompt: Revel metrics view documented, closest-figure adoption forbidden",
      "v_revel_sales_metrics_daily_llm" in sp and "never pick whichever" in sp)
meta = C.meta_profile(None, P11[0], P11[1], domains=["r365"], cache={})
fcov = {c["store_name"]: c for c in meta["r365"].get("forecast_coverage", [])}
check("check_data r365.forecast_coverage lists exactly the non-FULL stores",
      set(fcov) == {"LCF Airtex", "LCF Beaumont", "LCF Ella", "LCF Downtown Houston"}, str(sorted(fcov)))
check("check_data coverage entry is JSON-safe and exact",
      fcov["LCF Airtex"]["days_without_forecast"] == [["2026-10-29", "2026-11-04"]]
      and fcov["LCF Airtex"]["sales_on_forecast_days"] == 175134.93
      and json.dumps(meta, default=str))
check("R365 gate verdict for P11 unchanged (PASS)", meta["analysis_permitted"] is True)
check("check_data says the coverage check itself was VERIFIED",
      meta["r365"].get("forecast_coverage_check") == "VERIFIED")
tf = C.r365_trusted_figures(None, P11[0], P11[1])
check("trusted figures (blocked path) carry sales_on_forecast_days and coverage",
      all("sales_on_forecast_days" in r for r in tf["by_store"])
      and {c["store_name"] for c in tf["forecast_coverage"]} == set(fcov))
tok = store_scope.activate({32})
try:
    scoped, _unavail = C._guard_coverage([(None, P11[0], P11[1])], {})
finally:
    store_scope.deactivate(tok)
check("store-restricted credential: guard coverage limited to the allowed store",
      [c["store_name"] for c in scoped] == ["LCF Airtex"], str([c["store_name"] for c in scoped]))

# Rollback script: recreates migration 54's views exactly (checked as TEMP views).
RB = open("docs/rollback/62_63_financial_reporting_rollback.sql").read()
check("rollback drops the new view and both R365 views, restores the laynes_ro grants",
      all(x in RB for x in ("DROP VIEW IF EXISTS v_revel_sales_metrics_daily_llm;",
                            "DROP VIEW v_r365_store_period_llm;", "DROP VIEW v_r365_store_weekly_llm;",
                            "GRANT SELECT ON v_r365_store_weekly_llm  TO laynes_ro;",
                            "GRANT SELECT ON v_r365_store_period_llm  TO laynes_ro;")))
check("migration 62 pins the new view's owner", "ALTER VIEW v_revel_sales_metrics_daily_llm OWNER TO laynes_user;" in MIG)
conn = rw_conn()
try:
    cur = conn.cursor()
    rb_names = migration_sandbox.install(cur, RB)
    same = []
    for v in rb_names:
        cur.execute("SELECT pg_get_viewdef(%s::regclass, true), pg_get_viewdef(%s::regclass, true)",
                    ("pg_temp." + v, "public." + v))
        a, b = cur.fetchone()
        cur.execute(f"SELECT count(*) FROM ((SELECT * FROM public.{v} EXCEPT SELECT * FROM pg_temp.{v}) "
                    f"UNION ALL (SELECT * FROM pg_temp.{v} EXCEPT SELECT * FROM public.{v})) x")
        # inside this session production's own definition names the shadowed
        # weekly view as public.<name>; that qualifier is the only allowed diff
        same.append(a == b.replace("public.v_r365_store_weekly_llm", "v_r365_store_weekly_llm")
                    and cur.fetchone()[0] == 0)
    check("rollback recreates the current production R365 views exactly (definition and values)",
          rb_names == ["v_r365_store_weekly_llm", "v_r365_store_period_llm"] and all(same), str(same))
finally:
    conn.rollback()
    conn.close()

mig63 = open("migrations/63_financial_reporting_grants.sql").read()
grants63 = [l for l in mig63.splitlines() if l.strip().upper().startswith("GRANT")]
check("migration 63 grants SELECT on the new view only",
      grants63 == ["GRANT SELECT ON v_revel_sales_metrics_daily_llm TO laynes_ro;"], str(grants63))
check("migrations 62/63 never grant raw tables or write privileges",
      not re.search(r"\b(INSERT|UPDATE|DELETE|TRUNCATE|DROP)\b|\bALTER\s+(TABLE|ROLE|SCHEMA|DATABASE|DEFAULT)\b|ALL PRIVILEGES|GRANT\s+\w+\s+ON\s+"
                    r"(PAYMENTS_V2|R365_STORE_DAILY|ORDERS_V2)\b",
                    re.sub(r"--[^\n]*", "", (MIG + mig63)).upper().replace("DELETED", "")))
ro = C._ro_conn()
try:
    rc = ro.cursor()
    rc.execute("SELECT to_regclass('public.v_revel_sales_metrics_daily_llm') IS NOT NULL")
    applied = rc.fetchone()[0]
    if applied:
        rc.execute("SELECT has_table_privilege('laynes_ro', 'public.v_revel_sales_metrics_daily_llm', 'SELECT'),"
                   " has_table_privilege('laynes_ro', 'public.payments_v2', 'SELECT')")
        sel, raw = rc.fetchone()
        check("after 62/63: laynes_ro reads the view but still not payments_v2", sel and not raw)
    else:
        print("  [SKIP] live grant check: migrations 62/63 not applied to this database yet")
finally:
    ro.rollback()
    ro.close()

# ═══ G. features_daily_summary_v2 vs verified orders (Priority 4) ═══════════
section("G. daily summary differences are open tickets, not stale data")
ro = C._ro_conn()
try:
    rc = ro.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    rc.execute("""
        WITH v AS (SELECT establishment_id, business_date,
                          sum(final_total) FILTER (WHERE txn_class IN ('REAL', 'COMP')) verified,
                          COALESCE(sum(final_total) FILTER (WHERE txn_class = 'REAL' AND closed IS NOT TRUE), 0) open_real,
                          COALESCE(sum(final_total) FILTER (WHERE closed = true AND deleted = false), 0) feature_rule
                     FROM v_orders_classified
                    WHERE business_date >= '2026-10-15' AND business_date < '2026-11-15' GROUP BY 1, 2)
        SELECT count(*) n,
               count(*) FILTER (WHERE abs(f.total_revenue - v.verified) > 0.01) differing,
               count(*) FILTER (WHERE abs(f.total_revenue - v.verified) > 0.01
                                  AND abs((v.verified - f.total_revenue) - v.open_real) > 0.01) unexplained,
               count(*) FILTER (WHERE abs(f.total_revenue - v.feature_rule) > 0.01) stale
          FROM features_daily_summary_v2 f
          JOIN v ON v.establishment_id = f.establishment_id AND v.business_date = f.date""")
    r = rc.fetchone()
finally:
    ro.rollback()
    ro.close()
check("every summary/verified difference equals the open (unclosed) REAL tickets",
      r["differing"] > 0 and r["unexplained"] == 0, str(dict(r)))
check("no stale summary row: stored total_revenue = live recompute with its own rule",
      r["stale"] == 0, str(dict(r)))

# ═══ H. review findings (PR #34) ════════════════════════════════════════════
section("H. review findings: store-specific disclosure, unavailable coverage, "
        "no-forecast sales, timeouts")
three = [dict(c, store_name=n) for c, n in zip(partial_cov, ("LCF Airtex", "LCF Beaumont", "LCF Ella"))]
undis = lambda t: {i.row for i in G.check_coverage(t, three) if i.kind == "COVERAGE_UNDISCLOSED"}
check("H1 an Airtex-only '21 de 28' does not disclose Beaumont or Ella",
      undis("Airtex: forecast en 21 de 28 días.") == {"LCF Beaumont", "LCF Ella"})
check("H1 a fraction in a different sentence from the store name does not count",
      undis("Beaumont tuvo buen mes. La cobertura fue 21 de 28 días para otra tienda.") == {"LCF Airtex", "LCF Beaumont", "LCF Ella"})
check("H1 one sentence naming all three stores with their fraction discloses all three",
      undis("Airtex, Beaumont y Ella tuvieron forecast en solo 21 de 28 días.") == set())
check("H1 per-store table rows disclose each store", undis(CORRECTED_ANSWER.replace("Ella | 21 de 28", "Ella | 21 de 28")) == set())
check("H1 a table row for Airtex with the fraction does not cover Ella's row without one",
      undis("| Tienda | Días con forecast |\n|---|---|\n| Airtex | 21 de 28 |\n| Ella | n/a |\n"
            "Beaumont: 21/28.") == {"LCF Ella"})
check("H1 store names match whole words only ('Ella' is not found inside 'Estrella')",
      "LCF Ella" in undis("Estrella: 21 de 28 días."))
check("H1 the final note lists only the stores still undisclosed",
      "LCF Ella" in G.final_note(G.check_coverage("Airtex 21/28. Beaumont 21/28.", three), three,
                                 "Airtex 21/28. Beaumont 21/28.")
      and "LCF Airtex: forecast on" not in G.final_note(
          G.check_coverage("Airtex 21/28. Beaumont 21/28.", three), three, "Airtex 21/28. Beaumont 21/28."))

iss_u = G.review("| Store | Sales | Forecast | Difference |\n|---|---|---|---|\n| A | $10.00 | $5.00 | +$5.00 |",
                 [], coverage_unavailable=True)
check("H2 unavailable coverage is reported as COVERAGE_UNVERIFIED, not as full coverage",
      [i.kind for i in iss_u] == ["COVERAGE_UNVERIFIED"])
check("H2 COVERAGE_UNVERIFIED is not sent back for a rewrite (the model has no facts)",
      "COVERAGE_UNVERIFIED" not in G.REWRITABLE)
check("H2 a full-period claim is still flagged while coverage is unverified",
      "FULL_PERIOD_CLAIM" in [i.kind for i in G.review(
          "The comparison covers the full period for every store against forecast.", [], True)])
nu = G.final_note(iss_u, [], "Here are the stores against forecast.")
check("H2 the note says coverage was not verified and never calls it full-period",
      "Forecast coverage not verified" in nu and "not as a full-period comparison" in nu)
orig_fc = G.forecast_coverage
def _boom(*a, **k):
    raise psycopg2.OperationalError("simulated coverage read failure")
G.forecast_coverage = _boom          # only the coverage read fails; the query itself runs
try:
    cache = {}
    cov_f, unav_f = C._guard_coverage([(None, P11[0], P11[1])], cache)
    res5, fake5 = replay([resp("tool_use", p11_call()), resp("end_turn", text(CORRECTED_ANSWER))])
finally:
    G.forecast_coverage = orig_fc
check("H2 a failed coverage read returns (no rows, unavailable) and caches None, not []",
      cov_f == [] and unav_f is True and cache[(None, P11[0], P11[1])] is None)
g5 = [s["financial_guard"] for s in res5.steps if "financial_guard" in s]
check("H2 replay with the coverage read failing: no rewrite, unverified note appended",
      len(fake5.calls) == 2 and not any(x.get("action") == "rewrite_requested" for x in g5)
      and any(x.get("coverage_unavailable") for x in g5) and "Cobertura del forecast no verificada" in res5.answer)
conn = rw_conn()
try:
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    orig_fc = G.forecast_coverage
    G.forecast_coverage = lambda c, *a: c.execute("SELECT * FROM no_such_relation_for_test")
    try:
        r = C._r365_forecast_coverage(cur, None, P11[0], P11[1])
    finally:
        G.forecast_coverage = orig_fc
    cur.execute("SELECT 1 AS ok")
    still_ok = cur.fetchone()["ok"] == 1
finally:
    conn.rollback()
    conn.close()
check("H2 check_data: a coverage failure says UNAVAILABLE (not an empty 'all FULL' list) "
      "and the profile transaction survives",
      r["forecast_coverage_check"] == "UNAVAILABLE" and r["forecast_coverage"] == [] and still_ok, str(r))

conn = rw_conn()
try:
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    migration_sandbox.install(cur, MIG)
    cur.execute("""WITH d AS (SELECT establishment_id, week_start,
                                     COALESCE(sum(net_sales) FILTER (WHERE COALESCE(forecasted_sales, 0) <= 0), 0) nofc
                                FROM v_r365_store_daily_llm GROUP BY 1, 2)
                   SELECT count(*) n,
                          count(*) FILTER (WHERE abs(w.sales_on_days_without_forecast - d.nofc) > 0.005) bad
                     FROM v_r365_store_weekly_llm w JOIN d USING (establishment_id, week_start)""")
    r1 = cur.fetchone()
    cur.execute("""SELECT count(*) n, count(*) FILTER (WHERE abs(p.sales_on_days_without_forecast - x.s) > 0.005) bad
                     FROM v_r365_store_period_llm p
                     JOIN (SELECT establishment_id, fiscal_year, period_number,
                                  COALESCE(sum(net_sales) FILTER (WHERE COALESCE(forecasted_sales, 0) <= 0), 0) s
                             FROM v_r365_store_daily_llm WHERE fiscal_year IS NOT NULL GROUP BY 1, 2, 3) x
                    USING (establishment_id, fiscal_year, period_number)""")
    r2 = cur.fetchone()
    cur.execute("""SELECT sales_on_days_without_forecast s FROM v_r365_store_period_llm
                    WHERE fiscal_year = 2026 AND period_number = 11 AND store_name = 'LCF Airtex'""")
    r3 = cur.fetchone()
finally:
    conn.rollback()
    conn.close()
check("H3 weekly sales_on_days_without_forecast = SUM(daily net_sales) on no-forecast days",
      r1["n"] >= 156 and r1["bad"] == 0, str(dict(r1)))
check("H3 period sales_on_days_without_forecast = the same, from the daily rows",
      r2["n"] >= 48 and r2["bad"] == 0, str(dict(r2)))
check("H3 Airtex P11: equals its uncovered week's sales (61,758.01)", money(r3["s"]) == money("61758.01"))
check("H3 the migration no longer derives it as net_sales - forecast_sales_base",
      "w.net_sales - CASE WHEN" not in MIG and "a.net_sales - COALESCE(" not in MIG
      and "FILTER (WHERE COALESCE(d.forecasted_sales, 0) <= 0)" in MIG)

orig_to = C.STATEMENT_TIMEOUT_MS
C.STATEMENT_TIMEOUT_MS = 50
err = None
try:
    C.run_sql("SELECT establishment_id, sum(final_total) FROM v_orders_classified "
              "WHERE business_date >= '2026-08-13' AND business_date < '2026-11-13' GROUP BY 1")
except Exception as e:
    err = e
finally:
    C.STATEMENT_TIMEOUT_MS = orig_to
check("H4 a real statement timeout (50 ms) is recognised as a timeout",
      err is not None and G.is_timeout_error(err), repr(err)[:120])
import psycopg2.errors as PE  # noqa: E402
orig_run = C.run_sql
def _slow(sql):
    raise PE.QueryCanceled("canceling statement due to statement timeout")
C.run_sql = _slow
try:
    rev_sql = ("SELECT establishment_id, sum(net_sales_excluding_tax) FROM v_orders_classified "
               "WHERE business_date >= '2026-08-13' AND business_date < '2026-11-13' GROUP BY 1")
    res6, fake6 = replay([resp("tool_use", tool("run_sql", sql=rev_sql, establishment_id=None,
                                                period_start="2026-08-13", period_end_exclusive="2026-11-13")),
                          resp("end_turn", text("Lo siento, las ventas de la red no están disponibles para ese rango."))],
                         question="ventas netas sin impuesto de la red, ago-nov")
    res7, _ = replay([resp("tool_use", tool("run_sql", sql=rev_sql, establishment_id=None,
                                            period_start="2026-08-13", period_end_exclusive="2026-11-13")),
                      resp("end_turn", text("That query was too large; I'll need one month at a time."))],
                     question="network net sales Aug-Nov")
finally:
    C.run_sql = orig_run
tr = [m for m in fake6.calls[-1]["messages"] if m["role"] == "user" and isinstance(m["content"], list)]
tr_txt = json.dumps(tr)
gate_ran = any(s.get("timeout") for s in res6.steps)
check("H4 the model is told: timed out, NOT missing data, narrow and retry",
      gate_ran and "QUERY TIMED OUT" in tr_txt and "NOT missing or unavailable data" in tr_txt,
      str([(s.get("gate"), s.get("error")) for s in res6.steps if s.get("sql")])[:200])
check("H4 an answer claiming 'no están disponibles' after a timeout gets the timeout note",
      "una consulta superó el tiempo límite" in res6.answer)
check("H4 an answer that already explains the timeout gets no extra note",
      res7.answer == "That query was too large; I'll need one month at a time.")
check("H4 prompt documents the Revel view cost and that a timeout is not missing data",
      "can exceed the 15 s statement" in sp and "NEVER evidence that data is missing" in sp)

passed = sum(ok for _, ok, _ in results)
print(f"\nFinancial reporting accuracy: {passed} passed, {len(results) - passed} failed, {len(results)} total")
sys.exit(0 if passed == len(results) else 1)
