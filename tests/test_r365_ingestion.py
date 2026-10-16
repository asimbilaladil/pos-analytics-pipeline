#!/usr/bin/env python3
"""Deterministic tests for R365 ingestion (r365_load / r365_weekly_ingest).

Run:  venv/bin/python tests/test_r365_ingestion.py     (exit 0 = pass)

Payloads are SYNTHETIC and built here, shaped exactly like the proven
/weekly-data contract, so nothing depends on the network or on archived files.
Database checks run inside one transaction that is ROLLED BACK: nothing is
written, and the real facts are never touched.
"""
import copy
import datetime
import hashlib
import json
import os
import stat
import sys
import tempfile
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from dotenv import load_dotenv

load_dotenv(".env", override=True)
import r365_load as L  # noqa: E402
import r365_weekly_ingest as I  # noqa: E402

D = datetime.date
UTC = datetime.timezone.utc
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


def raises(fn, exc=L.SourceSchemaError):
    try:
        fn()
    except exc as e:
        return str(e) or True
    return False


# ---------------------------------------------------------------------------
# Synthetic payload builder (numbers internally consistent, like the source)
# ---------------------------------------------------------------------------
A_ID, B_ID = "83a5757d-6ffc-41c1-875a-5adb6e693e99", "fe62609a-7f59-40e5-a023-fce3636191f3"
MAPPING = {
    A_ID: {"r365_store_name": "LCF Airtex", "establishment_id": 32, "status": "VERIFIED",
           "active": True, "opening_date": None},
    B_ID: {"r365_store_name": "LCF Beaumont", "establishment_id": 14, "status": "VERIFIED",
           "active": True, "opening_date": None},
}


def day_row(sid, name, date, net, forecast=Decimal("1000")):
    net = Decimal(net)
    checks, guests = (50, 48) if net else (0, 0)
    labor = Decimal("200.5") if net else Decimal("42.5")
    disc = Decimal("10") if net else Decimal(0)
    has_fc = forecast is not None and forecast > 0
    return {
        "locationId": sid, "locationName": name, "date": date,
        "netSales": net, "grossSales": net, "guestCount": guests, "checkCount": checks,
        "avgCheck": (net / checks) if checks else 0, "avgPerGuest": (net / guests) if guests else 0,
        "totalLaborHours": Decimal("12.25"), "totalLaborAmount": labor,
        "laborPct": round(labor / net, 2) if net else 0,
        "totalDeposit": Decimal("100"), "expectedCash": Decimal("99.5"),
        "paidInTotal": 0, "paidOutTotal": Decimal("1.25"), "overShort": Decimal("0.5"),
        "discounts": Decimal("6"), "comps": Decimal("3"), "coupons": Decimal("1"),
        "totalDiscounts": disc, "discountPctOfSales": (disc / net) if net else 0,
        "gcardRedeem": 0, "gcardSold": Decimal("25"), "declaredTips": 0,
        "creditCardTips": Decimal("4.4"), "voidCount": 1, "voidAmount": Decimal("7.5"),
        "forecastedSales": forecast if forecast is not None else 0,
        "salesVsForecast": (net - forecast) if has_fc else None,
        "salesVsForecastPct": ((net - forecast) / forecast) if has_fc else None,
    }


