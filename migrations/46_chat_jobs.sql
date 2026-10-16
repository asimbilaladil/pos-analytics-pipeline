-- 46_chat_jobs.sql
-- Detach a chat generation from the browser connection that started it.
--
-- Problem
-- -------
-- On mobile, submitting a question and switching tabs/apps lost the answer. The
-- BACKEND was never the problem: the endpoint is a sync `def`, FastAPI runs it
-- in a threadpool, and a disconnect cancels nothing. Verified empirically --
-- both sync AND async handlers on uvicorn 0.49/starlette 1.3 ran to completion
-- after the client aborted at 3s. The assistant message was always persisted.
--
-- The loss was entirely client-side: `await api.ask()` was the ONLY path to the
-- answer. When a backgrounded mobile browser dropped the socket the promise
-- rejected, the spinner cleared, and a completed answer sat in chat_messages
-- unseen. For a NEW conversation it was worse -- conversation_id came back only
-- in that response body, so the client never learned where the answer went.
--
-- This table gives the answer a durable address the client can come back to.
--
-- Restart semantics (stated honestly): generation runs in an in-process thread,
-- so a process restart loses in-flight work. Those jobs do NOT silently hang --
-- a startup sweep marks any still-'processing' job as 'failed' with
-- error_code='interrupted', which surfaces the existing Retry UI.

CREATE TABLE IF NOT EXISTS chat_jobs (
    id                  bigserial PRIMARY KEY,
    job_id              uuid        NOT NULL UNIQUE,
    conversation_id     bigint      NOT NULL REFERENCES chat_conversations(id) ON DELETE CASCADE,
    user_id             bigint      NOT NULL REFERENCES app_users(id),
    -- Ownership is stored on the job itself so the status endpoint can authorise
    -- without a join, and cannot be widened by a conversation being reassigned.
    user_message_id     bigint      REFERENCES chat_messages(id) ON DELETE SET NULL,
    assistant_message_id bigint     REFERENCES chat_messages(id) ON DELETE SET NULL,
    status              text        NOT NULL DEFAULT 'queued',
    -- Idempotency: one logical submission -> at most one active generation.
    -- A client retry reuses the key and is handed the SAME job back.
    idempotency_key     text        NOT NULL,
    question            text        NOT NULL,
    model               text,
    attempt             int         NOT NULL DEFAULT 1,
    error_code          text,
    error_message       text,
    duration_ms         int,
    created_at          timestamptz NOT NULL DEFAULT now(),
    started_at          timestamptz,
    completed_at        timestamptz,
    CONSTRAINT chat_jobs_status_ck
        CHECK (status IN ('queued', 'processing', 'completed', 'failed'))
);

-- One ACTIVE job per idempotency key per user. Completed/failed rows stay for
-- history, so a deliberate retry after a real failure can create a new attempt
-- while a double-tap during generation cannot.
CREATE UNIQUE INDEX IF NOT EXISTS chat_jobs_active_idem_uk
    ON chat_jobs (user_id, idempotency_key)
    WHERE status IN ('queued', 'processing');

CREATE INDEX IF NOT EXISTS chat_jobs_conversation_idx
    ON chat_jobs (conversation_id, created_at DESC);
CREATE INDEX IF NOT EXISTS chat_jobs_active_idx
    ON chat_jobs (status, created_at) WHERE status IN ('queued', 'processing');

COMMENT ON TABLE  chat_jobs IS
  'Durable state for one chat generation, so a dropped browser connection never loses an answer. Polled by the client; never re-runs generation.';
COMMENT ON COLUMN chat_jobs.idempotency_key IS
  'Client-supplied per-submission key. The partial unique index makes a double submit return the SAME in-flight job instead of starting a second generation.';
COMMENT ON COLUMN chat_jobs.error_message IS
  'User-safe text only. Provider internals, SQL and stack traces stay in the server log.';
COMMENT ON COLUMN chat_jobs.status IS
  'queued -> processing -> completed|failed. A process restart leaves no job in processing: the startup sweep fails them with error_code=interrupted.';
