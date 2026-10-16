-- 55_r365_llm_grants.sql
-- Grant laynes_ro SELECT on the four LLM-safe R365 views ONLY.
--
-- Grants live in their own migration so the privilege surface is reviewable
-- on its own (the 43/44 and 49/50 convention). Apply this AND add the same four
-- views to chat_sql._ALLOWED_RELATIONS: granting without the allowlist is inert,
-- allowlisting without the grant produces permission errors.
--
-- Deliberately NOT granted, now or later:
--   r365_store_daily, r365_store_weekly, r365_ingest_run, r365_store_mapping.
-- The views are the only path. That is what keeps the R365 store id, the
-- source store name, run errors and archive paths out of reach, and what makes
-- a name-based cross-system join unexpressible.

BEGIN;

GRANT SELECT ON v_r365_store_daily_llm   TO laynes_ro;
GRANT SELECT ON v_r365_store_weekly_llm  TO laynes_ro;
GRANT SELECT ON v_r365_store_period_llm  TO laynes_ro;
GRANT SELECT ON v_r365_completeness_llm  TO laynes_ro;

COMMIT;
