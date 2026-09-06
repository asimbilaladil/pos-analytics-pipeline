#!/usr/bin/env python3
"""A3 tests: maintained entrée classification and product_form.

Run:  venv/bin/python tests/test_entree_classification.py     (exit 0 = pass)

The central property under test is that entrée status comes from the maintained
table and never from a product name, and that unresolved products are counted as
NOT entrées so every entrée figure is a floor rather than a guess.
"""
import os
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

    print("=== A. classification table integrity ===")
    cur.execute("SELECT COUNT(*) FROM product_analysis_classification")
    total = cur.fetchone()[0]
    check("every sold product has a row", total >= 2500, f"{total} rows")
    cur.execute("""SELECT COUNT(*) FROM order_items_v2 i
                   WHERE i.deleted IS NOT TRUE AND i.product_id IS NOT NULL
                     AND NOT EXISTS (SELECT 1 FROM product_analysis_classification c
                                     WHERE c.product_id = i.product_id)""")
    check("no sold product is missing from the table", cur.fetchone()[0] == 0)
    cur.execute("""SELECT COUNT(*) FROM product_analysis_classification
                   WHERE is_entree AND confidence = 'unknown'""")
    check("nothing is an entrée at unknown confidence", cur.fetchone()[0] == 0)
    cur.execute("""SELECT COUNT(*) FROM product_analysis_classification
                   WHERE classification_source = 'unresolved' AND is_entree""")
    check("unresolved products are never entrées", cur.fetchone()[0] == 0)

    print("=== B. classification is structural, not name-based ===")
    # The invariant is about EVIDENCE, not names. A product may be an entrée
    # only via verified structure (combo position) or a recorded human decision
    # -- never because its name looks like one. These two assertions previously
    # required name-pattern products to be non-entrées, which held only while
    # everything was unreviewed; they now assert the durable property instead.
    cur.execute("""SELECT COUNT(*) FROM product_analysis_classification
                   WHERE is_entree
                     AND classification_source NOT IN
                         ('verified_structure', 'verified_product_list', 'manual_review')""")
    check("entrées only come from structure or a recorded human decision",
          cur.fetchone()[0] == 0)
    cur.execute("""SELECT COUNT(*) FROM product_analysis_classification
                   WHERE is_entree AND classification_source = 'verified_product_list'
                     AND (reviewed_by IS NULL OR reviewed_at IS NULL
                          OR review_note IS NULL)""")
    check("every human-decided entrée records who, when and why",
          cur.fetchone()[0] == 0)
    cur.execute("""SELECT COUNT(*) FROM product_analysis_classification
                   WHERE product_name_snapshot ILIKE '%party pack%' AND is_entree""")
    check("party packs are still NOT entrées (no assumed count)",
          cur.fetchone()[0] == 0)
    cur.execute("""SELECT COUNT(*) FROM product_analysis_classification
                   WHERE product_name_snapshot ILIKE '%a la carte%' AND is_entree""")
    check("a-la-carte fingers are still NOT entrées", cur.fetchone()[0] == 0)
    cur.execute("""SELECT COUNT(*) FROM product_analysis_classification
                   WHERE classification_source = 'verified_category'""")
    check("no row claims a verified category (that route was disproved)",
          cur.fetchone()[0] == 0)

    print("=== C. product_form ===")
    cur.execute("""SELECT product_form, COUNT(*) FROM v_order_items_classified
                   WHERE establishment_id = 26
                     AND business_date >= '2026-06-01' AND business_date < '2026-07-01'
                   GROUP BY 1 ORDER BY 2 DESC""")
    forms = dict(cur.fetchall())
    check("combo_component present", forms.get("combo_component", 0) > 0, str(forms))
    check("every item has a form",
          set(forms) <= {"combo_component", "single_line_combo", "standalone", "unknown"})
    cur.execute("""SELECT COUNT(*) FROM v_order_items_classified
                   WHERE combo_uuid IS NOT NULL AND product_form = 'standalone'""")
    check("an item inside a combo is never 'standalone'", cur.fetchone()[0] == 0)

    print("=== D. entree_count semantics ===")
    cur.execute("""SELECT COUNT(*) FROM v_orders_classified
                   WHERE entree_count > 0 AND item_count = 0""")
    check("no order has entrées without items", cur.fetchone()[0] == 0)
    cur.execute("""SELECT COUNT(*) FROM v_orders_classified o
                   WHERE o.unresolved_item_count = 0 AND NOT o.entree_fully_resolved""")
    check("entree_fully_resolved agrees with unresolved_item_count",
          cur.fetchone()[0] == 0)

    print("=== E. migration 21/23 contracts preserved (A12 must not break) ===")
    cur.execute("""SELECT column_name FROM information_schema.columns
                   WHERE table_name = 'v_orders_classified'""")
    cols = {r[0] for r in cur.fetchall()}
    for needed in ("txn_class", "business_date", "item_count", "combo_count",
                   "combo_sales", "standalone_sales", "identity_captured",
                   "entree_count", "unresolved_item_count", "entree_fully_resolved"):
        check(f"v_orders_classified exposes {needed}", needed in cols)

    print("=== F. coverage reaches meta_extract ===")
    meta = cs.meta_extract(26, "2026-06-01", "2026-07-01")
    ec = meta["entree_classification"]
    check("entree_classification present in payload", bool(ec))
    check("coverage_pct computed", isinstance(ec["coverage_pct"], float))
    check("entrees is a floor, documented as such", "floor" in ec["source"])
    check("reconciliation still PASSes for Nederland June",
          meta["reconciliation"]["status"] == "PASS")
    check("REAL count unchanged by A3", meta["volumes"]["real_count"] == 4565)
    if ec["coverage_pct"] < cs.ENTREE_COVERAGE_FLOOR_PCT:
        check("below-floor coverage raises an advisory",
              any("entree classification covers only" in a for a in meta["advisories"]))

    print("=== G. the assistant cannot write classification ===")
    for priv in ("INSERT", "UPDATE", "DELETE"):
        cur.execute("SELECT has_table_privilege(%s, %s, %s)",
                    (os.environ["DB_RO_USER"], "product_analysis_classification", priv))
        check(f"read-only role has no {priv}", cur.fetchone()[0] is False)
    cur.execute("SELECT has_table_privilege(%s, %s, %s)",
                (os.environ["DB_RO_USER"], "product_analysis_classification", "SELECT"))
    check("read-only role cannot even SELECT the raw table",
          cur.fetchone()[0] is False)
    try:
        cs._validate("SELECT * FROM product_analysis_classification")
        check("validator blocks the raw table", False, "ACCEPTED")
    except cs.SqlError:
        check("validator blocks the raw table", True)

    print("=== H. prompt guardrails (static, no API call) ===")
    prompt = cs._system_static()
    check("prompt no longer claims entrée classification is missing",
          "no maintained entrée classification" not in prompt
          and "cannot be answered from this database today" not in prompt)
    check("entrées per check is not listed as unavailable",
          "entrees_per_check" not in cs.UNAVAILABLE_METRIC_KEYS)
    # Written during A3, when category analysis genuinely was unavailable; the
    # intent was "A3 must not accidentally unlock category". A5 later unlocked
    # it legitimately, so assert the separation that actually matters instead:
    # entree classification and category remain distinct dimensions.
    check("category mix is no longer listed as unavailable (A5 delivered it)",
          "product_category_mix" not in cs.UNAVAILABLE_METRIC_KEYS)
    check("entree classification is not substituted for category",
          "Category is NOT entrée classification" in cs.CATEGORY_RULES)
    check("category rules keep entree fields out of the category dimension",
          "is_entree, product_form" in cs.CATEGORY_RULES)
    check("items-per-order guardrail present", "ITEMS PER ORDER" in prompt)
    check("guardrail forbids leading with the raw count",
          "never lead with it" in prompt.lower())
    check("guardrail names the maintained redirect metrics",
          all(t in prompt for t in ("entree_count per REAL transaction",
                                    "combo_count per REAL transaction",
                                    "0 / 1 / 2 / 3+")))
    check("guardrail supplies the exact raw-density label",
          "raw Revel line items per REAL order" in prompt)
    check("guardrail forbids denying the classification exists",
          "Never say entrée classification is unavailable" in prompt)

    print("=== I. Nederland June is the accepted authoritative result ===")
    cur2 = psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"), dbname=os.getenv("DB_NAME", "laynes"),
        user=os.environ["DB_USER"], password=os.environ["DB_PASS"])
    cur2.set_session(readonly=True)
    cc = cur2.cursor()
    cc.execute("""SELECT COUNT(*), SUM(entree_count),
                         ROUND(SUM(entree_count)::numeric / COUNT(*), 4),
                         ROUND(100.0 * COUNT(*) FILTER (WHERE entree_count = 1)
                               / COUNT(*), 2)
                  FROM v_orders_classified
                  WHERE establishment_id = 26 AND txn_class = 'REAL'
                    AND business_date >= '2026-06-01'
                    AND business_date <  '2026-07-01'""")
    real, entrees, per_check, pct_one = cc.fetchone()
    cur2.close()
    # Accepted 2026-09-05 as the authoritative business definition. These pin the
    # numbers so a later classification change cannot move them unnoticed; the
    # superseded 6,155 benchmark is deliberately NOT asserted.
    check("REAL transactions = 4565", real == 4565, str(real))
    check("entrées = 6323", float(entrees) == 6323.0, str(entrees))
    check("entrées/check = 1.3851", float(per_check) == 1.3851, str(per_check))
    check("exactly-one-entrée = 64.10%", float(pct_one) == 64.10, str(pct_one))

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
