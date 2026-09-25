-- 45_identity_scan_indexes.sql
-- Two partial indexes that make the non-individual identity scan viable.
--
-- Problem
-- -------
-- _suspected_non_individual_ids() grouped v_orders_classified over ALL history.
-- That view derives txn_class with a correlated per-order aggregate against
-- order_items_v2, so the plan was a Nested Loop Left Join running one aggregate
-- per order row (112,986 rows, plan cost ~16,031,419). It consistently hit the
-- 15,000 ms statement_timeout, and the caller swallowed the timeout and returned
-- [] WITHOUT caching -- so every request paid a full 15 s and then silently
-- degraded identity analysis to "no non-individual accounts known".
--
-- Fix
-- ---
-- The query is rewritten in chat_sql.py to the provably equivalent EXISTS form
-- (verified: 111,758 = 111,758 rows, and the HAVING result sets are identical
-- with symmetric difference 0). These indexes then make that form cheap:
--
--   before (view, nested loop)     15,000 ms -> statement timeout
--   after  (EXISTS rewrite)         4,566 ms
--   after  (EXISTS + these indexes)   786 ms
--
-- CONCURRENTLY so no write traffic is blocked; that is also why each statement
-- stands alone with no surrounding transaction.

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_oiv2_order_id_live
    ON order_items_v2 (order_id)
    WHERE deleted IS NOT TRUE AND is_voided IS NOT TRUE;

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_orders_v2_customer_live
    ON orders_v2 (customer_id, establishment_id)
    WHERE customer_id IS NOT NULL AND deleted IS NOT TRUE AND final_total > 0;
