from datetime import date

import pytest
from fakes import (FakeDB, days, fin_row, gate, hme_row, labor_row, period, r365_period_row,
                   r365_row, revel_row)

from mcp_server import analytics as a
from mcp_server import security
from mcp_server.security import ToolInputError

D0 = date(2026, 10, 22)


@pytest.fixture
def db(monkeypatch):
    return FakeDB(monkeypatch)


# ── Tool 1 ───────────────────────────────────────────────────────────────────
def test_sales_by_day_keeps_sources_separate_and_null_is_not_zero(db):
    ds = days(D0, 3)
    db.revel = [revel_row(6, d) for d in ds]
    db.r365 = [r365_row(6, ds[0]), r365_row(6, ds[1])]            # day 3 absent from R365
    r = a.sales_guest_counts([6], D0, ds[-1], "day")
    assert r["status"] == "ok"
    by = {x["date"]: x for x in r["rows"]}
    assert by["2026-10-22"]["revel"]["pos_sales"] == 1000.0
    assert by["2026-10-22"]["r365"]["guest_count"] == 60 and by["2026-10-22"]["r365"]["check_count"] == 55
    assert by["2026-10-24"]["r365"] is None                         # missing -> null, never 0
    assert r["stores"][0]["r365"]["days_without_data"] == ["2026-10-24"]
    assert "guest_count_r365" in r["definitions"] and "real_transactions_revel" in r["definitions"]
    assert r["period"]["timezone"] == "America/Chicago"


def test_sales_by_store_sums_only_covered_days(db):
    ds = days(D0, 3)
    db.revel = [revel_row(6, d, sales="100.00", real=10) for d in ds]
    db.r365 = [r365_row(6, ds[0], net="500.00")]
    r = a.sales_guest_counts([6], D0, ds[-1], "store")
    row = r["rows"][0]
    assert row["revel"]["pos_sales"] == 300.0 and row["revel"]["real_transactions"] == 30
    assert row["r365"]["net_sales"] == 500.0 and row["r365"]["days_included"] == 1
    assert row["r365"]["days_requested"] == 3


def test_failed_revel_gate_withholds_figures(db):
    ds = days(D0, 2)
    db.revel = [revel_row(6, d) for d in ds]
    db.gate_for("revel", 6, gate(False, ["reconciliation FAILED: delta 3.2%"]))
    r = a.sales_guest_counts([6], D0, ds[-1], "store")
    assert r["rows"][0]["revel"] is None
    assert r["stores"][0]["revel"]["figures_withheld"] is True
    assert r["stores"][0]["revel"]["blocking_reasons"]


def test_gate_that_cannot_be_evaluated_withholds_not_guesses(db, monkeypatch):
    db.revel = [revel_row(6, D0)]

    def boom(*a_):
        raise RuntimeError("db down: password=hunter2")
    monkeypatch.setattr(a, "_gate", boom)
    r = a.sales_guest_counts([6], D0, D0, "day")
    assert r["status"] == "unavailable"
    assert "hunter2" not in str(r)


def test_missing_revel_days_are_reported_as_gaps(db):
    db.revel = [revel_row(6, D0)]
    r = a.sales_guest_counts([6], D0, days(D0, 3)[-1], "day")
    assert r["status"] == "partial"
    assert r["stores"][0]["revel"]["days_without_data"] == ["2026-10-23..2026-10-24"]


def test_only_requested_store_ids_reach_the_query(db):
    db.revel = [revel_row(6, D0), revel_row(7, D0, sales="9999.00")]
    r = a.sales_guest_counts([6], D0, D0, "day")
    assert {x["store_id"] for x in r["rows"]} == {6}
    assert "9999" not in str(r)
    assert all(c[2]["ids"] == [6] for c in db.calls if c[0] == "query")


def test_group_by_validated(db):
    with pytest.raises(ToolInputError):
        a.sales_guest_counts([6], D0, D0, "hour")


# ── Tool 2 ───────────────────────────────────────────────────────────────────
def test_labor_auto_uses_r365_when_fully_covered(db):
    ds = days(D0, 7)
    db.r365 = [r365_row(6, d, net="1000.00", labor_cost="180.00", hours="42.0") for d in ds]
    r = a.labor_metrics([6], D0, ds[-1], None)
    row = r["rows"][0]
    assert r["source"] == "r365" and row["source"] == "r365"
    assert row["labor_cost"] == 1260.0 and row["labor_hours"] == 294.0
    assert row["labor_pct"] == 18.0 and row["sales_denominator"]["basis"] == "r365_net_sales"
    assert row["sales_denominator"]["amount"] == 7000.0


