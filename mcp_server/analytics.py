"""Read-only analytics behind tools 1-4.

Every query here is a fixed, parameterised SELECT against an allowlisted view
(chat_sql._ALLOWED_RELATIONS), executed as the read-only laynes_ro role in a
read-only transaction with a statement timeout. Store filters are always bound
parameters taken from security.resolve_stores(), never from client text.

Trust gates are not reimplemented: chat_sql.meta_profile() is called per store,
exactly as the website assistant calls it, and its verdict decides whether
figures are returned. Missing data is returned as null with an explicit
coverage record; it is never converted to zero.

Two seams (_query and _gate) are the only database touchpoints, so tests can
replace them with fixtures.
"""

from __future__ import annotations

import concurrent.futures as cf
import contextlib
import time
from datetime import date, timedelta

import psycopg2
import psycopg2.extras

import chat_sql

from . import settings
from .security import ToolInputError, num, ranges_of

TZ = "America/Chicago"
GATE_WORKERS = 4


# ── database seams ──────────────────────────────────────────────────────────
@contextlib.contextmanager
def _cursor():
    conn = chat_sql._ro_conn()                    # laynes_ro, never the RW role
    try:
        conn.set_session(readonly=True, autocommit=False)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SET statement_timeout = {int(settings.STATEMENT_TIMEOUT_MS)}")
            yield cur
    finally:
        conn.rollback()
        conn.close()


def _query(sql: str, params: dict) -> list[dict]:
    with _cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def _gate(est: int, start: date, end_excl: date, domains: list[str]) -> dict:
    """The existing A12/domain trust profile for ONE permitted store."""
    return chat_sql.meta_profile(est, start.isoformat(), end_excl.isoformat(),
                                 domains=domains)


def _gates(ids, start, end_excl, domains) -> dict[int, dict]:
    """Per-store gates, run in a small pool. A gate that cannot be computed is
    reported as such -- the store's figures are then withheld, not guessed."""
    out: dict[int, dict] = {}
    with cf.ThreadPoolExecutor(max_workers=GATE_WORKERS) as pool:
        futs = {pool.submit(_gate, i, start, end_excl, domains): i for i in ids}
        for f in cf.as_completed(futs):
            i = futs[f]
            try:
                out[i] = f.result()
            except Exception:                                   # noqa: BLE001
                out[i] = {"analysis_permitted": False,
                          "blocking_reasons": ["trust gate could not be evaluated"],
                          "warnings": [], "_error": True}
    return out


def _names(ids) -> dict[int, str]:
    return {i: chat_sql.ESTABLISHMENTS.get(i, f"store {i}") for i in ids}


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _end_excl(end: date) -> date:
    return end + timedelta(days=1)


def _gate_summary(g: dict, domain: str) -> dict:
    s = {"gate": "PASS" if g.get("analysis_permitted") else "FAIL",
         "blocking_reasons": list(g.get("blocking_reasons") or []),
         "warnings": list(g.get("warnings") or [])}
    if domain == "revel":
        rec, fr = g.get("reconciliation") or {}, g.get("freshness") or {}
        s["reconciliation"] = {k: rec.get(k) for k in
                               ("status", "delta_pct", "delta_pct_display", "tolerance_pct",
                                "basis") if k in rec}
        s["freshness"] = {k: fr.get(k) for k in
                          ("latest_source_timestamp", "latest_ingested_at",
                           "source_lag_hours", "expected_days_in_period",
                           "days_with_data", "missing_business_days") if k in fr}
    elif domain == "r365":
        r = g.get("r365") or {}
        s["freshness"] = {k: r.get(k) for k in
                          ("freshness_status", "freshness_reason", "latest_daily_date",
                           "latest_complete_week_end", "ingest_schedule") if k in r}
        s["coverage"] = {k: r.get(k) for k in
                         ("covered_dates_in_scope", "requested_dates",
                          "partial_weeks_in_scope") if k in r}
    elif domain == "hme":
        h = g.get("hme") or {}
        s["freshness"] = {k: h.get(k) for k in
                          ("freshness_status", "freshness_reason", "latest_business_date",
                           "expected_business_date", "next_ingest_window") if k in h}
    elif domain == "r365_financial":
        f = g.get("r365_financial") or {}
        s["freshness"] = {k: f.get(k) for k in
                          ("last_refreshed_at", "refresh_age_hours", "source_sync_stale",
                           "refresh_schedule") if k in f}
    return s