def week_row(sid, name, days):
    s = lambda k: sum((Decimal(d[k]) for d in days), Decimal(0))  # noqa: E731
    net, checks, guests = s("netSales"), int(s("checkCount")), int(s("guestCount"))
    labor, hours = s("totalLaborAmount"), s("totalLaborHours")
    fc = [d for d in days if d["forecastedSales"] and d["forecastedSales"] > 0]
    fsum = sum((d["forecastedSales"] for d in fc), Decimal(0)) if fc else None
    base = sum((Decimal(d["netSales"]) for d in fc), Decimal(0))
    w = {"locationId": sid, "locationName": name,
         "netSales": net, "grossSales": net, "guestCount": guests, "checkCount": checks,
         "avgCheck": net / checks if checks else 0, "avgPerGuest": net / guests if guests else 0,
         "totalLaborHours": hours, "totalLaborAmount": labor,
         "laborPct": labor / net if net else 0, "salesPerLaborHour": net / hours if hours else 0,
         "totalDeposit": s("totalDeposit"), "overShort": s("overShort"),
         "paidInTotal": s("paidInTotal"), "paidOutTotal": s("paidOutTotal"),
         "discounts": s("discounts"), "comps": s("comps"), "coupons": s("coupons"),
         "totalDiscounts": s("totalDiscounts"),
         "discountPctOfSales": s("totalDiscounts") / net if net else 0,
         "gcardRedeem": s("gcardRedeem"), "gcardSold": s("gcardSold"),
         "declaredTips": s("declaredTips"), "creditCardTips": s("creditCardTips"),
         "voidCount": int(s("voidCount")), "voidAmount": s("voidAmount"),
         "forecastedSales": fsum, "forecastSalesBase": base,
         "salesVsForecast": (base - fsum) if fsum else None,
         "salesVsForecastPct": ((base - fsum) / fsum) if fsum else None,
         "daysWithData": len(days), "days": days}
    return w


def payload(week_start, stores=((A_ID, "LCF Airtex"), (B_ID, "LCF Beaumont")),
            net=Decimal("1234.56")):
    locs = []
    for sid, name in stores:
        days = [day_row(sid, name, (week_start + datetime.timedelta(days=i)).isoformat(),
                        net + i) for i in range(7)]
        locs.append(week_row(sid, name, days))
    fleet = {"netSales": sum(l["netSales"] for l in locs),
             "guestCount": sum(l["guestCount"] for l in locs),
             "checkCount": sum(l["checkCount"] for l in locs),
             "locationCount": len(locs)}
    return {"week": "x", "weekStart": week_start.isoformat(),
            "weekEnd": (week_start + datetime.timedelta(days=6)).isoformat(),
            "complete": True, "missingDays": [], "warning": None,
            "fleet": fleet, "locations": locs}


def rebuild(p):
    """Recompute weekly rows + fleet after a test mutates days."""
    for i, l in enumerate(p["locations"]):
        present = [d for d in l["days"] if not d.get("noData")]
        days_with = len([d for d in present
                         if (l["locationName"], d["date"]) not in
                         {(m["location"], m["date"]) for m in p["missingDays"]}])
        w = week_row(l["locationId"], l["locationName"],
                     [d for d in present if (l["locationName"], d["date"]) not in
                      {(m["location"], m["date"]) for m in p["missingDays"]}])
        w["days"] = l["days"]
        w["daysWithData"] = days_with
        p["locations"][i] = w
    p["fleet"]["netSales"] = sum(l["netSales"] for l in p["locations"])
    p["fleet"]["guestCount"] = sum(l["guestCount"] for l in p["locations"])
    p["fleet"]["checkCount"] = sum(l["checkCount"] for l in p["locations"])
    return p


WK = D(2026, 10, 8)

# ---------------------------------------------------------------------------
# 1 -- Thursday -> Wednesday arithmetic, scheduled targets, Chicago/DST
# ---------------------------------------------------------------------------
print("1. business-week arithmetic")
check("every weekday maps to its Thursday week start",
      all(L.week_start_for(D(2026, 10, 8) + datetime.timedelta(days=i)) == D(2026, 10, 8)
          for i in range(7)))
check("Wednesday 10-14 still belongs to the 10-08 week",
      L.week_start_for(D(2026, 10, 14)) == D(2026, 10, 8))
check("Thursday 10-15 starts a new week", L.week_start_for(D(2026, 10, 15)) == D(2026, 10, 15))
check("week_end = week_start + 6 (a Wednesday)",
      L.week_end_for(WK) == D(2026, 10, 14) and L.week_end_for(WK).isoweekday() == 3)
check("run on Thursday 2026-10-15 targets the previous completed week 10-08..10-14",
      I.latest_completed_week_start(D(2026, 10, 15)) == D(2026, 10, 8))
