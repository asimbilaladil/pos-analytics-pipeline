-- Task 16 prerequisite — race-safe dirty-date queue for v2 feature
-- recomputation. Correction to the original Task 15 remediation design: a
-- dirty event arriving while (establishment_id, business_date) is already
-- status='processing' must NEVER be silently dropped. dirty_at is bumped on
-- every dirty event unconditionally (even mid-processing); the worker
-- compares dirty_at to processing_started_at at completion time to decide
-- done vs. re-pending. See reprocess_dirty_features_v2.py.
--
-- Written by sync_orders/sync_order_items_and_modifiers (target="shadow_v2"
-- only -- see the enqueue_dirty_date() call sites) whenever an Order/
-- OrderItem UPSERT actually changes/inserts a row. Drained by
-- reprocess_dirty_features_v2.py. Never read by any live dashboard --
-- purely internal recompute-scheduling state.

CREATE TABLE feature_recompute_queue (
    id                     BIGSERIAL PRIMARY KEY,
    establishment_id       INTEGER NOT NULL REFERENCES establishments(id),
    business_date          DATE NOT NULL,
    dirty_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processing_started_at  TIMESTAMPTZ,
    status                 VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending | processing | done | failed
    attempts               INTEGER NOT NULL DEFAULT 0,
    last_error             TEXT,
    processed_at           TIMESTAMPTZ,
    source_run_id          VARCHAR(100),
    UNIQUE (establishment_id, business_date)
);

CREATE INDEX ix_feature_recompute_queue_status ON feature_recompute_queue(status);
CREATE INDEX ix_feature_recompute_queue_dirty_at ON feature_recompute_queue(dirty_at);