def _overall(statuses: list[str]) -> str:
    if statuses and all(s == "ok" for s in statuses):
        return "ok"
    if any(s in ("ok", "partial") for s in statuses):
        return "partial"
    return "unavailable"


# ═══ Tool 1: sales and guest counts ════════════════════════════════════════
_REVEL_SQL = """
SELECT establishment_id, business_date,
       count(*) FILTER (WHERE txn_class = 'REAL')            AS real_transactions,
       count(*) FILTER (WHERE txn_class = 'COMP')            AS comp_tickets,
       count(*) FILTER (WHERE txn_class = 'EMPTY')           AS empty_tickets,
       sum(final_total) FILTER (WHERE txn_class = 'REAL')    AS real_sales
  FROM v_orders_classified
 WHERE establishment_id = ANY(%(ids)s)
   AND business_date >= %(start)s AND business_date < %(end)s
 GROUP BY establishment_id, business_date
"""

_R365_DAILY_SQL = """
SELECT establishment_id, business_date, day_status, net_sales, gross_sales,
       guest_count, check_count, labor_hours, labor_cost
  FROM v_r365_store_daily_llm
 WHERE establishment_id = ANY(%(ids)s)
   AND business_date >= %(start)s AND business_date < %(end)s
"""

SALES_DEFINITIONS = {
    "pos_sales_revel": ("SUM(final_total) over Revel REAL tickets (txn_class='REAL': "
                        "final_total > 0 with items). Revel POS total; the project's "
                        "definition of 'sales'. Comped, empty and deleted tickets are "
                        "excluded from sales and from the real transaction count."),
    "real_transactions_revel": ("Count of Revel REAL tickets (a transaction/check as the "
                                "POS records it). NOT guests: Revel has no verified guest "
                                "count."),
    "comp_tickets_revel": "$0 tickets that have items (employee meals / de-facto voids).",
    "empty_tickets_revel": "$0 tickets with no items (POS artefacts).",
    "net_sales_r365": "R365 daily-report net sales (R365 is authoritative for net sales).",
    "gross_sales_r365": "R365 daily-report gross sales.",
    "guest_count_r365": "R365 daily-report guest count (R365's own figure).",
    "check_count_r365": ("R365 daily-report check count. Differs from Revel REAL "
                         "transactions by definition; the two are never merged."),
}


