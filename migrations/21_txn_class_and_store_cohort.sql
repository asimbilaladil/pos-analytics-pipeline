-- Migration 21: transaction classification + store cohort indexing
--
-- Supports the Revel -> LLM analysis spec (Parts A1, A2, A7). Purely additive:
-- one nullable column on establishments and three views. No existing table,
-- column or pipeline write path is touched, so this is safe to apply while the
-- nightly run and the chat assistant are live, and safe to roll back (see the
-- DROP block at the bottom).
--
-- WHY, measured on this database rather than assumed from the spec:
--   * The spec's Nederland pull found 67.7% empty tickets, overstating
--     transactions by 48%. Here the pipeline's existing `closed = TRUE` filter
--     already removes 78,281 of 78,941 empty tickets (30d). The residual
--     contaminant is COMP -- 4,161 closed $0-with-items orders in 30 days --
--     which overstates order count by ~3.4% and depresses AOV by ~3.8%
--     ($19.43 reported vs $20.19 real). Smaller than the spec, still wrong,
--     and silent.
--   * COMP is also the employee-meal / de-facto-void bucket, so it is kept and
--     labelled, never dropped.

BEGIN;

-- ── A7. store cohort ───────────────────────────────────────────────────────
-- weeks_since_open is the only valid basis for cross-store comparison. There is
-- no open date anywhere in the Revel data, so it is a maintained field.
-- open_date_source records whether a date is confirmed by a human or inferred
-- from the first observed order, because an inferred date is only as good as
-- the backfill window behind it.
ALTER TABLE establishments
    ADD COLUMN IF NOT EXISTS open_date        DATE,
    ADD COLUMN IF NOT EXISTS open_date_source TEXT
        CHECK (open_date_source IN ('confirmed', 'inferred_first_order'));

COMMENT ON COLUMN establishments.open_date IS
    'Store opening date. Maintained by hand -- Revel does not carry it. '
    'Drives weeks_since_open, the only valid cross-store comparison basis.';
COMMENT ON COLUMN establishments.open_date_source IS
    'confirmed = verified by a human. inferred_first_order = earliest order in '
    'orders_v2, which is bounded by the backfill window and is a floor, not a '
    'fact. Treat inferred cohort figures as low confidence.';

-- Seed inferred dates only where nothing is set. A store whose first order
-- equals the start of the backfill window (2026-01-01) is almost certainly
-- older than that, so it is left NULL rather than given a wrong date.
WITH first_order AS (
    SELECT establishment_id, MIN((created_date AT TIME ZONE 'America/Chicago')::date) AS d
    FROM orders_v2
    WHERE deleted IS NOT TRUE
    GROUP BY establishment_id
)
UPDATE establishments e
SET open_date        = f.d,
    open_date_source = 'inferred_first_order'
FROM first_order f
WHERE e.id = f.establishment_id
  AND e.open_date IS NULL
  AND f.d > DATE '2026-01-07';   -- inside the window, not clipped by it

-- ── A1. transaction classification ─────────────────────────────────────────
-- Never drops non-REAL rows: empty tickets expose POS procedure defects and
-- COMP is the employee-meal / void bucket. Every consumer must state which
-- class it counted.
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
    END                                                          AS txn_class
FROM orders_v2 o
-- LATERAL, not a grouped subquery: a plain GROUP BY subquery cannot have the
-- caller's establishment/date predicate pushed into it, so Postgres aggregated
-- all 5.4M order_items rows before joining (~4.1s for one store over 90 days).
-- Correlating on o.id lets it use ix_oiv2_order_id per order instead (~1.1s,
-- identical results).
LEFT JOIN LATERAL (
    SELECT COUNT(*)                                                 AS item_count,
           COUNT(DISTINCT oi.combo_uuid)                            AS combo_count,
           SUM(oi.pure_sales) FILTER (WHERE oi.combo_uuid IS NOT NULL) AS combo_sales,
           SUM(oi.pure_sales) FILTER (WHERE oi.combo_uuid IS NULL)     AS standalone_sales
    FROM order_items_v2 oi
    WHERE oi.order_id = o.id
      AND oi.deleted IS NOT TRUE AND oi.is_voided IS NOT TRUE
) i ON TRUE;

COMMENT ON VIEW v_orders_classified IS
    'orders_v2 plus txn_class (REAL/EMPTY/COMP/DELETED), business_date and '
    'combo rollups. Default every rate and average to txn_class = ''REAL'' and '
    'say so. Non-REAL rows are diagnostics, not noise -- do not filter them out '
    'of the view.';

-- ── A2. line items with combo structure made explicit ──────────────────────
-- A combo is N rows sharing combo_uuid, so counting line items is not counting
-- products bought and "items per order" is not order size.
CREATE OR REPLACE VIEW v_order_items_classified AS
SELECT
    oi.*,
    (oi.combo_uuid IS NOT NULL)                                  AS in_combo,
    oi.combo_uuid                                                AS combo_group_id,
    ROW_NUMBER() OVER (PARTITION BY oi.combo_uuid ORDER BY oi.id) AS combo_seq,
    (oi.created_date AT TIME ZONE 'America/Chicago')::date        AS business_date
FROM order_items_v2 oi
WHERE oi.deleted IS NOT TRUE;

COMMENT ON VIEW v_order_items_classified IS
    'order_items_v2 plus in_combo / combo_group_id / combo_seq. 89% of line '
    'items sit inside a combo. There is no combo price field -- a combo price '
    'is the sum of its component rows.';

-- ── A7. cohort view ────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW v_store_cohort AS
SELECT
    e.id                AS establishment_id,
    e.name              AS store_name,
    e.open_date,
    e.open_date_source,
    CASE WHEN e.open_date IS NULL THEN NULL
         ELSE (CURRENT_DATE - e.open_date) / 7 + 1 END           AS weeks_since_open,
    CASE WHEN e.open_date IS NULL THEN NULL
         ELSE ((CURRENT_DATE - e.open_date) / 7 + 1)             END::int
         AS weeks_since_open_int,
    CASE
        WHEN e.open_date IS NULL                              THEN 'unknown'
        WHEN (CURRENT_DATE - e.open_date) / 7 + 1 <= 8        THEN 'honeymoon'
        WHEN (CURRENT_DATE - e.open_date) / 7 + 1 <= 26       THEN 'ramp'
        WHEN (CURRENT_DATE - e.open_date) / 7 + 1 <= 52       THEN 'maturing'
        ELSE 'mature'
    END                                                          AS store_age_bucket
FROM establishments e;

COMMENT ON VIEW v_store_cohort IS
    'Cohort indexing for cross-store comparison. Stores of different ages are '
    'not comparable on calendar alignment. Weeks 1-8 (honeymoon) must be '
    'excluded from any baseline, and no year-over-year comparison is valid for '
    'a store under 18 months old.';

COMMIT;

-- The chat assistant queries as the read-only role, which does not inherit
-- rights on views created later. Without this every classified-view query the
-- model writes fails with "permission denied for view".
GRANT SELECT ON v_orders_classified, v_order_items_classified, v_store_cohort TO laynes_ro;

-- Rollback:
--   DROP VIEW IF EXISTS v_store_cohort, v_order_items_classified, v_orders_classified;
--   ALTER TABLE establishments DROP COLUMN IF EXISTS open_date,
--                              DROP COLUMN IF EXISTS open_date_source;