check("run on Wednesday 10-14 does NOT target the in-progress 10-08 week",
      I.latest_completed_week_start(D(2026, 10, 14)) == D(2026, 10, 1))
check("scheduled run re-fetches the latest TWO completed weeks, oldest first",
      I.scheduled_targets(D(2026, 10, 15)) == [D(2026, 10, 1), D(2026, 10, 8)])
check("scheduled overlap never reaches before the 2026-08-13 boundary",
      I.scheduled_targets(D(2026, 8, 20)) == [D(2026, 8, 13)])
check("backfill = every Thursday from the boundary to the latest completed week",
      I.backfill_targets(D(2026, 10, 16)) ==
      [D(2026, 8, 13) + datetime.timedelta(days=7 * i) for i in range(9)])

# The timer fires Thu 20:00 Chicago = Fri 01:00 UTC (CDT) / Fri 02:00 UTC (CST).
cdt_fire = datetime.datetime(2026, 10, 23, 1, 0, tzinfo=UTC)
cst_fire = datetime.datetime(2026, 11, 6, 2, 0, tzinfo=UTC)
check("CDT: Fri 01:00 UTC is Thursday 2026-10-22 in Chicago (not the UTC Friday)",
      I.chicago_today(cdt_fire) == D(2026, 10, 22))
check("CDT fire targets weeks 10-08 and 10-15",
      I.scheduled_targets(I.chicago_today(cdt_fire)) == [D(2026, 10, 8), D(2026, 10, 15)])
check("CST: Fri 02:00 UTC is Thursday 2026-11-05 in Chicago",
      I.chicago_today(cst_fire) == D(2026, 11, 5))
check("CST fire targets weeks 10-22 and 10-29",
      I.scheduled_targets(I.chicago_today(cst_fire)) == [D(2026, 10, 22), D(2026, 10, 29)])
check("a UTC calendar would have been wrong: the UTC date at the CDT fire is Friday",
      cdt_fire.date().isoweekday() == 5)

check("refuses a non-Thursday", "not a Thursday" in (I.check_target(D(2026, 10, 9), D(2026, 10, 20)) or ""))
check("refuses a pre-boundary week (2026-08-06)",
      "before the approved" in (I.check_target(D(2026, 8, 6), D(2026, 10, 20)) or ""))
check("refuses an incomplete week (ends today or later)",
      "not complete" in (I.check_target(D(2026, 10, 15), D(2026, 10, 21)) or ""))
check("accepts the week the day after its Wednesday",
      I.check_target(D(2026, 10, 8), D(2026, 10, 15)) is None)

# ---------------------------------------------------------------------------
# 2 -- parsing: exact decimals, every field persisted, percentage convention
# ---------------------------------------------------------------------------
print("2. parsing and field coverage")
exact = L.parse_payload(b'{"a": 11443.755799999999, "b": 0.20995856330530707, "c": 3359}')
check("JSON floats parse to exact Decimal (no float rounding)",
      exact["a"] == Decimal("11443.755799999999") and exact["b"] == Decimal("0.20995856330530707"))
check("JSON integers stay integers", exact["c"] == 3359 and isinstance(exact["c"], int))

check("daily field map covers all 28 daily source metrics", len(L.DAILY_FIELDS) == 28)
check("weekly field map covers all 30 weekly source metrics", len(L.WEEKLY_FIELDS) == 30)
src_day_keys = set(day_row(A_ID, "x", "2026-10-08", 1).keys()) - L.DAILY_IDENTITY
check("every daily source key is mapped (none silently dropped)",
      src_day_keys == {k for k, _, _ in L.DAILY_FIELDS})
src_week_keys = set(week_row(A_ID, "x", [day_row(A_ID, "x", "2026-10-08", 1)]).keys()) - L.WEEKLY_IDENTITY
check("every weekly source key is mapped", src_week_keys == {k for k, _, _ in L.WEEKLY_FIELDS})
check("expectedCash is daily-only",
      "expected_cash" in {c for _, c, _ in L.DAILY_FIELDS}
      and "expected_cash" not in {c for _, c, _ in L.WEEKLY_FIELDS})