def test_labor_auto_falls_back_to_one_source_never_mixing(db):
    ds = days(D0, 3)
    db.r365 = [r365_row(6, ds[0])]                         # R365 incomplete
    db.revel = [revel_row(6, d, sales="1000.00") for d in ds]
    db.labor = [labor_row(6, d, cost="200.00") for d in ds]
    r = a.labor_metrics([6], D0, ds[-1], None)
    assert r["source"] == "revel" and "never mixed" in r["source_selection"]
    row = r["rows"][0]
    assert row["sales_denominator"]["basis"] == "revel_real_ticket_sales"
    assert row["labor_pct"] == 20.0 and "estimated" in row["labor_cost_basis"]
    assert row["labor_cost"] == 600.0                       # not blended with R365's 200


def test_labor_pct_null_when_denominator_missing(db):
    ds = days(D0, 2)
    db.labor = [labor_row(6, d) for d in ds]
    db.revel = [revel_row(6, ds[0])]                        # one sales day missing
    r = a.labor_metrics([6], D0, ds[-1], "revel")
    row = r["rows"][0]
    assert row["days_included"] == 1 and row["status"] == "partial"


def test_labor_r365_without_sales_denominator_is_not_a_percentage(db):
    db.r365 = [r365_row(6, D0, net=None)]
    r = a.labor_metrics([6], D0, D0, "r365")
    assert r["rows"][0]["labor_pct"] is None and r["status"] != "ok"


def test_labor_unknown_source_rejected(db):
    with pytest.raises(ToolInputError):
        a.labor_metrics([6], D0, D0, "payroll")


def test_labor_no_data_is_unavailable_not_zero(db):
    r = a.labor_metrics([6], D0, D0, "r365")
    assert r["status"] == "unavailable"
    assert r["rows"][0]["labor_cost"] is None and r["rows"][0]["labor_hours"] is None


# ── Tool 3 ───────────────────────────────────────────────────────────────────
def _pnl_db(db, store=6, covered=True, status="COMPLETED", no_gas=True):
    db.periods = [period(status=status)]
    db.r365_period = [r365_period_row(store, covered=covered)]
    db.fin = [fin_row(store, "ELECTRICITY", "1200.00"), fin_row(store, "RENT", "12500.00"),
              fin_row(store, "WATER", "300.00"), fin_row(store, "REPAIRS_MAINTENANCE", "500.00"),
              fin_row(store, "GAS", "0.00", posting=not no_gas)]


def test_pnl_uses_fiscal_calendar_and_never_invents_food_cost_or_profit(db):
    _pnl_db(db)
    r = a.pnl_food_cost([6], 2026, 10, "summary")
    p = r["period"]
    assert (p["start_date"], p["end_date"], p["days"]) == ("2026-09-10", "2026-10-07", 28)
    assert "13 periods x 28 days" in p["calendar"]
    s = r["stores"][0]
    assert s["net_sales"]["amount"] == 100000.0
    assert s["labor"]["cost"] == 19000.0 and s["labor"]["pct_of_net_sales"] == 19.0
    assert s["food_cost"]["status"] == "unavailable" and "COGS" in s["food_cost"]["missing_dependency"]
    assert s["operating_profit"]["status"] == "unavailable"
    assert r["status"] == "partial"                         # can never be 'ok'
    assert "Unapproved" in r["approval_basis"]


def test_no_posting_is_not_zero_and_subtotal_is_labelled(db):
    db.periods = [period()]
    db.r365_period = [r365_period_row(6)]
    db.fin = [fin_row(6, "ELECTRICITY", "1200.00"), fin_row(6, "GAS", "0.00", posting=False)]
    r = a.pnl_food_cost([6], 2026, 10, "pnl")
    s = r["stores"][0]
    cats = s["verified_expense_categories"]
    assert cats["GAS"] == {"status": "NO_POSTING", "amount": None}
    assert cats["WATER"] == {"status": "unavailable", "amount": None}
    sub = s["verified_expense_subtotal"]
    assert sub["amount"] == 1200.0 and sub["includes"] == ["ELECTRICITY"]
    assert "NOT total operating expense" in sub["warning"]
    assert "GAS" in sub["no_posting"]


