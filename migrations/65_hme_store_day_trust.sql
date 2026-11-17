-- 65_hme_store_day_trust.sql
-- Per-store-day HME trust grade for the LLM role (AI Chat and MCP).
--
-- Until now the chat/MCP HME gate judged reconciliation only per NETWORK DAY
-- (v_hme_day_completeness_llm.reconciliation_ok). A day can reconcile for the
-- network while one store's own checks fail: LCF Mission Bend (establishment
-- 25) on 2026-11-03 and 2026-11-06 is graded PARTIAL / RECONCILIATION_MISMATCH
-- in hme_store_ingest_result, yet both chat and MCP served it as trusted
-- because the per-store grade was not visible to laynes_ro.
--
-- This view grades every canonical store-day row by the ingestion result OF
-- THE RUN THAT WROTE THAT ROW (hme_store_daily.run_id), so the grade always
-- describes the figures actually served:
--
--   grade_source = 'STORE_RESULT'  the run recorded a per-store result
--                                  (migration 61 onward): VERIFIED only when
--                                  that result is SUCCESS.
--   grade_source = 'NETWORK_RUN'   a row written before per-store results
--                                  existed: those runs failed the WHOLE day on
--                                  any per-store check, so the row is VERIFIED
--                                  only when its network-wide run succeeded or
--                                  was partial WITH reconciliation_ok = true.
--   grade_source = 'NONE'          no run on record: never VERIFIED.
--
-- trust_status is 'VERIFIED' or 'UNVERIFIED'; there is no third value, so a
-- consumer that only knows "VERIFIED" fails closed on anything new.
--
-- Network-wide verdicts are unchanged: v_hme_day_completeness_llm is not
-- touched, and the network gate keeps applying on top of this one.
-- Additive only: one new view and one grant. No table, row or existing view
-- changes. No HME store number, run id or error text is exposed.

BEGIN;

CREATE OR REPLACE VIEW v_hme_store_day_trust_llm AS
SELECT m.revel_establishment_id                         AS establishment_id,
       m.revel_store_name                               AS store_name,
       d.business_date,
       CASE
         WHEN x.status IS NOT NULL THEN
           CASE WHEN x.status = 'SUCCESS' THEN 'VERIFIED' ELSE 'UNVERIFIED' END
         WHEN r.run_id IS NOT NULL
              AND r.status IN ('succeeded', 'partial')
              AND r.reconciliation_ok IS TRUE THEN 'VERIFIED'
         ELSE 'UNVERIFIED'
       END                                              AS trust_status,
       CASE WHEN x.status IS NOT NULL THEN 'STORE_RESULT'
            WHEN r.run_id IS NOT NULL THEN 'NETWORK_RUN'
            ELSE 'NONE' END                             AS grade_source,
       x.status                                         AS store_result_status,
       x.reason_code                                    AS store_result_reason,
       COALESCE(x.detail ->> 'store_check_failed' = 'true', false)
                                                        AS store_check_failed,
       CASE WHEN jsonb_typeof(x.detail -> 'day_checks_failed') = 'array'
            THEN jsonb_array_length(x.detail -> 'day_checks_failed') > 0
            ELSE false END                              AS day_check_failed,
       (r.store_scope IS NOT NULL)                      AS loaded_by_store_scoped_run
  FROM hme_store_daily d
  JOIN hme_store_mapping m ON m.hme_store_number = d.hme_store_number
  LEFT JOIN hme_ingest_run r ON r.run_id = d.run_id
  LEFT JOIN hme_store_ingest_result x
         ON x.run_id = d.run_id
        AND x.revel_establishment_id = m.revel_establishment_id
 WHERE m.mapping_status = 'VERIFIED' AND m.active;

COMMENT ON VIEW v_hme_store_day_trust_llm IS
  'Per-store-day HME trust grade (VERIFIED / UNVERIFIED) of the row actually loaded, from '
  'the per-store ingestion result of the run that wrote it (or, for rows that predate '
  'per-store results, that network run''s reconciliation). One row per VERIFIED-mapped '
  'establishment_id x business_date that has HME data. No HME identifiers.';

ALTER VIEW v_hme_store_day_trust_llm OWNER TO laynes_user;
GRANT SELECT ON v_hme_store_day_trust_llm TO laynes_ro;

COMMIT;