def sales_guest_counts(ids: list[int], start: date, end: date, group_by: str) -> dict:
    if group_by not in ("day", "store"):
        raise ToolInputError("group_by must be 'day' or 'store'")
    end_x = _end_excl(end)
    names = _names(ids)
    requested_days = _days(start, end)
    params = {"ids": ids, "start": start, "end": end_x}

    revel_gates = _gates(ids, start, end_x, ["revel"])
    r365_gates = _gates(ids, start, end_x, ["r365"])
    revel = {(r["establishment_id"], r["business_date"]): r
             for r in _query(_REVEL_SQL, params)}
    r365 = {(r["establishment_id"], r["business_date"]): r
            for r in _query(_R365_DAILY_SQL, params)}

    stores, rows, statuses = [], [], []
    for i in ids:
        rg, cg = revel_gates[i], r365_gates[i]
        revel_ok = bool(rg.get("analysis_permitted"))
        r365_ok = bool(cg.get("analysis_permitted"))
        rev_days = [d for d in requested_days if (i, d) in revel] if revel_ok else []
        r365_days = [d for d in requested_days if (i, d) in r365] if r365_ok else []
        missing_revel = [d for d in requested_days if (i, d) not in revel]
        missing_r365 = [d for d in requested_days if (i, d) not in r365]
        st = "ok" if (revel_ok and not missing_revel) else (
            "partial" if (rev_days or r365_days) else "unavailable")
        statuses.append(st)
        stores.append({
            "store_id": i, "store_name": names[i], "status": st,
            "revel": {**_gate_summary(rg, "revel"),
                      "days_requested": len(requested_days),
                      "days_with_data": len(rev_days) if revel_ok else None,
                      "days_without_data": ranges_of(missing_revel),
                      "figures_withheld": not revel_ok},
            "r365": {**_gate_summary(cg, "r365"),
                     "days_requested": len(requested_days),
                     "days_with_data": len(r365_days) if r365_ok else None,
                     "days_without_data": ranges_of(missing_r365),
                     "note": ("R365 is loaded weekly; recent days are normally absent "
                              "and are unknown, not zero")},
        })

        def day_row(d: date):
            rv = revel.get((i, d)) if revel_ok else None
            rr = r365.get((i, d)) if r365_ok else None
            return rv, rr

        if group_by == "day":
            for d in requested_days:
                rv, rr = day_row(d)
                rows.append({
                    "store_id": i, "store_name": names[i], "date": d.isoformat(),
                    "revel": None if rv is None else {
                        "pos_sales": num(rv["real_sales"]),
                        "real_transactions": int(rv["real_transactions"]),
                        "comp_tickets": int(rv["comp_tickets"]),
                        "empty_tickets": int(rv["empty_tickets"])},
                    "r365": None if rr is None else {
                        "day_status": rr["day_status"],
                        "net_sales": num(rr["net_sales"]), "gross_sales": num(rr["gross_sales"]),
                        "guest_count": rr["guest_count"], "check_count": rr["check_count"]},
                })
        else:
            rvs = [revel[(i, d)] for d in rev_days]
            rrs = [r365[(i, d)] for d in r365_days]

            def tot(rows_, key, nd=2):
                vals = [r[key] for r in rows_ if r[key] is not None]
                return round(float(sum(vals)), nd) if vals else None

            rows.append({
                "store_id": i, "store_name": names[i],
                "period": {"start_date": start.isoformat(), "end_date": end.isoformat()},
                "revel": None if not rvs else {
                    "pos_sales": tot(rvs, "real_sales"),
                    "real_transactions": int(sum(r["real_transactions"] for r in rvs)),
                    "comp_tickets": int(sum(r["comp_tickets"] for r in rvs)),
                    "empty_tickets": int(sum(r["empty_tickets"] for r in rvs)),
                    "days_included": len(rvs), "days_requested": len(requested_days)},
                "r365": None if not rrs else {
                    "net_sales": tot(rrs, "net_sales"), "gross_sales": tot(rrs, "gross_sales"),
                    "guest_count": int(sum(r["guest_count"] for r in rrs
                                           if r["guest_count"] is not None)),
                    "check_count": int(sum(r["check_count"] for r in rrs
                                           if r["check_count"] is not None)),
                    "days_included": len(rrs), "days_requested": len(requested_days)},
            })

    return {
        "status": _overall(statuses),
        "period": {"start_date": start.isoformat(), "end_date": end.isoformat(),
                   "timezone": TZ,
                   "business_day": ("calendar day in America/Chicago (no verified "
                                    "service-day rollover rule exists)")},
        "group_by": group_by,
        "rows": rows,
        "stores": stores,
        "definitions": SALES_DEFINITIONS,
        "notes": ["Revel and R365 figures are separate sources and are never added or "
                  "blended. A null means no verified data, never zero."],
    }


# ═══ Tool 2: labor ═════════════════════════════════════════════════════════
_LABOR_REVEL_SQL = """
SELECT establishment_id, business_date, labor_hours, estimated_labor_cost,
       missing_wage_shift_count, open_shift_count
  FROM v_labor_daily_context
 WHERE establishment_id = ANY(%(ids)s)
   AND business_date >= %(start)s AND business_date < %(end)s
"""

LABOR_DEFINITIONS = {
    "r365": {"labor_cost": "R365 labour cost (R365's own figure; not payroll).",
             "labor_hours": "R365 labour hours.",
             "sales_denominator": "R365 net sales for the same store-days.",
             "labor_pct": "SUM(labor_cost) / SUM(net_sales) * 100, recomputed from sums."},
    "revel": {"labor_cost": ("ESTIMATED cost = clocked hours x role wage. Not payroll; "
                             "excludes overtime premium, taxes and burden."),
              "labor_hours": ("Elapsed clocked time; breaks are not recorded so it "
                              "includes unpaid break time and overstates paid hours."),
              "sales_denominator": "Revel REAL-ticket sales (SUM(final_total), txn_class REAL).",
              "labor_pct": "SUM(estimated_labor_cost) / SUM(REAL sales) * 100."},
}


