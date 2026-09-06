#!/usr/bin/env python3
"""A11 tests: customer identity, selection bias, and privacy.

Run:  venv/bin/python tests/test_identity_selection_bias.py     (exit 0 = pass)

The central risks: speaking for all customers from a ~11% identified subset,
mistaking staff ids for customers, mistaking anonymity for non-membership, and
computing per-customer averages over accounts that are not people.
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from dotenv import load_dotenv

load_dotenv(".env", override=True)
import psycopg2  # noqa: E402
import chat_sql as cs  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail else ""))


def _llm_can_read(cur, table):
    """True if the assistant's DB role can SELECT from this relation."""
    cur.execute("SELECT has_table_privilege('laynes_ro', %s, 'SELECT')", (table,))
    return cur.fetchone()[0]


def main() -> int:
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"), dbname=os.getenv("DB_NAME", "laynes"),
        user=os.environ["DB_USER"], password=os.environ["DB_PASS"])
    conn.set_session(readonly=True)
    cur = conn.cursor()
    flat = re.sub(r"\s+", " ", cs._system_static())
    meta = cs.meta_extract(26, "2026-06-01", "2026-07-01")
    idn = meta["identity"]

    print("=== A. operator ids are not customer identity ===")
    cur.execute("""SELECT pg_get_viewdef('v_order_identity_context'::regclass, true)""")
    vdef = cur.fetchone()[0]
    for col in ("created_by_user_id", "updated_by_user_id", "voided_by_user_id",
                "discounted_by_user_id", "opened_by_user_id", "closed_by_user_id"):
        check(f"identity view does not use {col}", col not in vdef)
    check("identity source is orders_v2.customer_id",
          idn["identity_field_source"] == "orders_v2.customer_id")
    check("prompt states operator ids are staff",
          "OPERATOR IDS ARE NOT CUSTOMERS" in flat)

    print("=== B. anonymous is unknown, not non-member ===")
    # Loyalty tables now EXIST (order_loyalty_v2), but are deliberately not
    # granted to the assistant's role until an agreed aggregate view is added.
    # The guarantee under test is "the assistant cannot read loyalty", not
    # "loyalty does not exist" -- the latter was never true, only unverified.
    cur.execute("""SELECT c.table_name, c.column_name
                   FROM information_schema.columns c
                   WHERE c.table_schema = 'public'
                     AND c.column_name ~* 'loyalty|reward|member'""")
    readable = [(t, col) for t, col in cur.fetchall()
                if _llm_can_read(cur, t)]
    check("no loyalty/rewards/member column is readable by the assistant",
          not readable, str(readable))
    cur.execute("""SELECT table_name FROM information_schema.tables
                   WHERE table_schema = 'public'
                     AND table_name ~* 'customer|loyalty|reward|member'""")
    readable_t = [t for (t,) in cur.fetchall() if _llm_can_read(cur, t)]
    check("no customer or loyalty table is readable by the assistant",
          not readable_t, str(readable_t))
    check("loyalty tables are absent from the relation allowlist",
          not [r for r in cs._ALLOWED_RELATIONS
               if re.search(r"loyalty|reward", r, re.I)])
    check("meta says loyalty is NOT INGESTED rather than non-existent",
          "NOT INGESTED" in idn["loyalty_membership"]
          and "not non-existent" in idn["loyalty_membership"])
    check("meta does not claim loyalty is unrecorded anywhere",
          "not recorded anywhere" not in idn["loyalty_membership"])
    check("meta names the upstream source",
          "gift_reward_data" in idn["loyalty_membership"])
    check("prompt forbids equating anonymous with non-member",
          "ANONYMOUS MEANS UNKNOWN, NOT NON-MEMBER" in flat)
    check("prompt says loyalty is not yet ingested, not that it does not exist",
          "not yet ingested" in flat and "Do NOT say loyalty does not exist" in flat)
    check("prompt states loyalty analysis is blocked until the backfill",
          "until the safe loyalty backfill" in flat)
    check("prompt separates customer identity from loyalty membership",
          "CUSTOMER IDENTITY IS NOT LOYALTY MEMBERSHIP" in flat)

    print("=== C. capture rate uses the REAL denominator ===")
    cur.execute("""SELECT COUNT(*), COUNT(customer_id) FROM v_orders_classified
                   WHERE establishment_id = 26 AND txn_class = 'REAL'
                     AND business_date >= '2026-06-01' AND business_date < '2026-07-01'""")
    real_n, ident_n = cur.fetchone()
    check("REAL transactions = 4565", real_n == 4565, str(real_n))
    check("identified transactions = 488", ident_n == 488, str(ident_n))
    check("capture rate = identified / REAL, 0-100 scale",
          abs(idn["identity_capture_rate"] - (100.0 * ident_n / real_n)) < 0.01,
          str(idn["identity_capture_rate"]))
    check("capture rate is 10.69%", idn["identity_capture_rate"] == 10.69)
    check("unidentified = REAL - identified",
          idn["unidentified_transactions"] == real_n - ident_n)
    # The wrong denominator would give a wildly different number.
    check("denominator is NOT distinct customers",
          idn["identity_capture_rate"] != round(100.0 * ident_n / 88, 2))

    print("=== D. identified subset is not all customers ===")
    check("distinct identifiable customers = 88",
          idn["distinct_identified_customers"] == 88)
    check("prompt forbids claiming total unique customers",
          "UNIQUE CUSTOMERS CANNOT BE COUNTED" in flat)
    check("prompt requires the 'identified subset' framing",
          "THE IDENTIFIED SUBSET" in flat)
    check("selection bias flagged as material below 30% capture",
          idn["selection_bias_status"] == "material")

    print("=== E. per-customer averages must exclude non-individuals ===")
    cur.execute("""
        SELECT COUNT(*), SUM(v), ROUND(AVG(v), 2), MAX(v) FROM (
            SELECT o.customer_id, COUNT(*) v FROM v_orders_classified o
            WHERE o.establishment_id = 26 AND o.txn_class = 'REAL'
              AND o.business_date >= '2026-06-01' AND o.business_date < '2026-07-01'
              AND o.customer_id IS NOT NULL GROUP BY 1) t""")
    n_all, visits_all, avg_all, max_all = cur.fetchone()
    check("naive average is inflated by non-individuals",
          float(avg_all) > 5 and max_all > 300, f"avg {avg_all}, max {max_all}")
    ids = cs._suspected_non_individual_ids()
    check("suspected non-individual identities are detected", len(ids) >= 20, str(len(ids)))
    cur.execute("""
        SELECT COUNT(*), ROUND(AVG(v), 2), MAX(v) FROM (
            SELECT o.customer_id, COUNT(*) v FROM v_orders_classified o
            WHERE o.establishment_id = 26 AND o.txn_class = 'REAL'
              AND o.business_date >= '2026-06-01' AND o.business_date < '2026-07-01'
              AND o.customer_id IS NOT NULL AND NOT (o.customer_id = ANY(%s))
            GROUP BY 1) t""", (ids,))
    n_ex, avg_ex, max_ex = cur.fetchone()
    check("excluding them changes the answer materially",
          float(avg_ex) < 2 and float(avg_all) > 5, f"{avg_all} -> {avg_ex}")
    check("Nederland June has 2 suspected non-individual identities",
          idn["suspected_non_individual_ids"] == 2)
    cur.execute("SELECT pg_get_viewdef('v_identity_profile'::regclass, true)")
    pdef = cur.fetchone()[0]
    check("the flag is named as a suspicion, not a verdict",
          "suspected_non_individual" in pdef
          and "confirmed" not in pdef.lower())
    cur.execute("""SELECT COUNT(*) FROM information_schema.columns
                   WHERE table_name = 'v_identity_profile'
                     AND column_name IN ('is_bot', 'confirmed_non_individual')""")
    check("no column asserts a confirmed non-individual", cur.fetchone()[0] == 0)
    check("prompt requires checking the flag before per-ID averages",
          "CHECK suspected_non_individual BEFORE any per-ID average" in flat)
    check("prompt routes around the slow profile join",
          "Do NOT join that profile view row-wise into a scoped query" in flat)

    print("=== F. repeat definition states its window ===")
    check("prompt requires the repeat window to be stated",
          "REPEAT CUSTOMER must state its window" in flat)
    check("prompt distinguishes in-period from historical repeat",
          "is not \"a repeat customer historically\"" in flat)

    print("=== G. identity capture is advisory, never an A12 failure ===")
    check("advisory_only is True", idn["advisory_only"] is True)
    check("A12 still PASS despite 10.69% capture",
          meta["reconciliation"]["status"] == "PASS")
    check("analysis remains permitted", meta["analysis_permitted"] is True)
    check("limitations say capture never affects reconciliation",
          "never affects the A12 reconciliation status" in idn["limitations"])
    # _reconcile MAY read identity capture -- but only to append an advisory.
    # It must never contribute to fails or warns, which are what move the gate.
    import inspect
    recon_src = inspect.getsource(cs._reconcile)
    idx = recon_src.find("identity_capture_rate")
    check("_reconcile touches identity capture only once", idx != -1)
    following = recon_src[idx:idx + 400]
    check("...and only to append an advisory note",
          "notes.append" in following
          and "fails.append" not in following and "warns.append" not in following)
    check("both capture fields now use the same 0-100 scale",
          meta["identity_capture_rate"] == idn["identity_capture_rate"],
          f"{meta['identity_capture_rate']} vs {idn['identity_capture_rate']}")
    check("no warnings were raised by low capture", meta["warnings"] == [])
    check("an advisory WAS raised", any("identity captured on only" in a
                                        for a in meta["advisories"]))

    print("=== H. no causal loyalty claim ===")
    check("prompt forbids causal language", "NO CAUSAL CLAIMS" in flat)
    check("prompt explains the channel confound",
          "confounded by channel" in flat)

    print("=== I. privacy ===")
    cur.execute("""SELECT table_name, column_name FROM information_schema.columns
                   WHERE table_schema = 'public'
                     AND column_name ~* 'email|phone|first_name|last_name|address'""")
    pii = cur.fetchall()
    check("the only PII column in the database is app_users.email",
          pii == [("app_users", "email")], str(pii))
    ro = psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"), dbname=os.getenv("DB_NAME", "laynes"),
        user=os.environ["DB_RO_USER"], password=os.environ["DB_RO_PASS"])
    ro.set_session(readonly=True)
    roc = ro.cursor()
    ro.rollback()
    try:
        roc.execute("SELECT email FROM app_users LIMIT 1")
        check("database denies the assistant any email column", False, "READABLE")
    except psycopg2.Error:
        check("database denies the assistant any email column", True)
    ro.rollback()
    roc.execute("SELECT safe_customer_key FROM v_order_identity_context "
                "WHERE safe_customer_key IS NOT NULL LIMIT 1")
    key = roc.fetchone()[0]
    check("safe_customer_key is an opaque hash", len(key) == 32 and key.isalnum(), key[:12])
    check("it is not the raw customer id", not key.isdigit())
    ro.close()
    cur.execute("""SELECT COUNT(*) FROM information_schema.columns
                   WHERE table_name IN ('v_order_identity_context', 'v_identity_profile')
                     AND column_name IN ('customer_id', 'email', 'phone', 'customer_name')""")
    check("no view exposes a raw customer identifier or PII", cur.fetchone()[0] == 0)

    print("=== J. nothing else moved ===")
    check("A3 entrées unchanged", meta["entree_classification"]["entrees"] == 6323.0)
    check("A5 category coverage unchanged",
          meta["category_mapping"]["historically_verified_revenue_pct"] == 83.64)
    check("A6 business date method unchanged",
          meta["time"]["business_date_method"] == "local_calendar_date")
    check("A7 open date still unknown",
          meta["store_cohort"]["verified_open_date"] is None)
    check("A8 channel status unchanged",
          meta["channel_mapping"]["status"]
          == "ordering_pattern_verified_service_mode_unverified")
    check("A10 payment capture unchanged",
          meta["payment_linkage"]["payment_capture_rate"] == 100.0)
    check("REAL count unchanged", meta["volumes"]["real_count"] == 4565)

    print("=== K. identity relations are routed correctly ===")
    check("the per-order identity view is gated business data",
          "v_order_identity_context" not in cs._REFERENCE_RELATIONS)
    check("the whole-history profile is ungated reference data",
          "v_identity_profile" in cs._REFERENCE_RELATIONS)
    try:
        cs.enforce_scope("SELECT COUNT(*) FROM v_order_identity_context "
                         "WHERE establishment_id = 26", None, None, None, {})
        check("per-order identity rejected without a scope", False, "ALLOWED")
    except cs.ScopeError:
        check("per-order identity rejected without a scope", True)
    gate = cs.enforce_scope("SELECT safe_customer_key FROM v_identity_profile "
                            "WHERE suspected_non_individual", None, None, None, {})
    check("profile answerable with no scope (it has no dates to scope)",
          gate is None)
    cur.execute("""SELECT COUNT(*) FROM information_schema.columns
                   WHERE table_name = 'v_identity_profile'
                     AND column_name IN ('business_date', 'final_total', 'order_id')""")
    check("...because it carries no per-order rows, dates or revenue",
          cur.fetchone()[0] == 0)

    print("=== L. filtered figures never replace raw ones silently ===")
    check("raw identified-ID count is still exposed",
          idn["distinct_identified_customers"] == 88)
    check("suspected count is exposed separately",
          idn["suspected_non_individual_ids"] == 2)
    check("meta declares the heuristic is NOT a verified rule",
          "NOT a verified business rule" in idn["heuristic_status"]
          and "not been manually confirmed" in idn["heuristic_status"])
    check("meta carries the dual-reporting rule",
          "TOGETHER" in idn["reporting_rule"]
          and "never replaces the raw" in idn["reporting_rule"])
    check("prompt requires showing both", "ALWAYS SHOW BOTH" in flat)
    check("prompt states the filtered view never silently replaces raw",
          "never silently replaces the raw one" in flat)
    check("prompt gives the exact exclusion wording",
          "after excluding suspected non-individual IDs" in flat)
    check("prompt forbids 'removing platform accounts'",
          'do NOT say "after removing platform accounts"' in flat)
    check("prompt forbids calling the remainder proven customers",
          "do NOT call the remaining IDs proven, real or human customers" in flat)
    check("prompt allows 'human-like subset' only when qualified",
          "acceptable only if qualified as heuristic" in flat)
    # Both figures must be independently computable, not just documented.
    ids = cs._suspected_non_individual_ids()
    cur.execute("""SELECT COUNT(DISTINCT customer_id),
                          COUNT(DISTINCT customer_id) FILTER (WHERE NOT (customer_id = ANY(%s)))
                   FROM v_orders_classified
                   WHERE establishment_id = 26 AND txn_class = 'REAL'
                     AND business_date >= '2026-06-01' AND business_date < '2026-07-01'
                     AND customer_id IS NOT NULL""", (ids,))
    raw_ids, filt_ids = cur.fetchone()
    check("raw = 88 identified IDs", raw_ids == 88, str(raw_ids))
    check("filtered = 86 remaining IDs", filt_ids == 86, str(filt_ids))
    check("the two differ, so disclosure matters", raw_ids != filt_ids)
    # And the view must not hard-code an exclusion of its own.
    cur.execute("SELECT pg_get_viewdef('v_order_identity_context'::regclass, true)")
    ictx = cur.fetchone()[0]
    check("the per-order view excludes nothing on its own",
          "suspected" not in ictx.lower())
    check("so raw identified data is always reachable",
          "customer_id IS NOT NULL" in ictx or "safe_customer_key" in ictx)

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
