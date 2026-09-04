-- Migration 18: persisted chat conversations for Laynes Intelligence (admin_chat.py)
--
-- Until now, chat history lived only in Streamlit's st.session_state — gone the
-- moment a tab closed or the app restarted. This adds real persistence: a
-- sidebar list of past conversations per user, reopenable at any time.
--
-- chat_query_log (migration 17) is unchanged and still logs every individual
-- Q&A for audit/tuning; conversation_id here is an optional link from a log
-- row back to the conversation it belonged to, so the two can be cross-referenced
-- without either depending on the other.

CREATE TABLE IF NOT EXISTS chat_conversations (
    id              BIGSERIAL PRIMARY KEY,
    user_id         BIGINT NOT NULL REFERENCES app_users(id),
    title           TEXT NOT NULL DEFAULT 'New chat',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_chat_conversations_user_updated
    ON chat_conversations (user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS chat_messages (
    id              BIGSERIAL PRIMARY KEY,
    conversation_id BIGINT NOT NULL REFERENCES chat_conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content         TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_conversation
    ON chat_messages (conversation_id, created_at);

-- optional traceability link from an existing log row to the conversation it
-- was asked in; nullable so historical rows (pre-migration) stay valid
ALTER TABLE chat_query_log
    ADD COLUMN IF NOT EXISTS conversation_id BIGINT REFERENCES chat_conversations(id);
