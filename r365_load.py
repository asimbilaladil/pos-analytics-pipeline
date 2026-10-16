#!/usr/bin/env python3
"""Validate and load one R365 /weekly-data response into the canonical facts.

The fetch, archive, lock and schedule live in r365_weekly_ingest.py; this
module is the part that decides what is true:

  * plan_week()  -- PURE. Parses a payload into daily/weekly rows, assigns
                    day_status, grades completeness and reconciles daily sums
                    against the official weekly row. No I/O, fully testable.
  * write_plan() -- idempotent UPSERT of a plan inside the caller's transaction.
  * start_run() / finish_run() / fail_run() -- r365_ingest_run provenance.

Rules enforced here (see migrations/54_r365_canonical.sql for the evidence):
  * Source values are stored EXACTLY. The payload is parsed with
    parse_float=Decimal, so 11443.755799999999 stays 11443.755799999999.
  * A day in missingDays, or flagged noData, is never inserted. Missing is
    absence, not zero. A weekly row with daysWithData = 0 is likewise absent.
  * Only ACTIVE VERIFIED mappings load. An unknown R365 store is surfaced and
    recorded as UNMAPPED, never matched by name.
  * The official weekly row is authoritative; daily sums are a check only.
  * Nothing token-shaped is ever read from the payload or written anywhere.
"""
import datetime
import json
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=False)
import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402

LOADER_VERSION = "r365_load/1.0.0"
# Approved canonical boundary. Earlier weeks exist live in R365 but are out of
# scope; the DB CHECK constraints enforce the same date.
BOUNDARY = datetime.date(2026, 8, 13)
FORBIDDEN_KEYS = ("token", "password", "authorization", "cookie", "secret")

STATUS_COMPLETE, STATUS_PARTIAL, STATUS_FAILED = "COMPLETE", "PARTIAL", "FAILED"
DAY_NORMAL, DAY_CLOSED, DAY_PRE_OPENING = "NORMAL", "CLOSED", "PRE_OPENING"

# (source key, column, kind). kind: money = required numeric, count = required
# integer, nullable = numeric that the source may send as null.
_COMMON = [
    ("netSales", "net_sales", "money"),
    ("grossSales", "gross_sales", "money"),
    ("guestCount", "guest_count", "count"),
    ("checkCount", "check_count", "count"),
    ("avgCheck", "avg_check", "money"),
    ("avgPerGuest", "avg_per_guest", "money"),
    ("totalLaborHours", "labor_hours", "money"),
    ("totalLaborAmount", "labor_cost", "money"),
    ("laborPct", "labor_pct_fraction", "money"),
    ("totalDeposit", "total_deposit", "money"),
    ("paidInTotal", "paid_in", "money"),
    ("paidOutTotal", "paid_out", "money"),
    ("overShort", "over_short", "money"),
    ("discounts", "discounts", "money"),
    ("comps", "comps", "money"),
    ("coupons", "coupons", "money"),
    ("totalDiscounts", "total_discounts", "money"),
    ("discountPctOfSales", "discount_pct_fraction", "money"),
    ("gcardRedeem", "gift_cards_redeemed", "money"),
    ("gcardSold", "gift_cards_sold", "money"),
    ("declaredTips", "tips_declared", "money"),
    ("creditCardTips", "credit_card_tips", "money"),
    ("voidCount", "void_count", "count"),
    ("voidAmount", "void_amount", "money"),
    ("forecastedSales", "forecasted_sales", "nullable"),
    ("salesVsForecast", "sales_vs_forecast_amount", "nullable"),
    ("salesVsForecastPct", "sales_vs_forecast_pct_fraction", "nullable"),
]
DAILY_FIELDS = _COMMON + [("expectedCash", "expected_cash", "money")]
WEEKLY_FIELDS = _COMMON + [
    ("salesPerLaborHour", "sales_per_labor_hour", "money"),
    ("forecastSalesBase", "forecast_sales_base", "money"),
    ("daysWithData", "days_with_data", "count"),
]
DAILY_IDENTITY = {"locationId", "locationName", "date"}
DAILY_OPTIONAL = {"noData"}
WEEKLY_IDENTITY = {"locationId", "locationName", "days"}
TOP_KEYS = {"week", "weekStart", "weekEnd", "complete", "missingDays",
            "warning", "fleet", "locations"}

