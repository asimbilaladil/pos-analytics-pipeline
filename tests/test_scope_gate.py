#!/usr/bin/env python3
"""A12 hard-enforcement tests: the gate must not depend on the model.

Run:  venv/bin/python tests/test_scope_gate.py     (exit 0 = pass)

enforce_scope() is the server-side boundary. It runs before any SQL reaches the
database, reconciles the declared scope itself, and rejects a query whose scope
cannot be proven to sit inside what was reconciled. check_data is an optional
inspection tool for the model and is deliberately NOT tested as a boundary here.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from dotenv import load_dotenv

load_dotenv(".env", override=True)
import chat_sql as cs  # noqa: E402

NED, JUN_A, JUN_B = 26, "2026-06-01", "2026-07-01"
BEA, SEP_A, SEP_B = 14, "2026-09-01", "2026-10-01"

NED_JUNE_SQL = ("SELECT COUNT(*) FROM v_orders_classified "
                "WHERE establishment_id = 26 AND business_date >= '2026-06-01' "
                "AND business_date < '2026-07-01'")
results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail else ""))


def allowed(sql, est, a, b, cache=None):
    try:
        return True, cs.enforce_scope(sql, est, a, b, cache)
    except cs.ScopeError as e:
        return False, str(e)


def main() -> int:
    cache: dict = {}

    print("=== A. run_sql without check_data still reconciles server-side ===")
    ok, meta = allowed(NED_JUNE_SQL, NED, JUN_A, JUN_B, cache)
    check("gated query returns a reconciliation payload", ok and meta is not None)
    check("payload is for the declared scope",
          ok and meta["scope"]["establishment_id"] == NED
          and meta["scope"]["period_start"] == JUN_A)
    check("status computed without any check_data call",
          ok and meta["reconciliation"]["status"] == "PASS")
    check("scope cached for the turn", (NED, JUN_A, JUN_B) in cache)

    print("=== B. failing scope is blocked BEFORE execution ===")
    bea_sql = ("SELECT COUNT(*) FROM v_orders_classified WHERE establishment_id = 14 "
               "AND business_date >= '2026-09-01' AND business_date < '2026-10-01'")
    ok, err = allowed(bea_sql, BEA, SEP_A, SEP_B, cache)
    check("Beaumont September rejected", not ok)
    check("rejection names the gate", not ok and "reconciliation gate" in err)
    check("rejection carries blocking reasons",
          not ok and ("incomplete" in err or "business days" in err))

    print("=== C. passing scope is allowed ===")
    ok, meta = allowed(NED_JUNE_SQL, NED, JUN_A, JUN_B, cache)
    check("Nederland June allowed", ok)
    check("real count preserved for the answer",
          ok and meta["volumes"]["real_count"] == 4565)

    print("=== D. cannot widen the period after reconciling a narrow one ===")
    wider = ("SELECT COUNT(*) FROM v_orders_classified WHERE establishment_id = 26 "
             "AND business_date >= '2026-01-01' AND business_date < '2026-07-01'")
    ok, err = allowed(wider, NED, JUN_A, JUN_B, cache)
    check("wider dates under a June scope rejected", not ok)
    check("rejection names the offending date", not ok and "2026-01-01" in err)
    ok2, meta2 = allowed(wider, NED, "2026-01-01", JUN_B, cache)
    check("same SQL allowed once the scope is declared honestly",
          ok2 and meta2["scope"]["period_start"] == "2026-01-01")
    check("widened scope reconciled separately",
          ("2026-01-01" in str(list(cache.keys()))))

    print("=== E. cannot switch store after reconciling another ===")
    other = ("SELECT COUNT(*) FROM v_orders_classified WHERE establishment_id = 40 "
             "AND business_date >= '2026-06-01' AND business_date < '2026-07-01'")
    ok, err = allowed(other, NED, JUN_A, JUN_B, cache)
    check("store 40 under a store 26 scope rejected", not ok)
    check("rejection names the mismatch", not ok and "40" in err)
    noest = ("SELECT COUNT(*) FROM v_orders_classified "
             "WHERE business_date >= '2026-06-01' AND business_date < '2026-07-01'")
    ok, err = allowed(noest, NED, JUN_A, JUN_B, cache)
    check("missing establishment filter under a store scope rejected", not ok)

    print("=== F. network scope ===")
    ok, meta = allowed(noest, None, JUN_A, JUN_B, cache)
    check("network scope allowed without an establishment filter", ok)
    check("network meta covers all stores",
          ok and meta["scope"]["store_name"] == "ALL STORES")
    check("network volumes exceed one store",
          ok and meta["volumes"]["real_count"] > 4565,
          f"{meta['volumes']['real_count'] if ok else '-'} REAL")
    ok, meta = allowed(
        "SELECT COUNT(*) FROM v_orders_classified WHERE establishment_id = 26 "
        "AND business_date >= '2026-06-01' AND business_date < '2026-07-01'",
        None, JUN_A, JUN_B, cache)
    check("a single store is a valid subset of a network scope", ok)

    print("=== G. reference lookups stay ungated ===")
    for name, sql in (
            ("establishments", "SELECT COUNT(*) FROM establishments"),
            ("products", "SELECT COUNT(*) FROM products"),
            ("weather", "SELECT COUNT(*) FROM weather_daily"),
            ("store cohort", "SELECT * FROM v_store_cohort"),
            ("join of reference tables",
             "SELECT * FROM establishments e JOIN v_store_cohort c "
             "ON c.establishment_id = e.id")):
        ok, meta = allowed(sql, None, None, None, cache)
        check(f"{name} allowed with no scope", ok and meta is None)

    print("=== H. business tables are never exempt ===")
    for name, sql in (
            ("orders_v2", "SELECT COUNT(*) FROM orders_v2"),
            ("order_items_v2", "SELECT COUNT(*) FROM order_items_v2"),
            ("features", "SELECT COUNT(*) FROM features_daily_summary_v2"),
            ("payments view", "SELECT COUNT(*) FROM v_payments_daily_v2"),
            ("business joined to reference",
             "SELECT * FROM establishments e JOIN orders_v2 o ON o.establishment_id = e.id")):
        ok, err = allowed(sql, None, None, None, cache)
        check(f"{name} rejected without a scope", not ok)

    print("=== I. unprovable scopes ===")
    rel = ("SELECT COUNT(*) FROM v_orders_classified WHERE establishment_id = 26 "
           "AND business_date >= current_date - 30")
    ok, err = allowed(rel, NED, JUN_A, JUN_B, cache)
    check("current_date arithmetic rejected", not ok)
    ok, err = allowed(
        "SELECT COUNT(*) FROM v_orders_classified WHERE establishment_id = 26",
        NED, JUN_A, JUN_B, cache)
    check("no date bound at all rejected", not ok)
    ok, err = allowed(
        "SELECT COUNT(*) FROM v_orders_classified WHERE establishment_id = 26 "
        "AND business_date >= '2026-06-01' AND business_date < now()",
        NED, JUN_A, JUN_B, cache)
    check("now() rejected", not ok)

    print("=== J. FAIL is per-scope, not turn-wide ===")
    # One shared cache stands in for a single assistant turn.
    turn: dict = {}
    bea_sep = ("SELECT SUM(final_total) FROM v_orders_classified "
               "WHERE establishment_id = 14 AND business_date >= '2026-09-01' "
               "AND business_date < '2026-10-01'")
    bea_aug = ("SELECT SUM(final_total) FROM v_orders_classified "
               "WHERE establishment_id = 14 AND business_date >= '2026-08-01' "
               "AND business_date < '2026-09-01'")

    ok, err = allowed(bea_sep, BEA, SEP_A, SEP_B, turn)
    check("A1. Beaumont September fails", not ok)

    # A. the same store, a sound period, in the same turn
    ok, meta = allowed(bea_aug, BEA, "2026-08-01", "2026-09-01", turn)
    check("A2. Beaumont August allowed after September failed", ok,
          "" if ok else str(meta)[:90])
    check("A3. August reconciled on its own merits",
          ok and meta["scope"]["period_start"] == "2026-08-01"
          and meta["reconciliation"]["status"] in ("PASS", "WARN"))

    # B. the failed scope stays failed
    ok, err = allowed(bea_sep, BEA, SEP_A, SEP_B, turn)
    check("B. repeating the failed September scope is still blocked", not ok)
    check("B. failed scope cached as failed",
          turn[(BEA, SEP_A, SEP_B)]["analysis_permitted"] is False)

    # C. a different store is judged independently
    ok, meta = allowed(NED_JUNE_SQL, NED, JUN_A, JUN_B, turn)
    check("C. another store allowed after Beaumont failed", ok)

    # D. every distinct scope gets its own reconciliation entry
    check("D. each scope cached separately",
          {(BEA, SEP_A, SEP_B), (BEA, "2026-08-01", "2026-09-01"),
           (NED, JUN_A, JUN_B)} <= set(turn),
          f"{len(turn)} scopes cached")
    narrower = ("SELECT SUM(final_total) FROM v_orders_classified "
                "WHERE establishment_id = 14 AND business_date >= '2026-08-01' "
                "AND business_date < '2026-08-15'")
    ok, meta = allowed(narrower, BEA, "2026-08-01", "2026-08-15", turn)
    check("D. narrower scope reconciled separately",
          ok and (BEA, "2026-08-01", "2026-08-15") in turn)
    check("D. narrower scope has its own totals",
          ok and meta["reconciliation"]["computed_total"]
          != turn[(BEA, "2026-08-01", "2026-09-01")]["reconciliation"]["computed_total"])

    # E. changing scope is not a way around the gate
    ok, err = allowed(bea_sep, None, None, None, turn)
    check("E. omitted scope still blocked for business relations", not ok)
    ok, err = allowed(bea_sep, BEA, "2026-08-01", "2026-09-01", turn)
    check("E. September SQL under an August scope rejected", not ok,
          "" if not ok else "ALLOWED — scope mismatch not caught")

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("FAILED: " + ", ".join(failed))
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
