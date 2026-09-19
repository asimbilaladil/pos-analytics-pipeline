-- Migration 41: goal-history load-order semantics + richer ingest provenance
--
-- Problem this fixes
-- ------------------
-- hme_goal_history.effective_from_date recorded the date that happened to be
-- loaded FIRST, not the earliest date on which the values were actually
-- observed. The 7-day validation loaded 2026-09-14 first (the proof day), so
-- every open row was anchored there even though identical goals were
-- independently observed on 2026-09-08..14. The table understated the known
-- stable period by six days.
--
-- The loader now handles four cases explicitly (see hme_load.apply_goal_observation):
--   A older observation, identical values  -> extend effective_from_date backward
--   B older observation, DIFFERENT values  -> never rewrite; log a conflict for review
--   C newer observation, identical values  -> no new row
--   D newer observation, changed values    -> close the open period, open a new one
-- Values are never fabricated for dates before an actual observation.
--
-- Case B needs somewhere to go, hence hme_goal_conflict.

BEGIN;

CREATE TABLE IF NOT EXISTS hme_goal_conflict (
    conflict_id       bigserial   PRIMARY KEY,
    hme_store_number  text        NOT NULL REFERENCES hme_store_mapping(hme_store_number),
    observed_date     date        NOT NULL,
    -- what the older/overlapping observation said
    observed_goal_a   integer NULL,
    observed_goal_b   integer NULL,
    observed_goal_c   integer NULL,
    observed_goal_d   integer NULL,
    -- what history currently records for the period covering observed_date
    existing_from     date    NULL,
    existing_to       date    NULL,
    existing_goal_a   integer NULL,
    existing_goal_b   integer NULL,
    existing_goal_c   integer NULL,
    existing_goal_d   integer NULL,
    reason            text    NOT NULL,
    run_id            bigint  NULL REFERENCES hme_ingest_run(run_id),
    detected_at       timestamptz NOT NULL DEFAULT now(),
    resolved_at       timestamptz NULL,
    resolved_by       text    NULL,
    resolution_note   text    NULL,
    -- One open conflict per (store, date); re-runs must not pile up duplicates.
    CONSTRAINT hme_goal_conflict_unique UNIQUE (hme_store_number, observed_date, reason)
);

COMMENT ON TABLE hme_goal_conflict IS
  'Case B of the goal-history rules: an observation disagrees with the period history '
  'already records. History is NEVER rewritten automatically; a human resolves these.';

-- Richer provenance for the automated daily job.
ALTER TABLE hme_ingest_run
    ADD COLUMN IF NOT EXISTS verified_store_count     integer NULL,
    ADD COLUMN IF NOT EXISTS out_of_scope_store_count integer NULL,
    ADD COLUMN IF NOT EXISTS extractor_version        text    NULL,
    ADD COLUMN IF NOT EXISTS loader_version           text    NULL,
    ADD COLUMN IF NOT EXISTS attempt                  integer NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS freshness_ok             boolean NULL,
    ADD COLUMN IF NOT EXISTS freshness_detail         jsonb   NULL;

-- 'partial' joins the existing status vocabulary: the source returned something
-- but not enough to call the day complete, which must never read as success.
ALTER TABLE hme_ingest_run DROP CONSTRAINT IF EXISTS hme_ingest_run_status_check;
ALTER TABLE hme_ingest_run ADD CONSTRAINT hme_ingest_run_status_check CHECK (
    status IN ('running','succeeded','failed','aborted_tenant_scope','partial'));

COMMENT ON COLUMN hme_ingest_run.freshness_ok IS
  'Whether the source looked complete for the requested day (date literal present, '
  'full tenant inventory returned, non-empty results, reconciliation completed).';
COMMENT ON COLUMN hme_ingest_run.attempt IS
  'Attempt number within one scheduled execution (bounded backoff retries).';

COMMIT;
