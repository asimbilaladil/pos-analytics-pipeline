#!/usr/bin/env python3
"""A10 tests: safe order-level payment linkage.

Run:  venv/bin/python tests/test_payment_linkage.py     (exit 0 = pass)

Two properties matter here: the analysis layer gets useful payment structure,
and no sensitive payment field is reachable by the assistant under any path.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from dotenv import load_dotenv

load_dotenv(".env", override=True)
import psycopg2  # noqa: E402
import chat_sql as cs  # noqa: E402

SENSITIVE = ("transaction_id", "refund_transaction_id", "card_type",
             "transaction_status", "processor_accepted", "station_id",
             "amount_authorized", "other_payment_type")
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

    print("=== A. safe view exposes no sensitive payment field ===")
    for view in ("v_order_payment_summary", "v_orders_payment_classified"):
        cur.execute("""SELECT column_name FROM information_schema.columns
                       WHERE table_name = %s""", (view,))
        cols = {r[0] for r in cur.fetchall()}
        leaked = sorted(cols & set(SENSITIVE))
        check(f"{view} leaks no sensitive payment column", not leaked, str(leaked))
    # There is no cardholder/last4/email column in payments_v2 at all -- assert
    # that stays true, so a future pipeline change cannot quietly add one.
    cur.execute("""SELECT column_name FROM information_schema.columns
                   WHERE table_name = 'payments_v2'
                     AND column_name ~* 'last4|cardholder|holder_name|email|receipt|pan'""")
    check("payments_v2 still has no cardholder/last4/email column",
          cur.fetchall() == [])

    print("=== B. raw payments_v2 unreachable by the assistant ===")
    for sql, label in (("SELECT * FROM payments_v2", "select *"),
                       ("SELECT transaction_id FROM payments_v2", "transaction_id"),
                       ("SELECT card_type FROM payments_v2", "card_type"),
                       ("SELECT * FROM orders_v2 o JOIN payments_v2 p ON p.order_id=o.id",
                        "join"),
                       ("WITH x AS (TABLE payments_v2) SELECT * FROM x", "TABLE cmd"),
                       ("SELECT * FROM public.payments_v2", "schema-qualified"),
                       ('SELECT * FROM "payments_v2"', "quoted")):
        try:
            cs._validate(sql)
            check(f"validator blocks payments_v2 via {label}", False, "ACCEPTED")
        except cs.SqlError:
            check(f"validator blocks payments_v2 via {label}", True)

    ro = psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"), dbname=os.getenv("DB_NAME", "laynes"),
        user=os.environ["DB_RO_USER"], password=os.environ["DB_RO_PASS"])
    ro.set_session(readonly=True)
    roc = ro.cursor()
    for table in ("payments_v2", "payments", "app_users", "chat_query_log"):
        ro.rollback()
        try:
            roc.execute(f"SELECT 1 FROM {table} LIMIT 1")
            check(f"database denies {table}", False, "READABLE")
        except psycopg2.Error:
            check(f"database denies {table}", True)
    ro.rollback()
    roc.execute("SELECT COUNT(*) FROM v_order_payment_summary")
    check("database allows the safe summary view", roc.fetchone()[0] > 0)
    ro.close()

    print("=== C. payment type mapping is honestly absent ===")
    pl = cs.meta_extract(26, "2026-06-01", "2026-07-01")["payment_linkage"]
    m = pl["payment_type_mapping"]
    # The A10 brief fixes these two field names and values explicitly.
    check("payment_type_mapping_status == 'unavailable'",
          pl["payment_type_mapping_status"] == "unavailable")
    check("payment_type_mapping_confidence == 'none'",
          pl["payment_type_mapping_confidence"] == "none")
    check("mapping block agrees", m["status"] == "unavailable"
          and m["confidence"] == "none" and m["version"] == "none")
    check("reason cites the empty PaymentType resource", "PaymentType" in m["reason"])
    check("reason rejects circumstantial naming",
          "NOT sufficient" in m["reason"])
    prompt = cs._system_static()
    check("prompt forbids naming codes cash/card",
          'never "card", "cash"' in prompt)
    import re as _re
    flat = _re.sub(r"\s+", " ", prompt)   # the phrase wraps a line in the source
    check("prompt tells the model to say 'payment type code'",
          "payment type code" in flat)
    check("prompt states has_payment=FALSE is not lost revenue",
          "not lost revenue" in flat.lower())

    print("=== D. Nederland June linkage ===")
    check("orders_with_payment = 4565", pl["orders_with_payment"] == 4565,
          str(pl["orders_with_payment"]))
    check("orders_without_payment = 0", pl["orders_without_payment"] == 0)
    check("capture rate = 100%", pl["payment_capture_rate"] == 100.0)
    check("split tender = 68", pl["split_tender_count"] == 68,
          str(pl["split_tender_count"]))
    check("refunded payments = 0", pl["refunded_payment_count"] == 0)

    cur.execute("""SELECT COUNT(*) FILTER (WHERE NOT has_payment),
                          COUNT(*) FILTER (WHERE NOT has_payment
                                             AND txn_class IN ('EMPTY','COMP'))
                   FROM v_orders_payment_classified
                   WHERE establishment_id = 26
                     AND business_date >= '2026-06-01'
                     AND business_date <  '2026-07-01'""")
    no_pay, explained = cur.fetchone()
    check("every zero-payment order is EMPTY or COMP", no_pay == explained,
          f"{explained}/{no_pay}")

    print("=== E. classification is unaffected by payment presence ===")
    cur.execute("""SELECT COUNT(*) FROM v_orders_payment_classified
                   WHERE txn_class = 'REAL' AND NOT has_payment
                     AND business_date >= '2026-06-01'
                     AND business_date <  '2026-07-01'
                     AND establishment_id = 26""")
    check("REAL orders are not reclassified by missing payment",
          cur.fetchone()[0] == 0)
    cur.execute("""SELECT column_name FROM information_schema.columns
                   WHERE table_name = 'v_orders_classified'
                     AND column_name LIKE 'payment%'""")
    check("v_orders_classified stays payment-free (A12 performance)",
          cur.fetchall() == [])

    print("=== F. A12 contract preserved ===")
    meta = cs.meta_extract(26, "2026-06-01", "2026-07-01")
    check("reconciliation still PASS", meta["reconciliation"]["status"] == "PASS")
    check("REAL count unchanged", meta["volumes"]["real_count"] == 4565)
    check("entrée count unchanged by A10",
          meta["entree_classification"]["entrees"] == 6323.0)
    check("payment_linkage present in payload", "payment_linkage" in meta)

    print("=== G. percentage scale contract ===")
    import re as _re2
    meta2 = cs.meta_extract(26, "2026-06-01", "2026-07-01")
    rec = meta2["reconciliation"]
    # The assistant once rendered delta_pct 0.0293 as "2.9%" -- a hundredfold
    # error on the number that decides whether data is trustworthy. The payload
    # now carries the units and a ready-to-quote string.
    check("delta_pct is 0.0293 for Nederland June", rec["delta_pct"] == 0.0293,
          str(rec["delta_pct"]))
    check("delta_pct_display renders as 0.0293%",
          rec["delta_pct_display"] == "+0.0293%", str(rec["delta_pct_display"]))
    check("rounded display renders as 0.03%",
          rec["delta_pct_rounded_display"] == "+0.03%",
          str(rec["delta_pct_rounded_display"]))
    check("neither display can read as 2.9%",
          "2.9" not in rec["delta_pct_display"]
          and "2.9" not in rec["delta_pct_rounded_display"])
    check("units field states percentage points",
          "percentage points" in rec["delta_pct_units"]
          and "NOT 2.93%" in rec["delta_pct_units"])
    flat2 = _re2.sub(r"\s+", " ", cs._system_static())
    check("prompt states percentages are already percentages",
          "PERCENTAGES ARE ALREADY PERCENTAGES" in flat2)
    check("prompt names the exact wrong reading",
          "It is NOT 2.93% and NOT 2.9%" in flat2)
    check("prompt forbids multiplying by 100",
          "Never multiply a *_pct value by 100" in flat2)
    check("tool schema repeats the scale contract",
          "0.0293 means 0.0293%, not 2.93%"
          in _re2.sub(r"\s+", " ", cs.CHECK_DATA_TOOL["description"]))
    # Same contract for the other percentage fields.
    check("coverage_pct is on a 0-100 scale",
          0 <= meta2["entree_classification"]["coverage_pct"] <= 100
          and meta2["entree_classification"]["coverage_pct"] > 1)
    check("payment_capture_rate is on a 0-100 scale",
          meta2["payment_linkage"]["payment_capture_rate"] == 100.0)

    print("=== H. payment denominator defaults to REAL ===")
    cur.execute("""SELECT COUNT(*) FILTER (WHERE txn_class = 'REAL'
                                             AND is_split_tender),
                          COUNT(*) FILTER (WHERE is_split_tender),
                          COUNT(*) FILTER (WHERE txn_class = 'REAL')
                   FROM v_orders_payment_classified
                   WHERE establishment_id = 26
                     AND business_date >= '2026-06-01'
                     AND business_date <  '2026-07-01'""")
    split_real, split_all, real_n = cur.fetchone()
    check("REAL split-tender = 68", split_real == 68, str(split_real))
    check("all-classes split-tender = 73", split_all == 73, str(split_all))
    check("REAL denominator = 4565", real_n == 4565, str(real_n))
    check("the two figures genuinely differ, so labelling matters",
          split_real != split_all)
    check("meta_extract reports the REAL figure, not the all-class one",
          meta2["payment_linkage"]["split_tender_count"] == split_real)
    check("prompt defaults payment figures to REAL",
          "DEFAULT EVERY PAYMENT FIGURE TO txn_class = 'REAL'" in flat2)
    check("prompt requires labelling an all-classes number",
          "all raw transaction classes" in flat2)

    print("=== I. refund path is disclosed as unvalidated ===")
    pl2 = meta2["payment_linkage"]
    cur.execute("SELECT COALESCE(SUM(refunded_count), 0) FROM v_payments_daily_v2")
    observed_all_time = int(cur.fetchone()[0])
    check("all-time refund observations match the database",
          pl2["refund_observations_all_time"] == observed_all_time,
          str(observed_all_time))
    check("Nederland June observed refunds = 0", pl2["refunded_payment_count"] == 0)
    # The caveat is data-driven: it must retire itself the moment a real
    # refunded=true row appears, rather than lingering as a stale disclaimer.
    if observed_all_time == 0:
        check("refund_path_validated is False while no sample exists",
              pl2["refund_path_validated"] is False)
        check("a caveat is supplied", bool(pl2["refund_caveat"]))
        cav = pl2["refund_caveat"]
        check("caveat says no positive sample exists",
              "has ever been observed" in cav and "No refunded = true" in cav)
        check("caveat says the path is not empirically validated",
              "NOT empirically validated" in cav)
        check("caveat forbids calling it high confidence",
              "high confidence" in cav)
        check("caveat does not imply refunds are impossible",
              "does not mean refunds are impossible" in cav)
    else:
        check("caveat retires once a real refund is observed",
              pl2["refund_path_validated"] is True and pl2["refund_caveat"] is None)
    check("prompt carries the refund rule", "REFUNDS ARE UNVALIDATED" in flat2)
    check("prompt requires reporting the observed count",
          "give the observed count" in flat2)
    check("prompt forbids 'refunds are impossible'",
          "Do NOT say refunds are impossible" in flat2)
    check("prompt forbids high-confidence framing",
          "do NOT describe it as high confidence" in flat2)

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