# Daily-to-weekly reconciliation: additive measures only. Ratios and averages
# are never summed.
ADDITIVE = ["net_sales", "gross_sales", "guest_count", "check_count",
            "labor_hours", "labor_cost", "total_deposit", "paid_in", "paid_out",
            "over_short", "discounts", "comps", "coupons", "total_discounts",
            "gift_cards_redeemed", "gift_cards_sold", "tips_declared",
            "credit_card_tips", "void_count", "void_amount"]
MONEY_TOLERANCE = Decimal("0.01")
# Fleet totals vs the sum of every location in the payload.
FLEET_ADDITIVE = [("netSales", "net_sales"), ("guestCount", "guest_count"),
                  ("checkCount", "check_count"), ("totalLaborHours", "labor_hours"),
                  ("totalLaborAmount", "labor_cost"),
                  ("totalDiscounts", "total_discounts"), ("overShort", "over_short"),
                  ("voidCount", "void_count"), ("voidAmount", "void_amount"),
                  ("forecastedSales", "forecasted_sales"),
                  ("forecastSalesBase", "forecast_sales_base")]


class SourceSchemaError(Exception):
    """The payload does not have the proven shape. Fail closed: load nothing."""


def parse_payload(raw):
    """Decode JSON with exact decimals. Accepts bytes or str."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw, parse_float=Decimal)


def week_end_for(week_start):
    return week_start + datetime.timedelta(days=6)


def week_start_for(d):
    """Thursday that opens the Thursday->Wednesday week containing d."""
    return d - datetime.timedelta(days=(d.isoweekday() + 3) % 7)


def assert_no_secrets(doc):
    def walk(node, path=""):
        if isinstance(node, dict):
            for k, v in node.items():
                if any(f in str(k).lower() for f in FORBIDDEN_KEYS):
                    raise SourceSchemaError(f"payload carries forbidden key at {path}.{k}")
                walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
    walk(doc)


def _num(v, kind, where):
    """Validate one source value against its kind. Never coerces null to 0."""
    if v is None:
        if kind == "nullable":
            return None
        raise SourceSchemaError(f"{where}: null where a number is required")
    if isinstance(v, bool) or not isinstance(v, (int, Decimal)):
        raise SourceSchemaError(f"{where}: expected a number, got {type(v).__name__}")
    if kind == "count":
        if v != int(v):
            raise SourceSchemaError(f"{where}: count is not integral ({v})")
        return int(v)
    return Decimal(v) if isinstance(v, int) else v


def _extract(obj, fields, where):
    return {col: _num(obj.get(key), kind, f"{where}.{key}") for key, col, kind in fields}


def _check_keys(obj, required, optional, where, warnings):
    missing = sorted(required - obj.keys())
    if missing:
        raise SourceSchemaError(f"{where}: missing source field(s) {missing}")
    extra = sorted(obj.keys() - required - optional)
    if extra:
        warnings.append(f"{where}: unexpected new source field(s) {extra} (ignored)")


def classify_day(row, business_date, opening_date):
    """NORMAL, CLOSED or PRE_OPENING for a PRESENT source day.

    Zero trading = no sales, no checks, no guests. PRE_OPENING requires a
    CONFIRMED opening_date; without one a zero-trading day is CLOSED. Zero
    sales alone is never used to infer that a store had not opened yet.
    """
    if row["net_sales"] == 0 and row["check_count"] == 0 and row["guest_count"] == 0:
        if opening_date is not None and business_date < opening_date:
            return DAY_PRE_OPENING
        return DAY_CLOSED
    return DAY_NORMAL


def _close(a, b, tol):
    if a is None or b is None:
        return a is None and b is None
    return abs(Decimal(a) - Decimal(b)) <= tol


def reconcile_store(weekly, dailies):
    """Daily sums vs the official weekly row for one store. Returns checks."""
    checks = []
    for col in ADDITIVE:
        s = sum((d[col] for d in dailies), Decimal(0))
        tol = Decimal(0) if col in ("guest_count", "check_count", "void_count") else MONEY_TOLERANCE
        checks.append({"field": col, "weekly": weekly[col], "daily_sum": s,
                       "ok": _close(weekly[col], s, tol)})
    fc = [d for d in dailies if d["forecasted_sales"] and d["forecasted_sales"] > 0]
    exp_fc = sum((d["forecasted_sales"] for d in fc), Decimal(0)) if fc else None
    checks.append({"field": "forecasted_sales", "weekly": weekly["forecasted_sales"],
                   "daily_sum": exp_fc,
                   "ok": _close(weekly["forecasted_sales"], exp_fc, MONEY_TOLERANCE)})
    base = sum((d["net_sales"] for d in fc), Decimal(0))
    checks.append({"field": "forecast_sales_base", "weekly": weekly["forecast_sales_base"],
                   "daily_sum": base,
                   "ok": _close(weekly["forecast_sales_base"], base, MONEY_TOLERANCE)})
    checks.append({"field": "days_with_data", "weekly": weekly["days_with_data"],
                   "daily_sum": len(dailies),
                   "ok": weekly["days_with_data"] == len(dailies)})
    return checks


def plan_week(payload, week_start, mapping):
    """PURE: turn one payload into a load plan.

    mapping: {r365_store_id: {"establishment_id", "status", "active",
                               "opening_date", "r365_store_name"}}
    Raises SourceSchemaError when the payload cannot be trusted at all.
    """
    warnings = []
    if not isinstance(payload, dict):
        raise SourceSchemaError("payload is not a JSON object")
    assert_no_secrets(payload)
    _check_keys(payload, TOP_KEYS, set(), "payload", warnings)
    week_end = week_end_for(week_start)
    if payload["weekStart"] != week_start.isoformat() or payload["weekEnd"] != week_end.isoformat():
        raise SourceSchemaError(
            f"source returned week {payload['weekStart']}..{payload['weekEnd']}, "
            f"requested {week_start}..{week_end}")
    if not isinstance(payload["locations"], list):
        raise SourceSchemaError("locations is not a list")

    week_dates = {(week_start + datetime.timedelta(days=i)).isoformat() for i in range(7)}
    src_missing = {(m.get("location"), m.get("date")) for m in (payload["missingDays"] or [])}

    expected = {sid: m for sid, m in mapping.items()
                if m["status"] == "VERIFIED" and m["active"]}
    daily_rows, weekly_rows, unmapped, observed_ids = [], [], [], set()
    per_store, recon, all_weekly = {}, {}, []
    source_dates = set()

    for i, loc in enumerate(payload["locations"]):
        where = f"locations[{i}]"
        _check_keys(loc, {k for k, _, _ in WEEKLY_FIELDS} | WEEKLY_IDENTITY, set(), where, warnings)
        sid, name = loc["locationId"], loc["locationName"]
        if sid in observed_ids:
            raise SourceSchemaError(f"{where}: duplicate locationId {sid}")
        observed_ids.add(sid)
        weekly = _extract(loc, WEEKLY_FIELDS, where)
        all_weekly.append(weekly)

        m = expected.get(sid)
        if m is None:
            known = mapping.get(sid)
            unmapped.append({"r365_store_id": sid, "r365_store_name": name,
                             "mapping_status": known["status"] if known else "UNKNOWN"})
            continue
        if name != m["r365_store_name"]:
            warnings.append(f"store {sid} name changed in source: "
                            f"{m['r365_store_name']!r} -> {name!r} (id-keyed, still loaded)")

        present, seen = [], set()
        for j, day in enumerate(loc["days"]):
            dw = f"{where}.days[{j}]"
            _check_keys(day, {k for k, _, _ in DAILY_FIELDS} | DAILY_IDENTITY,
                        DAILY_OPTIONAL, dw, warnings)
            if day["locationId"] != sid:
                raise SourceSchemaError(f"{dw}: locationId differs from its parent")
            if day["date"] not in week_dates:
                raise SourceSchemaError(f"{dw}: date {day['date']} outside the requested week")
            if day["date"] in seen:
                raise SourceSchemaError(f"{dw}: duplicate date {day['date']}")
            seen.add(day["date"])
            if day.get("noData") or (name, day["date"]) in src_missing:
                continue                      # missing: absence, never a zero row
            row = _extract(day, DAILY_FIELDS, dw)
            bd = datetime.date.fromisoformat(day["date"])
            row.update(establishment_id=m["establishment_id"], business_date=bd,
                       week_start=week_start, r365_store_id=sid,
                       source_store_name=name,
                       day_status=classify_day(row, bd, m.get("opening_date")))
            present.append(row)
            source_dates.add(bd)
        daily_rows.extend(present)

        if weekly["days_with_data"] > 0:
            weekly.update(establishment_id=m["establishment_id"], week_start=week_start,
                          week_end=week_end, r365_store_id=sid, source_store_name=name)
            weekly_rows.append(weekly)
            recon[sid] = reconcile_store(weekly, present)
        per_store[sid] = {"store": name, "days_present": len(present),
                          "days_with_data": weekly["days_with_data"]}

    missing_stores = [{"r365_store_id": sid, "r365_store_name": m["r365_store_name"]}
                      for sid, m in expected.items() if sid not in observed_ids]
    full = [sid for sid, s in per_store.items()
            if s["days_present"] == 7 and s["days_with_data"] == 7]
    missing_store_days = 7 * len(expected) - sum(s["days_present"] for s in per_store.values())

    # Fleet totals vs every location in the payload (mapped or not).
    fleet_checks = []
    fleet = payload["fleet"] or {}
    for key, col in FLEET_ADDITIVE:
        if key not in fleet:
            continue
        vals = [w[col] for w in all_weekly if w[col] is not None]
        s = sum(vals, Decimal(0))
        fv = fleet[key]
        tol = Decimal(0) if col in ("guest_count", "check_count", "void_count") else MONEY_TOLERANCE
        fleet_checks.append({"field": col, "fleet": fv, "sum_locations": s,
                             "ok": fv is not None and _close(fv, s, tol)})
    if "locationCount" in fleet:
        fleet_checks.append({"field": "location_count", "fleet": fleet["locationCount"],
                             "sum_locations": len(all_weekly),
                             "ok": fleet["locationCount"] == len(all_weekly)})

    failed = [{"store": per_store[sid]["store"], **c}
              for sid, cs in recon.items() for c in cs if not c["ok"]]
    failed += [{"store": "FLEET", **c} for c in fleet_checks if not c["ok"]]
    reconciliation_ok = not failed
    source_complete = bool(payload["complete"]) and not src_missing

    complete = (not missing_stores and len(full) == len(expected) and source_complete
                and reconciliation_ok and not unmapped and missing_store_days == 0)
    status = STATUS_COMPLETE if complete else STATUS_PARTIAL
    if expected and not per_store:
        raise SourceSchemaError("no mapped store present in the source response")

    return {
        "week_start": week_start, "week_end": week_end, "status": status,
        "daily_rows": daily_rows, "weekly_rows": weekly_rows,
        "daily_rows_received": sum(len(l.get("days") or []) for l in payload["locations"]),
        "weekly_rows_received": len(payload["locations"]),
        "expected_store_count": len(expected),
        "observed_store_count": len(observed_ids),
        "mapped_observed_count": len(per_store),
        "stores_with_full_week": len(full),
        "missing_store_days": missing_store_days,
        "missing_stores": missing_stores,
        "unmapped_stores": unmapped,
        "source_complete": source_complete,
        "source_missing_days": sorted([{"location": a, "date": b} for a, b in src_missing],
                                      key=lambda x: (x["date"] or "", x["location"] or "")),
        "source_warning": payload.get("warning"),
        "source_min_date": min(source_dates) if source_dates else None,
        "source_max_date": max(source_dates) if source_dates else None,
        "reconciliation_ok": reconciliation_ok,
        "reconciliation": {"store_checks": sum(len(c) for c in recon.values()),
                           "fleet_checks": len(fleet_checks),
                           "failed": failed,
                           "per_store": per_store},
        "schema_warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Database side
# ---------------------------------------------------------------------------
def connect():
    return psycopg2.connect(
        host=os.environ["DB_HOST"], port=os.environ["DB_PORT"],
        dbname=os.environ["DB_NAME"], user=os.environ["DB_USER"],
        password=os.environ["DB_PASS"])


def load_mapping(cur):
    cur.execute("""SELECT r365_store_id, r365_store_name, establishment_id, status,
                          active, opening_date FROM r365_store_mapping""")
    return {r[0]: {"r365_store_name": r[1], "establishment_id": r[2], "status": r[3],
                   "active": r[4], "opening_date": r[5]} for r in cur.fetchall()}


def _jsonable(o):
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, (datetime.date, datetime.datetime)):
        return o.isoformat()
    raise TypeError(type(o).__name__)


def _json(v):
    return json.dumps(v, default=_jsonable)


def start_run(conn, week_start, trigger, batch_id):
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO r365_ingest_run
                         (batch_id, trigger, requested_week_start, requested_week_end,
                          loader_version)
                       VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                    (batch_id, trigger, week_start, week_end_for(week_start), LOADER_VERSION))
        run_id = cur.fetchone()[0]
    conn.commit()
    return run_id


def fail_run(conn, run_id, code, message, fetch=None):
    conn.rollback()
    fetch = fetch or {}
    with conn.cursor() as cur:
        cur.execute("""UPDATE r365_ingest_run
                          SET status = 'FAILED', completed_at = now(),
                              error_code = %s, error_message = %s,
                              http_status = %s, fetch_seconds = %s,
                              payload_sha256 = %s, raw_archive_path = %s
                        WHERE id = %s""",
                    (code, str(message)[:2000], fetch.get("http_status"),
                     fetch.get("fetch_seconds"), fetch.get("payload_sha256"),
                     fetch.get("raw_archive_path"), run_id))
    conn.commit()


_D_COLS = [c for _, c, _ in DAILY_FIELDS]
_W_COLS = [c for _, c, _ in WEEKLY_FIELDS]


def _upsert(cur, table, pk_cols, value_cols, rows, run_id, fetched_at):
    """Deterministic UPSERT on pk_cols. Returns (upserted, changed).

    Every non-key column is overwritten from the latest fetch. changed counts
    rows that were new or whose stored values differ from the fetch -- the
    late-correction signal the overlap re-fetch exists for.
    """
    if not rows:
        return 0, 0
    keys = [tuple(r[k] for k in pk_cols) for r in rows]
    cur.execute(f"""SELECT {', '.join(pk_cols)}, {', '.join(value_cols)}
                      FROM {table}
                     WHERE ({', '.join(pk_cols)}) IN %s""", (tuple(keys),))
    before = {tuple(r[:len(pk_cols)]): tuple(r[len(pk_cols):]) for r in cur.fetchall()}
    changed = sum(1 for r, k in zip(rows, keys)
                  if before.get(k) != tuple(r[c] for c in value_cols))
    cols = pk_cols + value_cols + ["ingest_run_id", "source_fetched_at"]
    upd = ", ".join(f"{c} = EXCLUDED.{c}" for c in value_cols
                    + ["ingest_run_id", "source_fetched_at"]) + ", updated_at = now()"
    psycopg2.extras.execute_values(
        cur,
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES %s "
        f"ON CONFLICT ({', '.join(pk_cols)}) DO UPDATE SET {upd}",
        [tuple(r[c] for c in pk_cols + value_cols) + (run_id, fetched_at) for r in rows])
    return len(rows), changed


def write_plan(conn, run_id, plan, fetch):
    """Upsert facts and close the run, in ONE transaction. Caller commits."""
    fetched_at = fetch["fetched_at"]
    with conn.cursor() as cur:
        for u in plan["unmapped_stores"]:
            if u["mapping_status"] == "UNKNOWN":
                cur.execute("""INSERT INTO r365_store_mapping
                                 (r365_store_id, r365_store_name, status, notes)
                               VALUES (%s, %s, 'UNMAPPED', %s)
                               ON CONFLICT (r365_store_id) DO NOTHING""",
                            (u["r365_store_id"], u["r365_store_name"],
                             f"first seen in source week {plan['week_start']}; "
                             "needs an explicit evidence-based mapping"))
        seen = [r["r365_store_id"] for r in plan["weekly_rows"]] + \
               [u["r365_store_id"] for u in plan["unmapped_stores"]]
        if seen:
            cur.execute("UPDATE r365_store_mapping SET last_seen_at = now() "
                        "WHERE r365_store_id = ANY(%s)", (seen,))
        d_up, d_ch = _upsert(cur, "r365_store_daily",
                             ["establishment_id", "business_date"],
                             ["week_start", "r365_store_id", "day_status",
                              "source_store_name"] + _D_COLS,
                             plan["daily_rows"], run_id, fetched_at)
        w_up, w_ch = _upsert(cur, "r365_store_weekly",
                             ["establishment_id", "week_start"],
                             ["week_end", "r365_store_id", "source_store_name"] + _W_COLS,
                             plan["weekly_rows"], run_id, fetched_at)
        cur.execute("""UPDATE r365_ingest_run SET
                         status = %s, completed_at = now(),
                         expected_store_count = %s, observed_store_count = %s,
                         mapped_observed_count = %s, stores_with_full_week = %s,
                         missing_store_days = %s,
                         daily_rows_received = %s, weekly_rows_received = %s,
                         daily_rows_upserted = %s, weekly_rows_upserted = %s,
                         daily_rows_changed = %s, weekly_rows_changed = %s,
                         source_complete = %s, source_min_date = %s, source_max_date = %s,
                         reconciliation_ok = %s, unmapped_stores = %s, missing_stores = %s,
                         source_missing_days = %s, reconciliation = %s,
                         schema_warnings = %s, source_warning = %s,
                         http_status = %s, fetch_seconds = %s,
                         payload_sha256 = %s, raw_archive_path = %s
                       WHERE id = %s""",
                    (plan["status"], plan["expected_store_count"],
                     plan["observed_store_count"], plan["mapped_observed_count"],
                     plan["stores_with_full_week"], plan["missing_store_days"],
                     plan["daily_rows_received"], plan["weekly_rows_received"],
                     d_up, w_up, d_ch, w_ch,
                     plan["source_complete"], plan["source_min_date"], plan["source_max_date"],
                     plan["reconciliation_ok"], _json(plan["unmapped_stores"]),
                     _json(plan["missing_stores"]), _json(plan["source_missing_days"]),
                     _json(plan["reconciliation"]), _json(plan["schema_warnings"]),
                     plan["source_warning"], fetch.get("http_status"),
                     fetch.get("fetch_seconds"), fetch.get("payload_sha256"),
                     fetch.get("raw_archive_path"), run_id))
    return {"daily_upserted": d_up, "daily_changed": d_ch,
            "weekly_upserted": w_up, "weekly_changed": w_ch}