check("salesPerLaborHour / forecastSalesBase / daysWithData are weekly-only",
      {"sales_per_labor_hour", "forecast_sales_base", "days_with_data"}
      <= {c for _, c, _ in L.WEEKLY_FIELDS}
      and not ({"sales_per_labor_hour", "forecast_sales_base", "days_with_data"}
               & {c for _, c, _ in L.DAILY_FIELDS}))
check("source ratios land in *_fraction columns (stored 0-1, never as points)",
      {c for _, c, _ in L.WEEKLY_FIELDS if "pct" in c}
      == {"labor_pct_fraction", "discount_pct_fraction", "sales_vs_forecast_pct_fraction"})

plan = L.plan_week(payload(WK), WK, MAPPING)
check("clean synthetic week -> COMPLETE", plan["status"] == "COMPLETE", plan["status"])
check("14 daily + 2 weekly rows", len(plan["daily_rows"]) == 14 and len(plan["weekly_rows"]) == 2)
r0 = plan["weekly_rows"][0]
check("labor_pct_fraction kept as the source fraction (0-1), unscaled",
      r0["labor_pct_fraction"] == r0["labor_cost"] / r0["net_sales"] and r0["labor_pct_fraction"] < 1)
check("rows are keyed by canonical establishment_id",
      {r["establishment_id"] for r in plan["daily_rows"]} == {32, 14})
check("daily week_start is the Thursday", all(r["week_start"] == WK for r in plan["daily_rows"]))
check("reconciliation passes with consistent data", plan["reconciliation_ok"],
      str(plan["reconciliation"]["failed"][:2]))

# ---------------------------------------------------------------------------
# 3 -- missing != zero, partial weeks, day status
# ---------------------------------------------------------------------------
print("3. completeness and day status")
p = payload(WK)
p["missingDays"] = [{"location": "LCF Beaumont", "date": "2026-10-10"}]
p["complete"] = False
for d in p["locations"][1]["days"]:
    if d["date"] == "2026-10-10":
        d.update({k: 0 for k in d if k not in ("locationId", "locationName", "date")})
        d.update(forecastedSales=None, salesVsForecast=None, salesVsForecastPct=None, noData=True)
plan = L.plan_week(rebuild(p), WK, MAPPING)
check("a missing source day produces NO fact row (absence, not zero)",
      not any(r["establishment_id"] == 14 and r["business_date"] == D(2026, 10, 10)
              for r in plan["daily_rows"]))
check("missing day -> week PARTIAL", plan["status"] == "PARTIAL")
check("missing_store_days counts it", plan["missing_store_days"] == 1)
check("store with 6 days is not counted as a full week", plan["stores_with_full_week"] == 1)

p = payload(WK)
p["locations"][0]["days"][2]["noData"] = True     # noData flag alone, no missingDays entry
plan = L.plan_week(rebuild(p), WK, MAPPING)
check("noData flag alone also excludes the day",
      len([r for r in plan["daily_rows"] if r["establishment_id"] == 32]) == 6)

p = payload(WK)
p["locations"][0]["daysWithData"] = 6
plan = L.plan_week(p, WK, MAPPING)
check("weekly daysWithData < 7 -> PARTIAL even with 7 daily rows", plan["status"] == "PARTIAL")

p = payload(WK)
p["locations"] = p["locations"][:1]
p["fleet"]["locationCount"] = 1
plan = L.plan_week(rebuild(p), WK, MAPPING)
check("a mapped store absent from the source -> PARTIAL and listed",
      plan["status"] == "PARTIAL" and plan["missing_stores"][0]["r365_store_name"] == "LCF Beaumont")
check("absent store gets no zero rows",
      not any(r["establishment_id"] == 14 for r in plan["daily_rows"] + plan["weekly_rows"]))

