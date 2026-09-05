-- Migration 31: verified product -> category dimension (A5)
--
-- WHAT WAS FOUND, AND WHY IT IS NOT product_class -----------------------------
-- A3 disproved products.product_class as a category source. This migration does
-- NOT revisit that: it uses a different, explicit field.
-- Revel's Product resource carries BOTH, side by side, on the same object:
--     category      = /products/ProductCategory/2/     <- the real dimension
--     productclass  = /products/ProductClass/34/       <- a separate namespace
-- Their coexistence on one record is direct proof the two are different things,
-- which is why the numeric-ID coincidence exploited in A3 was wrong. The
-- category route is verified semantically as well: Fries -> "Fries - Choice",
-- LOBBY DRINK -> "Drinks", "** 3 Finger Spicy **" -> "Meals", with a real parent
-- hierarchy ("Items to Make Combos", "Main Menu"). ProductClass, by contrast,
-- filed meals and shakes under "Merch".
-- products.category_id stays NULL and untouched: the nightly sync owns that
-- table and would overwrite anything written there.
--
-- HISTORICAL VALIDITY IS BOUNDED, NOT ASSUMED ---------------------------------
-- The raw archive holds Product snapshots ONLY from 2026-09-02 to 2026-09-05 --
-- five runs, all within the last few days. There is no June snapshot, so a
-- June category assignment cannot be read directly and today's mapping must NOT
-- be backdated.
-- What the archive does give is each product's updated_date, which is the last
-- moment ANY field on that record changed. If a product has not been touched
-- since before a period began, its category necessarily held throughout that
-- period. That is evidence, not inference, and it is stored as
-- category_stable_since so validity can be evaluated per period:
--     category_stable_since < period_start  ->  historically verified
--     otherwise                             ->  current mapping only
-- historical validity is therefore period-relative and is computed by the
-- consumer, never frozen into a boolean here.
--
-- Measured for Nederland June 2026: 100% of REAL order-item rows and revenue
-- carry a category, of which 95.82% of items but only 83.64% of REVENUE are
-- historically verified -- the high-value meal products were edited after June.
--
-- The assistant cannot write here; it reads the views below.

BEGIN;

CREATE TABLE IF NOT EXISTS product_category_mapping (
    product_id                    INTEGER PRIMARY KEY,
    establishment_id              INTEGER,
    category_id                   INTEGER,
    category_name_snapshot        TEXT,
    parent_category_id            INTEGER,
    parent_category_name_snapshot TEXT,
    -- Last date the product record changed. The category is proven to have
    -- held from here onward; before this we have no evidence either way.
    category_stable_since         DATE,
    effective_from                DATE,
    effective_to                  DATE,
    mapping_source                TEXT NOT NULL
        CHECK (mapping_source IN ('revel_product_api', 'manual_review',
                                  'verified_product_list', 'unresolved')),
    mapping_confidence            TEXT NOT NULL
        CHECK (mapping_confidence IN ('verified_current', 'verified_historical',
                                      'unknown')),
    snapshot_taken_at             TIMESTAMPTZ,
    reviewed_at                   TIMESTAMPTZ,
    reviewed_by                   TEXT,
    review_note                   TEXT
);

COMMENT ON TABLE product_category_mapping IS
    'Product -> ProductCategory from Revel''s explicit Product.category field. '
    'NOT derived from product_class, which is a different namespace (see A3). '
    'mapping_confidence is verified_current: the archive holds no snapshot older '
    'than 2026-09-02, so historical validity must be judged per period using '
    'category_stable_since, never assumed.';

COMMIT;

-- ── LLM-facing views ───────────────────────────────────────────────────────
-- Kept OUT of v_order_items_classified so ordinary non-category analytics are
-- unaffected; join this only when a question is about categories.
CREATE OR REPLACE VIEW v_order_items_category_context AS
SELECT
    i.id                              AS order_item_id,
    i.order_id,
    i.establishment_id,
    i.product_id,
    i.product_name,
    (i.created_date AT TIME ZONE 'America/Chicago')::date AS business_date,
    m.category_id,
    m.category_name_snapshot          AS category_name,
    m.parent_category_id,
    m.parent_category_name_snapshot   AS parent_category_name,
    m.category_stable_since,
    COALESCE(m.mapping_source, 'unresolved')     AS category_mapping_source,
    COALESCE(m.mapping_confidence, 'unknown')    AS category_mapping_confidence,
    -- Period-relative: TRUE only when the mapping is proven to predate the row's
    -- own business date. A June sale is historically verified only if the
    -- product had not been edited since before that June day.
    (m.category_stable_since IS NOT NULL
     AND m.category_stable_since
         < (i.created_date AT TIME ZONE 'America/Chicago')::date)
                                      AS historical_category_verified
FROM order_items_v2 i
LEFT JOIN product_category_mapping m ON m.product_id = i.product_id
WHERE i.deleted IS NOT TRUE;

COMMENT ON VIEW v_order_items_category_context IS
    'Order items with their verified category. historical_category_verified is '
    'computed per row against that row''s own date -- FALSE means the product '
    'was edited after the sale, so only TODAY''s category is known for it. '
    'Category is never inferred from a product name, and never from '
    'product_class.';

CREATE OR REPLACE VIEW v_category_review_queue AS
SELECT m.product_id, m.category_id, m.category_name_snapshot,
       m.category_stable_since, m.mapping_confidence,
       COUNT(*)                    AS line_items_90d,
       ROUND(SUM(i.pure_sales), 2) AS revenue_90d
FROM product_category_mapping m
JOIN order_items_v2 i ON i.product_id = m.product_id
WHERE (m.category_id IS NULL OR m.mapping_confidence = 'unknown')
  AND i.deleted IS NOT TRUE AND i.is_voided IS NOT TRUE
  AND i.created_date >= now() - interval '90 days'
GROUP BY 1, 2, 3, 4, 5
ORDER BY revenue_90d DESC NULLS LAST;

COMMENT ON VIEW v_category_review_queue IS
    'Products with no verified category, ranked by revenue at stake.';

GRANT SELECT ON v_order_items_category_context, v_category_review_queue TO laynes_ro;

-- Rollback:
--   DROP VIEW IF EXISTS v_category_review_queue, v_order_items_category_context;
--   DROP TABLE IF EXISTS product_category_mapping;
