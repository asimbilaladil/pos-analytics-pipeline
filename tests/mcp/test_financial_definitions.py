"""MCP backward compatibility of the explicit Revel sales metrics (2026-11 audit).

Pinned:
  * every pre-existing field keeps its name AND its value (pos_sales is still
    SUM(final_total) of REAL tickets, tax included; labor_pct still divides by it);
  * new fields are additive: gross before discounts, discounts, comps, net excl.
    tax, tax, total incl. tax, payments received, labor % of net sales excl. tax;
  * the arithmetic between them holds; definitions say which figures include tax;
  * a store-day without payment rows reports payments_received null, not 0.
"""
from datetime import date
from decimal import Decimal as D

import pytest
from fakes import FakeDB, days, labor_row, r365_row, revel_row

from mcp_server import analytics as a

D0 = date(2026, 11, 5)

# Cypress 2026-11-05..11 per-store totals from the audit, split over two days so
# the store grouping sums them.
CYPRESS_DAYS = [
    {"real_sales": D("32528.26"), "gross_sales_before_discounts": D("31723.79"),
     "discounts": D("832.14"), "comps": D("682.33"), "tax": D("2318.94")},
    {"real_sales": D("32528.26"), "gross_sales_before_discounts": D("31723.79"),
     "discounts": D("832.14"), "comps": D("682.34"), "tax": D("2318.95")},
]


def cy_row(store, d, parts, real=1500):
    r = revel_row(store, d, sales=str(parts["real_sales"]), real=real)
    r.update({k: v for k, v in parts.items() if k != "real_sales"})
    return r


@pytest.fixture
def db(monkeypatch):
    return FakeDB(monkeypatch)


def test_legacy_fields_unchanged_and_new_metrics_additive(db):
    ds = days(D0, 2)
    db.revel = [cy_row(54, d, p) for d, p in zip(ds, CYPRESS_DAYS)]
    db.payments = [{"establishment_id": 54, "business_date": ds[0],
                    "payment_amount": D("32528.27")}]
    r = a.sales_guest_counts([54], D0, ds[-1], "day")
    by = {x["date"]: x["revel"] for x in r["rows"]}
    d0 = by[ds[0].isoformat()]
    # legacy
    assert d0["pos_sales"] == 32528.26
    assert {"real_transactions", "comp_tickets", "empty_tickets"} <= set(d0)
    # additive + arithmetic
    assert d0["total_including_tax"] == d0["pos_sales"]
    assert d0["net_sales_excluding_tax"] == round(
        d0["gross_sales_before_discounts"] - d0["discounts"] - d0["comps"], 2)
    assert abs(d0["net_sales_excluding_tax"] + d0["tax"] - d0["total_including_tax"]) < 0.02
    assert d0["payments_received"] == 32528.27
    assert by[ds[1].isoformat()]["payments_received"] is None      # no rows: null, never 0
    assert r["definitions_version"] == a.DEFINITIONS_VERSION


def test_store_totals_match_the_audited_cypress_week(db):
    ds = days(D0, 2)
    db.revel = [cy_row(54, d, p) for d, p in zip(ds, CYPRESS_DAYS)]
    db.payments = [{"establishment_id": 54, "business_date": d,
                    "payment_amount": D("32528.275")} for d in ds]
    rv = a.sales_guest_counts([54], D0, ds[-1], "store")["rows"][0]["revel"]
    assert rv["pos_sales"] == 65056.52 == rv["total_including_tax"]
    assert rv["gross_sales_before_discounts"] == 63447.58
    assert rv["discounts"] == 1664.28 and rv["comps"] == 1364.67
    assert rv["net_sales_excluding_tax"] == 60418.63
    assert rv["tax"] == 4637.89
    assert rv["payments_received"] == 65056.55


def test_definitions_name_the_tax_treatment(db):
    db.revel = [revel_row(6, D0)]
    defs = a.sales_guest_counts([6], D0, D0, "day")["definitions"]
    assert "INCLUDES SALES TAX" in defs["pos_sales_revel"]
    for k in ("gross_sales_before_discounts_revel", "discounts_revel", "comps_revel",
              "net_sales_excluding_tax_revel", "tax_revel", "total_including_tax_revel",
              "payments_received_revel", "revel_definitions_note"):
        assert k in defs
    assert "verified against a revel ui report" in defs["revel_definitions_note"].lower()


def test_revel_labor_keeps_legacy_pct_and_adds_net_ex_tax(db):
    ds = days(D0, 2)
    db.revel = [cy_row(54, d, p) for d, p in zip(ds, CYPRESS_DAYS)]
    db.labor = [labor_row(54, d, cost="6000.00") for d in ds]
    r = a.labor_metrics([54], D0, ds[-1], "revel")
    row = r["rows"][0]
    # legacy semantics unchanged: denominator = REAL final_total (tax included)
    assert row["labor_pct"] == round(12000 / 65056.52 * 100, 2)
    assert row["sales_denominator"] == {"basis": "revel_real_ticket_sales",
                                        "includes_tax": True, "amount": 65056.52}
    # new, explicit
    assert row["labor_pct_net_sales_excluding_tax"] == round(12000 / 60418.63 * 100, 2)
    assert row["sales_denominator_net_excluding_tax"]["amount"] == 60418.63
    assert row["sales_denominator_net_excluding_tax"]["includes_tax"] is False
    assert row["labor_pct_net_sales_excluding_tax"] > row["labor_pct"]
    assert "INCLUDES SALES TAX" in r["definitions"]["sales_denominator"]
    assert r["definitions_version"] == a.DEFINITIONS_VERSION


def test_r365_labor_denominator_marked_ex_tax(db):
    ds = days(D0, 2)
    db.r365 = [r365_row(6, d) for d in ds]
    row = a.labor_metrics([6], D0, ds[-1], "r365")["rows"][0]
    assert row["sales_denominator"]["basis"] == "r365_net_sales"
    assert row["sales_denominator"]["includes_tax"] is False
    assert "labor_pct" in row and "labor_pct_net_sales_excluding_tax" not in row


def test_legacy_rows_without_new_columns_still_work(db):
    # A fake row from before the change (only real_sales): legacy fields intact.
    db.revel = [revel_row(6, D0, sales="100.00")]
    rv = a.sales_guest_counts([6], D0, D0, "day")["rows"][0]["revel"]
    assert rv["pos_sales"] == 100.0 and rv["total_including_tax"] == 100.0
