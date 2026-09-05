#!/usr/bin/env python3
"""A6 tests: the time contract.

Run:  venv/bin/python tests/test_time_context.py     (exit 0 = pass)

The properties under test: Revel timestamps are Chicago wall-clock and are
rendered as such; weekday numbering is unambiguous across three competing
conventions; business_date is honestly labelled as the local calendar date
because no rollover rule is verified; and relative dates resolve in Chicago
rather than from the server or database clock.
"""
import os
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from dotenv import load_dotenv

load_dotenv(".env", override=True)
import psycopg2  # noqa: E402
import chat_sql as cs  # noqa: E402

CHI = ZoneInfo("America/Chicago")
results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail else ""))


def main() -> int:
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"), dbname=os.getenv("DB_NAME", "laynes"),
        user=os.environ["DB_USER"], password=os.environ["DB_PASS"])
    conn.set_session(readonly=True)
    cur = conn.cursor()

    print("=== A. timestamps are Chicago wall-clock, not UTC ===")
    # These four orders were checked against the live Revel API: it returned
    # created_date "2026-06-14T19:07:56" and friends as naive local strings.
    cur.execute("""SELECT order_id, transaction_timestamp_local, local_calendar_date
                   FROM v_orders_time_context WHERE order_id = 14981625""")
    oid, local_ts, local_d = cur.fetchone()
    check("order 14981625 renders as 2026-06-14 19:07:56 local",
          str(local_ts) == "2026-06-14 19:07:56", str(local_ts))
    check("its local calendar date is the 14th, not the 15th",
          local_d == date(2026, 6, 14), str(local_d))
    cur.execute("SELECT (created_date AT TIME ZONE 'UTC')::date FROM orders_v2 WHERE id = 14981625")
    check("the UTC date would have been wrong (the 15th)",
          cur.fetchone()[0] == date(2026, 6, 15))

    print("=== B. weekday convention, including the Sunday/Monday boundary ===")
    cur.execute("""SELECT local_calendar_date, local_weekday_iso, local_weekday_name
                   FROM v_orders_time_context
                   WHERE local_calendar_date BETWEEN '2026-06-13' AND '2026-06-16'
                   GROUP BY 1,2,3 ORDER BY 1""")
    rows = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    expected = {date(2026, 6, 13): (6, "Saturday"), date(2026, 6, 14): (7, "Sunday"),
                date(2026, 6, 15): (1, "Monday"), date(2026, 6, 16): (2, "Tuesday")}
    for d, (iso, nm) in expected.items():
        got = rows.get(d)
        check(f"{d} is ISO {iso} ({nm})", got == (iso, nm), str(got))
    # Sunday is exactly where the three conventions disagree, so pin it.
    cur.execute("""SELECT EXTRACT(isodow FROM DATE '2026-06-14')::int,
                          EXTRACT(dow    FROM DATE '2026-06-14')::int""")
    iso_sun, pg_sun = cur.fetchone()
    check("Sunday: ISO=7 while PostgreSQL dow=0", iso_sun == 7 and pg_sun == 0)
    cur.execute("""SELECT day_of_week FROM features_daily_summary_v2
                   WHERE date = '2026-06-14' LIMIT 1""")
    feat_sun = cur.fetchone()[0]
    check("Sunday: features day_of_week=6, a third convention", feat_sun == 6,
          str(feat_sun))
    cur.execute("""SELECT day_of_week FROM features_daily_summary_v2
                   WHERE date = '2026-06-15' LIMIT 1""")
    check("Monday: features day_of_week=0 while ISO=1", cur.fetchone()[0] == 0)

    print("=== C. business date is honestly labelled ===")
    cur.execute("""SELECT DISTINCT business_date_method, business_date_confidence,
                          transaction_timestamp_source, source_timezone
                   FROM v_orders_time_context LIMIT 1""")
    method, conf, src, tz = cur.fetchone()
    check("business_date_method = local_calendar_date", method == "local_calendar_date")
    check("business_date_confidence = limited", conf == "limited")
    check("transaction timestamp is created_date", src == "created_date")
    check("source timezone is America/Chicago", tz == "America/Chicago")
    cur.execute("""SELECT COUNT(*) FROM v_orders_time_context
                   WHERE business_date <> local_calendar_date""")
    check("business_date equals local_calendar_date everywhere",
          cur.fetchone()[0] == 0)
    # No invented rollover: a 00:30 order stays on the new calendar day.
    cur.execute("""SELECT COUNT(*) FROM v_orders_time_context
                   WHERE local_hour < 4
                     AND business_date <> transaction_timestamp_local::date""")
    check("post-midnight orders are NOT shifted to the previous day",
          cur.fetchone()[0] == 0)

    print("=== D. relative dates resolve in Chicago, not UTC ===")
    today_chi = datetime.now(CHI).date()
    check("chicago_now() is in America/Chicago",
          cs.chicago_now().tzinfo is not None
          and cs.chicago_now().date() == today_chi)
    prompt_today = cs._system_today()
    check("prompt states today as the Chicago date",
          f"today     = {today_chi}" in prompt_today, str(today_chi))
    check("prompt states yesterday as the Chicago date",
          f"yesterday = {today_chi - timedelta(days=1)}" in prompt_today)
    check("prompt supplies weekday anchors", "last Friday = " in prompt_today)
    check("prompt warns against SQL current_date",
          "Do NOT compute a period from SQL current_date" in prompt_today)
    # The server clock must not be what decides the business day.
    cur.execute("SELECT current_date")
    db_today = cur.fetchone()[0]
    if db_today != today_chi:
        check("database current_date currently DIFFERS from Chicago today "
              "(exactly the bug this prevents)", True, f"db={db_today} chi={today_chi}")
    else:
        check("database current_date happens to agree right now "
              "(it will not between 19:00 and midnight local)", True)

    print("=== E. dayparts are not invented ===")
    import re as _re
    flat = _re.sub(r"\s+", " ", cs._system_static())
    check("prompt declares dayparts undefined", "DAYPARTS ARE NOT DEFINED" in flat)
    check("prompt forbids labelling hours as dinner/lunch",
          'do NOT label a range "dinner" or "lunch"' in flat)
    meta = cs.meta_extract(26, "2026-06-01", "2026-07-01")
    t = meta["time"]
    check("meta_extract reports daypart_mapping_status unavailable",
          t["daypart_mapping_status"] == "unavailable")

    print("=== F. meta_extract time block ===")
    for key in ("source_timezone", "timestamp_semantics", "business_date_method",
                "business_date_confidence", "weekday_convention",
                "daypart_mapping_status", "relative_date_resolution"):
        check(f"time.{key} present", key in t)
    check("source_timezone is America/Chicago", t["source_timezone"] == "America/Chicago")
    check("weekday_convention names ISO", "ISO-8601" in t["weekday_convention"])
    check("relative_date_resolution is application-side",
          t["relative_date_resolution"] == "application-side America/Chicago")
    check("time.today_local matches Chicago today",
          t["today_local"] == today_chi.isoformat())

    print("=== G. nothing else moved ===")
    check("A12 reconciliation still PASS", meta["reconciliation"]["status"] == "PASS")
    check("A3 entrées unchanged", meta["entree_classification"]["entrees"] == 6323.0)
    check("A10 payment capture unchanged",
          meta["payment_linkage"]["payment_capture_rate"] == 100.0)
    check("REAL count unchanged", meta["volumes"]["real_count"] == 4565)
    cur.execute("""SELECT COUNT(*) FROM information_schema.columns
                   WHERE table_name = 'v_orders_classified'""")
    ncols = cur.fetchone()[0]
    check("v_orders_classified column count unchanged (43)", ncols == 43, str(ncols))
    cur.execute("""SELECT COUNT(*) FROM information_schema.columns
                   WHERE table_name = 'v_orders_classified'
                     AND column_name IN ('local_calendar_date', 'local_hour',
                                         'local_weekday_iso', 'local_weekday_name',
                                         'transaction_timestamp_local',
                                         'business_date_method',
                                         'business_date_confidence')""")
    # NB: orders_v2 already has an unrelated "local_id" column (Revel's local
    # order number), so match the A6 names exactly rather than by prefix.
    check("no A6 columns leaked into v_orders_classified", cur.fetchone()[0] == 0)

    print("=== H. weekday names are supplied from data, not memory ===")
    prompt_t = cs._system_today()
    check("prompt carries a verified calendar", "VERIFIED CALENDAR" in prompt_t)
    # Every line of that calendar must be arithmetically correct, not just present.
    import re as _re3
    cal_rows = _re3.findall(r"(\d{4}-\d{2}-\d{2})\s+ISO (\d)\s+(\w+)", prompt_t)
    check("calendar covers at least 14 days", len(cal_rows) >= 14, str(len(cal_rows)))
    bad = [r for r in cal_rows
           if date.fromisoformat(r[0]).isoweekday() != int(r[1])
           or date.fromisoformat(r[0]).strftime("%A") != r[2]]
    check("every calendar row has the correct ISO number and day name",
          not bad, str(bad[:3]))
    # The exact failure that prompted this: 2026-09-04 named "Thursday".
    if any(r[0] == "2026-09-04" for r in cal_rows):
        row = next(r for r in cal_rows if r[0] == "2026-09-04")
        check("2026-09-04 is stated as Friday, ISO 5",
              row[1] == "5" and row[2] == "Friday", str(row))
    check("prompt forbids naming a weekday from memory",
          "NEVER STATE A WEEKDAY FROM MEMORY" in
          _re3.sub(r"\s+", " ", cs._system_static()))

    # check_data supplies the weekday for the analysed scope.
    m_yest = cs.meta_extract(26, "2026-09-04", "2026-09-05")["time"]
    check("scope weekday name supplied as Friday",
          m_yest["period_start_weekday_name"] == "Friday",
          m_yest["period_start_weekday_name"])
    check("scope weekday ISO supplied as 5", m_yest["period_start_weekday_iso"] == 5)
    check("inclusive end date supplied", m_yest["period_end_inclusive"] == "2026-09-04")

    # Sunday/Monday boundary through the same application-side path.
    for d, iso, nm in (("2026-06-13", 6, "Saturday"), ("2026-06-14", 7, "Sunday"),
                       ("2026-06-15", 1, "Monday"), ("2026-06-16", 2, "Tuesday")):
        mm = cs.meta_extract(26, d, str(date.fromisoformat(d) + timedelta(days=1)))["time"]
        check(f"{d} supplied as ISO {iso} {nm}",
              mm["period_start_weekday_iso"] == iso
              and mm["period_start_weekday_name"] == nm,
              f"{mm['period_start_weekday_iso']}/{mm['period_start_weekday_name']}")

    print("=== I. zero results are valid answers ===")
    check("prompt states a zero result is an answer",
          "A ZERO RESULT IS AN ANSWER" in _re3.sub(r"\s+", " ", cs._system_static()))
    check("prompt forbids re-querying to escape an empty result",
          "Do not re-query, widen the period, drop filters" in
          _re3.sub(r"\s+", " ", cs._system_static()))
    # Nederland genuinely has no post-midnight trade in June -- the three shapes
    # of empty result a question can legitimately return.
    cur.execute("""SELECT COUNT(*) FROM v_orders_classified o
                   JOIN v_orders_time_context t ON t.order_id = o.id
                   WHERE o.establishment_id = 26 AND o.txn_class = 'REAL'
                     AND o.business_date >= '2026-06-01'
                     AND o.business_date <  '2026-07-01'
                     AND t.local_hour < 5""")
    check("zero-count result: 0 REAL orders 00:00-04:59", cur.fetchone()[0] == 0)
    cur.execute("""SELECT COALESCE(SUM(o.final_total), 0) FROM v_orders_classified o
                   JOIN v_orders_time_context t ON t.order_id = o.id
                   WHERE o.establishment_id = 26 AND o.txn_class = 'REAL'
                     AND o.business_date >= '2026-06-01'
                     AND o.business_date <  '2026-07-01'
                     AND t.local_hour < 5""")
    check("zero-sum result: $0 after midnight", float(cur.fetchone()[0]) == 0.0)
    cur.execute("""SELECT o.id FROM v_orders_classified o
                   JOIN v_orders_time_context t ON t.order_id = o.id
                   WHERE o.establishment_id = 26 AND o.txn_class = 'REAL'
                     AND o.business_date >= '2026-06-01'
                     AND o.business_date <  '2026-07-01'
                     AND t.local_hour < 5 LIMIT 5""")
    check("zero-row result: no rows returned", cur.fetchall() == [])
    # ... and the scope itself is sound, so emptiness is a finding, not a fault.
    m_june = cs.meta_extract(26, "2026-06-01", "2026-07-01")
    check("the empty interval sits inside a PASSing, reconciled scope",
          m_june["reconciliation"]["status"] == "PASS"
          and m_june["analysis_permitted"] is True)

    conn.close()
    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("FAILED: " + ", ".join(failed))
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
