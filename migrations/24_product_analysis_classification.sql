-- Migration 24: maintained entrée classification (A3)
--
-- The assistant must never decide what an entrée is by looking at a product
-- name at query time. This table is the single maintained answer, and the
-- analysis views read from it.
--
-- WHY NOT products.category_id / ProductCategory  ------------------------------
-- Investigated first and rejected on evidence, not assumption:
--   * products.category_id is NULL for all 36,645 rows.
--   * products.product_class holds a URI like /products/ProductClass/166/, and
--     those integers DO join to product_categories.id -- but the join is
--     coincidental, not semantic. Class 166 maps to the category "Merch" while
--     containing "5 Finger Meal*", "Oreo Shake*" and "Texas Toast**"; class 35
--     maps to "Catering Food" and is entirely fountain drinks. ProductClass and
--     ProductCategory are separate Revel ID spaces that happen to overlap.
--   * products.is_combo is FALSE even for "5 Finger Meal*", so it does not
--     identify single-line combos either.
-- Historical category assignment therefore CANNOT be proven from this database,
-- and nothing here is derived from it.
--
-- WHAT IS PROVABLE: combo position  --------------------------------------------
-- Within a combo_uuid group, ordering by order_items_v2.id, the first line is
-- the combo's entrée and the rest are its sides, drink and sauces. Measured
-- across all 2026 line items: 363 products appear ONLY in first position, 1,182
-- appear ONLY in later positions, and just 2 appear in both -- each explained by
-- a single stray row out of hundreds ("** Wrap Crispy**" 882 first / 1 later;
-- "Regular Vanilla Shake" 1 first / 168 later). Nothing at first position is a
-- drink, side or sauce.
-- This is structure recorded by the POS, not a guess about a name, so it is
-- treated as verified evidence and recorded as classification_source
-- 'verified_structure'. That value is an addition to the source list in the A3
-- brief; it was added because it is the only provable evidence available once
-- the category route is ruled out, and conflating it with 'verified_category'
-- would overstate what is known.
--
-- WHAT IS NOT RESOLVED  --------------------------------------------------------
-- Products that never appear inside a combo have no structural evidence at all.
-- That set includes the single-line combos ("5 Finger Meal*" at $17.35,
-- "Family Pack*" at $51.74) and standalone items. Name patterns are NOT used to
-- decide these: they are seeded as is_entree = FALSE, product_form 'unknown',
-- confidence 'unknown', and surfaced through v_entree_review_queue for a human
-- to resolve. For Nederland June 2026 that is 53 products, 1,624 line items and
-- about $15.8k -- roughly 17% of revenue -- so entrée metrics are understated
-- until reviewed, and the coverage figures in the views say so.
--
-- The read-only assistant role gets SELECT on the views only. It cannot write
-- here: classification is maintained by people, through migrations.

BEGIN;

CREATE TABLE IF NOT EXISTS product_analysis_classification (
    product_id            INTEGER PRIMARY KEY,
    product_name_snapshot TEXT        NOT NULL,
    is_entree             BOOLEAN     NOT NULL DEFAULT FALSE,
    product_form          TEXT        NOT NULL DEFAULT 'unknown'
        CHECK (product_form IN ('combo_component', 'single_line_combo',
                                'standalone', 'non_entree', 'unknown')),
    classification_source TEXT        NOT NULL
        CHECK (classification_source IN ('verified_category', 'verified_structure',
                                         'manual_review', 'verified_product_list',
                                         'unresolved')),
    confidence            TEXT        NOT NULL DEFAULT 'unknown'
        CHECK (confidence IN ('high', 'medium', 'unknown')),
    effective_from        DATE        NOT NULL DEFAULT DATE '2026-01-01',
    effective_to          DATE,
    reviewed_at           TIMESTAMPTZ,
    review_note           TEXT
);

COMMENT ON TABLE product_analysis_classification IS
    'Maintained entrée definition. The assistant reads this and never infers '
    'entrée status from a product name. Rows with confidence = ''unknown'' are '
    'awaiting human review -- see v_entree_review_queue.';

-- ── seed 1: structural entrées (first line of a combo) ─────────────────────
WITH combo_pos AS (
    SELECT i.product_id, i.product_name,
           ROW_NUMBER() OVER (PARTITION BY i.combo_uuid ORDER BY i.id) AS seq
    FROM order_items_v2 i
    WHERE i.deleted IS NOT TRUE AND i.is_voided IS NOT TRUE
      AND i.combo_uuid IS NOT NULL
),
agg AS (
    SELECT product_id,
           MIN(product_name)                        AS nm,
           COUNT(*) FILTER (WHERE seq = 1)          AS first_n,
           COUNT(*) FILTER (WHERE seq > 1)          AS later_n
    FROM combo_pos GROUP BY product_id
)
INSERT INTO product_analysis_classification
    (product_id, product_name_snapshot, is_entree, product_form,
     classification_source, confidence, reviewed_at, review_note)
