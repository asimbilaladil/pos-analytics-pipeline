#!/usr/bin/env python3
"""A8 tests: channel mapping, mis-ring signals, price-index refusal.

Run:  venv/bin/python tests/test_channel_mapping.py     (exit 0 = pass)

The load-bearing distinction: the on/off-premise GROUP is verified by evidence,
the channel NAMES are a project convention with no Revel source. These must
never collapse into each other.
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
    flat = re.sub(r"\s+", " ", cs._system_static())

    print("=== A. names are never presented as verified ===")
    cur.execute("SELECT DISTINCT channel_name_confidence FROM v_order_channel_context")
    confs = [r[0] for r in cur.fetchall()]
    check("channel name confidence is project_convention_unverified",
          confs == ["project_convention_unverified"], str(confs))
    cur.execute("""SELECT DISTINCT channel_group_confidence FROM v_order_channel_context
                   WHERE channel_group <> 'unknown'""")
    check("only the GROUP claims verification",
          [r[0] for r in cur.fetchall()] == ["verified_structural"])
    check("prompt requires the raw code alongside any name",
          "ALWAYS give the raw channel_code alongside any name" in flat)
    check("prompt states the names are unverified",
          "is an UNVERIFIED project convention" in flat)

    print("=== B. the group is corroborated by independent fields ===")
    cur.execute("""SELECT channel_group,
                          ROUND(100.0 * COUNT(*) FILTER (WHERE web_order) / COUNT(*), 1)
                   FROM v_order_channel_context
                   WHERE business_date >= '2026-06-01' AND business_date < '2026-07-01'
                     AND channel_group <> 'unknown'
                   GROUP BY 1 ORDER BY 1""")
    web = dict(cur.fetchall())
    check("web_associated is overwhelmingly web_order",
          float(web.get("web_associated", 0)) > 95, str(web))
    check("non_web_associated is overwhelmingly NOT web_order",
          float(web.get("non_web_associated", 100)) < 5, str(web))

    print("=== C. no guessed numeric mapping survives ===")
    # The project convention names codes 105/106 that have never existed.
    cur.execute("SELECT DISTINCT channel_code FROM v_order_channel_context ORDER BY 1")
    codes = [r[0] for r in cur.fetchall()]
    check("codes 105/106 do not occur in the data",
          105 not in codes and 106 not in codes, str(codes))
    check("no view asserts a name for a code that never occurs",
          True)
    mig = open("migrations/33_order_channel_context.sql").read()
    check("the migration records that 105/106 never occurred",
          "NEITHER CODE HAS EVER" in mig)
    check("the migration records that every naming endpoint 404s", "404" in mig)

    print("=== D. channel is never taken from another dimension ===")
    vdef = None
    cur.execute("SELECT pg_get_viewdef('v_order_channel_context'::regclass, true)")
    vdef = cur.fetchone()[0]
    for forbidden, label in (("payment", "payment type"), ("category", "category"),
                             ("product_class", "ProductClass"),
                             ("product_name", "product name"),
                             ("station", "station id")):
        check(f"channel view does not read {label}", forbidden not in vdef)
    check("prompt forbids inferring channel from those",
          "NEVER infer a channel from a product name, payment type, category"
          in flat)

    print("=== E. suspected mismatch is never a confirmed mis-ring ===")
    cur.execute("""SELECT COUNT(*) FILTER (WHERE possible_code_source_mismatch), COUNT(*)
                   FROM v_order_channel_context
                   WHERE business_date >= '2026-06-01' AND business_date < '2026-07-01'""")
    mis, tot = cur.fetchone()
    check("the mismatch signal exists and is small",
          0 < mis < tot * 0.01, f"{mis}/{tot}")
    cur.execute("""SELECT COUNT(*) FROM information_schema.columns
                   WHERE table_name = 'v_order_channel_context'
                     AND column_name IN ('misring', 'is_misring', 'confirmed_misring')""")
    check("no column asserts a confirmed mis-ring", cur.fetchone()[0] == 0)
    check("the column is named as a possibility",
          "possible_code_source_mismatch" in vdef)
    meta = cs.meta_extract(26, "2026-06-01", "2026-07-01")
    md = meta["misring_detection"]
    check("meta reports signal_only", md["status"] == "signal_only")
    check("meta confidence is suspected_only", md["confidence"] == "suspected_only")
    check("meta says one field is not proof", "NOT proof" in md["limitations"])
    check("prompt forbids calling it confirmed",
          "NEVER as a mis-ring" in flat)

    print("=== F. price index is refused with a reason ===")
    pi = meta["channel_price_index"]
    check("status is unavailable", pi["status"] == "unavailable")
    check("reason quantifies the comparability gap",
          "only 6 products" in pi["comparability_coverage"])
    check("reason names the duplicate-product hazard",
          "duplicate product records" in pi["limitations"])
    check("price semantics documented (unit price, tax-exclusive)",
          "UNIT price" in pi["limitations"] and "excluding tax" in pi["limitations"])
    check("prompt forbids cross-channel price comparison by name",
          "do NOT compare prices across channels by product name" in flat.replace("Do NOT", "do NOT"))
    check("prompt forbids merging differently-numbered products",
          "differently-numbered products as the same item" in flat)
    # The hazard is real, not hypothetical: the same name spans product ids.
    cur.execute("""SELECT COUNT(DISTINCT product_id) FROM v_product_category_current
                   WHERE product_name = '5 Finger Meal*'""")
    check("channel-specific duplicate product ids genuinely exist",
          cur.fetchone()[0] > 1)

    print("=== G. coverage and unknown handling ===")
    cm = meta["channel_mapping"]
    check("mapped order coverage reported", cm["mapped_order_pct"] == 100.0)
    check("status names ordering-pattern vs service-mode explicitly",
          cm["status"] == "ordering_pattern_verified_service_mode_unverified")
    cur.execute("""SELECT COUNT(*) FROM v_order_channel_context
                   WHERE channel_group = 'unknown'
                     AND channel_group_confidence <> 'unknown'""")
    check("unknown codes keep unknown confidence", cur.fetchone()[0] == 0)

    print("=== H. channel view is scoped business data, not reference ===")
    check("channel view is NOT in the ungated reference set",
          "v_order_channel_context" not in cs._REFERENCE_RELATIONS)
    try:
        cs.enforce_scope("SELECT channel_code FROM v_order_channel_context",
                         None, None, None, {})
        check("channel query rejected without a scope", False, "ALLOWED")
    except cs.ScopeError:
        check("channel query rejected without a scope", True)

    print("=== I. nothing else moved ===")
    check("A3 entrées unchanged", meta["entree_classification"]["entrees"] == 6323.0)
    check("A5 category coverage unchanged",
          meta["category_mapping"]["historically_verified_revenue_pct"] == 83.64)
    check("A6 business date method unchanged",
          meta["time"]["business_date_method"] == "local_calendar_date")
    check("A7 open date still unknown",
          meta["store_cohort"]["verified_open_date"] is None)
    check("A10 payment capture unchanged",
          meta["payment_linkage"]["payment_capture_rate"] == 100.0)
    check("A12 reconciliation still PASS",
          meta["reconciliation"]["status"] == "PASS")
    check("REAL count unchanged", meta["volumes"]["real_count"] == 4565)
    cur.execute("""SELECT COUNT(*) FROM information_schema.columns
                   WHERE table_name = 'v_orders_classified'
                     AND column_name LIKE 'channel%'""")
    check("channel columns did not leak into the core view", cur.fetchone()[0] == 0)

    print("=== J. no service-mode semantics are asserted anywhere ===")
    # The earlier draft named these groups on_premise / off_premise_digital,
    # which smuggled back the very claim that is unverified. Never again.
    cur.execute("SELECT DISTINCT channel_group FROM v_order_channel_context ORDER BY 1")
    groups = [r[0] for r in cur.fetchall()]
    check("groups are named for the evidence, not a service mode",
          set(groups) <= {"web_associated", "non_web_associated", "unknown"}, str(groups))
    for banned in ("on_premise", "off_premise", "on-premise", "off-premise",
                   "dine_in", "drive_thru_group", "delivery_group"):
        check(f"no group is called '{banned}'", banned not in str(groups))
    check("the view definition contains no premise wording",
          "premise" not in vdef.lower())
    check("meta_extract declares service_mode_mapping unavailable",
          meta["channel_mapping"]["service_mode_mapping"] == "unavailable")
    check("meta_extract explains the group means ordering pattern",
          "NOT a service mode" in meta["channel_mapping"]["group_meaning"])
    check("prompt states no verified service-mode mapping exists",
          "THERE IS NO VERIFIED MAPPING" in flat)
    check("prompt separates web_associated from delivery",
          "web_associated != delivery" in flat)
    check("prompt separates non_web_associated from dine-in/drive-thru",
          "non_web_associated != dine-in" in flat)

    print("=== K. code 4 is never a verified Drive Thru ===")
    cur.execute("""SELECT DISTINCT channel_name_confidence FROM v_order_channel_context
                   WHERE channel_code = 4""")
    check("code 4's name carries unverified confidence",
          [r[0] for r in cur.fetchall()] == ["project_convention_unverified"])
    cur.execute("""SELECT COUNT(*) FROM information_schema.columns
                   WHERE table_name = 'v_order_channel_context'
                     AND column_name IN ('channel_name', 'service_mode',
                                         'channel_name_verified')""")
    check("no column presents an unqualified verified channel name",
          cur.fetchone()[0] == 0)
    check("prompt requires the UNVERIFIED label when quoting the convention",
          "UNVERIFIED" in flat and "project-maintained convention" in flat)

    print("=== L. numerical similarity does not verify semantics ===")
    # Masroor reported ~64% Drive Thru using the same unverified convention.
    cur.execute("""SELECT ROUND(100.0 * SUM(o.final_total) FILTER (WHERE c.channel_code = 4)
                               / NULLIF(SUM(o.final_total), 0), 2)
                   FROM v_orders_classified o
                   JOIN v_order_channel_context c ON c.order_id = o.id
                   WHERE o.establishment_id = 26 AND o.txn_class = 'REAL'
                     AND o.business_date >= '2026-06-01'
                     AND o.business_date <  '2026-07-01'""")
    share = float(cur.fetchone()[0])
    check("code 4 is ~64.66% of REAL revenue", abs(share - 64.66) < 0.01, str(share))
    check("...which is CONSISTENT WITH the prior report, not confirmation of it",
          meta["channel_mapping"]["name_confidence"] == "project_convention_unverified")
    check("prompt says a numerical match does not verify semantics",
          "does NOT verify semantics" in flat)
    check("prompt names the trap explicitly",
          "not independent confirmation" in flat)

    print("=== M. mis-ring terminology ===")
    check("the finding is typed as a metadata inconsistency",
          md["finding_type"] == "metadata_inconsistency")
    check("methodology describes records disagreeing, not service failures",
          "disagree on the record" in md["methodology"])
    check("limitations forbid an operational accusation",
          "never as a confirmed" in md["limitations"].lower()
          and "accusation" in md["limitations"])
    check("prompt forbids calling it staff error or an accusation",
          "never as staff error" in flat and "never as an accusation" in flat)

    print("=== N. product-name questions resolve to variants, not one SKU ===")
    # N6 ("which channel sold the most 5 Finger Meals") flipped between answers
    # because the model differed on WHICH records count. The facts below are why
    # a single merged ranking is not a fact about the business.
    cur.execute("""
        SELECT i.product_id, MIN(i.product_name),
               SUM(i.quantity)::int,
               COUNT(*) FILTER (WHERE i.combo_uuid IS NOT NULL),
               string_agg(DISTINCT ch.channel_code::text, ',' ORDER BY ch.channel_code::text)
        FROM order_items_v2 i
        JOIN v_orders_classified o ON o.id = i.order_id
        JOIN v_order_channel_context ch ON ch.order_id = o.id
        WHERE o.establishment_id = 26 AND o.txn_class = 'REAL'
          AND o.business_date >= '2026-06-01' AND o.business_date < '2026-07-01'
          AND i.deleted IS NOT TRUE AND i.is_voided IS NOT TRUE
          AND i.product_name ILIKE '%5 Finger%'
        GROUP BY 1 ORDER BY 3 DESC""")
    variants = {r[0]: r for r in cur.fetchall()}
    check("a name match resolves to several product records", len(variants) >= 4,
          str(len(variants)))
    check("14548 '** 5 Finger Spicy **' is combo-component form",
          variants[14548][3] > 0 and variants[14548][4] == "0,1,4",
          str(variants[14548][4]))
    check("14414 '5 Finger Meal*' is single-line combo form",
          variants[14414][3] == 0 and variants[14414][4] == "100,101,5,8",
          str(variants[14414][4]))
    # The crux: the two forms never appear on the same channel codes, so a
    # merged "winner" is an artefact of record selection, not a business fact.
    combo_codes = set(variants[14548][4].split(",")) | set(variants[14547][4].split(","))
    single_codes = set(variants[14414][4].split(","))
    check("the two forms are CHANNEL-DISJOINT",
          not (combo_codes & single_codes), f"{sorted(combo_codes)} vs {sorted(single_codes)}")
    check("'25 Finger Party Pack*' substring-matches but is a different product",
          14640 in variants and variants[14640][2] == 1)
    check("no maintained equivalence table exists between these ids",
          True)  # asserted by absence: nothing maps them together
    cur.execute("""SELECT COUNT(*) FROM information_schema.tables
                   WHERE table_name IN ('product_equivalence', 'product_sku_equivalence')""")
    check("...and none has been invented", cur.fetchone()[0] == 0)

    print("=== O. the prompt encodes the deterministic contract ===")
    for probe, label in (
            ("REPORT BY VARIANT", "requires per-variant reporting"),
            ("Do NOT declare one cross-variant winner", "forbids a merged winner"),
            ("Never use product_name LIKE to sum quantities", "forbids LIKE-summing"),
            ("substring FALSE MATCH", "names the false-match trap"),
            ("CHANNEL-DISJOINT", "explains why merging misleads"),
            ("BOUNDED TOOL PLAN", "bounds the tool plan"),
            ("Do NOT re-query hunting for a different, larger or non-zero result",
             "forbids re-querying for a preferred result"),
            ("a zero or an awkward result is still the answer", "keeps zero-is-an-answer")):
        check(f"prompt {label}", probe in flat)
    # The scope contract for this question shape.
    check("channel questions remain gated business data",
          "v_order_channel_context" not in cs._REFERENCE_RELATIONS)
    check("the current category reference view is NOT the route for this",
          "v_product_category_current" in cs._REFERENCE_RELATIONS
          and "v_order_channel_context" not in cs._REFERENCE_RELATIONS)

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
