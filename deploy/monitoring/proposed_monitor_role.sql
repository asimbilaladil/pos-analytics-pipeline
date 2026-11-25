-- PROPOSED, NOT APPLIED. Needs explicit approval before running in production.
--
-- A dedicated SELECT-only role for the data monitor, so it does not need the
-- read-write application credentials. It can read status/run tables and the
-- analytics views the checks use -- nothing else, and nothing writable.
-- The monitor additionally forces default_transaction_read_only=on and checks it.
--
-- Every relation below is one a check (or chat_sql.revel_store_breakdown) reads;
-- tests/test_monitoring_isolated_db.py applies THIS grant list verbatim and runs
-- the whole data tier on it, so a missing grant fails the test.
--
-- Apply (approval required):   psql -v ON_ERROR_STOP=1 -v pw="'<generated>'" -f proposed_monitor_role.sql
-- Roll back:                    DROP OWNED BY laynes_monitor; DROP ROLE laynes_monitor;

BEGIN;
CREATE ROLE laynes_monitor LOGIN PASSWORD :pw
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
    CONNECTION LIMIT 4;
ALTER ROLE laynes_monitor SET default_transaction_read_only = on;
ALTER ROLE laynes_monitor SET statement_timeout = '30s';
ALTER ROLE laynes_monitor SET lock_timeout = '2s';
ALTER ROLE laynes_monitor SET idle_in_transaction_session_timeout = '60s';

GRANT CONNECT ON DATABASE laynes TO laynes_monitor;
GRANT USAGE ON SCHEMA public TO laynes_monitor;
GRANT SELECT ON
    -- reference
    establishments, ayg_business_periods,
    -- Revel: sync status and the open-ticket explanation
    sync_state, v_orders_classified, v_order_payment_summary,
    -- Revel: what chat_sql.revel_store_breakdown reads (_STORE_BREAKDOWN_SQL,
    -- _STORE_LINKED_PAYMENTS_SQL). The monitor runs that function on THIS role
    -- (checks/revel.py monitor_connection). The views run with their owner's
    -- rights (no security_invoker; verified 2026-11-25), so SELECT on the view
    -- is sufficient and no base-table grant is needed for them.
    orders_v2, v_payments_daily_v2, v_labor_daily_context,
    -- HME
    hme_ingest_run, hme_store_ingest_result, hme_store_mapping,
    v_hme_store_day_trust_llm,
    -- R365
    r365_ingest_run, r365_store_daily, r365_store_mapping, v_r365_completeness_llm,
    r365_financial_ingest_run
TO laynes_monitor;
-- AI Chat: COLUMN-level grants only. The monitor can read job status and error
-- classes, never the question/answer text or conversations.
GRANT SELECT (id, status, error_code, created_at, started_at, completed_at) ON chat_jobs
    TO laynes_monitor;
GRANT SELECT (id, asked_at, error, steps) ON chat_query_log TO laynes_monitor;
COMMIT;
