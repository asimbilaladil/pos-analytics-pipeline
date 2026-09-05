-- Migration 28: order-level payment context as a separate view (A10 phase 3)
--
-- WHY NOT ADDED DIRECTLY TO v_orders_classified:
-- That was tried first and measured. Adding a payment LATERAL to
-- v_orders_classified cost every consumer of the view, including the A12
-- reconciliation gate that runs before EVERY analytical request:
--     one store / one month   0.65s -> 0.84s
--     all stores / one month  6.68s -> 8.93s
--     all stores / 8 months  12.21s -> statement timeout
-- Postgres evaluates the lateral even when no payment column is selected, so
-- meta_extract paid for payment aggregation it never reads. Making the trust
-- gate slower -- or timing it out -- to add a field most queries do not use is
-- the wrong trade, so v_orders_classified is left exactly as migration 25 left
-- it and the payment context lives in this view instead.
--
-- v_orders_payment_classified carries every column of v_orders_classified plus
-- the safe payment fields, so anything wanting payment context at order level
-- selects from here and pays the cost only when it actually wants it.
-- Classification is unchanged: txn_class is inherited, never recomputed, and
-- payment presence never influences it.

BEGIN;

CREATE OR REPLACE VIEW v_orders_payment_classified AS
SELECT
    o.*,
    COALESCE(pay.payment_record_count, 0)      AS payment_record_count,
    (pay.order_id IS NOT NULL)                 AS has_payment,
    pay.payment_type_codes,
    pay.payment_type_single,
    COALESCE(pay.is_split_tender, FALSE)       AS is_split_tender,
    COALESCE(pay.refunded_payment_count, 0)    AS refunded_payment_count,
    COALESCE(pay.has_refund, FALSE)            AS has_refund,
    pay.payment_amount_total,
    pay.tip_total,
    pay.gratuity_total
FROM v_orders_classified o
LEFT JOIN v_order_payment_summary pay ON pay.order_id = o.id;

COMMENT ON VIEW v_orders_payment_classified IS
    'v_orders_classified plus safe order-level payment context. Use this when a '
    'question needs payment structure; use v_orders_classified otherwise, as it '
    'is materially faster. has_payment = FALSE is normal rather than lost '
    'revenue -- EMPTY and COMP orders legitimately have none. payment_type_codes '
    'are raw Revel integers with NO verified name mapping: say "payment type '
    'code N", never "cash" or "card".';

COMMIT;

GRANT SELECT ON v_orders_payment_classified TO laynes_ro;

-- Rollback:
--   DROP VIEW IF EXISTS v_orders_payment_classified;
