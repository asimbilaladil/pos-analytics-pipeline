-- 59_r365_financial_transaction_grants.sql
-- Grant laynes_ro SELECT on the two LLM-safe R365 financial TRANSACTION views
-- ONLY (the 56/57 convention). Apply this AND add the same views to
-- chat_sql._ALLOWED_RELATIONS.
--
-- Approved lines are the default view; unapproved lines live in a separate
-- view so they can only enter an answer when explicitly asked for.
--
-- Deliberately NOT granted, now or later: r365_financial_transaction.

BEGIN;

GRANT SELECT ON v_r365_financial_transaction_llm             TO laynes_ro;
GRANT SELECT ON v_r365_financial_unapproved_transaction_llm  TO laynes_ro;

COMMIT;
