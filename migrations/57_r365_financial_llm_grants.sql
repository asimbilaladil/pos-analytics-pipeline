-- 57_r365_financial_llm_grants.sql
-- Grant laynes_ro SELECT on the three LLM-safe R365 FINANCIAL views ONLY.
--
-- Grants live in their own migration so the privilege surface is reviewable
-- on its own (the 43/44, 49/50 and 54/55 convention). Apply this AND add the
-- same three views to chat_sql._ALLOWED_RELATIONS: granting without the
-- allowlist is inert, allowlisting without the grant produces permission errors.
--
-- Deliberately NOT granted, now or later:
--   r365_financial_ingest_run, r365_financial_account,
--   r365_financial_period_expense, r365_financial_candidate_expense,
--   r365_financial_account_period.
-- The views are the only path. That is what keeps R365 location ids, source
-- location names, unverified/unmapped amounts, run errors and archive paths
-- out of reach.

BEGIN;

GRANT SELECT ON v_r365_financial_period_llm    TO laynes_ro;
GRANT SELECT ON v_r365_financial_coverage_llm  TO laynes_ro;
GRANT SELECT ON v_r365_financial_account_llm   TO laynes_ro;

COMMIT;
