-- 61_hme_store_ingest_result.sql
-- Per-store HME ingestion results and per-store freshness.
--
-- Until now one absent VERIFIED store (Mission Bend, HME 1158614, no longer
-- visible to the HME account since late Oct 2026) refused the WHOLE day, so
-- nothing was ingested for any store after 2026-10-13. Ingestion now grades
-- each store on its own and records the outcome here, one row per store per
-- run, so per-store freshness is explicit instead of inferred from a day count.
--
--   status       SUCCESS  all four fact sets present, per-store checks pass
--                PARTIAL  present, but a fact set is missing or a per-store
--                         reconciliation check disagrees
--                SKIPPED  not ingestible by design (no VERIFIED HME mapping)
--                FAILED   VERIFIED store returned no rows for the date
--   reason_code  OK | FACT_SET_MISSING | RECONCILIATION_MISMATCH |
--                UNMAPPED | NO_ACCESS | NO_DATA | INGEST_ERROR
--
-- NO_ACCESS = the store's identity appeared in no captured response at all
-- (not even a store list): the account cannot see it. NO_DATA = visible to
-- the account, but no fact rows for the date. Absence is never written as
-- zero: no hme_store_daily row is synthesised for a FAILED store.
--
-- Additive only: no existing table, view or grant changes.

BEGIN;

CREATE TABLE IF NOT EXISTS hme_store_ingest_result (
    run_id                 bigint      NOT NULL REFERENCES hme_ingest_run(run_id),
    business_date          date        NOT NULL,
    revel_establishment_id bigint      REFERENCES establishments(id),
    hme_store_number       text,
    status                 text        NOT NULL,
    reason_code            text        NOT NULL,
    detail                 jsonb,
    recorded_at            timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT hme_store_ingest_result_status_ck
        CHECK (status IN ('SUCCESS', 'PARTIAL', 'SKIPPED', 'FAILED')),
    CONSTRAINT hme_store_ingest_result_reason_ck
        CHECK (reason_code IN ('OK', 'FACT_SET_MISSING', 'RECONCILIATION_MISMATCH',
                               'UNMAPPED', 'NO_ACCESS', 'NO_DATA', 'INGEST_ERROR')),
    CONSTRAINT hme_store_ingest_result_identity_ck
        CHECK (revel_establishment_id IS NOT NULL OR hme_store_number IS NOT NULL)
);

-- One result per store per run (an UNMAPPED store has no HME number).
CREATE UNIQUE INDEX IF NOT EXISTS hme_store_ingest_result_run_store_uk
    ON hme_store_ingest_result (run_id, COALESCE(revel_establishment_id, -1),
                                COALESCE(hme_store_number, ''));
CREATE INDEX IF NOT EXISTS hme_store_ingest_result_est_date_idx
    ON hme_store_ingest_result (revel_establishment_id, business_date DESC, run_id DESC);

-- Per-store HME status for every Laynes establishment, LLM-safe: no HME store
-- number, no run errors. latest_business_date is the latest date with a
-- canonical row; the latest_result_* columns are the most recent graded
-- attempt for that store (any date), which is what explains a stale store.
CREATE OR REPLACE VIEW v_hme_store_status_llm AS
WITH latest_row AS (
    SELECT m.revel_establishment_id AS establishment_id, max(d.business_date) AS latest_business_date
      FROM hme_store_daily d
      JOIN hme_store_mapping m ON m.hme_store_number = d.hme_store_number
     WHERE m.mapping_status = 'VERIFIED' AND m.active
     GROUP BY 1
), latest_result AS (
    SELECT DISTINCT ON (r.revel_establishment_id)
           r.revel_establishment_id AS establishment_id, r.business_date,
           r.status, r.reason_code, r.recorded_at
      FROM hme_store_ingest_result r
     WHERE r.revel_establishment_id IS NOT NULL
     ORDER BY r.revel_establishment_id, r.business_date DESC, r.run_id DESC
), latest_success AS (
    SELECT r.revel_establishment_id AS establishment_id, max(r.business_date) AS d
      FROM hme_store_ingest_result r
     WHERE r.status IN ('SUCCESS', 'PARTIAL')
     GROUP BY 1
)
SELECT e.id   AS establishment_id,
       e.name AS store_name,
       CASE WHEN m.revel_establishment_id IS NULL THEN 'UNMAPPED' ELSE 'VERIFIED' END
              AS hme_mapping_status,
       lr.latest_business_date,
       lres.business_date AS latest_result_date,
       lres.status        AS latest_result_status,
       lres.reason_code   AS latest_result_reason,
       ls.d               AS latest_graded_success_date
  FROM establishments e
  LEFT JOIN hme_store_mapping m
         ON m.revel_establishment_id = e.id AND m.mapping_status = 'VERIFIED' AND m.active
  LEFT JOIN latest_row lr     ON lr.establishment_id = e.id
  LEFT JOIN latest_result lres ON lres.establishment_id = e.id
  LEFT JOIN latest_success ls ON ls.establishment_id = e.id;

COMMENT ON VIEW v_hme_store_status_llm IS
  'Per-store HME freshness: mapping status, latest loaded date, and the latest graded ingestion result (SUCCESS/PARTIAL/SKIPPED/FAILED + reason). No HME identifiers.';

-- The grant follows the 43/44 convention but is small enough to keep here:
-- one view, status columns only, no figures.
GRANT SELECT ON v_hme_store_status_llm TO laynes_ro;

COMMIT;
