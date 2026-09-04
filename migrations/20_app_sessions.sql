-- Migration 20: persistent login sessions for Laynes Intelligence.
--
-- Streamlit's st.session_state does NOT survive a full page reload — it's
-- tied to the WebSocket connection, and a browser refresh opens a fresh one
-- with a blank session, so login was silently lost on every refresh. This
-- table backs a session token carried in the URL query string (?s=<token>)
-- that's checked on load and restores the logged-in user without a password.

CREATE TABLE IF NOT EXISTS app_sessions (
    token       TEXT PRIMARY KEY,             -- secrets.token_urlsafe(32)
    user_id     BIGINT NOT NULL REFERENCES app_users(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at  TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_app_sessions_user ON app_sessions (user_id);
CREATE INDEX IF NOT EXISTS idx_app_sessions_expires ON app_sessions (expires_at);
