-- Task 13 — modifier_items_v2: isolated shadow table for the historical
-- ModifierItems backfill, kept completely separate from production
-- `modifier_items`. Same rationale as payments_v2/order_history_v2 (Tasks
-- 11/12): production modifier_items already uses PRIMARY KEY(id) alone --
-- it never had the duplicate-row problem orders_v2/order_items_v2 exist to
-- fix. modifier_items_v2 exists purely for isolation during the historical
-- backfill, not to correct a schema bug.
--
-- Unlike payments_v2/order_history_v2, this backfill has NO live Revel
-- component at all -- it replays Task 10's already-archived raw OrderItem
-- responses (verified complete/uncorrupted, Task 13 Phase 1) and extracts
-- the nested modifieritems, exactly as pipeline.build_modifier_row already
-- does for the live daily pipeline. Task 13 Phase 2 found zero mutations
-- across 32 sampled OrderItems / 65 ModifierItems spanning 4 establishments
-- and ~6 months of age, so UPSERT semantics stay ON CONFLICT DO NOTHING --
-- unchanged from production's existing (unverified-until-now) assumption.
--
-- Deliberately NO FOREIGN KEY at creation time (production modifier_items
-- has FKs on establishment_id/modifier_id but none on order_item_id/
-- order_id either). Orphan checking against order_items_v2/orders_v2 is a
-- reporting step run after the pilot populates data, not a constraint
-- enforced here.

CREATE TABLE modifier_items_v2 (
    id                  BIGINT PRIMARY KEY,
    order_item_id       BIGINT NOT NULL,
    order_id            BIGINT NOT NULL,
    establishment_id    INTEGER NOT NULL,
    modifier_id         INTEGER,
    modifier_name       VARCHAR(200),
    qty                 NUMERIC DEFAULT 1,
    modifier_price      NUMERIC DEFAULT 0,
    is_discounted       BOOLEAN DEFAULT FALSE,
    created_date        DATE NOT NULL,
    ingested_at         TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX ix_miv2_order_item ON modifier_items_v2(order_item_id);
CREATE INDEX ix_miv2_order ON modifier_items_v2(order_id);
CREATE INDEX ix_miv2_establishment ON modifier_items_v2(establishment_id, created_date);
CREATE INDEX ix_miv2_modifier_id ON modifier_items_v2(modifier_id, created_date);