zero = day_row(B_ID, "LCF Beaumont", "2026-09-03", 0)
zrow = L._extract(zero, L.DAILY_FIELDS, "t")
check("zero-trading day with no confirmed opening date -> CLOSED",
      L.classify_day(zrow, D(2026, 9, 3), None) == "CLOSED")
check("zero-trading day before a CONFIRMED opening date -> PRE_OPENING",
      L.classify_day(zrow, D(2026, 9, 3), D(2026, 9, 10)) == "PRE_OPENING")
check("zero-trading day after the opening date -> CLOSED",
      L.classify_day(zrow, D(2026, 9, 3), D(2026, 9, 1)) == "CLOSED")
check("a trading day -> NORMAL",
      L.classify_day(L._extract(day_row(A_ID, "x", "2026-10-08", 5), L.DAILY_FIELDS, "t"),
                     D(2026, 10, 8), None) == "NORMAL")
check("zero source ratios on a CLOSED day are preserved verbatim in the fact row",
      zrow["labor_pct_fraction"] == 0 and zrow["labor_cost"] == Decimal("42.5"))

# Forecast: forecast present (0 placeholder) + salesVsForecast null -> no crash, no %
nofc = day_row(A_ID, "x", "2026-10-08", 500, forecast=Decimal(0))
nrow = L._extract(nofc, L.DAILY_FIELDS, "t")
check("forecast=0 with salesVsForecast null parses without inventing a percentage",
      nrow["forecasted_sales"] == 0 and nrow["sales_vs_forecast_amount"] is None
      and nrow["sales_vs_forecast_pct_fraction"] is None)
p = payload(WK)
for d in p["locations"][0]["days"][:3]:
    d.update(forecastedSales=0, salesVsForecast=None, salesVsForecastPct=None)
plan = L.plan_week(rebuild(p), WK, MAPPING)
w = [r for r in plan["weekly_rows"] if r["establishment_id"] == 32][0]
check("weekly forecast_sales_base = net of forecast-covered days only",
      w["forecast_sales_base"] == sum(Decimal(d["netSales"]) for d in p["locations"][0]["days"][3:]))
check("partial-forecast week still reconciles", plan["reconciliation_ok"],
      str(plan["reconciliation"]["failed"][:2]))
check("a null required number is rejected, never coerced to 0",
      raises(lambda: L._num(None, "money", "x")))
check("a null nullable number stays None", L._num(None, "nullable", "x") is None)

# ---------------------------------------------------------------------------
# 4 -- mapping enforcement, no fuzzy matching
# ---------------------------------------------------------------------------
print("4. mapping enforcement")
p = payload(WK, stores=((A_ID, "LCF Airtex"), (B_ID, "LCF Beaumont"),
                        ("00000000-0000-0000-0000-000000000001", "LCF Airtex")))
plan = L.plan_week(p, WK, MAPPING)
check("unknown store id is NOT loaded even when its NAME equals a mapped store's",
      all(r["r365_store_id"] != "00000000-0000-0000-0000-000000000001"
          for r in plan["daily_rows"] + plan["weekly_rows"]))
check("unknown store is surfaced as UNKNOWN/unmapped",
      plan["unmapped_stores"] == [{"r365_store_id": "00000000-0000-0000-0000-000000000001",
                                   "r365_store_name": "LCF Airtex", "mapping_status": "UNKNOWN"}])
check("an unmapped store makes the week PARTIAL", plan["status"] == "PARTIAL")

m2 = copy.deepcopy(MAPPING)
m2[B_ID]["status"] = "CANDIDATE"
plan = L.plan_week(payload(WK), WK, m2)
check("a CANDIDATE (unverified) mapping does not load",
      not any(r["r365_store_id"] == B_ID for r in plan["daily_rows"]))
check("...and is reported with its status",
      plan["unmapped_stores"][0]["mapping_status"] == "CANDIDATE")
check("expected store count counts only ACTIVE VERIFIED mappings", plan["expected_store_count"] == 1)

