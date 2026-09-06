-- Migration 35: safe loyalty facts derived from Order.gift_reward_data (A11 follow-up)
--
-- WHY THIS TABLE EXISTS AND WHAT IT DELIBERATELY OMITS ------------------------
-- Revel's Order.gift_reward_data is the ONLY place loyalty is observable for
-- this account: Customer.loyalty_ref_id and loyalty_number are 0% populated,
-- Order.loyalty_account_id is 0% (it is Paytronix-specific), and Discount.
-- loyalty_code is empty across all 3,654 discounts.
--
-- That field is a JSON blob that ALSO carries plaintext PII -- customerName,
-- firstName, lastName, phoneNumber and birthday are all populated. It cannot be
-- narrowed server-side either: `fields=` returns the whole blob because the PII
-- is INSIDE the value. So the raw payload is parsed in memory, the safe facts
-- below are extracted, and the structure is discarded before anything is
-- written or logged. The raw value is never stored, never archived, never
-- printed.
--
-- externalId is a stable per-customer token present on 100% of registered rows.
-- It is useful for linking repeat loyalty activity, but it is still an
-- identifier, so only an HMAC-SHA256 of it is stored, keyed by a server-side
-- secret that lives in .env and is not readable by the assistant. Without the
-- secret the hash cannot be reversed or correlated against any other system.
--
-- Coverage measured before building: ~3.2-3.4% of Nederland orders carry a
-- payload consistently from January 2026 to September 2026, and ~6% network
-- wide on a sampled day. This is a minority signal and must never be presented
-- as the loyalty behaviour of all guests.

BEGIN;

CREATE TABLE IF NOT EXISTS order_loyalty_v2 (
    order_id              BIGINT PRIMARY KEY,
    establishment_id      INTEGER,
    order_created_date    TIMESTAMPTZ,
    source_updated_date   TIMESTAMPTZ,
    has_loyalty_payload   BOOLEAN NOT NULL DEFAULT FALSE,
    loyalty_registered    BOOLEAN,
    has_applied_reward    BOOLEAN,
    applied_rewards_count INTEGER,
    total_points_snapshot INTEGER,
    has_reward_card       BOOLEAN,
    -- HMAC-SHA256(externalId, server secret). NOT the raw id, not reversible
    -- without the secret, and not exposed to the assistant.
    loyalty_key_hash      TEXT,
    extracted_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_ol_v2_est_date
    ON order_loyalty_v2 (establishment_id, order_created_date);
CREATE INDEX IF NOT EXISTS ix_ol_v2_updated
    ON order_loyalty_v2 (source_updated_date);

COMMENT ON TABLE order_loyalty_v2 IS
    'Safe loyalty facts extracted from Order.gift_reward_data. The raw payload '
    'is NEVER stored: it contains customerName, firstName, lastName, '
    'phoneNumber and birthday. No column here holds a name, phone, birthday or '
    'card number. loyalty_key_hash is an HMAC of externalId, not the id itself.';
COMMENT ON COLUMN order_loyalty_v2.total_points_snapshot IS
    'Loyalty point balance AT THE MOMENT OF THIS ORDER, as reported by the POS. '
    'A running balance, not points earned on this order -- do not sum it.';
COMMENT ON COLUMN order_loyalty_v2.loyalty_key_hash IS
    'HMAC-SHA256 of the payload externalId under a server-side secret. Enables '
    'repeat-activity linkage without storing or exposing the identifier.';

COMMIT;
-- Grant is deliberately NOT issued here. Expose loyalty to the assistant only
-- through an aggregate view once the shape is agreed.

-- Rollback:
--   DROP TABLE IF EXISTS order_loyalty_v2;
