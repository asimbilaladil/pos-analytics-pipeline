-- Task 12 — order_history_v2: isolated shadow table for the historical
-- OrderHistory backfill, kept completely separate from production
-- `order_history` (which the ongoing updated-mode sync writes to
-- concurrently). Same rationale as payments_v2 (Task 11): production
-- order_history already uses PRIMARY KEY(id) alone with no created_date in
-- the identity key -- it never had the duplicate-row problem orders_v2/
-- order_items_v2 exist to fix. order_history_v2 exists purely for isolation
-- during the historical backfill, not to correct a schema bug.
--
-- Starts EMPTY -- no population from production `order_history`. Task 12's
-- backfill_order_history_v2.py populates it entirely from live Revel
-- fetches, using order ids sourced from orders_v2 (order__in= batching,
-- same strategy as order_items_v2/Task 10, since OrderHistory has no usable
-- date-range filter of its own).
--
-- Deliberately NO FOREIGN KEY on order_id -> orders_v2(id) at creation time
-- (production order_history has no FK on order_id either, and Task 11 showed
-- orphans are expected/explainable at this point in the project, not schema
-- violations). Orphan checking is a reporting step run after the pilot
-- populates data, not a constraint enforced here.

CREATE TABLE order_history_v2 (
    id                      BIGINT PRIMARY KEY,
    uuid                    UUID,
    order_id                BIGINT NOT NULL,
    opened_at               TIMESTAMPTZ,
    closed_at               TIMESTAMPTZ,
    opened_by_user_id       INTEGER,
    closed_by_user_id       INTEGER,
    opened_at_station_id    INTEGER,
    closed_at_station_id    INTEGER,
    ingested_at             TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX ix_ohv2_order ON order_history_v2(order_id);
