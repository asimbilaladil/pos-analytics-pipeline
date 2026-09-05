-- Migration 22: lock the LLM's database role down to an explicit allowlist
--
-- CRITICAL SECURITY FIX. Before this migration laynes_ro -- the role that
-- executes model-authored SQL from the chat assistant -- held SELECT on 58
-- objects in schema public, including:
--
--     app_users      (email, role, and bcrypt password_hash for every account)
--     chat_query_log (every question every user has ever asked)
--
-- chat_sql.py's _validate() blocks DML/DDL *keywords* but has no notion of
-- which relations may be read, so `SELECT password_hash FROM app_users` passed
-- validation and executed. Any user of the chat box -- or any successful prompt
-- injection -- could read the credential table.
--
-- Two independent exposure paths are closed here:
--   1. The 58 blanket grants (from a GRANT SELECT ON ALL TABLES).
--   2. A default ACL owned by postgres ({laynes_ro=r/postgres}) that
--      automatically granted SELECT on every FUTURE table postgres creates.
--      Fixing only (1) would leave the hole to silently reopen on the next
--      table the pipeline's superuser created.
--
-- The allowlist below is exactly the set of relations documented to the model
-- in chat_sql.py SCHEMA_DOC. Anything the prompt does not describe is not
-- granted -- fail closed. Legacy non-v2 tables are deliberately excluded: the
-- prompt already states they MUST NOT be used, so this makes that enforceable
-- rather than advisory.
--
-- Defence in depth: this migration is the database half. chat_sql.py carries a
-- matching relation allowlist that rejects a bad query *before* it reaches the
-- database, so a forbidden read fails with a clear message rather than a raw
-- permission error, and never reaches the connection at all.
--
-- Login/session/chat-history reads are unaffected: admin_chat.py performs those
-- as laynes_user (_rw_conn), a different role that this migration does not touch.

BEGIN;

-- ── 1. strip everything laynes_ro currently holds ──────────────────────────
REVOKE ALL ON ALL TABLES    IN SCHEMA public FROM laynes_ro;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM laynes_ro;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM laynes_ro;

-- USAGE on the schema is retained: without it the role cannot name any object
-- at all, including the approved views. It confers no read access by itself.
GRANT USAGE ON SCHEMA public TO laynes_ro;

-- Ensure the role can never create objects of its own in the schema.
REVOKE CREATE ON SCHEMA public FROM laynes_ro;

-- ── 2. grant back ONLY the documented analytics surface ────────────────────
-- Classified analysis views (migration 21) -- the preferred entry points.
GRANT SELECT ON v_orders_classified      TO laynes_ro;
GRANT SELECT ON v_order_items_classified TO laynes_ro;
GRANT SELECT ON v_store_cohort           TO laynes_ro;

-- Pre-aggregated feature tables -- what the prompt tells the model to prefer.
GRANT SELECT ON features_daily_summary_v2 TO laynes_ro;
GRANT SELECT ON features_hourly_v2        TO laynes_ro;
GRANT SELECT ON features_product_daily_v2 TO laynes_ro;

-- Raw v2 fact tables -- documented for questions the feature tables cannot
-- answer. No credential, session or free-text-customer content.
GRANT SELECT ON orders_v2      TO laynes_ro;
GRANT SELECT ON order_items_v2 TO laynes_ro;

-- Dimensions.
GRANT SELECT ON establishments TO laynes_ro;
GRANT SELECT ON products       TO laynes_ro;
GRANT SELECT ON weather_daily  TO laynes_ro;

COMMIT;

-- ── 3. revoke the future-table default ACL ─────────────────────────────────
-- Must run as the owning role (postgres); ALTER DEFAULT PRIVILEGES only affects
-- ACLs owned by the role executing it, so laynes_user cannot clear this one.
-- Run separately as postgres:
--
--   ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
--       REVOKE SELECT ON TABLES FROM laynes_ro;
--
-- Verify afterwards with:  SELECT defaclacl FROM pg_default_acl;

-- Rollback (restores the pre-migration state -- reopens the vulnerability,
-- for emergency use only):
--   GRANT SELECT ON ALL TABLES IN SCHEMA public TO laynes_ro;