def labor_metrics(ids: list[int], start: date, end: date, source: str | None) -> dict:
    if source not in (None, "auto", "r365", "revel"):
        raise ToolInputError("source must be 'r365', 'revel' or omitted")
    end_x = _end_excl(end)
    names = _names(ids)
    req_days = _days(start, end)
    params = {"ids": ids, "start": start, "end": end_x}

    r365_gates = _gates(ids, start, end_x, ["r365"]) if source in (None, "auto", "r365") else {}
    r365 = {(r["establishment_id"], r["business_date"]): r
            for r in _query(_R365_DAILY_SQL, params)} if r365_gates else {}

    def r365_complete(i):
        g = r365_gates.get(i) or {}
        return (bool(g.get("analysis_permitted"))
                and all((i, d) in r365 and r365[(i, d)]["labor_cost"] is not None
                        and r365[(i, d)]["net_sales"] is not None for d in req_days))

    if source in (None, "auto"):
        chosen = "r365" if all(r365_complete(i) for i in ids) else "revel"
        reason = ("R365 covers every requested store-day" if chosen == "r365" else
                  "R365 does not cover every requested store-day (it is loaded weekly); "
                  "the whole result uses the Revel timesheet estimate instead -- the "
                  "two sources are never mixed")
    else:
        chosen, reason = source, "requested explicitly"

    out_rows, statuses, store_meta = [], [], []
    if chosen == "r365":
        for i in ids:
            g = r365_gates.get(i) or {}
            ok = bool(g.get("analysis_permitted"))
            days = [d for d in req_days if (i, d) in r365] if ok else []
            rrs = [r365[(i, d)] for d in days]
            cost = [r["labor_cost"] for r in rrs if r["labor_cost"] is not None]
            hrs = [r["labor_hours"] for r in rrs if r["labor_hours"] is not None]
            sales = [r["net_sales"] for r in rrs if r["net_sales"] is not None]
            # Ratio only when numerator and denominator cover the same days.
            same = len(cost) == len(sales) == len(rrs) and rrs
            sum_sales = float(sum(sales)) if sales else None
            pct = (round(float(sum(cost)) / sum_sales * 100, 2)
                   if same and sum_sales else None)
            st = "ok" if ok and len(days) == len(req_days) and pct is not None else (
                "partial" if days else "unavailable")
            statuses.append(st)
            out_rows.append({
                "store_id": i, "store_name": names[i], "source": "r365",
                "labor_cost": round(float(sum(cost)), 2) if cost else None,
                "labor_hours": round(float(sum(hrs)), 2) if hrs else None,
                "labor_pct": pct,
                "sales_denominator": {"basis": "r365_net_sales",
                                      "amount": round(sum_sales, 2) if sum_sales else None},
                "days_included": len(days), "days_requested": len(req_days),
                "status": st})
            gs = _gate_summary(g, "r365") if g else {}
            gs["days_without_data"] = ranges_of([d for d in req_days if (i, d) not in r365])
            store_meta.append({"store_id": i, "r365": gs})
    else:
        revel_gates = _gates(ids, start, end_x, ["revel"])
        lab = {(r["establishment_id"], r["business_date"]): r
               for r in _query(_LABOR_REVEL_SQL, params)}
        sales = {(r["establishment_id"], r["business_date"]): r
                 for r in _query(_REVEL_SQL, params)}
        for i in ids:
            g = revel_gates[i]
            ok = bool(g.get("analysis_permitted"))
            days = [d for d in req_days if (i, d) in lab and (i, d) in sales] if ok else []
            cost = [lab[(i, d)]["estimated_labor_cost"] for d in days
                    if lab[(i, d)]["estimated_labor_cost"] is not None]
            hrs = [lab[(i, d)]["labor_hours"] for d in days
                   if lab[(i, d)]["labor_hours"] is not None]
            sl = [sales[(i, d)]["real_sales"] for d in days
                  if sales[(i, d)]["real_sales"] is not None]
            same = days and len(cost) == len(days) == len(sl)
            sum_sales = float(sum(sl)) if sl else None
            pct = (round(float(sum(cost)) / sum_sales * 100, 2)
                   if same and sum_sales else None)
            open_shifts = sum(int(lab[(i, d)]["open_shift_count"] or 0) for d in days)
            missing_wage = sum(int(lab[(i, d)]["missing_wage_shift_count"] or 0) for d in days)
            st = "ok" if ok and len(days) == len(req_days) and pct is not None else (
                "partial" if days else "unavailable")
            statuses.append(st)
            out_rows.append({
                "store_id": i, "store_name": names[i], "source": "revel",
                "labor_cost": round(float(sum(cost)), 2) if cost else None,
                "labor_cost_basis": "estimated (hours x wage), not payroll",
                "labor_hours": round(float(sum(hrs)), 2) if hrs else None,
                "labor_pct": pct,
                "sales_denominator": {"basis": "revel_real_ticket_sales",
                                      "amount": round(sum_sales, 2) if sum_sales else None},
                "days_included": len(days), "days_requested": len(req_days),
                "open_shifts_excluded": open_shifts,
                "shifts_missing_wage": missing_wage, "status": st})
            gs = _gate_summary(g, "revel")
            gs["days_without_data"] = ranges_of(
                [d for d in req_days if (i, d) not in lab or (i, d) not in sales])
            store_meta.append({"store_id": i, "revel": gs})

    return {
        "status": _overall(statuses),
        "source": chosen, "source_selection": reason,
        "period": {"start_date": start.isoformat(), "end_date": end.isoformat(),
                   "timezone": TZ},
        "rows": out_rows, "coverage": store_meta,
        "definitions": LABOR_DEFINITIONS[chosen],
        "notes": ["labor_pct is computed only where the labor numerator and the sales "
                  "denominator come from the same source and cover the same days; "
                  "otherwise it is null with status partial/unavailable."],
    }


