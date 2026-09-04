-- Task 09 — backfill_progress: explicit checkpoint state for the historical
-- Orders backfill (orders_v2). Separate from sync_state, which tracks the
-- ongoing daily/updated-sync watermark -- a different mechanism for a
-- different purpose: sync_state answers "where does the next incremental
-- sync resume from"; backfill_progress answers "which historical
-- establishment x month chunks have been fully re-fetched and verified."
-- Neither should ever advance/complete based on the other's state.

CREATE TABLE backfill_progress (
    id                  BIGSERIAL PRIMARY KEY,
    resource            VARCHAR(50) NOT NULL,
    establishment_id    INTEGER NOT NULL,
    window_start        TIMESTAMPTZ NOT NULL,
    window_end          TIMESTAMPTZ NOT NULL,
    run_id              VARCHAR(50),
    status              VARCHAR(20) NOT NULL DEFAULT 'pending',
    pages               INTEGER,
    rows_fetched        INTEGER,
    rows_affected       INTEGER,
    started_at          TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    error               TEXT,
    UNIQUE (resource, establishment_id, window_start, window_end)
);

CREATE INDEX ix_backfill_progress_status ON backfill_progress(status);
CREATE INDEX ix_backfill_progress_est ON backfill_progress(establishment_id);
