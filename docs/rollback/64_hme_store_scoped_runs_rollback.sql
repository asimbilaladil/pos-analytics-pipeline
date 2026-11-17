-- Rollback for migration 64. NOT a migration; apply only to undo 64, as the
-- table owner (laynes_user), AFTER rolling the code back to a86366d.
--
-- It REFUSES while any store-scoped run exists: without the column such a run
-- would look network-wide, and the old view could take it as a day's verdict.
-- If scoped runs exist, leave migration 64 in place -- it is inert for code
-- that does not use it (the safe rollback is then code-only).

BEGIN;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM hme_ingest_run WHERE store_scope IS NOT NULL) THEN
        RAISE EXCEPTION 'store-scoped HME runs exist; keep migration 64 (code-only rollback)';
    END IF;
END $$;

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

ALTER TABLE hme_ingest_run DROP CONSTRAINT IF EXISTS hme_ingest_run_store_scope_ck;
ALTER TABLE hme_ingest_run DROP COLUMN IF EXISTS store_scope;

COMMIT;