# ═══ Tool 3: R365 P&L / food cost ══════════════════════════════════════════
_PERIOD_SQL = """
SELECT fiscal_year, period_number, period_label, start_date, end_date,
       period_status, days_total, days_elapsed
  FROM v_ayg_business_periods_llm
 WHERE fiscal_year = %(fy)s AND period_number = %(pn)s
"""

_R365_PERIOD_SQL = """
SELECT establishment_id, weeks_in_period, weeks_loaded, weeks_complete,
       period_fully_covered, first_week_start, last_week_end,
       net_sales, gross_sales, labor_hours, labor_cost, labor_pct
  FROM v_r365_store_period_llm
 WHERE establishment_id = ANY(%(ids)s)
   AND fiscal_year = %(fy)s AND period_number = %(pn)s
"""

_R365_FIN_SQL = """
SELECT establishment_id, analytics_category, expense_amount, has_posting,
       data_status, source_quality, transaction_count
  FROM v_r365_financial_period_llm
 WHERE establishment_id = ANY(%(ids)s)
   AND fiscal_year = %(fy)s AND period_number = %(pn)s
"""

VERIFIED_FIN_CATEGORIES = ("ELECTRICITY", "GAS", "WATER", "RENT", "REPAIRS_MAINTENANCE")

FOOD_COST_UNAVAILABLE = {
    "status": "unavailable",
    "missing_dependency": (
        "No verified food-cost / COGS source exists in this system. The R365 financial "
        "integration publishes only five VERIFIED GL categories (ELECTRICITY, GAS, WATER, "
        "RENT, REPAIRS_MAINTENANCE); COGS/food accounts are not mapped to a verified "
        "category, and no inventory-usage or purchase feed is loaded. The R365 weekly "
        "report carries no COGS fields."),
    "not_substituted": ("Food cost is not estimated from sales, recipes, labour or "
                        "candidate GL accounts."),
}
OPERATING_PROFIT_UNAVAILABLE = {
    "status": "unavailable",
    "missing_dependency": (
        "Operating profit needs COGS and the full operating-expense set; only five "
        "verified expense categories exist and COGS is unavailable, so any profit "
        "figure would be invented."),
}
UNVERIFIED_ACCOUNTS_NOTE = (
    "Candidate/unmapped GL accounts (e.g. telephone, waste, insurance, storage rent) are "
    "excluded and are not mixed into verified categories.")