p = payload(WK, stores=((A_ID, "LCF Airtex Renamed"), (B_ID, "LCF Beaumont")))
plan = L.plan_week(p, WK, MAPPING)
check("a renamed store still loads by id, with a warning",
      any(r["r365_store_id"] == A_ID for r in plan["daily_rows"])
      and any("name changed" in w for w in plan["schema_warnings"]))

# ---------------------------------------------------------------------------
# 5 -- reconciliation (daily sums vs the official weekly row)
# ---------------------------------------------------------------------------
print("5. reconciliation")
p = payload(WK)
p["locations"][0]["days"][0]["netSales"] += Decimal("5")
plan = L.plan_week(p, WK, MAPPING)
failed = {(f["store"], f["field"]) for f in plan["reconciliation"]["failed"]}
check("a daily/weekly net_sales disagreement is detected",
      ("LCF Airtex", "net_sales") in failed, str(failed))
check("an unreconciled week is PARTIAL, never COMPLETE", plan["status"] == "PARTIAL")
check("the OFFICIAL weekly value is loaded, not the daily sum",
      [r for r in plan["weekly_rows"] if r["establishment_id"] == 32][0]["net_sales"]
      == payload(WK)["locations"][0]["netSales"])
p = payload(WK)
p["locations"][0]["days"][0]["laborPct"] = Decimal("0.99")
check("ratios are never reconciled by summing (a wrong daily ratio is not an additive error)",
      L.plan_week(p, WK, MAPPING)["reconciliation_ok"])
check("ratio/average fields are excluded from the additive reconciliation list",
      not ({"avg_check", "avg_per_guest", "labor_pct_fraction", "discount_pct_fraction",
            "sales_per_labor_hour", "sales_vs_forecast_pct_fraction"} & set(L.ADDITIVE)))
p = payload(WK)
p["fleet"]["netSales"] += 1
check("fleet total disagreeing with the store sum is detected",
      not L.plan_week(p, WK, MAPPING)["reconciliation_ok"])

# ---------------------------------------------------------------------------
# 6 -- schema guards
# ---------------------------------------------------------------------------
print("6. schema guards")
p = payload(WK)
del p["locations"][0]["days"][0]["expectedCash"]
check("a missing source field fails the week closed",
      raises(lambda: L.plan_week(p, WK, MAPPING)))
p = payload(WK)
p["locations"][0]["days"][0]["brandNewMetric"] = 1
plan = L.plan_week(p, WK, MAPPING)
check("an unexpected NEW field warns but does not block",
      any("brandNewMetric" in w for w in plan["schema_warnings"]) and plan["status"] == "COMPLETE")
check("a response for a different week is rejected",
      raises(lambda: L.plan_week(payload(D(2026, 10, 1)), WK, MAPPING)))
p = payload(WK)
p["locations"][0]["days"][0]["date"] = "2026-10-20"
check("a day outside the requested week is rejected", raises(lambda: L.plan_week(p, WK, MAPPING)))
p = payload(WK)
p["locations"][0]["days"][1]["date"] = p["locations"][0]["days"][0]["date"]
check("a duplicate day is rejected", raises(lambda: L.plan_week(p, WK, MAPPING)))
p = payload(WK)
p["locations"][0]["days"][0]["checkCount"] = Decimal("3.5")
check("a non-integral count is rejected", raises(lambda: L.plan_week(p, WK, MAPPING)))
p = payload(WK)
p["authToken"] = "x"
check("a credential-shaped key in the payload is refused", raises(lambda: L.plan_week(p, WK, MAPPING)))
p = payload(WK)
p["locations"] = [l for l in p["locations"] if l["locationId"] not in MAPPING]
check("a response with no mapped store fails instead of loading nothing as success",
      raises(lambda: L.plan_week(p, WK, MAPPING)))

# ---------------------------------------------------------------------------
# 7 -- fetch retries, archive, token hygiene
# ---------------------------------------------------------------------------
print("7. fetch and archive")


class FakeResp:
    def __init__(self, status, body=b"{}"):
        self.status_code, self.content, self.text = status, body, body.decode()


