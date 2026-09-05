-- Migration 32: current-category reference lookup (A5 follow-up)
--
-- "What category is 5 Finger Meal in?" is a reference question, not an
-- analysis of a period. Before this it could only be answered through
-- v_order_items_category_context, which joins order_items_v2 and is therefore
-- gated business data -- so the assistant had to invent a store and a date
-- scope, pass A12 reconciliation, and scan transactions to look up a fact that
-- has nothing to do with any of them. It took four tool calls and 57s, one of
-- which hit a statement timeout.
--
-- This view reads ONLY the mapping table and the product catalogue. It touches
-- no order, item or payment row, which is what makes it safe to treat as
-- ungated reference data alongside products / establishments / v_store_cohort.
--
-- IT MAKES NO HISTORICAL CLAIM. mapping_confidence stays 'verified_current' and
-- current_snapshot_at says exactly when the snapshot was taken. Answering "it
-- is currently in Meals" is fair; answering "the June sale was in Meals" is not,
-- and that question must still go through v_order_items_category_context, which
-- keeps its per-row historical_category_verified flag and its A12 gating.
--
-- FUTURE CATEGORY HISTORY (documented, deliberately not built here):
-- The nightly run already archives every Product page, so snapshots accumulate
-- from 2026-09-02 onward. Those preserved snapshots -- not updated_date -- are
-- the authoritative path to real category-version history later: comparing
-- consecutive runs yields true effective_from/effective_to periods. Until then
-- category_stable_since (product.updated_date) is used as a LOWER-BOUND
-- stability signal: it proves a category held since that date, never before it.
-- This is deliberately conservative, and it means verification coverage for an
-- OLD period can only fall as products are edited. That is correct behaviour,
-- not drift to be corrected, and it must not be papered over by backfilling
-- effective periods that were never observed.

BEGIN;

CREATE OR REPLACE VIEW v_product_category_current AS
SELECT
    m.product_id,
    m.establishment_id,
    p.name                            AS product_name,
    m.category_id,
    m.category_name_snapshot          AS category_name,
    m.parent_category_id,
    m.parent_category_name_snapshot   AS parent_category_name,
    m.mapping_source,
    m.mapping_confidence,
    m.snapshot_taken_at               AS current_snapshot_at,
    m.category_stable_since
FROM product_category_mapping m
LEFT JOIN products p ON p.id = m.product_id;

COMMENT ON VIEW v_product_category_current IS
    'CURRENT product -> category reference. Reference data only: it reads no '
    'order, item or payment row, so it needs no store/period scope and no A12 '
    'reconciliation. It states what a product is categorised as TODAY '
    '(current_snapshot_at) and proves nothing about a past period -- for that '
    'use v_order_items_category_context and its historical_category_verified '
    'flag. Category comes from Revel Product.category, never product_class.';

COMMIT;

GRANT SELECT ON v_product_category_current TO laynes_ro;

-- Rollback:
--   DROP VIEW IF EXISTS v_product_category_current;
