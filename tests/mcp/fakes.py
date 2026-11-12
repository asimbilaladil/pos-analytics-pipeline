"""Fixture data standing in for the read-only analytics views."""
from datetime import date, timedelta
from decimal import Decimal as D

from mcp_server import analytics


def days(start, n):
    return [start + timedelta(days=i) for i in range(n)]


def gate(ok=True, reasons=(), warns=(), **blocks):
    return {"analysis_permitted": ok, "blocking_reasons": list(reasons),
            "warnings": list(warns), **blocks}


class FakeDB:
    """Patches analytics._query/_gate. Rows are filtered by the bound `ids`
    parameter exactly as the real SQL does, and every call is recorded."""

    def __init__(self, monkeypatch):
        self.calls = []
        self.revel, self.r365, self.labor = [], [], []
        self.hme, self.hme_mapped = [], set()
        self.periods, self.r365_period, self.fin = [], [], []
        self.gates = {}          # (domain, store) -> gate dict
        monkeypatch.setattr(analytics, "_query", self._query)
        monkeypatch.setattr(analytics, "_gate", self._gate)

    def gate_for(self, domain, store, g):
        self.gates[(domain, store)] = g

    def _gate(self, est, start, end_x, domains):
        self.calls.append(("gate", est, tuple(domains)))
        d = domains[0]
        if (d, est) in self.gates:
            return self.gates[(d, est)]
        base = {"revel": {"reconciliation": {"status": "PASS", "delta_pct": 0.01},
                          "freshness": {"days_with_data": 7, "missing_business_days": 0}},
                "r365": {"r365": {"freshness_status": "CURRENT", "latest_daily_date": "2026-10-28"}},
                "hme": {"hme": {"freshness_status": "CURRENT", "unreconciled_days": []}},
                "r365_financial": {"r365_financial": {"refresh_age_hours": 3}},
                "r365_financial_detail": {"r365_financial_detail": {"approved_lines_in_scope": 10}},
                }[d]
        return gate(**base)

    def _query(self, sql, params):
        self.calls.append(("query", sql.split("FROM")[1].split()[0] if "FROM" in sql else sql[:30], params))
        ids = set(params.get("ids") or [])

        def pick(rows):
            return [r for r in rows if r["establishment_id"] in ids]
        if ("DISTINCT establishment_id FROM v_hme_store_daily_llm" in sql
                or "FROM v_hme_store_status_llm" in sql):
            # The maintained HME mapping (VERIFIED establishments).
            return [{"establishment_id": i} for i in self.hme_mapped]
        if "FROM v_orders_classified" in sql:
            return pick(self.revel)
        if "FROM v_r365_store_daily_llm" in sql:
            return pick(self.r365)
        if "FROM v_labor_daily_context" in sql:
            return pick(self.labor)
        if "FROM v_hme_store_daily_llm" in sql:
            return pick(self.hme)
        if "FROM v_ayg_business_periods_llm" in sql:
            return [p for p in self.periods
                    if p["fiscal_year"] == params["fy"] and p["period_number"] == params["pn"]]
        if "FROM v_r365_store_period_llm" in sql:
            return pick(self.r365_period)
        if "FROM v_r365_financial_period_llm" in sql:
            return pick(self.fin)
        raise AssertionError("unexpected query: " + sql[:80])


def revel_row(store, d, sales="1000.00", real=50, comp=2, empty=3):
    return {"establishment_id": store, "business_date": d, "real_transactions": real,
            "comp_tickets": comp, "empty_tickets": empty, "real_sales": D(sales)}


def r365_row(store, d, net="1100.00", labor_cost="200.00", hours="40.0", guests=60, checks=55):
    return {"establishment_id": store, "business_date": d, "day_status": "NORMAL",
            "net_sales": None if net is None else D(net), "gross_sales": D("1120.00"),
            "guest_count": guests, "check_count": checks,
            "labor_hours": None if hours is None else D(hours),
            "labor_cost": None if labor_cost is None else D(labor_cost)}


def labor_row(store, d, hours="45.5", cost="230.00"):
    return {"establishment_id": store, "business_date": d, "labor_hours": D(hours),
            "estimated_labor_cost": D(cost), "missing_wage_shift_count": 0, "open_shift_count": 0}


def hme_row(store, d, cars=100, total=200, queue=50, total2=190):
    return {"establishment_id": store, "business_date": d, "drive_thru_cars": cars,
            "regular_cars": cars - 5, "disastrous_cars": 5, "lane_total_avg_seconds": total,
            "lane_queue_avg_seconds": queue, "lane_total_2_avg_seconds": total2}


def period(fy=2026, pn=10, start=date(2026, 9, 10), status="COMPLETED", n=28):
    return {"fiscal_year": fy, "period_number": pn, "period_label": f"P{pn}",
            "start_date": start, "end_date": start + timedelta(days=n - 1),
            "period_status": status, "days_total": n, "days_elapsed": n}


def r365_period_row(store, net="100000.00", labor="19000.00", covered=True):
    return {"establishment_id": store, "weeks_in_period": 4, "weeks_loaded": 4 if covered else 3,
            "weeks_complete": 4, "period_fully_covered": covered, "first_week_start": date(2026, 9, 10),
            "last_week_end": date(2026, 10, 7), "net_sales": D(net), "gross_sales": D(net),
            "labor_hours": D("1500.0"), "labor_cost": D(labor),
            "labor_pct": D(labor) / D(net) * 100}


def fin_row(store, cat, amount="1000.00", posting=True):
    return {"establishment_id": store, "analytics_category": cat,
            "expense_amount": D(amount), "has_posting": posting, "data_status": "COMPLETE",
            "source_quality": "API", "transaction_count": 3}