def parse_period(period) -> int:
    if isinstance(period, bool) or (isinstance(period, float) and not period.is_integer()):
        raise ToolInputError("period must be a fiscal period number 1-13 (or 'P10')")
    if isinstance(period, str):
        p = period.strip().upper().lstrip("P")
        period = p
    try:
        n = int(period)
    except (TypeError, ValueError):
        raise ToolInputError("period must be a fiscal period number 1-13 (or 'P10')") from None
    if not 1 <= n <= 13:
        raise ToolInputError("period must be between 1 and 13 (AYG has 13 periods of 28 days)")
    return n


def pnl_food_cost(ids: list[int], fiscal_year: int, period, report_type: str) -> dict:
    if report_type not in ("summary", "pnl", "food_cost"):
        raise ToolInputError("report_type must be 'summary', 'pnl' or 'food_cost'")
    if isinstance(fiscal_year, bool) or not isinstance(fiscal_year, int):
        raise ToolInputError("fiscal_year must be an integer such as 2026")
    pn = parse_period(period)
    cal = _query(_PERIOD_SQL, {"fy": fiscal_year, "pn": pn})
    if not cal:
        raise ToolInputError(
            f"FY{fiscal_year} P{pn} is not in the official AYG fiscal calendar "
            "(defined for FY2026 and FY2027 only)")
    p = cal[0]
    if (p["end_date"] - p["start_date"]).days + 1 != 28:
        raise ToolInputError("calendar period is not 28 days; refusing to compute")
    start, end_x = p["start_date"], p["end_date"] + timedelta(days=1)
    names = _names(ids)
    period_out = {
        "fiscal_year": fiscal_year, "period_number": pn, "period_label": p["period_label"],
        "start_date": p["start_date"].isoformat(), "end_date": p["end_date"].isoformat(),
        "days": 28, "period_status": p["period_status"],
        "calendar": "AYG fiscal calendar: 13 periods x 28 days (not calendar months)",
    }

    ops_gates = _gates(ids, start, end_x, ["r365"])
    fin_gates = _gates(ids, start, end_x, ["r365_financial"])
    detail_gates = _gates(ids, start, end_x, ["r365_financial_detail"])
    ops = {r["establishment_id"]: r for r in _query(_R365_PERIOD_SQL,
                                                     {"ids": ids, "fy": fiscal_year, "pn": pn})}
    fin_rows = _query(_R365_FIN_SQL, {"ids": ids, "fy": fiscal_year, "pn": pn})
    fin: dict[int, dict] = {}
    for r in fin_rows:
        fin.setdefault(r["establishment_id"], {})[r["analytics_category"]] = r

    stores, statuses = [], []
    for i in ids:
        o, f = ops.get(i), fin.get(i, {})
        og, fg, dg = ops_gates[i], fin_gates[i], detail_gates[i]
        ops_ok = o is not None and bool(og.get("analysis_permitted"))
        fin_ok = bool(fg.get("analysis_permitted")) and bool(f)
        if period_out["period_status"] == "FUTURE":
            ops_ok = fin_ok = False

        sales = num(o["net_sales"]) if ops_ok else None
        labor_cost = num(o["labor_cost"]) if ops_ok else None
        labor_pct = num(o["labor_pct"]) if ops_ok else None
        cats, no_posting = {}, []
        for c in VERIFIED_FIN_CATEGORIES:
            r = f.get(c) if fin_ok else None
            if r is None:
                cats[c] = {"status": "unavailable", "amount": None}
            elif not r["has_posting"]:
                cats[c] = {"status": "NO_POSTING", "amount": None}
                no_posting.append(c)
            else:
                cats[c] = {"status": "posted", "amount": num(r["expense_amount"]),
                           "data_status": r["data_status"],
                           "source_quality": r["source_quality"]}
        posted = [v["amount"] for v in cats.values() if v["status"] == "posted"]
        detail_recon = (dg.get("r365_financial_detail") or {})
        entry = {
            "store_id": i, "store_name": names[i],
            "net_sales": {"amount": sales, "source": "r365_weekly_report",
                          "weeks_loaded": o["weeks_loaded"] if o else None,
                          "weeks_in_period": o["weeks_in_period"] if o else None,
                          "period_fully_covered": bool(o["period_fully_covered"]) if o else None},
            "gross_sales": num(o["gross_sales"]) if ops_ok else None,
            "labor": {"cost": labor_cost, "pct_of_net_sales": labor_pct,
                      "hours": num(o["labor_hours"]) if ops_ok else None,
                      "source": "r365_weekly_report (R365 labour, not payroll)"},
            "verified_expense_categories": cats,
            "verified_expense_subtotal": {
                "amount": round(sum(posted), 2) if posted else None,
                "includes": [c for c, v in cats.items() if v["status"] == "posted"],
                "no_posting": no_posting,
                "warning": ("Sum of the verified categories that have postings ONLY. "
                            "This is NOT total operating expense."),
            },
            "food_cost": dict(FOOD_COST_UNAVAILABLE),
            "operating_profit": dict(OPERATING_PROFIT_UNAVAILABLE),
            "coverage": {
                "r365_weekly": _gate_summary(og, "r365"),
                "r365_financial": _gate_summary(fg, "r365_financial"),
                "transaction_reconciliation": {
                    "gate": "PASS" if dg.get("analysis_permitted") else "FAIL",
                    "detail": {k: detail_recon.get(k) for k in
                               ("approved_lines_in_scope", "migrated_summary_lines_in_scope",
                                "reconciliation", "last_refreshed_at")
                               if k in detail_recon},
                    "blocking_reasons": list(dg.get("blocking_reasons") or []),
                },
            },
        }
        st = "ok" if (ops_ok and fin_ok and not no_posting and o["period_fully_covered"]
                      and period_out["period_status"] == "COMPLETED") else (
            "partial" if (ops_ok or fin_ok) else "unavailable")
        entry["status"] = st
        statuses.append(st)
        stores.append(entry)

    # food_cost can never be 'ok': report_type shapes the payload, not the truth.
    if report_type == "food_cost":
        for s in stores:
            s["report"] = {
                "food_cost": s["food_cost"],
                "sales_denominator": s["net_sales"],
                "note": "food cost % cannot be computed without a verified COGS source"}
            for k in ("verified_expense_categories", "verified_expense_subtotal",
                      "operating_profit", "labor", "gross_sales"):
                s.pop(k, None)
    elif report_type == "summary":
        for s in stores:
            s.pop("verified_expense_categories", None)

    overall = _overall(statuses)
    if report_type == "food_cost":
        overall = "unavailable"
    elif overall == "ok":
        overall = "partial"            # COGS/profit are always unavailable
    return {
        "status": overall,
        "report_type": report_type,
        "period": period_out,
        "stores": stores,
        "approval_basis": ("Approved GL journal lines only. Unapproved entries are excluded "
                           "and never treated as approved. No posting is reported as "
                           "NO_POSTING, never as $0."),
        "limitations": [
            "Food cost / COGS: unavailable (see missing_dependency).",
            "Operating profit: unavailable.",
            UNVERIFIED_ACCOUNTS_NOTE,
            "Period grain only; expenses are not split into days or weeks.",
        ],
        "limits_note": ("Totals across stores are not returned; each figure is per store "
                        "within the credential's permitted stores."),
    }


