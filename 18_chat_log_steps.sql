-- Migration 18: keep the full per-question query trace on chat_query_log.
--
-- 2026-09-02. The chat UI no longer shows a "Query details" expander to end
-- users; instead the whole trace (every SQL statement the assistant ran for a
-- question, with row counts / errors) is kept here for the super admin's
-- Query log view. `generated_sql` / `row_count` still hold the LAST step for
-- quick scanning; `steps` holds the ordered list.

ALTER TABLE chat_query_log ADD COLUMN IF NOT EXISTS steps JSONB;
