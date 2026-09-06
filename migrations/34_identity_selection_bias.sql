-- Migration 34: honest customer-identity context (A11)
--
-- PRIVACY FIRST, AND IT IS EASY HERE: there is NO customer or loyalty table in
-- this database at all, and the only PII column anywhere in it is
-- app_users.email, which the assistant's role has never been able to read.
-- No customer name, email, phone or address is stored, so none can leak.
-- orders_v2.customer_id is a bare integer with nothing behind it; it is still
-- hashed into safe_customer_key here so the assistant aggregates by an opaque
-- token rather than handling the raw identifier.
--
-- OPERATOR IDS ARE NOT CUSTOMERS. created_by_user_id, updated_by_user_id,
-- voided_by_user_id, discounted_by_user_id, opened_by_user_id and
-- closed_by_user_id identify STAFF. They are never used here and must never be
-- mistaken for customer identity.
--
-- LOYALTY IS NOT KNOWN. There is no loyalty, rewards or membership field
-- anywhere in the data, and no Revel loyalty resource is ingested. So an
-- identified customer is NOT known to be a loyalty member, and an anonymous
-- transaction is NOT a non-member -- it is simply unknown. Any "loyalty vs
-- non-loyalty" framing must be refused rather than approximated with identity.
--
-- THE FINDING THAT MAKES THIS WORTH DOING: identified visits are dominated by
-- accounts that cannot be individuals. Network-wide, 23 identities hold 78.5%
-- of all identified visits -- one has 58,167 visits across all 12 stores, and
-- when it stops on 2026-08-05 another starts the same day with the same
-- pattern. Their orders are 90-100% web_associated, overwhelmingly codes 100
-- and 101, i.e. SUSPECTED non-individual references rather than people. Not manually verified.
-- At Nederland in June, 2 of the 88 "customers" hold 389 of 488 identified
-- visits. Naive arithmetic therefore reports 5.55 visits per customer; with
-- those two excluded it is 1.15. Reporting the first number as customer
-- behaviour would be badly wrong, so the profile flags them.
--
-- The flag is SUSPECTED, not confirmed, and its criteria are stated so they can
-- be audited: an identity seen at 6+ establishments with 90%+ web_associated
-- orders, OR one with 365+ visits (more than daily across the whole data
-- window). Measured separation is sharp -- the largest UNflagged identity has
-- 34 visits at a single store, entirely plausible for a regular.

BEGIN;

CREATE OR REPLACE VIEW v_order_identity_context AS
SELECT
    o.id                                        AS order_id,
    o.establishment_id,
    o.business_date,
    o.txn_class,
    (o.customer_id IS NOT NULL)                 AS has_customer_identity,
    (o.customer_id IS NULL)                     AS anonymous_flag,
    -- Opaque, stable within the database, carries no personal data.
    CASE WHEN o.customer_id IS NOT NULL
         THEN md5('laynes_identity_v1:' || o.customer_id::text) END
                                                AS safe_customer_key,
    o.final_total
FROM v_orders_classified o;

COMMENT ON VIEW v_order_identity_context IS
    'Per-order identity context. safe_customer_key is an opaque hash -- there is '
    'no customer name, email or phone anywhere in this database to expose. '
    'anonymous_flag means identity was NOT captured; it does NOT mean the guest '
    'is a non-member. Loyalty membership is unknown for every transaction.';

CREATE OR REPLACE VIEW v_identity_profile AS
SELECT
    md5('laynes_identity_v1:' || o.customer_id::text)   AS safe_customer_key,
    COUNT(*)                                            AS visits_all_time,
    COUNT(DISTINCT o.establishment_id)                  AS distinct_establishments,
    -- Uses orders_v2.web_order directly rather than joining the channel view.
    -- The channel join made this view aggregate 100k identified orders across
    -- two passes of orders_v2, so a scoped query against it took ~10s and blew
    -- the statement timeout in normal use. web_order is the raw corroborating
    -- field anyway, so this is both cheaper and closer to the evidence.
    ROUND(100.0 * COUNT(*) FILTER (WHERE o.web_order) / COUNT(*), 1)
                                                        AS pct_web_associated,
    MIN(o.business_date)                                AS first_seen,
    MAX(o.business_date)                                AS last_seen,
    -- SUSPECTED marketplace/aggregator account, not a confirmed classification.
    ((COUNT(DISTINCT o.establishment_id) >= 6
      AND 100.0 * COUNT(*) FILTER (WHERE o.web_order) / COUNT(*) >= 90)
     OR COUNT(*) >= 365)                                AS suspected_non_individual
FROM v_orders_classified o
WHERE o.txn_class = 'REAL' AND o.customer_id IS NOT NULL
GROUP BY o.customer_id;

COMMENT ON VIEW v_identity_profile IS
    'Aggregate behaviour per opaque identity. suspected_non_individual marks an '
    'identity that looks like a third-party marketplace account rather than a '
    'person (6+ stores with 90%+ web orders, or 365+ visits). It is a SUSPICION '
    'with stated criteria, never a confirmed fact -- but 23 such identities hold '
    '78.5% of all identified visits, so excluding them changes visits-per-'
    'customer from 6.19 to 1.45 network-wide.';

COMMIT;

GRANT SELECT ON v_order_identity_context, v_identity_profile TO laynes_ro;

-- Rollback:
--   DROP VIEW IF EXISTS v_identity_profile, v_order_identity_context;