class FakeSession:
    def __init__(self, seq):
        self.seq, self.calls = list(seq), []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers))
        return self.seq.pop(0)


TOKEN = "test-token-abc123"
s = FakeSession([FakeResp(500), FakeResp(502), FakeResp(200, b'{"ok": 1}')])
f = I.fetch_week("https://example.test", TOKEN, WK, session=s, sleep=lambda _: None)
check("5xx is retried with bounded attempts, then succeeds",
      f["raw"] == b'{"ok": 1}' and f["attempts"] == 3)
check("the token travels only in the Authorization header",
      s.calls[0][2] == {"Authorization": f"Bearer {TOKEN}"}
      and TOKEN not in s.calls[0][0] and TOKEN not in json.dumps(s.calls[0][1]))
check("the request is GET /weekly-data?week=<Thursday>",
      s.calls[0][0] == "https://example.test/weekly-data" and s.calls[0][1] == {"week": "2026-10-08"})
s = FakeSession([FakeResp(401)])
try:
    I.fetch_week("https://example.test", TOKEN, WK, session=s, sleep=lambda _: None)
    err = None
except I.FetchError as e:
    err = e
check("401 is an AUTH failure and is not retried", err and err.code == "AUTH" and len(s.calls) == 1)
check("the error message never contains the token", err and TOKEN not in str(err))
s = FakeSession([FakeResp(500)] * 3)
try:
    I.fetch_week("https://example.test", TOKEN, WK, session=s, sleep=lambda _: None)
    err = None
except I.FetchError as e:
    err = e
check("retries are bounded (3 attempts, then FAILED)", err and len(s.calls) == I.MAX_ATTEMPTS)

with tempfile.TemporaryDirectory() as tmp:
    old = I.RAW_DIR
    I.RAW_DIR = os.path.join(tmp, "raw")
    raw = json.dumps({"weekStart": "2026-10-08", "n": 1.5}).encode()
    path, digest = I.archive(raw, 42, WK, datetime.datetime(2026, 10, 16, 1, 0, tzinfo=UTC), 200)
    I.RAW_DIR = old
    doc = open(path, "rb").read()
    check("archive file is 0600 and its directory 0700",
          stat.S_IMODE(os.stat(path).st_mode) == 0o600
          and stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode) == 0o700)
    check("archive embeds the payload byte-for-byte; sha256 verifies it",
          raw in doc and digest == hashlib.sha256(raw).hexdigest())
    meta = json.loads(doc)["meta"]
    check("archive carries run id, fetch timestamp and target week",
          meta["run_id"] == 42 and meta["target_week_start"] == "2026-10-08"
          and meta["fetched_at"].startswith("2026-10-16"))
    check("archive contains no Authorization header or token", b"Bearer" not in doc
          and b"uthorization" not in doc)

# ---------------------------------------------------------------------------
# 8 -- database: idempotent upsert, overlap, late correction (ROLLED BACK)
# ---------------------------------------------------------------------------
print("8. database upsert (single transaction, rolled back)")
conn = L.connect()
cur = conn.cursor()
TW1, TW2 = D(2030, 1, 3), D(2030, 1, 10)           # far-future Thursdays, never real data
assert TW1.isoweekday() == 4


def run_row(week):
    cur.execute("""INSERT INTO r365_ingest_run (batch_id, trigger, requested_week_start,
                   requested_week_end, loader_version) VALUES ('test','manual',%s,%s,'test')
                   RETURNING id""", (week, week + datetime.timedelta(days=6)))
    return cur.fetchone()[0]


