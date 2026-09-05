-- Migration 25: expose the maintained entrée classification through the
-- analysis views (A3 phases 4-5).
--
-- Additive only. txn_class, business_date, combo_count, combo_sales,
-- standalone_sales and identity_captured keep their existing definitions and
-- positions, so migration 21's contract and the A12 reconciliation gate that
-- reads v_orders_classified are unaffected. New columns are appended.
--
-- product_form is resolved per LINE ITEM, because the same product legitimately
-- appears in two structural forms:
--   combo_component   -- a line inside a combo_uuid group ("** 4 Finger Spicy **")
--   single_line_combo -- one row that IS the whole combo ("4 Finger Meal*")
--   standalone        -- sold on its own
--   unknown           -- product not yet reviewed, so its form is not decided
-- The first is read from combo_uuid, which is POS structure. The second comes
-- from the maintained table, never from the trailing "*" in the name.

BEGIN;

CREATE OR REPLACE VIEW v_order_items_classified AS
SELECT
    oi.*,
    (oi.combo_uuid IS NOT NULL)                                  AS in_combo,
    oi.combo_uuid                                                AS combo_group_id,
    ROW_NUMBER() OVER (PARTITION BY oi.combo_uuid ORDER BY oi.id) AS combo_seq,
    (oi.created_date AT TIME ZONE 'America/Chicago')::date        AS business_date,
    COALESCE(c.is_entree, FALSE)                                 AS is_entree,
    CASE
        WHEN c.product_id IS NULL              THEN 'unknown'
        WHEN c.confidence = 'unknown'          THEN 'unknown'
        WHEN oi.combo_uuid IS NOT NULL         THEN 'combo_component'
        WHEN c.product_form = 'single_line_combo' THEN 'single_line_combo'
        ELSE 'standalone'
    END                                                          AS product_form,
    COALESCE(c.confidence, 'unknown')                            AS classification_confidence
FROM order_items_v2 oi
LEFT JOIN product_analysis_classification c ON c.product_id = oi.product_id
WHERE oi.deleted IS NOT TRUE;

COMMENT ON VIEW v_order_items_classified IS
    'order_items_v2 plus combo structure and the maintained entrée '
    'classification. is_entree comes from product_analysis_classification and '
    'is FALSE for any product not yet reviewed -- check '
    'classification_confidence before trusting an entrée metric.';

CREATE OR REPLACE VIEW v_orders_classified AS
SELECT
    o.*,
    (o.created_date AT TIME ZONE 'America/Chicago')::date        AS business_date,
    COALESCE(i.item_count, 0)                                    AS item_count,
    COALESCE(i.combo_count, 0)                                   AS combo_count,
    COALESCE(i.combo_sales, 0)                                   AS combo_sales,
    COALESCE(i.standalone_sales, 0)                              AS standalone_sales,
    (o.customer_id IS NOT NULL)                                  AS identity_captured,
    CASE
        WHEN o.deleted IS TRUE                                            THEN 'DELETED'
        WHEN o.final_total > 0 AND COALESCE(i.item_count, 0) > 0          THEN 'REAL'
        WHEN COALESCE(o.final_total, 0) = 0 AND COALESCE(i.item_count, 0) = 0 THEN 'EMPTY'
        WHEN COALESCE(o.final_total, 0) = 0 AND COALESCE(i.item_count, 0) > 0 THEN 'COMP'
        ELSE 'OTHER'
    END                                                          AS txn_class,
    -- A3, appended so existing consumers are untouched.
    COALESCE(i.entree_count, 0)                                  AS entree_count,
    COALESCE(i.unresolved_item_count, 0)                         AS unresolved_item_count,
    (COALESCE(i.unresolved_item_count, 0) = 0)                   AS entree_fully_resolved
FROM orders_v2 o
LEFT JOIN LATERAL (
    SELECT COUNT(*)                                                 AS item_count,
           COUNT(DISTINCT oi.combo_uuid)                            AS combo_count,
           SUM(oi.pure_sales) FILTER (WHERE oi.combo_uuid IS NOT NULL) AS combo_sales,
           SUM(oi.pure_sales) FILTER (WHERE oi.combo_uuid IS NULL)     AS standalone_sales,
           -- Quantity, not row count: one line can carry two entrées.
           COALESCE(SUM(oi.quantity) FILTER (WHERE c.is_entree), 0)  AS entree_count,
           COUNT(*) FILTER (WHERE c.product_id IS NULL
                               OR c.confidence = 'unknown')         AS unresolved_item_count
    FROM order_items_v2 oi
    LEFT JOIN product_analysis_classification c ON c.product_id = oi.product_id
    WHERE oi.order_id = o.id
      AND oi.deleted IS NOT TRUE AND oi.is_voided IS NOT TRUE
) i ON TRUE;

COMMENT ON VIEW v_orders_classified IS
    'orders_v2 plus txn_class (REAL/EMPTY/COMP/DELETED), business_date, combo '
    'rollups and entrée counts. entree_count counts only products with a '
    'verified classification; unresolved_item_count says how many lines on the '
    'order are not yet reviewed, and entree_fully_resolved is FALSE when the '
    'entrée total for that order may be understated.';

COMMIT;

-- Coverage, so the assistant can qualify an entrée answer instead of guessing.
CREATE OR REPLACE VIEW v_entree_coverage AS
SELECT
    o.establishment_id,
    o.business_date,
    COUNT(*)                                                   AS real_orders,
    COUNT(*) FILTER (WHERE o.entree_fully_resolved)             AS fully_resolved_orders,
    ROUND(100.0 * COUNT(*) FILTER (WHERE o.entree_fully_resolved)
          / NULLIF(COUNT(*), 0), 2)                             AS pct_orders_resolved,
    SUM(o.entree_count)                                         AS entrees,
    SUM(o.unresolved_item_count)                                AS unresolved_items
FROM v_orders_classified o
WHERE o.txn_class = 'REAL'
GROUP BY 1, 2;

COMMENT ON VIEW v_entree_coverage IS
    'Per store and day: how much of the entrée classification is resolved. '
    'Below the coverage floor an entrée metric must be qualified or refused.';

-- Recreating a view drops its grants, so they are restated here rather than
-- relying on migration 21 having run. Without this the assistant fails with
-- "permission denied for view v_orders_classified" after a replay.
GRANT SELECT ON v_orders_classified, v_order_items_classified,
                v_entree_coverage, v_entree_review_queue TO laynes_ro;

-- Rollback: re-run migration 21 for the two view definitions, then
--   DROP VIEW IF EXISTS v_entree_coverage;
