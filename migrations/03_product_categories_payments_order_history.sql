-- Task 03 — Create New Database Tables
-- Source: revel_internal_data_backfill_plan.md, section "TASK 03"
-- Schema-only migration. No backfill, no data changes to existing tables.

-- ── 3.1 Product Categories ──────────────────────────────────────────────
CREATE TABLE product_categories (
    id              INTEGER PRIMARY KEY,
    name            VARCHAR(255),
    parent_id       INTEGER REFERENCES product_categories(id),
    active          BOOLEAN,
    sorting         INTEGER,
    description     TEXT,
    created_date    TIMESTAMPTZ,
    updated_date    TIMESTAMPTZ,
    ingested_at     TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX ix_product_categories_parent
    ON product_categories(parent_id);

CREATE INDEX ix_product_categories_active
    ON product_categories(active);


-- ── 3.2 Payments ─────────────────────────────────────────────────────────
CREATE TABLE payments (
    id                     BIGINT PRIMARY KEY,
    uuid                   UUID,

    order_id               BIGINT NOT NULL,
    establishment_id       INTEGER REFERENCES establishments(id),

    payment_type           SMALLINT,
    other_payment_type     VARCHAR(255),

    amount                 NUMERIC,
    amount_authorized      NUMERIC,
    tip                    NUMERIC,
    gratuity               NUMERIC,
    change                 NUMERIC,

    refunded               BOOLEAN,
    refund_transaction_id  VARCHAR(255),

    transaction_id         VARCHAR(255),
    transaction_status     VARCHAR(100),
    transaction_captured   BOOLEAN,
    processor_accepted     BOOLEAN,

    card_type              SMALLINT,
    online                 BOOLEAN,
    source_type            SMALLINT,

    executed               BOOLEAN,
    deleted                BOOLEAN,
    exchanged              BOOLEAN,

    station_id             INTEGER,

    payment_date           TIMESTAMPTZ,
    created_date           TIMESTAMPTZ,
    updated_date            TIMESTAMPTZ,

    created_by_user_id     INTEGER,
    updated_by_user_id     INTEGER,

    ingested_at            TIMESTAMPTZ DEFAULT NOW(),
    ingestion_date          DATE
);

CREATE INDEX ix_payments_order
    ON payments(order_id);

CREATE INDEX ix_payments_est_date
    ON payments(establishment_id, payment_date);

CREATE INDEX ix_payments_updated
    ON payments(updated_date);

CREATE INDEX ix_payments_refunded
    ON payments(refunded)
    WHERE refunded = TRUE;


-- ── 3.3 Order History ────────────────────────────────────────────────────
CREATE TABLE order_history (
    id                     BIGINT PRIMARY KEY,
    uuid                   UUID,

    order_id               BIGINT NOT NULL,

    opened_at              TIMESTAMPTZ,
    closed_at              TIMESTAMPTZ,

    opened_by_user_id      INTEGER,
    closed_by_user_id      INTEGER,

    opened_at_station_id   INTEGER,
    closed_at_station_id   INTEGER,

    ingested_at            TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX ix_order_history_order
    ON order_history(order_id);