fetch = {"fetched_at": datetime.datetime(2030, 1, 17, tzinfo=UTC), "http_status": 200}
try:
    real = L.load_mapping(cur)
    check("DB mapping holds exactly 12 ACTIVE VERIFIED stores",
          sum(1 for m in real.values() if m["status"] == "VERIFIED" and m["active"]) == 12)
    p1 = L.plan_week(payload(TW1), TW1, real)
    c1 = L.write_plan(conn, run_row(TW1), p1, fetch)
    c1b = L.write_plan(conn, run_row(TW1), L.plan_week(payload(TW1), TW1, real), fetch)
    cur.execute("SELECT count(*), count(DISTINCT (establishment_id, business_date)) "
                "FROM r365_store_daily WHERE week_start = %s", (TW1,))
    n, nd = cur.fetchone()
    check("re-loading the same week creates no duplicates", n == nd == 14, f"{n}/{nd}")
    check("second identical load reports 0 changed rows",
          c1["daily_changed"] == 14 and c1b["daily_changed"] == 0 and c1b["weekly_changed"] == 0)
    corrected = payload(TW1)
    corrected["locations"][0]["days"][0]["netSales"] += Decimal("10.01")
    corrected = rebuild(corrected)
    c2 = L.write_plan(conn, run_row(TW1), L.plan_week(corrected, TW1, real), fetch)
    cur.execute("SELECT net_sales FROM r365_store_daily WHERE establishment_id = 32 "
                "AND business_date = %s", (TW1,))
    check("a late source correction updates the stored value",
          cur.fetchone()[0] == Decimal("1234.56") + Decimal("10.01"))
    check("...and is counted as changed (1 daily, 1 weekly)",
          c2["daily_changed"] == 1 and c2["weekly_changed"] == 1, str(c2))
    L.write_plan(conn, run_row(TW2), L.plan_week(payload(TW2), TW2, real), fetch)
    cur.execute("SELECT count(DISTINCT week_start) FROM r365_store_weekly "
                "WHERE week_start IN (%s, %s)", (TW1, TW2))
    check("two-week overlap: both weeks present, each once", cur.fetchone()[0] == 2)
    cur.execute("SELECT net_sales FROM r365_store_daily WHERE establishment_id = 32 "
                "AND business_date = %s", (TW1,))
    check("loading the next week leaves the earlier week's corrected value intact",
          cur.fetchone()[0] == Decimal("1244.57"))
    cur.execute("SELECT labor_pct_fraction FROM r365_store_weekly WHERE establishment_id = 32 "
                "AND week_start = %s", (TW1,))
    frac = cur.fetchone()[0]
    cur.execute("SELECT labor_pct FROM v_r365_store_weekly_llm WHERE establishment_id = 32 "
                "AND week_start = %s", (TW1,))
    check("fact holds the 0-1 fraction; the safe view exposes percentage points",
          frac < 1 and abs(cur.fetchone()[0] - frac * 100) < Decimal("1e-12"))
    cur.execute("SAVEPOINT sp")
    try:
        cur.execute("""INSERT INTO r365_store_weekly (establishment_id, week_start, week_end,
                       r365_store_id, net_sales, gross_sales, guest_count, check_count, avg_check,
                       avg_per_guest, labor_hours, labor_cost, labor_pct_fraction,
                       sales_per_labor_hour, total_deposit, paid_in, paid_out, over_short,
                       discounts, comps, coupons, total_discounts, discount_pct_fraction,
                       gift_cards_redeemed, gift_cards_sold, tips_declared, credit_card_tips,
                       void_count, void_amount, forecast_sales_base, days_with_data,
                       source_store_name, ingest_run_id, source_fetched_at)
                       SELECT 32, '2026-08-06', '2026-08-12', %s, 0,0,0,0,0,0,0,0,0,0,0,0,0,0,
                              0,0,0,0,0,0,0,0,0,0,0,0,7,'x', %s, now()""",
                    (A_ID, run_row(TW1)))
        boundary_blocked = False
    except Exception:
        boundary_blocked = True
    cur.execute("ROLLBACK TO SAVEPOINT sp")
    check("the DB itself rejects a pre-boundary week (CHECK constraint)", boundary_blocked)
finally:
    conn.rollback()
    conn.close()

conn = L.connect()
cur = conn.cursor()
cur.execute("SELECT count(*) FROM r365_store_weekly WHERE week_start >= '2030-01-01'")
check("rollback left no test rows behind", cur.fetchone()[0] == 0)
conn.close()

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
