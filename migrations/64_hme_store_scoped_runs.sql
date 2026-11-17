-- 64_hme_store_scoped_runs.sql
-- Store-scoped HME ingest runs (Mission Bend backfill, 2026-11).
--
-- HME 1158614 (LCF Mission Bend, establishment 25) was invisible to the
-- integration account from 2026-10-14 until 2026-11-16, so it has no HME rows
-- for 2026-10-14..11-15 while the other ten stores are loaded. Re-running the
-- normal backfill for those dates would re-write all ten other stores, and --
-- because v_hme_day_completeness_llm takes the LATEST run per date as the
-- day's network verdict -- a Mission-Bend-only load would become that verdict
-- and could flip a day that failed network reconciliation (2026-10-14/15) to
-- reconciled for every store.
--
--   hme_ingest_run.store_scope  NULL      = a normal, network-wide run (every
--                                           existing row; every run the daily
--                                           ingest and retry write)
--                               '{1158614}' = a run that loaded ONLY these HME
--                                           store numbers
--
-- v_hme_day_completeness_llm now reads network-wide runs only, so a
-- store-scoped run can never change a day's network status, reconciliation
-- verdict or counts. Its output for existing data is unchanged (no existing
-- row has a store_scope). Per-store freshness (v_hme_store_status_llm) already
-- reads per-store results and needs no change.
--
-- Additive: one nullable column (metadata-only), one CHECK, one view replaced
-- with identical columns. No data is changed; grants are kept by CREATE OR
-- REPLACE. Code that does not know the column keeps working.

BEGIN;

ALTER TABLE hme_ingest_run ADD COLUMN IF NOT EXISTS store_scope text[];

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'hme_ingest_run_store_scope_ck'
                      AND conrelid = 'hme_ingest_run'::regclass) THEN
        ALTER TABLE hme_ingest_run ADD CONSTRAINT hme_ingest_run_store_scope_ck
            CHECK (store_scope IS NULL OR cardinality(store_scope) >= 1);
    END IF;
END $$;

COMMENT ON COLUMN hme_ingest_run.store_scope IS
  'NULL = network-wide run. Otherwise the HME store numbers this run loaded '
  '(store-scoped backfill); such a run never sets the day''s network verdict.';

CREATE OR REPLACE VIEW v_hme_day_completeness_llm AS
 WITH current_run AS (
         SELECT DISTINCT ON (hme_ingest_run.business_date) hme_ingest_run.business_date,
            hme_ingest_run.status,
            hme_ingest_run.reconciliation_ok,
            hme_ingest_run.store_count,
            hme_ingest_run.verified_store_count,
            hme_ingest_run.out_of_scope_store_count,
            hme_ingest_run.freshness_detail
           FROM hme_ingest_run
          WHERE hme_ingest_run.status = ANY (ARRAY['succeeded'::text, 'partial'::text])
            AND hme_ingest_run.store_scope IS NULL
          ORDER BY hme_ingest_run.business_date, hme_ingest_run.run_id DESC
        ), expectation AS (
         SELECT count(*) AS expected_total,
            count(*) FILTER (WHERE hme_store_mapping.mapping_status = 'VERIFIED'::text) AS expected_verified
           FROM hme_store_mapping
          WHERE hme_store_mapping.active
        )
 SELECT r.business_date,
    r.status,
    COALESCE(((r.freshness_detail -> 'completeness'::text) ->> 'complete'::text)::boolean, r.store_count = e.expected_total) AS source_complete,
    r.reconciliation_ok,
    r.store_count AS observed_store_count,
    e.expected_total AS expected_store_count,
    r.verified_store_count AS verified_observed_count,
    e.expected_verified AS verified_expected_count,
    r.store_count = (r.verified_store_count + r.out_of_scope_store_count) AS counts_consistent
   FROM current_run r
     CROSS JOIN expectation e;

COMMIT;
