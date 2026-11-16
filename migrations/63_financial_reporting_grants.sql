-- 63_financial_reporting_grants.sql
-- Grant laynes_ro SELECT on the one NEW view from migration 62.
--
-- v_r365_store_weekly_llm / v_r365_store_period_llm were replaced in place by
-- 62 (columns appended); CREATE OR REPLACE VIEW keeps their existing grants, so
-- they need nothing here. Apply this AND add v_revel_sales_metrics_daily_llm
-- to chat_sql._ALLOWED_RELATIONS: one without the other is inert or an error.
-- The view reads payments_v2 with its owner's rights and exposes only per-store
-- daily SUMS: no transaction id, card, processor or operator field crosses.

BEGIN;

GRANT SELECT ON v_revel_sales_metrics_daily_llm TO laynes_ro;

COMMIT;
