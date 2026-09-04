-- Migration 17: application user accounts for the web UI (login + chat assistant)
--
-- Introduced 2026-09-02 alongside admin_chat.py (the NL-to-SQL chat page served
-- at the site root). Auth was briefly going to live in .env as a single hard-coded
-- credential; moved here so more users can be added without a redeploy.
--
-- Roles:
--   super_admin  full access + can manage other users (add / deactivate / reset)
--   admin        full chat access, no user management
--   viewer       chat access (kept distinct so we can scope it down later)
--
-- Password hashing: bcrypt (via the `bcrypt` Python package), cost 12. The app
-- never stores or logs plaintext. password_hash is the modular-crypt string,
-- e.g. $2b$12$....  (60 chars).

-- CITEXT for case-insensitive emails (must exist before the table uses the type)
CREATE EXTENSION IF NOT EXISTS citext;

CREATE TABLE IF NOT EXISTS app_users (
    id              BIGSERIAL PRIMARY KEY,
    email           CITEXT UNIQUE NOT NULL,          -- case-insensitive login
    password_hash   TEXT NOT NULL,
    full_name       TEXT,
    role            TEXT NOT NULL DEFAULT 'viewer'
                        CHECK (role IN ('super_admin', 'admin', 'viewer')),
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by      BIGINT REFERENCES app_users(id),
    last_login_at   TIMESTAMPTZ,
    password_changed_at TIMESTAMPTZ
);

-- Optional audit of who asked the assistant what (handy for tuning the prompt
-- and for a rough usage picture). Kept deliberately small; no PII beyond user id.
CREATE TABLE IF NOT EXISTS chat_query_log (
    id              BIGSERIAL PRIMARY KEY,
    user_id         BIGINT NOT NULL REFERENCES app_users(id),
    asked_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    question        TEXT NOT NULL,
    generated_sql   TEXT,
    row_count       INTEGER,
    error           TEXT,
    duration_ms     INTEGER,
    model           TEXT
);

CREATE INDEX IF NOT EXISTS idx_chat_query_log_user_time
    ON chat_query_log (user_id, asked_at DESC);
