#!/usr/bin/env python3
"""A7 tests: store age vs data history.

Run:  venv/bin/python tests/test_store_cohort.py     (exit 0 = pass)

The property under test is restraint: no opening date is invented from our own
data, store age stays unknown until a verified source supplies it, and the
difference between "we have N months of data" and "the store is N months old"
is preserved everywhere.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from dotenv import load_dotenv

load_dotenv(".env", override=True)
import re  # noqa: E402
import psycopg2  # noqa: E402
import chat_sql as cs  # noqa: E402

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

    print("=== A. no opening date is inferred from our own data ===")
    cur.execute("SELECT COUNT(*) FROM establishments WHERE open_date_source = 'inferred_first_order'")
    check("no establishment carries an inferred open_date", cur.fetchone()[0] == 0)
    cur.execute("SELECT COUNT(*) FROM v_store_cohort WHERE verified_open_date IS NOT NULL")
    check("verified_open_date is NULL for every store (none is verified yet)",
          cur.fetchone()[0] == 0)
    cur.execute("SELECT COUNT(*) FROM v_store_cohort WHERE weeks_since_open IS NOT NULL")
    check("weeks_since_open is NULL wherever the open date is unknown",
          cur.fetchone()[0] == 0)
    # The specific bad value this migration removed.
    cur.execute("SELECT open_date FROM establishments WHERE id = 48")
    check("Downtown Houston's 2026-04-21 inferred date is gone",
          cur.fetchone()[0] is None)
    cur.execute("""SELECT COUNT(*) FROM v_orders_classified
                   WHERE establishment_id = 48 AND business_date = '2026-04-21'
                     AND txn_class = 'REAL'""")
    check("...and that date had zero REAL orders, so it was never an opening",
          cur.fetchone()[0] == 0)

    print("=== B. store age and data history are separate ===")
    cur.execute("""SELECT establishment_id, first_seen_real_order_date,
                          history_truncated, available_history_days
                   FROM v_store_cohort ORDER BY establishment_id""")
    rows = cur.fetchall()
    check("all 12 stores present", len(rows) == 12, str(len(rows)))
    trunc = [r[0] for r in rows if r[2]]
    check("10 stores are flagged history_truncated", len(trunc) == 10, str(len(trunc)))
    check("the truncated ones all start at the backfill edge",
          all(str(r[1]) == "2026-01-01" for r in rows if r[2]))
    untrunc = sorted(r[0] for r in rows if not r[2])
    check("only Downtown Houston and Cypress are untruncated",
          untrunc == [48, 54], str(untrunc))
    check("every store reports available_history_days",
          all(r[3] is not None for r in rows))

    print("=== C. Revel provisioning date is a bound, not an opening ===")
    cur.execute("""SELECT establishment_id, revel_account_created_date,
                          first_seen_real_order_date
                   FROM v_store_cohort WHERE establishment_id IN (48, 54)
                   ORDER BY establishment_id""")
    for est, created, first_real in cur.fetchall():
        check(f"est {est}: provisioning precedes first real sale",
              created < first_real, f"{created} -> {first_real}")
    cur.execute("SELECT COUNT(*) FROM v_store_cohort WHERE revel_account_created_date IS NOT NULL")
    check("provisioning date recorded for all 12", cur.fetchone()[0] == 12)
    cur.execute("SELECT COUNT(*) FROM v_store_cohort WHERE verified_open_date = revel_account_created_date")
    check("provisioning date is never copied into verified_open_date",
          cur.fetchone()[0] == 0)

    print("=== D. no maturity threshold is asserted ===")
    cur.execute("SELECT DISTINCT maturity_threshold_status FROM v_store_cohort")
    st = [r[0] for r in cur.fetchall()]
    check("maturity_threshold_status says none is configured",
          st == ["no maintained threshold configured"], str(st))
    cur.execute("""SELECT COUNT(*) FROM information_schema.columns
                   WHERE table_name = 'v_store_cohort' AND column_name = 'store_age_bucket'""")
    check("the invented honeymoon/ramp/mature bucket is gone", cur.fetchone()[0] == 0)

    print("=== E. year-over-year is impossible and said so ===")
    cur.execute("SELECT MIN(business_date) FROM v_orders_classified")
    check("no data exists before 2026-01-01", str(cur.fetchone()[0]) == "2026-01-01")
    meta = cs.meta_extract(26, "2026-06-01", "2026-07-01")
    sc = meta["store_cohort"]
    check("meta_extract reports yoy_supported False", sc["yoy_supported"] is False)
    check("earliest network data stated", sc["earliest_data_network_wide"] == "2026-01-01")

    print("=== F. meta_extract cohort block ===")
    for key in ("verified_open_date", "open_date_source", "open_date_confidence",
                "first_seen_real_order_date", "available_history_start",
                "available_history_days", "weeks_since_open",
                "maturity_threshold_status", "comparison_limitations"):
        check(f"store_cohort.{key} present", key in sc)
    check("open_date_confidence is unknown", sc["open_date_confidence"] == "unknown")
    check("weeks_since_open is None", sc["weeks_since_open"] is None)
    check("history_truncated True for Nederland", sc["history_truncated"] is True)
    check("limitations separate data history from store age",
          "Data history is not store age" in sc["comparison_limitations"])

    print("=== G. assistant guardrails ===")
    flat = re.sub(r"\s+", " ", cs._system_static())
    for probe, label in (
            ("verified_open_date is NULL for ALL 12 stores", "states all open dates unknown"),
            ("DATA HISTORY IS NOT STORE AGE", "separates history from age"),
            ("NEVER compute weeks_since_open from first_seen_* dates", "forbids inferring age"),
            ("NEVER call a store new, young, mature or in a honeymoon period", "forbids maturity labels"),
            ("YEAR-OVER-YEAR IS IMPOSSIBLE HERE", "forbids YoY"),
            ("lower bound on the opening date", "frames provisioning as a bound")):
        check(f"prompt {label}", probe in flat)
    check("stale inferred-date documentation is gone",
          "INFERRED from the first order" not in flat)
    check("stale 'Cypress opened June 2026' example is gone",
          "Cypress opened June 2026" not in flat)

    print("=== H. nothing else moved ===")
    check("A12 reconciliation still PASS", meta["reconciliation"]["status"] == "PASS")
    check("A3 entrées unchanged", meta["entree_classification"]["entrees"] == 6323.0)
    check("A10 payment capture unchanged",
          meta["payment_linkage"]["payment_capture_rate"] == 100.0)
    check("A6 time contract unchanged",
          meta["time"]["business_date_method"] == "local_calendar_date")
    check("REAL count unchanged", meta["volumes"]["real_count"] == 4565)

    # ── A7 consistency: the prompt must not assert an opening date ──────────
    # DATA_NOTES once said "LCF Cypress (id 54) opened in June 2026", which
    # contradicted the cohort view's deliberate verified_open_date = NULL.
    # Cypress is the one store whose history is NOT truncated, so the temptation
    # to read first-seen as opening is strongest exactly there.
    print("\n=== Z. prompt asserts no opening date for Cypress ===")
    flat = re.sub(r"\s+", " ", cs.DATA_NOTES + cs.COHORT_RULES)
    # Every mention of the phrase must be a PROHIBITION, never an assertion.
    # Counting bare occurrences would fail on the prohibition itself, and
    # dropping the check would let a real assertion slip back in.
    asserted = [m.start() for m in re.finditer(r"Cypress[^.]{0,20}opened in June", flat)
                if not flat[max(0, m.start() - 12):m.start()].rstrip().endswith("Do NOT say")]
    check("prompt never ASSERTS that Cypress opened in June",
          not asserted, f"{len(asserted)} bare assertion(s)")
    check("the old affirmative sentence is gone",
          "Cypress (id 54) opened in June 2026" not in flat)
    check("prompt states it as a fact about our DATA, not the store",
          "NO observed sales history in this database before June 2026" in flat)
    check("prompt explicitly forbids calling it an opening date",
          "Do NOT say Cypress opened in June" in flat)
    check("prompt forbids reading first-seen as an opening date",
          "do NOT treat the first date we can see as an opening date" in flat)
    check("operational purpose preserved: missing history is not a decline",
          "as a real decline" in flat)
    cur.execute("""SELECT verified_open_date, open_date_confidence
                   FROM v_store_cohort WHERE establishment_id = 54""")
    row = cur.fetchone()
    check("Cypress verified_open_date is still NULL", row[0] is None)
    check("Cypress open_date_confidence is still 'unknown'", row[1] == "unknown")
    cur.execute("SELECT COUNT(*) FROM v_store_cohort WHERE verified_open_date IS NOT NULL")
    check("no store has a verified opening date", cur.fetchone()[0] == 0)

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
