-- Task 04 — Add Missing Fields to Existing Tables
-- Source: revel_internal_data_backfill_plan.md, section "TASK 04"
-- All columns nullable, no defaults, no backfill. Monetary fields use bare
-- NUMERIC (not NUMERIC(10,2)) per plan rule 4.2 to preserve raw precision.
-- orders/order_items are partitioned by created_date (RANGE); ADD COLUMN on
-- the parent cascades to all existing partitions automatically in Postgres
-- declarative partitioning.

-- ── 4.1 Orders ───────────────────────────────────────────────────────────
ALTER TABLE orders
    ADD COLUMN created_by_user_id     INTEGER,
    ADD COLUMN updated_by_user_id     INTEGER,
    ADD COLUMN discount_amount        NUMERIC,
    ADD COLUMN discount_reason        TEXT,
    ADD COLUMN discounted_by_user_id  INTEGER,
    ADD COLUMN exchanged              BOOLEAN,
    ADD COLUMN service_charge         NUMERIC,
    ADD COLUMN surcharge              NUMERIC,
    ADD COLUMN remaining_due          NUMERIC,
    ADD COLUMN notes                  TEXT;


-- ── 4.2 Order Items ──────────────────────────────────────────────────────
ALTER TABLE order_items
    ADD COLUMN ervc_type              SMALLINT,
    ADD COLUMN item_type              SMALLINT,
    ADD COLUMN initial_price          NUMERIC,
    ADD COLUMN discount_amount        NUMERIC,
    ADD COLUMN discount_reason        TEXT,
    ADD COLUMN discounted_by_user_id  INTEGER,
    ADD COLUMN cost                   NUMERIC,
    ADD COLUMN exchanged              BOOLEAN,
    ADD COLUMN void_ref_uuid          UUID;


-- ── 4.3 Products ─────────────────────────────────────────────────────────
ALTER TABLE products
    ADD COLUMN category_id INTEGER REFERENCES product_categories(id);
