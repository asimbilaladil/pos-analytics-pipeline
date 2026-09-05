-- Migration 27: safe order-level payment context (A10)
--
-- Gives the analysis layer payment structure without putting a single sensitive
-- payment field within its reach.
--
-- DELIBERATELY EXCLUDED, and the reason for the view existing at all:
--   transaction_id, refund_transaction_id  -- processor references
--   card_type                              -- card brand
--   transaction_status, processor_accepted -- processor data
--   station_id, created_by_user_id, updated_by_user_id -- operator identity
-- payments_v2 carries all of these, which is why the read-only role is not
-- granted the table and reads this view instead. There is no cardholder name,
-- last4 or receipt email column in payments_v2 at all -- verified, not assumed.
--
-- PAYMENT TYPE IS A RAW CODE ON PURPOSE.
-- No trustworthy name mapping exists. All three evidence routes were checked:
--   1. Revel reference data -- /resources/PaymentType/ returns HTTP 200 with
--      total_count = 0, and PaymentMethod / TenderType are 404.
--   2. The Payment schema endpoint declares payment_type as plain "Integer
--      data. Ex: 2673" with no choices/enum.
--   3. payments_v2.other_payment_type is NULL on all 882,141 rows.
-- Circumstantial evidence is suggestive -- code 2 always carries a card_type
-- and a Captured status, code 1 never does and accounts for all cash change
-- given -- but "suggestive" is how you end up telling a franchisee their cash
-- mix when you actually guessed. Codes are preserved verbatim and the
-- assistant is instructed to say "payment type code 2", never "card".
--
-- is_split_tender means MORE THAN ONE PAYMENT RECORD on the order. It does not
-- require the tenders to differ in type: two card swipes splitting a bill is a
-- split tender. Distinct types are available separately in payment_type_codes.

BEGIN;

CREATE OR REPLACE VIEW v_order_payment_summary AS
SELECT
    p.order_id,
    p.establishment_id,
    COUNT(*)                                          AS payment_record_count,
    TRUE                                              AS has_payment,
    -- Every distinct code on the order, never collapsed into one "primary"
    -- type -- a split across two tenders is two facts, not one.
    ARRAY_AGG(DISTINCT p.payment_type ORDER BY p.payment_type) AS payment_type_codes,
    CASE WHEN COUNT(DISTINCT p.payment_type) = 1
         THEN MIN(p.payment_type) END                 AS payment_type_single,
    (COUNT(*) > 1)                                    AS is_split_tender,
    COUNT(*) FILTER (WHERE p.refunded)                AS refunded_payment_count,
    (COUNT(*) FILTER (WHERE p.refunded) > 0)          AS has_refund,
    ROUND(SUM(p.amount), 2)                           AS payment_amount_total,
    ROUND(SUM(p.tip), 2)                              AS tip_total,
    ROUND(SUM(p.gratuity), 2)                         AS gratuity_total
FROM payments_v2 p
WHERE p.deleted IS NOT TRUE
GROUP BY p.order_id, p.establishment_id;

COMMENT ON VIEW v_order_payment_summary IS
    'One row per order that HAS at least one payment. Safe fields only: no '
    'transaction ids, card brands, processor status or operator ids. '
    'payment_type is a raw Revel integer code with no verified name mapping -- '
    'report it as "payment type code N", never as cash or card. An order absent '
    'from this view has no payment record, which is normal for EMPTY and COMP '
    'transactions.';

COMMIT;

GRANT SELECT ON v_order_payment_summary TO laynes_ro;

-- Rollback:
--   DROP VIEW IF EXISTS v_order_payment_summary;
