-- Migration 23: daily payment aggregate for the A12 reconciliation gate
--
-- The gate needs a reference total that was NOT derived from orders_v2, so a
-- store/period total can be cross-checked before the assistant analyses it.
--
-- Why payments_v2 is the reference:
--   * features_daily_summary_v2.total_revenue is computed FROM orders_v2, so
--     comparing the two only proves the aggregator ran -- it cannot detect a
--     missing or partial order sync. Measured on Nederland June 2026 the two
--     agree to the cent ($91,006.17 both), which is exactly why it is useless
--     as a check.
--   * payments_v2 is a separate Revel resource with its own sync_state
--     watermark, fetched in its own API pass. It is the only total in this
--     database that can disagree with orders_v2, and disagreement is the whole
--     point of a reconciliation gate.
--
-- Honest limitation, recorded here so nobody over-trusts the number: this is a
-- cross-resource check, not an independent ledger. Both sides come from the
-- same Revel account through the same nightly pipeline, so a fault upstream of
-- the API would move both together and pass. It detects partial syncs, dropped
-- pages, interrupted backfills and order/payment drift -- not Revel being wrong.
--
-- Why an aggregate view rather than granting payments_v2:
-- payments_v2 carries transaction ids, card types, processor flags and
-- created_by_user_id. None of that belongs in the assistant's reach. This view
-- exposes only a per-store, per-day count and sum, which is all the gate needs.
-- The read-only role is granted this view and still cannot read payments_v2.

BEGIN;

CREATE OR REPLACE VIEW v_payments_daily_v2 AS
SELECT
    p.establishment_id,
    (p.created_date AT TIME ZONE 'America/Chicago')::date        AS business_date,
    COUNT(*)                                                     AS payment_count,
    COUNT(DISTINCT p.order_id)                                   AS paid_order_count,
    SUM(p.amount)                                                AS payment_amount,
    SUM(p.tip)                                                   AS tip_amount,
    COUNT(*) FILTER (WHERE p.refunded IS TRUE)                   AS refunded_count
FROM payments_v2 p
WHERE p.deleted IS NOT TRUE
GROUP BY 1, 2;

COMMENT ON VIEW v_payments_daily_v2 IS
    'Per-store, per-day payment totals -- the reference side of the A12 '
    'reconciliation gate. Deliberately aggregate: no transaction ids, card '
    'types, processor flags or user ids are exposed. Independent of orders_v2 '
    'in sync path only, not in origin -- see migration 23 header.';

COMMIT;

-- The assistant queries as the read-only role, which does not inherit rights on
-- views created later. Without this the gate fails with "permission denied".
GRANT SELECT ON v_payments_daily_v2 TO laynes_ro;

-- Rollback:
--   DROP VIEW IF EXISTS v_payments_daily_v2;
