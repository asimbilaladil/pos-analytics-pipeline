-- Task 07 — durable, explicit per-establishment/per-resource sync watermark
--
-- One row per (resource, establishment_id). establishment_id is NULL for
-- establishment-independent resources (products, product_categories).
--
-- window_end (not last_success_at, not wall-clock "now") is what the next
-- run's start is derived from: next_start = window_end - 48h. Using the
-- window's own end, not completion time, means overlap coverage stays
-- continuous even if a run takes a while or the service was down for days.
--
-- A resource/establishment's watermark (window_start/window_end/
-- last_success_at) advances ONLY after: all pages for that resource/window
-- were fetched, every page was successfully archived, parsing succeeded,
-- and the DB write committed. See sync_updated.py record_sync_success() /
-- record_sync_failure() -- failure writes status='failed' and last_run_id
-- for visibility but never touches window_start/window_end/last_success_at,
-- so a failed resource/store can never advance past where it last actually
-- succeeded, and next run's window naturally covers the missed period.

DROP TABLE IF EXISTS sync_state;

CREATE TABLE sync_state (
    id                  BIGSERIAL PRIMARY KEY,
    resource            VARCHAR(50) NOT NULL,
    establishment_id    INTEGER,
    last_success_at     TIMESTAMPTZ,   -- wall-clock time the last successful sync completed
    window_start        TIMESTAMPTZ,   -- start of the query window used by the last successful sync
    window_end          TIMESTAMPTZ,   -- end of that window -- next run's start = this - 48h
    last_run_id         VARCHAR(50),
    status              VARCHAR(20),   -- 'success' or 'failed' (never 'running' -- see sync_updated.py)
    updated_at          TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE NULLS NOT DISTINCT (resource, establishment_id)
);

CREATE INDEX ix_sync_state_resource ON sync_state(resource);
CREATE INDEX ix_sync_state_status ON sync_state(status) WHERE status = 'failed';