# ═══ Tool 4: HME drive-thru ════════════════════════════════════════════════
_HME_SQL = """
SELECT d.establishment_id, d.business_date, d.drive_thru_cars, d.regular_cars,
       d.disastrous_cars, d.lane_total_avg_seconds, d.lane_queue_avg_seconds,
       d.lane_total_2_avg_seconds
  FROM v_hme_store_daily_llm d
 WHERE d.establishment_id = ANY(%(ids)s)
   AND d.business_date >= %(start)s AND d.business_date < %(end)s
"""

_HME_MAPPED_SQL = "SELECT DISTINCT establishment_id FROM v_hme_store_daily_llm"

HME_DEFINITIONS = {
    "avg_speed_of_service_seconds": (
        "HME lane_total_avg_seconds: average total lane time per car for the day, as "
        "HME reports it. Over several days this is the drive_thru_cars-weighted mean of "
        "the daily averages (a derived figure; per-car data is not available)."),
    "avg_queue_seconds": "HME lane_queue_avg_seconds (time in the lane queue), car-weighted.",
    "avg_total_2_seconds": "HME lane_total_2_avg_seconds (HME's second total timer), car-weighted.",
    "drive_thru_cars": ("HME lane vehicle observations. NOT Revel POS orders; never "
                        "equate or sum the two."),
}


