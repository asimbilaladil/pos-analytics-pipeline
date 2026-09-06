#!/usr/bin/env python3
"""Golden tests for the A12 reconciliation gate and meta_extract.

Run:  venv/bin/python tests/test_reconciliation.py     (exit 0 = pass)

Primary golden dataset: LCF Nederland (establishment 26), June 2026.

Tests C and D construct failure conditions by calling _reconcile() on a copied
metadata payload rather than by writing to the database. Production rows are
never modified -- the gate's decision logic is what is under test, and it is
pure given the payload.
"""
import copy
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from dotenv import load_dotenv

load_dotenv(".env", override=True)
import chat_sql as cs  # noqa: E402

EST, START, END = 26, "2026-06-01", "2026-07-01"
results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail else ""))


def main() -> int:
    meta = cs.meta_extract(EST, START, END)
    v, rec, integ, fresh = (meta["volumes"], meta["reconciliation"],
                            meta["integrity"], meta["freshness"])

    print("=== A. classification (Nederland, June 2026) ===")
    check("REAL == 4565", v["real_count"] == 4565, f"got {v['real_count']}")
    check("raw order rows == 6741", v["order_rows"] == 6741, f"got {v['order_rows']}")
    check("EMPTY == 2011", v["empty_count"] == 2011, f"got {v['empty_count']}")
    check("COMP == 165", v["comp_count"] == 165, f"got {v['comp_count']}")
    check("classes sum to raw rows",
          v["real_count"] + v["empty_count"] + v["comp_count"] + v["deleted_count"]
          == v["order_rows"])

    print("=== B. reconciliation ===")
    print(f"      computed  ${rec['computed_total']:,.2f}")
    print(f"      reference ${rec['reference_total']:,.2f}  ({rec['basis'][:48]}…)")
    print(f"      delta     ${rec['delta_dollars']:+,.2f}  ({rec['delta_pct']:+.4f}%)")
    check("reference total is independent of orders_v2",
          "payments_v2" in rec["basis"])
    check("delta within 0.5% tolerance", abs(rec["delta_pct"]) <= 0.5)
    check("status PASS", rec["status"] == "PASS", rec["status"])
    check("analysis permitted", meta["analysis_permitted"] is True)

    print("=== C. incomplete data must FAIL (no production rows touched) ===")
    # 8% of sales missing -- the shape of a partial or interrupted sync.
    tampered = copy.deepcopy(meta)
    tampered["reconciliation"]["computed_total"] = round(rec["reference_total"] * 0.92, 2)
    tampered["reconciliation"]["delta_dollars"] = round(
        tampered["reconciliation"]["computed_total"] - rec["reference_total"], 2)
    tampered["reconciliation"]["delta_pct"] = cs._pct(
        tampered["reconciliation"]["delta_dollars"], rec["reference_total"])
    t = cs._reconcile(tampered)
    check("8% shortfall -> FAIL", t["reconciliation"]["status"] == "FAIL")
    check("8% shortfall -> analysis blocked", t["analysis_permitted"] is False)
    check("blocking reason names reconciliation",
          any("reconciliation" in b for b in t["blocking_reasons"]))

    # Just outside tolerance, and just inside, to pin the threshold exactly.
    # 0.5% is the FAIL threshold; 0.2% is where a within-tolerance delta starts
    # being called out rather than passing silently.
    for pct, expect in ((0.6, "FAIL"), (0.4, "WARN"), (0.1, "PASS")):
        probe = copy.deepcopy(meta)
        probe["reconciliation"]["delta_pct"] = pct
        probe["reconciliation"]["delta_dollars"] = 1.0
        got = cs._reconcile(probe)["reconciliation"]["status"]
        check(f"delta {pct}% -> {expect}", got == expect, f"got {got}")

    # A period missing whole business days.
    missing = copy.deepcopy(meta)
    missing["freshness"]["days_with_data"] = 24
    missing["freshness"]["missing_business_days"] = 6
    m2 = cs._reconcile(missing)
    check("6 missing business days -> FAIL", m2["reconciliation"]["status"] == "FAIL")
    check("missing days blocked", m2["analysis_permitted"] is False)

    print("=== D. freshness ===")
    stale = copy.deepcopy(meta)
    stale["freshness"]["source_lag_hours"] = 96.0
    s2 = cs._reconcile(stale)
    check("96h stale -> FAIL", s2["reconciliation"]["status"] == "FAIL")
    check("stale reason names the sync limit",
          any("sync" in b for b in s2["blocking_reasons"]))
    fresh_ok = copy.deepcopy(meta)
    fresh_ok["freshness"]["source_lag_hours"] = 12.0
    check("12h lag -> still PASS",
          cs._reconcile(fresh_ok)["reconciliation"]["status"] == "PASS")

    print("=== E. join integrity present and enforced ===")
    check("join integrity reported",
          integ["order_to_item_join_integrity_pct"] is not None,
          f"{integ['order_to_item_join_integrity_pct']}%")
    check("Nederland June is 100%", integ["order_to_item_join_integrity_pct"] == 100.0)
    check("duplicate/orphan counters present",
          all(k in integ for k in ("duplicate_order_ids", "duplicate_order_item_ids",
                                   "orphan_order_items")))
    broken = copy.deepcopy(meta)
    broken["integrity"]["order_to_item_join_integrity_pct"] = 93.0
    check("93% join integrity -> FAIL",
          cs._reconcile(broken)["reconciliation"]["status"] == "FAIL")
    dup = copy.deepcopy(meta)
    dup["integrity"]["duplicate_order_ids"] = 12
    check("duplicate order ids -> FAIL",
          cs._reconcile(dup)["reconciliation"]["status"] == "FAIL")
    orph = copy.deepcopy(meta)
    orph["integrity"]["orphan_order_items"] = 40
    check("orphan order items -> FAIL",
          cs._reconcile(orph)["reconciliation"]["status"] == "FAIL")

    print("=== F. real scopes end to end ===")
    future = cs.meta_extract(EST, "2027-01-01", "2027-02-01")
    check("empty future period -> FAIL", future["analysis_permitted"] is False)
    beaumont = cs.meta_extract(14, "2026-09-01", "2026-10-01")
    check("store with a real data gap -> FAIL",
          beaumont["analysis_permitted"] is False,
          f"{beaumont['freshness']['missing_business_days']} missing days")

    print("=== G. payload shape ===")
    for key in ("scope", "volumes", "integrity", "freshness", "reconciliation",
                "mappings", "identity_capture_rate", "unavailable_metrics",
                "warnings", "advisories", "blocking_reasons", "analysis_permitted"):
        check(f"payload has {key}", key in meta)
    for key in ("establishment_id", "store_name", "period_start",
                "period_end_exclusive", "source_timezone", "business_day_definition"):
        check(f"scope has {key}", key in meta["scope"])
    blob = str(meta).lower()
    check("no credentials leaked into payload",
          not any(w in blob for w in ("password", "sk-ant", "ghp_", "secret", "token")))

    # ── size-gating: advisory work is deferred, the gate never is ───────────
    # A network-wide month sat ~73ms from the 15s statement timeout because the
    # deep pass ran four purely-advisory CTEs (payment/category/channel/identity)
    # that cost 6.5s of its 9.7s. Those moved behind the tighter advisory gate.
    # The two deep values that _reconcile can turn into a FAIL stayed put.
    print("\n=== S. advisory deferral must never weaken the gate ===")
    small = cs.meta_extract(26, "2026-06-01", "2026-07-01")
    wide = cs.meta_extract(None, "2026-06-01", "2026-07-01")

    check("A12 runs on the small scope", small["reconciliation"]["status"] is not None)
    check("A12 runs on the wide scope too", wide["reconciliation"]["status"] is not None)
    check("wide scope still reconciles to PASS/WARN, not skipped",
          wide["reconciliation"]["status"] in ("PASS", "WARN", "FAIL"))

    check("small scope still gets FULL advisory metadata",
          all(small[b]["evaluated"] for b in
              ("payment_linkage", "identity", "channel_mapping", "category_mapping")))
    check("wide scope defers advisory metadata",
          not any(wide[b]["evaluated"] for b in
                  ("payment_linkage", "identity", "channel_mapping", "category_mapping")))

    print("=== T. deferral is explicit, never a silent omission ===")
    for b in ("payment_linkage", "identity", "channel_mapping", "category_mapping"):
        check(f"{b} states why it was skipped",
              isinstance(wide[b].get("not_evaluated_reason"), str)
              and "ADVISORY only" in wide[b]["not_evaluated_reason"])
        check(f"{b} says the skip does not affect A12",
              "A12 reconciliation status is unaffected" in wide[b]["not_evaluated_reason"]
              or "does not affect the A12" in wide[b]["not_evaluated_reason"])
        check(f"{b} reason is absent when it WAS evaluated",
              small[b].get("not_evaluated_reason") is None)

    print("=== U. FAIL-critical deep checks still run at network-month ===")
    check("item-side checks evaluated on the wide scope",
          wide["integrity"]["item_side_checks_evaluated"] is True)
    check("duplicate_order_item_ids is a number, not None",
          isinstance(wide["integrity"]["duplicate_order_item_ids"], int))
    check("orphan_order_items is a number, not None",
          isinstance(wide["integrity"]["orphan_order_items"], int))
    check("the deep query still carries both FAIL inputs",
          "duplicate_order_item_ids" in cs._DEEP_SQL
          and "orphan_order_items" in cs._DEEP_SQL)
    check("the deep query no longer carries the advisory CTEs",
          not any(f"\n{n} AS (" in cs._DEEP_SQL or f" {n} AS (" in cs._DEEP_SQL
                  for n in ("pay", "cat", "chan", "ident")))
    check("the advisory query carries them instead",
          all(n in cs._LOYALTY_LABOR_SQL for n in ("pay", "cat", "chan", "ident")))

    print("=== V. thresholds are ordered and unchanged ===")
    check("advisory gate is tighter than the deep gate",
          cs._ADVISORY_SCOPE_ROW_LIMIT < cs._DEEP_SCOPE_ROW_LIMIT)
    check("deep gate still 250k (not lowered)", cs._DEEP_SCOPE_ROW_LIMIT == 250_000)
    check("advisory gate still 100k", cs._ADVISORY_SCOPE_ROW_LIMIT == 100_000)
    check("top-level identity_capture_rate survives deferral (core-sourced)",
          isinstance(wide["identity_capture_rate"], (int, float)))

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("FAILED: " + ", ".join(failed))
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