SELECT product_id, nm,
       TRUE, 'combo_component', 'verified_structure',
       -- A product that also shows up mid-combo is downgraded rather than
       -- silently trusted, even when the stray rows are negligible.
       CASE WHEN later_n = 0 THEN 'high' ELSE 'medium' END,
       now(),
       'Seeded from combo position: appears as the first line of a combo '
       || first_n || ' time(s), mid-combo ' || later_n || ' time(s).'
FROM agg
WHERE first_n > 0
  AND first_n >= later_n          -- dominant position must be first
ON CONFLICT (product_id) DO NOTHING;

-- ── seed 2: structural non-entrées (only ever a later combo line) ──────────
WITH combo_pos AS (
    SELECT i.product_id, i.product_name,
           ROW_NUMBER() OVER (PARTITION BY i.combo_uuid ORDER BY i.id) AS seq
    FROM order_items_v2 i
    WHERE i.deleted IS NOT TRUE AND i.is_voided IS NOT TRUE
      AND i.combo_uuid IS NOT NULL
),
agg AS (
    SELECT product_id, MIN(product_name) AS nm,
           COUNT(*) FILTER (WHERE seq = 1) AS first_n,
           COUNT(*) FILTER (WHERE seq > 1) AS later_n
    FROM combo_pos GROUP BY product_id
)
INSERT INTO product_analysis_classification
    (product_id, product_name_snapshot, is_entree, product_form,
     classification_source, confidence, reviewed_at, review_note)
SELECT product_id, nm, FALSE, 'non_entree', 'verified_structure',
       CASE WHEN first_n = 0 THEN 'high' ELSE 'medium' END,
       now(),
       'Seeded from combo position: only ever a side/drink/sauce line within a '
       'combo (' || later_n || ' occurrences, first-position ' || first_n || ').'
FROM agg
WHERE later_n > 0 AND first_n < later_n
ON CONFLICT (product_id) DO NOTHING;

-- ── seed 3: everything else sold, left UNRESOLVED for review ───────────────
-- Deliberately NOT decided from names. is_entree stays FALSE so no metric can
-- silently count them; confidence 'unknown' is what the coverage gate reads.
INSERT INTO product_analysis_classification
    (product_id, product_name_snapshot, is_entree, product_form,
     classification_source, confidence, review_note)
SELECT i.product_id, MIN(i.product_name), FALSE, 'unknown', 'unresolved', 'unknown',
       'Never observed inside a combo, so no structural evidence exists. '
       'Requires human review: may be a single-line combo, a standalone entrée, '
       'or a non-entrée sold on its own.'
FROM order_items_v2 i
WHERE i.deleted IS NOT TRUE
  -- 21 line items network-wide carry no product_id ("Store Credit", "gh"),
  -- $19.19 total and none in any analysed scope. They cannot be classified by
  -- product and are left out rather than given a synthetic id.
  AND i.product_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM product_analysis_classification p
                  WHERE p.product_id = i.product_id)
GROUP BY i.product_id
ON CONFLICT (product_id) DO NOTHING;

COMMIT;

-- ── review queue: what a human should resolve, most valuable first ─────────
-- Name patterns appear ONLY here, as a hint to the reviewer. They never feed
-- is_entree.
CREATE OR REPLACE VIEW v_entree_review_queue AS
SELECT c.product_id,
       c.product_name_snapshot,
       c.confidence,
       c.classification_source,
       COUNT(*)                       AS line_items_90d,
       SUM(i.quantity)::int           AS quantity_90d,
       ROUND(SUM(i.pure_sales), 2)    AS revenue_90d,
       ROUND(AVG(i.price), 2)         AS avg_price,
       CASE
           WHEN c.product_name_snapshot LIKE '%Meal*'      THEN 'likely single_line_combo'
           WHEN c.product_name_snapshot LIKE '%Combo*'     THEN 'likely single_line_combo'
           WHEN c.product_name_snapshot LIKE '%Pack*'      THEN 'likely single_line_combo (shareable)'
           WHEN c.product_name_snapshot LIKE '** %'        THEN 'entrée-style name, never seen in a combo'
           ELSE 'no hint'
       END                            AS reviewer_hint
FROM product_analysis_classification c
JOIN order_items_v2 i ON i.product_id = c.product_id
WHERE c.confidence = 'unknown'
  AND i.deleted IS NOT TRUE AND i.is_voided IS NOT TRUE
  AND i.created_date >= now() - interval '90 days'
GROUP BY c.product_id, c.product_name_snapshot, c.confidence, c.classification_source
ORDER BY revenue_90d DESC NULLS LAST;

COMMENT ON VIEW v_entree_review_queue IS
    'Products with no verified classification, ranked by the revenue at stake. '
    'reviewer_hint is a name pattern shown to a HUMAN only -- it is never used '
    'to set is_entree.';

-- Rollback:
--   DROP VIEW IF EXISTS v_entree_review_queue;
--   DROP TABLE IF EXISTS product_analysis_classification;