def drive_thru_speed(ids: list[int], start: date, end: date, daypart) -> dict:
    end_x = _end_excl(end)
    names = _names(ids)
    req_days = _days(start, end)
    base = {
        "period": {"start_date": start.isoformat(), "end_date": end.isoformat(),
                   "timezone": TZ},
        "definitions": HME_DEFINITIONS,
    }
    if daypart not in (None, "", "all"):
        return {**base, "status": "unavailable", "daypart": daypart, "stores": [],
                "missing_dependency": (
                    "No store x hour or daypart HME fact exists (HME is loaded at "
                    "store x day grain) and no verified daypart definition exists. "
                    "Daily figures are not approximated into a daypart. Call again "
                    "without 'daypart' for daily results.")}

    mapped = {r["establishment_id"] for r in _query(_HME_MAPPED_SQL, {})}
    covered = [i for i in ids if i in mapped]
    uncovered = [i for i in ids if i not in mapped]
    gates = _gates(covered, start, end_x, ["hme"]) if covered else {}
    rows = {(r["establishment_id"], r["business_date"]): r
            for r in _query(_HME_SQL, {"ids": covered or [0], "start": start, "end": end_x})}

    stores, statuses = [], []
    for i in ids:
        if i in uncovered:
            statuses.append("unavailable")
            stores.append({"store_id": i, "store_name": names[i], "status": "unavailable",
                           "reason": ("This store has no verified HME mapping, so there is "
                                      "no drive-thru timing data (not zero seconds)."),
                           "daypart": "all (daily grain)"})
            continue
        g = gates[i]
        # The HME gate fails a whole range when any day did not reconcile. If that
        # is the ONLY failure, serve the days that DID reconcile and name the
        # excluded ones; any other failure still withholds everything.
        reasons = list(g.get("blocking_reasons") or [])
        only_recon = bool(reasons) and all(
            r.startswith("HME cross-source reconciliation failed") for r in reasons)
        excluded = ({str(x) for x in (g.get("hme") or {}).get("unreconciled_days") or []}
                    if only_recon else set())
        ok = bool(g.get("analysis_permitted")) or only_recon
        days = [d for d in req_days
                if (i, d) in rows and d.isoformat() not in excluded] if ok else []
        rs = [rows[(i, d)] for d in days]
        cars = sum(int(r["drive_thru_cars"] or 0) for r in rs)

        def wavg(key):
            pairs = [(r[key], int(r["drive_thru_cars"] or 0)) for r in rs
                     if r[key] is not None and r["drive_thru_cars"]]
            w = sum(c for _, c in pairs)
            return round(sum(float(v) * c for v, c in pairs) / w, 1) if w else None

        st = "ok" if ok and len(days) == len(req_days) and rs else (
            "partial" if rs else "unavailable")
        statuses.append(st)
        stores.append({
            "store_id": i, "store_name": names[i], "status": st,
            "daypart": "all (daily grain)",
            "avg_speed_of_service_seconds": wavg("lane_total_avg_seconds"),
            "avg_queue_seconds": wavg("lane_queue_avg_seconds"),
            "avg_total_2_seconds": wavg("lane_total_2_avg_seconds"),
            "drive_thru_cars": cars if rs else None,
            "disastrous_cars": (sum(int(r["disastrous_cars"] or 0) for r in rs)
                                if rs else None),
            "days_included": len(days), "days_requested": len(req_days),
            "days_without_data": ranges_of([d for d in req_days if (i, d) not in rows]),
            "days_excluded_unreconciled": sorted(excluded),
            "coverage": {**_gate_summary(g, "hme"),
                         "figures_limited_to_reconciled_days": bool(excluded)},
        })
    return {**base, "status": _overall(statuses), "daypart": "all (daily grain)",
            "stores": stores,
            "not_mapped_to_hme": uncovered,
            "notes": ["HME has no daypart or hourly grain. A missing HME day is unknown, "
                      "never zero seconds."]}
