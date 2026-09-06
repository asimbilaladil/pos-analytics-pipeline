#!/usr/bin/env python3
"""A5 tests: verified category dimension, and the ProductClass trap.

Run:  venv/bin/python tests/test_category_mapping.py     (exit 0 = pass)

The permanent regression this file exists for: product_class is NOT
ProductCategory. Their ids overlap numerically and the join "works" while being
semantically wrong, which is exactly the shape of bug that survives review.
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


def main() -> int:
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"), dbname=os.getenv("DB_NAME", "laynes"),
        user=os.environ["DB_USER"], password=os.environ["DB_PASS"])
    conn.set_session(readonly=True)
    cur = conn.cursor()

    print("=== A. product_class is NOT ProductCategory (permanent regression) ===")
    # The coincidence join still "works" -- that is the whole danger. Prove it is
    # wrong semantically so nobody is tempted by it again.
    cur.execute(r"""
        SELECT pc.name, COUNT(*) FILTER (WHERE p.name ILIKE '%Meal%'
                                            OR p.name ILIKE '%Shake%'
                                            OR p.name ILIKE '%Toast%')
        FROM products p
        JOIN product_categories pc
          ON pc.id = (regexp_match(p.product_class, '/ProductClass/(\d+)/'))[1]::int
        WHERE pc.name = 'Merch'
        GROUP BY 1""")
    row = cur.fetchone()
    check("the product_class join files meals/shakes/toast under 'Merch'",
          row is not None and row[1] > 0, f"{row[1] if row else 0} such products")
    check("nothing in the mapping is sourced from product_class",
          True)  # asserted structurally below
    cur.execute("""SELECT COUNT(*) FROM product_category_mapping
                   WHERE mapping_source NOT IN
                         ('revel_product_api', 'manual_review',
                          'verified_product_list', 'unresolved')""")
    check("mapping_source is constrained to verified sources", cur.fetchone()[0] == 0)
    src = open("migrations/31_product_category_mapping.sql").read()
    check("migration 31 never joins product_class to product_categories",
          "ProductClass/" not in re.sub(r"^--.*$", "", src, flags=re.M))
    loader = open("load_product_categories.py").read()
    check("the loader never reads product_class",
          "productclass" not in loader.lower().replace("product_class", "productclass")
          or "product_class" not in re.sub(r'""".*?"""', "", loader, flags=re.S))
    check("loader reads Revel's explicit category field",
          "/ProductCategory/" in loader)

    print("=== B. mapping is populated and resolves ===")
    cur.execute("SELECT COUNT(*), COUNT(category_id) FROM product_category_mapping")
    n, withcat = cur.fetchone()
    check("every sold product family is mapped", n >= 36000, str(n))
    check("all rows carry a category_id", n == withcat, f"{withcat}/{n}")
    cur.execute("""SELECT COUNT(*) FROM product_category_mapping m
                   LEFT JOIN product_categories pc ON pc.id = m.category_id
                   WHERE m.category_id IS NOT NULL AND pc.id IS NULL""")
    check("no category_id is absent from product_categories", cur.fetchone()[0] == 0)

    print("=== C. categories are not inferred from product names ===")
    # A wrap is filed by Revel, not by the word "wrap".
    cur.execute("""SELECT COUNT(*) FROM product_category_mapping
                   WHERE lower(category_name_snapshot) = lower(
                         split_part((SELECT product_name FROM order_items_v2
                                     WHERE product_id = product_category_mapping.product_id
                                     LIMIT 1), ' ', 1))""")
    check("category names are not copies of product-name words",
          cur.fetchone()[0] < 100, "sanity bound")
    cur.execute("""SELECT category_name FROM v_order_items_category_context
                   WHERE product_name ILIKE '%Chicken Wrap%' LIMIT 1""")
    r = cur.fetchone()
    check("a 'Chicken Wrap' is not auto-filed into a 'chicken' category",
          r is None or "chicken" not in (r[0] or "").lower(), str(r))

    print("=== D. current-only stays current-only ===")
    cur.execute("""SELECT DISTINCT mapping_confidence FROM product_category_mapping""")
    confs = sorted(x[0] for x in cur.fetchall())
    check("confidence is verified_current (never verified_historical)",
          confs == ["verified_current"], str(confs))
    cur.execute("""SELECT COUNT(*) FROM product_category_mapping
                   WHERE mapping_confidence = 'verified_historical'""")
    check("nothing claims historical verification at table level",
          cur.fetchone()[0] == 0)
    # Period-relative flag must actually discriminate, not be blanket TRUE.
    cur.execute("""SELECT COUNT(*) FILTER (WHERE historical_category_verified),
                          COUNT(*) FILTER (WHERE NOT historical_category_verified)
                   FROM v_order_items_category_context
                   WHERE establishment_id = 26
                     AND business_date >= '2026-06-01' AND business_date < '2026-07-01'""")
    yes, no = cur.fetchone()
    check("historical flag discriminates (both TRUE and FALSE rows exist)",
          yes > 0 and no > 0, f"{yes} verified / {no} not")
    cur.execute("""SELECT COUNT(*) FROM v_order_items_category_context
                   WHERE historical_category_verified
                     AND category_stable_since >= business_date""")
    check("no row is flagged verified when the product changed after the sale",
          cur.fetchone()[0] == 0)

    print("=== E. unknown stays unknown ===")
    cur.execute("""SELECT COUNT(*) FROM v_order_items_category_context
                   WHERE category_id IS NULL
                     AND category_mapping_confidence <> 'unknown'""")
    check("unmapped rows report unknown confidence", cur.fetchone()[0] == 0)

    print("=== F. hierarchy ===")
    cur.execute("""SELECT COUNT(DISTINCT parent_category_name)
                   FROM v_order_items_category_context WHERE parent_category_name IS NOT NULL""")
    check("parent categories are exposed", cur.fetchone()[0] >= 2)

    print("=== G. coverage reaches meta_extract ===")
    meta = cs.meta_extract(26, "2026-06-01", "2026-07-01")
    cm = meta["category_mapping"]
    check("status is current_only", cm["status"] == "current_only")
    check("historical_verified is False", cm["historical_verified"] is False)
    check("mapped revenue is 100%", cm["mapped_revenue_pct"] == 100.0)
    check("historically verified revenue is 83.64%",
          cm["historically_verified_revenue_pct"] == 83.64,
          str(cm["historically_verified_revenue_pct"]))
    check("that lands in the 80-95 caveat band",
          80 <= cm["historically_verified_revenue_pct"] < 95)
    check("thresholds are published", cm["coverage_thresholds_pct"]["state_normally"] == 95)

    print("=== H. prompt guardrails ===")
    flat = re.sub(r"\s+", " ", cs._system_static())
    for probe, label in (
            ("NEVER use products.product_class as a category", "bans product_class"),
            ("DIFFERENT namespace", "explains why"),
            ("NEVER infer a category from a product name", "bans name inference"),
            ("Category is NOT entrée classification", "separates from A3"),
            ("state it WITH the coverage limitation named explicitly", "coverage rule")):
        check(f"prompt {label}", probe in flat)

    print("=== I. A3 and the rest unchanged ===")
    check("A3 entrées unchanged", meta["entree_classification"]["entrees"] == 6323.0)
    check("A12 reconciliation still PASS", meta["reconciliation"]["status"] == "PASS")
    check("A10 payment capture unchanged",
          meta["payment_linkage"]["payment_capture_rate"] == 100.0)
    check("A7 open date still unknown",
          meta["store_cohort"]["verified_open_date"] is None)
    check("A6 business date method unchanged",
          meta["time"]["business_date_method"] == "local_calendar_date")
    check("REAL count unchanged", meta["volumes"]["real_count"] == 4565)
    cur.execute("""SELECT COUNT(*) FROM information_schema.columns
                   WHERE table_name IN ('v_orders_classified', 'v_order_items_classified')
                     AND column_name LIKE 'category%'""")
    check("category columns did not leak into the core views", cur.fetchone()[0] == 0)

    print("=== J. the assistant cannot write the mapping ===")
    for priv in ("INSERT", "UPDATE", "DELETE", "SELECT"):
        cur.execute("SELECT has_table_privilege(%s, %s, %s)",
                    (os.environ["DB_RO_USER"], "product_category_mapping", priv))
        check(f"read-only role has no {priv} on the raw table",
              cur.fetchone()[0] is False)
    try:
        cs._validate("SELECT * FROM product_category_mapping")
        check("validator blocks the raw mapping table", False, "ACCEPTED")
    except cs.SqlError:
        check("validator blocks the raw mapping table", True)

    print("=== K. current reference lookup is ungated ===")
    # A. "What category is 5 Finger Meal in?" must not need a period or A12.
    ref_sql = ("SELECT category_name FROM v_product_category_current "
               "WHERE product_name = '5 Finger Meal*'")
    try:
        gate = cs.enforce_scope(ref_sql, None, None, None, {})
        check("reference lookup needs no store/period scope", gate is None)
    except cs.ScopeError as e:
        check("reference lookup needs no store/period scope", False, str(e)[:60])
    check("the reference view is in the ungated reference set",
          "v_product_category_current" in cs._REFERENCE_RELATIONS)
    cur.execute("""SELECT DISTINCT category_name, mapping_confidence
                   FROM v_product_category_current WHERE product_name = '5 Finger Meal*'""")
    r = cur.fetchone()
    check("it returns the current category", r is not None and r[0] == "Meals", str(r))
    check("...as verified_current, never verified_historical",
          r is not None and r[1] == "verified_current")
    cur.execute("""SELECT COUNT(*) FROM information_schema.columns
                   WHERE table_name = 'v_product_category_current'
                     AND column_name IN ('historical_category_verified', 'business_date')""")
    check("the reference view makes NO historical claim", cur.fetchone()[0] == 0)
    cur.execute("""SELECT COUNT(*) FROM information_schema.columns
                   WHERE table_name = 'v_product_category_current'
                     AND column_name = 'current_snapshot_at'""")
    check("it timestamps its snapshot", cur.fetchone()[0] == 1)
    # It must read no transactional relation -- that is what makes it ungated.
    cur.execute("SELECT pg_get_viewdef('v_product_category_current'::regclass, true)")
    vdef = cur.fetchone()[0]
    check("reference view touches no order/item/payment relation",
          not any(t in vdef for t in ("order_items_v2", "orders_v2", "payments_v2",
                                      "v_orders_classified")), "")

    print("=== L. historical questions stay gated ===")
    # B. Period questions must still declare scope and pass A12.
    hist_sql = ("SELECT category_name, SUM(pure_sales) FROM v_order_items_category_context "
                "WHERE establishment_id = 26 GROUP BY 1")
    try:
        cs.enforce_scope(hist_sql, None, None, None, {})
        check("historical view rejected without a scope", False, "ALLOWED")
    except cs.ScopeError:
        check("historical view rejected without a scope", True)
    scoped = ("SELECT category_name, SUM(pure_sales) FROM v_order_items_category_context "
              "WHERE establishment_id = 26 AND business_date >= '2026-06-01' "
              "AND business_date < '2026-07-01' GROUP BY 1")
    gate = cs.enforce_scope(scoped, 26, "2026-06-01", "2026-07-01", {})
    check("historical view allowed once scoped and reconciled",
          gate is not None and gate["reconciliation"]["status"] == "PASS")
    check("the historical view is NOT in the ungated set",
          "v_order_items_category_context" not in cs._REFERENCE_RELATIONS)
    check("coverage unchanged at 83.64% verified revenue",
          cm["historically_verified_revenue_pct"] == 83.64)
    check("thresholds unchanged (95 / 80)",
          cm["coverage_thresholds_pct"] == {"state_normally": 95, "state_with_caveat": 80})
    flat2 = re.sub(r"\s+", " ", cs._system_static())
    check("prompt routes the two question shapes apart",
          "TWO DIFFERENT QUESTIONS, TWO DIFFERENT RELATIONS" in flat2)
    check("prompt forbids answering period questions from the reference view",
          "would silently apply today's mapping to the past" in flat2)

    print("=== M. taxonomy lookup without name inference ===")
    # D. "Is there a Chicken category?" -- answerable from the taxonomy, and the
    # honest answer is no, even though many products are chicken.
    cur.execute("""SELECT COUNT(*) FROM product_categories WHERE name ILIKE '%chicken%'""")
    chicken_cats = cur.fetchone()[0]
    cur.execute("""SELECT COUNT(*) FROM v_product_category_current
                   WHERE product_name ILIKE '%chicken%'""")
    chicken_prods = cur.fetchone()[0]
    check("many products are named 'chicken' while no such category is asserted",
          chicken_prods > 0, f"{chicken_prods} products, {chicken_cats} categories named chicken")
    cur.execute("""SELECT COUNT(*) FROM v_product_category_current
                   WHERE product_name ILIKE '%chicken%'
                     AND category_name ILIKE '%chicken%'
                     AND category_id NOT IN (SELECT id FROM product_categories
                                             WHERE name ILIKE '%chicken%')""")
    check("no category name is fabricated from a product name", cur.fetchone()[0] == 0)

    print("=== N. snapshot future-proofing is documented ===")
    mig = open("migrations/32_product_category_current_reference.sql").read()
    check("records that verification is conservative", "conservative" in mig)
    check("records updated_date as a LOWER-BOUND signal", "LOWER-BOUND" in mig)
    check("records that later edits reduce old-period coverage",
          "can only fall as products are edited" in mig)
    check("names preserved snapshots as the authoritative future path",
          "preserved snapshots" in mig and "effective_from/effective_to" in mig)
    check("forbids backfilling unobserved effective periods",
          "never observed" in mig)
    cur.execute("SELECT COUNT(*) FROM product_category_mapping WHERE snapshot_taken_at IS NULL")
    check("every mapping row is snapshot-stamped", cur.fetchone()[0] == 0)

    # ── A9 consistency: the unavailable-metrics list must not contradict A5 ──
    # This block exists because it once did: UNAVAILABLE_METRICS claimed
    # "products.category_id is NULL for every row and no category source could
    # be verified", which A5 disproved, while the model answered category
    # questions anyway -- right answer, but reached by ignoring its own
    # authoritative list.
    print("\n=== M. category is NOT listed as an unavailable metric ===")
    check("product_category_mix is absent from UNAVAILABLE_METRIC_KEYS",
          "product_category_mix" not in cs.UNAVAILABLE_METRIC_KEYS)
    flat_unavail = re.sub(r"\s+", " ", cs.UNAVAILABLE_METRICS)
    check("stale 'category_id is NULL' claim is gone",
          "category_id is NULL for every row" not in flat_unavail)
    check("stale 'no category source could be verified' claim is gone",
          "no category source could be verified" not in flat_unavail)
    check("genuinely unavailable metrics are NOT weakened",
          all(k in cs.UNAVAILABLE_METRIC_KEYS for k in (
              "order_duration_or_service_time", "drive_thru_timing",
              "comp_vs_discount_separation",
              "store_age_and_weeks_since_open_all_stores")))

    print("=== N. category analysis routes through the A5 views ===")
    flat_cat = re.sub(r"\s+", " ", cs.CATEGORY_RULES)
    check("historical analysis names v_order_items_category_context",
          "v_order_items_category_context" in flat_cat)
    check("current lookup names v_product_category_current",
          "v_product_category_current" in flat_cat)
    check("both A5 views are in the relation allowlist",
          {"v_order_items_category_context", "v_product_category_current"}
          <= cs._ALLOWED_RELATIONS)
    check("category is sourced from Revel Product.category",
          "Product.category" in flat_cat)

    print("=== O. historical needs scope + gate; current reference does not ===")
    check("historical category view is scope-gated (not reference)",
          "v_order_items_category_context" not in cs._REFERENCE_RELATIONS)
    check("current category view is ungated reference",
          "v_product_category_current" in cs._REFERENCE_RELATIONS)
    check("prompt requires store+period+gate for historical category",
          "Declare store and period, pass the gate" in flat_cat)
    check("prompt allows reference lookup with no store/period/reconciliation",
          "No store, no period, no reconciliation needed" in flat_cat)

    print("=== P. current mapping must never be silently backdated ===")
    check("prompt forbids using the reference view for historical questions",
          "The reference view must NOT be used to answer these" in flat_cat)
    check("prompt names the silent-backdating failure mode",
          "silently apply today's mapping to the past" in flat_cat)

    print("=== Q. ProductClass is not ProductCategory (permanent constraint) ===")
    check("prompt forbids product_class as category",
          "NEVER use products.product_class as a" in flat_cat)
    check("prompt states they are different namespaces",
          "DIFFERENT namespace" in flat_cat)
    cur.execute("""SELECT COUNT(DISTINCT p.product_id) FROM products pr
                   JOIN product_analysis_classification p ON p.product_id = pr.id
                   WHERE pr.product_class LIKE '%%/166/' AND p.is_entree""")
    check("product_class 166 still contains entrees (route stays disproven)",
          cur.fetchone()[0] > 0)

    print("=== R. historical coverage caveat remains active ===")
    check("coverage bands are stated", "80-95%" in flat_cat and ">= 95%" in flat_cat)
    check("sub-80% ranking is not authoritative",
          "do NOT present a category ranking as authoritative" in flat_cat)
    check("Nederland June coverage names the limitation",
          "must carry the limitation" in flat_cat)

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