def test_a_zero_dollar_posting_is_distinguished_from_no_posting(db):
    db.periods = [period()]
    db.r365_period = [r365_period_row(6)]
    db.fin = [fin_row(6, "WATER", "0.00", posting=True)]
    cats = a.pnl_food_cost([6], 2026, 10, "pnl")["stores"][0]["verified_expense_categories"]
    assert cats["WATER"]["status"] == "posted" and cats["WATER"]["amount"] == 0.0


def test_food_cost_report_is_unavailable_with_dependency_and_no_estimate(db):
    _pnl_db(db)
    r = a.pnl_food_cost([6], 2026, 10, "food_cost")
    assert r["status"] == "unavailable"
    rep = r["stores"][0]["report"]
    assert rep["food_cost"]["status"] == "unavailable"
    assert "not estimated" in rep["food_cost"]["not_substituted"]
    assert "food_cost_pct" not in str(r)


@pytest.mark.parametrize("fy,pn", [(2025, 10), (2028, 1)])
def test_unknown_fiscal_year_rejected(db, fy, pn):
    db.periods = [period()]
    with pytest.raises(ToolInputError) as e:
        a.pnl_food_cost([6], fy, pn, "summary")
    assert "fiscal calendar" in str(e.value)


@pytest.mark.parametrize("bad", [0, 14, -1, "P0", "P14", "abc", None, 99.0, 3.5, True])
def test_invalid_period_numbers_rejected(db, bad):
    with pytest.raises(ToolInputError):
        a.pnl_food_cost([6], 2026, bad, "summary")


def test_period_accepts_p_prefix(db):
    _pnl_db(db)
    assert a.pnl_food_cost([6], 2026, "P10", "summary")["period"]["period_label"] == "P10"


def test_calendar_months_are_never_substituted(db):
    db.periods = [period(n=31)]                  # a 31-day "period" must be refused
    with pytest.raises(ToolInputError):
        a.pnl_food_cost([6], 2026, 10, "summary")


def test_current_or_partially_covered_period_is_partial(db):
    _pnl_db(db, covered=False, status="CURRENT")
    s = a.pnl_food_cost([6], 2026, 10, "summary")["stores"][0]
    assert s["status"] == "partial" and s["net_sales"]["period_fully_covered"] is False


def test_future_period_returns_no_figures(db):
    _pnl_db(db, status="FUTURE")
    s = a.pnl_food_cost([6], 2026, 10, "summary")["stores"][0]
    assert s["net_sales"]["amount"] is None and s["status"] == "unavailable"


def test_failed_financial_gate_withholds_expenses_but_keeps_sales(db):
    _pnl_db(db)
    db.gate_for("r365_financial", 6, gate(False, ["R365 FINANCIAL refresh failed"]))
    s = a.pnl_food_cost([6], 2026, 10, "pnl")["stores"][0]
    assert s["net_sales"]["amount"] == 100000.0
    assert all(c["amount"] is None for c in s["verified_expense_categories"].values())


def test_unreconciled_transaction_detail_is_reported(db):
    _pnl_db(db)
    db.gate_for("r365_financial_detail", 6, gate(False, ["R365 FINANCIAL DETAIL lines do not reconcile"]))
    s = a.pnl_food_cost([6], 2026, 10, "pnl")["stores"][0]
    assert s["coverage"]["transaction_reconciliation"]["gate"] == "FAIL"


def test_report_type_validated(db):
    with pytest.raises(ToolInputError):
        a.pnl_food_cost([6], 2026, 10, "balance_sheet")
    with pytest.raises(ToolInputError):
        a.pnl_food_cost([6], "2026", 10, "summary")


def test_pnl_no_network_total_is_returned(db):
    _pnl_db(db, 6)
    r = a.pnl_food_cost([6], 2026, 10, "summary")
    assert "network" not in str(r).lower().replace("network-wide", "")


# ── Tool 4 ───────────────────────────────────────────────────────────────────
def test_hme_weighted_average_and_unmapped_store(db):
    ds = days(D0, 2)
    db.hme_mapped = {6}
    db.hme = [hme_row(6, ds[0], cars=100, total=100), hme_row(6, ds[1], cars=300, total=200)]
    r = a.drive_thru_speed([6, 48], D0, ds[-1], None)
    s6 = next(s for s in r["stores"] if s["store_id"] == 6)
    assert s6["avg_speed_of_service_seconds"] == 175.0          # (100*100+200*300)/400
    assert s6["drive_thru_cars"] == 400 and s6["status"] == "ok"
    s48 = next(s for s in r["stores"] if s["store_id"] == 48)
    assert s48["status"] == "unavailable" and "no verified HME mapping" in s48["reason"]
    assert "avg_speed_of_service_seconds" not in s48            # never 0 seconds
    assert r["not_mapped_to_hme"] == [48] and r["status"] == "partial"
    assert "NOT Revel POS orders" in r["definitions"]["drive_thru_cars"]


