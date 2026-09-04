-- Task 11 — payments_v2: isolated shadow table for the historical Payments
-- backfill, kept completely separate from production `payments` (which the
-- ongoing updated-mode sync writes to concurrently). Unlike orders_v2/
-- order_items_v2 (Task 07.3), payments already uses PRIMARY KEY(id) alone
-- with no created_date in the identity key -- it never had the duplicate-row
-- problem those tables were built to fix. payments_v2 exists purely for
-- isolation during the historical backfill, not to correct an identity bug.
--
-- Starts EMPTY -- no DISTINCT ON dedup population from production `payments`
-- (unlike migration_v2.py's orders_v2/order_items_v2 bootstrap). Task 11's
-- backfill_payments_v2.py populates it entirely from live Revel fetches.
--
-- Deliberately NO FOREIGN KEY on order_id -> orders_v2(id) at creation time
-- (production `payments` itself has no FK on order_id either). Orphan
-- checking against orders_v2 is a reporting step run after the pilot
-- populates data, not a schema constraint enforced here.

CREATE TABLE payments_v2 (
    id                      BIGINT PRIMARY KEY,
    uuid                    UUID,
    order_id                BIGINT NOT NULL,
    establishment_id        INTEGER REFERENCES establishments(id),
    payment_type            SMALLINT,
    other_payment_type      VARCHAR(255),
    amount                  NUMERIC,
    amount_authorized       NUMERIC,
    tip                     NUMERIC,
    gratuity                NUMERIC,
    change                  NUMERIC,
    refunded                BOOLEAN,
    refund_transaction_id   VARCHAR(255),
    transaction_id          VARCHAR(255),
    transaction_status      VARCHAR(100),
    transaction_captured    BOOLEAN,
    processor_accepted      BOOLEAN,
    card_type               SMALLINT,
    online                  BOOLEAN,
    source_type             SMALLINT,
    executed                BOOLEAN,
    deleted                 BOOLEAN,
    exchanged               BOOLEAN,
    station_id              INTEGER,
    payment_date            TIMESTAMPTZ,
    created_date            TIMESTAMPTZ,
    updated_date            TIMESTAMPTZ,
    created_by_user_id      INTEGER,
    updated_by_user_id      INTEGER,
    ingested_at             TIMESTAMPTZ DEFAULT NOW(),
    ingestion_date          DATE
);

CREATE INDEX ix_pv2_order ON payments_v2(order_id);
CREATE INDEX ix_pv2_est_date ON payments_v2(establishment_id, payment_date);
CREATE INDEX ix_pv2_created ON payments_v2(created_date);
CREATE INDEX ix_pv2_updated ON payments_v2(updated_date);
CREATE INDEX ix_pv2_refunded ON payments_v2(refunded) WHERE refunded = true;
