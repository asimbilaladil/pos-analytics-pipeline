-- 44_hme_llm_grants.sql
-- ***********************************************************************
-- *** PREPARED BUT NOT APPLIED.                                       ***
-- *** Applying this file OPENS the HME boundary to the LLM read-only  ***
-- *** role. Do not run it until the enablement gate is met:           ***
-- ***   1. a clean unattended primary (05:30) + retry (10:15) cycle,  ***
-- ***   2. tests/test_hme_llm_layer.py green,                         ***
-- ***   3. the four views added to chat_sql._ALLOWED_RELATIONS.       ***
-- *** Granting without the allowlist change is inert but pointless;   ***
-- *** allowlisting without this grant produces permission errors.     ***
-- *** Apply this FIRST, then the allowlist.                           ***
-- ***********************************************************************
--
-- SELECT only, on the four LLM-facing views only. No raw hme_* relation is
-- granted, now or ever: hme_store_daily, hme_outliers_daily, hme_goal_history,
-- hme_ingest_run, hme_store_mapping, hme_goal_conflict and the internal
-- v_hme_store_daily_verified all stay denied to laynes_ro. The views are the
-- only path, which is what keeps OUT_OF_SCOPE stores and hme_store_number out
-- of reach rather than merely out of sight.

BEGIN;

GRANT SELECT ON v_hme_store_daily_llm       TO laynes_ro;
GRANT SELECT ON v_hme_outliers_daily_llm    TO laynes_ro;
GRANT SELECT ON v_hme_goal_history_llm      TO laynes_ro;
GRANT SELECT ON v_hme_day_completeness_llm  TO laynes_ro;

-- Deliberately NOT granted (listed so a future reader sees the intent, not an
-- omission):
--   hme_store_daily, hme_outliers_daily, hme_goal_history, hme_ingest_run,
--   hme_store_mapping, hme_goal_conflict, v_hme_store_daily_verified

COMMIT;
