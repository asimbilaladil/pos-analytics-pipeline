-- Migration 19: per-conversation model selection for Laynes Intelligence.
--
-- Lets a user switch which Claude model answers a given chat, at any time,
-- without affecting other conversations. NULL means "use the server default"
-- (chat_sql.MODEL / CHAT_MODEL env var) — existing conversations stay on the
-- default until someone explicitly changes them.

ALTER TABLE chat_conversations
    ADD COLUMN IF NOT EXISTS model TEXT;