def test_hme_daypart_is_unavailable_and_not_approximated(db):
    db.hme_mapped = {6}
    db.hme = [hme_row(6, D0)]
    r = a.drive_thru_speed([6], D0, D0, "dinner")
    assert r["status"] == "unavailable" and r["daypart"] == "dinner"
    assert r["stores"] == [] and "daypart" in r["missing_dependency"]
    assert not [c for c in db.calls if c[0] == "query"]          # no data was even read


def test_hme_unreconciled_days_excluded_not_served(db):
    ds = days(D0, 3)
    db.hme_mapped = {6}
    db.hme = [hme_row(6, d, total=100 + 100 * i) for i, d in enumerate(ds)]
    db.gate_for("hme", 6, gate(False, ["HME cross-source reconciliation failed on 2026-10-23; "
                                       "drive-thru figures for those dates are not reconciled"],
                               hme={"unreconciled_days": ["2026-10-23"]}))
    s = a.drive_thru_speed([6], D0, ds[-1], None)["stores"][0]
    assert s["days_included"] == 2 and s["days_excluded_unreconciled"] == ["2026-10-23"]
    assert s["avg_speed_of_service_seconds"] == 200.0            # days 1 and 3 only
    assert s["status"] == "partial"


def test_hme_other_gate_failures_still_withhold_everything(db):
    db.hme_mapped = {6}
    db.hme = [hme_row(6, D0)]
    db.gate_for("hme", 6, gate(False, ["HME is missing one or more VERIFIED stores on 2026-10-22"]))
    s = a.drive_thru_speed([6], D0, D0, None)["stores"][0]
    assert s["status"] == "unavailable" and s["avg_speed_of_service_seconds"] is None


def test_hme_missing_days_reported_not_zero(db):
    db.hme_mapped = {6}
    db.hme = [hme_row(6, D0)]
    s = a.drive_thru_speed([6], D0, days(D0, 4)[-1], None)["stores"][0]
    assert s["status"] == "partial" and s["days_without_data"] == ["2026-10-23..2026-10-25"]


def test_hme_stale_source_is_surfaced(db):
    db.hme_mapped = {6}
    db.hme = [hme_row(6, D0)]
    db.gate_for("hme", 6, gate(True, hme={"freshness_status": "STALE",
                                          "freshness_reason": "retry window passed",
                                          "latest_business_date": "2026-10-13",
                                          "unreconciled_days": []}))
    cov = a.drive_thru_speed([6], D0, D0, None)["stores"][0]["coverage"]
    assert cov["freshness"]["freshness_status"] == "STALE"


# ── ranges / validation (shared) ─────────────────────────────────────────────
@pytest.mark.parametrize("s,e,msg", [
    ("2026-10-01", "2026-12-31", "maximum"),
    ("2026-10-05", "2026-10-01", "before start_date"),
    ("10/01/2026", "2026-10-02", "YYYY-MM-DD"),
    ("2026-02-30", "2026-03-02", "valid calendar"),
    ("2025-01-01", "2025-01-31", "before 2026-01-01"),
    ("2999-01-01", "2999-01-02", "future"),
    ("2026-10-01; DROP TABLE x", "2026-10-02", "YYYY-MM-DD"),
])
def test_invalid_or_large_ranges_rejected(s, e, msg):
    with pytest.raises(ToolInputError) as ex:
        security.parse_range(s, e)
    assert msg in str(ex.value)


def test_range_limit_boundary():
    security.parse_range("2026-09-01", "2026-11-01")            # 62 days
    with pytest.raises(ToolInputError):
        security.parse_range("2026-09-01", "2026-11-02")        # 63 days


def test_store_id_validation():
    from mcp_server.keystore import Principal
    p = Principal("k", "l", frozenset(), frozenset({6, 7}), 10, 10)
    for bad in ("6", [True], ["a"], [[6]], 6):
        with pytest.raises(ToolInputError):
            security.resolve_stores(p, bad)
    assert security.resolve_stores(p, [7, 6, 6]) == [6, 7]


def test_num_keeps_none():
    assert security.num(None) is None and security.num(0) == 0.0
