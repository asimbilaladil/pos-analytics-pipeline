-- Migration 42: document the OBSERVED-vs-EXPECTED semantics of run counts
--
-- hme_ingest_run carried three store counts with silently mixed meanings:
-- store_count was OBSERVED (what the extract returned) while
-- verified_store_count and out_of_scope_store_count were counted from the
-- mapper inventory, i.e. EXPECTED. That was invisible for as long as every day
-- was complete, because observed == expected. The first incomplete day
-- (2026-09-18, 33 of 34 stores, Frisco absent) surfaced it as an arithmetic
-- impossibility in the run summary:
--
--     store_count 33 | verified 11 | out_of_scope 23        <- 11 + 23 != 33
--
-- while the facts themselves were correct (11 VERIFIED + 22 OUT_OF_SCOPE = 33).
--
-- All three columns are now OBSERVED and satisfy:
--     store_count = verified_store_count + out_of_scope_store_count
--                   (+ any store whose mapping_status is neither, which is
--                    warned about rather than silently absorbed)
--
-- Expected counts are not duplicated into columns; they live in
-- freshness_detail->'completeness' alongside the missing-store evidence:
--     expected_store_count, verified_expected_count, out_of_scope_expected_count
--     observed_store_count, verified_observed_count, out_of_scope_observed_count
--
-- No data is rewritten. Rows written before hme_load/1.4.0 keep whatever they
-- recorded; re-running a day's load writes a fresh row under the corrected
-- semantics, which is the normal idempotent path.

BEGIN;

COMMENT ON COLUMN hme_ingest_run.store_count IS
  'OBSERVED: distinct HME stores present in this extract. Expected inventory size '
  'is freshness_detail->''completeness''->''expected_store_count''.';
COMMENT ON COLUMN hme_ingest_run.verified_store_count IS
  'OBSERVED: stores in this extract whose mapping_status is VERIFIED. Expected is '
  'freshness_detail->''completeness''->''verified_expected_count''.';
COMMENT ON COLUMN hme_ingest_run.out_of_scope_store_count IS
  'OBSERVED: stores in this extract whose mapping_status is OUT_OF_SCOPE. Expected is '
  'freshness_detail->''completeness''->''out_of_scope_expected_count''.';
COMMENT ON COLUMN hme_ingest_run.reconciliation_ok IS
  'Whether the cross-source checks agreed ACROSS THE OBSERVED POPULATION. This is '
  'independent of completeness: a day can reconcile perfectly and still be missing '
  'stores. Never read reconciliation_ok=true as "the day is complete" -- check '
  'freshness_detail->''completeness''->''complete'', or require status=''succeeded'', '
  'which demands both.';

COMMIT;
