"""Durable chat-generation jobs, decoupled from the browser connection.

Why this exists
---------------
A mobile user submits a question, switches apps, and the answer is lost. The
backend was never at fault: the messages endpoint is a sync `def`, FastAPI runs
it in a threadpool, and a client disconnect cancels nothing. Verified
empirically on this stack (uvicorn 0.49 / starlette 1.3): both a sync and an
async handler ran to completion after the client aborted at 3s, and the
assistant message was persisted either way.

The loss was entirely client-side. `await api.ask()` was the ONLY route to the
answer, so when a backgrounded browser dropped the socket the promise rejected,
the spinner cleared, and a finished answer sat in chat_messages unseen. For a
new conversation it was worse: conversation_id existed only in that response
body, so the client never learned where its own answer had gone.

The fix is an address, not a longer timeout. The submission returns immediately
with a job_id; generation continues in a detached thread; the client polls
cheap persisted state and can reattach after a reload, a tab switch, or on a
different device.

Restart semantics, stated plainly
---------------------------------
Generation runs in an in-process daemon thread. A process restart therefore
loses in-flight work -- there is no external queue, which for a single-instance
deployment answering a handful of questions a day would be more moving parts
than the problem warrants. What matters is that a lost job never hangs: on
startup, sweep_interrupted() fails every job still marked 'processing' with
error_code='interrupted', which surfaces the normal Retry affordance. A job is
never silently abandoned, and a restart cannot produce a duplicate answer
because the assistant message is written once, inside the worker, keyed to the
job row.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid

from .db import query

log = logging.getLogger("laynes.chat")

# A job still 'processing' after this long cannot be alive in this process:
# chat_sql enforces its own 240s request deadline, so anything past that plus
# slack was orphaned by a restart or a hard crash.
STUCK_AFTER_SECONDS = 600

_ACTIVE = ("queued", "processing")


def _row(sql, params):
    return query(sql, params, fetch="one")


def create_or_reuse(*, conversation_id: int, user_id: int, question: str,
                    model: str | None, idempotency_key: str,
                    user_message_id: int | None) -> tuple[dict, bool]:
    """Return (job, created). An active job for the same key is REUSED.

    This is the duplicate guard: a double-tapped Send, or a client that
    reconnects and resubmits, lands on the partial unique index over
    (user_id, idempotency_key) WHERE status IN ('queued','processing') and gets
    the SAME job back rather than starting a second generation. Completed and
    failed rows are outside the index, so a deliberate retry after a genuine
    failure is free to start a new attempt.
    """
    existing = _row(
        "SELECT * FROM chat_jobs WHERE user_id = %s AND idempotency_key = %s "
        "AND status = ANY(%s) ORDER BY id DESC LIMIT 1",
        (user_id, idempotency_key, list(_ACTIVE)))
    if existing:
        log.info("chat_job reuse job_id=%s conversation_id=%s status=%s",
                 existing["job_id"], existing["conversation_id"], existing["status"])
        return dict(existing), False

    prior = _row(
        "SELECT coalesce(max(attempt), 0) AS a FROM chat_jobs "
        "WHERE user_id = %s AND idempotency_key = %s", (user_id, idempotency_key))
    attempt = int((prior or {}).get("a") or 0) + 1

    # fetch="one" WITH commit=True: db.query commits the same connection after
    # fetching, so the RETURNING row and the commit are the one transaction.
    # Committing on a separate query() call would open a new connection and
    # silently roll this insert back.
    job = query(
        "INSERT INTO chat_jobs (job_id, conversation_id, user_id, user_message_id, "
        "status, idempotency_key, question, model, attempt) "
        "VALUES (%s,%s,%s,%s,'queued',%s,%s,%s,%s) RETURNING *",
        (str(uuid.uuid4()), conversation_id, user_id, user_message_id,
         idempotency_key, question, model, attempt),
        fetch="one", commit=True)
    log.info("chat_job created job_id=%s conversation_id=%s attempt=%s",
             job["job_id"], conversation_id, attempt)
    return dict(job), True


def attach_user_message(job_id: str, user_message_id: int) -> None:
    query("UPDATE chat_jobs SET user_message_id=%s WHERE job_id=%s",
          (user_message_id, job_id), fetch=None, commit=True)


def get(job_id: str, user_id: int) -> dict | None:
    """Ownership-scoped read. A guessed job_id from another user returns None."""
    return _row("SELECT * FROM chat_jobs WHERE job_id = %s AND user_id = %s",
                (job_id, user_id))


def active_for_conversation(conversation_id: int, user_id: int) -> dict | None:
    """The newest active job, so a reopened conversation can reattach itself."""
    return _row(
        "SELECT * FROM chat_jobs WHERE conversation_id = %s AND user_id = %s "
        "AND status = ANY(%s) ORDER BY id DESC LIMIT 1",
        (conversation_id, user_id, list(_ACTIVE)))


def mark_processing(job_id: str) -> None:
    query("UPDATE chat_jobs SET status='processing', started_at=now() "
          "WHERE job_id=%s AND status='queued'", (job_id,), fetch=None, commit=True)


def mark_completed(job_id: str, assistant_message_id: int | None,
                   duration_ms: int) -> None:
    query("UPDATE chat_jobs SET status='completed', assistant_message_id=%s, "
          "duration_ms=%s, completed_at=now() WHERE job_id=%s",
          (assistant_message_id, duration_ms, job_id), fetch=None, commit=True)


def mark_failed(job_id: str, code: str, message: str,
                duration_ms: int | None = None) -> None:
    """message must already be user-safe; provider internals stay in the log."""
    query("UPDATE chat_jobs SET status='failed', error_code=%s, error_message=%s, "
          "duration_ms=%s, completed_at=now() WHERE job_id=%s",
          (code, message[:500], duration_ms, job_id), fetch=None, commit=True)


def sweep_interrupted() -> int:
    """Fail jobs this process cannot possibly still be running.

    Called at startup, and safe to call repeatedly. Without it a job orphaned by
    a restart would poll as 'processing' forever and the spinner would never
    clear -- the exact failure mode this whole change exists to remove.
    """
    rows = query(
        "UPDATE chat_jobs SET status='failed', error_code='interrupted', "
        "error_message='The server restarted while this answer was being "
        "generated. Please try again.', completed_at=now() "
        "WHERE status = ANY(%s) AND created_at < now() - (%s || ' seconds')::interval "
        "RETURNING job_id", (list(_ACTIVE), STUCK_AFTER_SECONDS),
        fetch="all", commit=True) or []
    if rows:
        log.warning("chat_job swept %d interrupted job(s): %s",
                    len(rows), ", ".join(str(r["job_id"]) for r in rows[:10]))
    return len(rows)


def to_public(job: dict) -> dict:
    """Client-facing shape. No question text, no internals, no user ids."""
    return {
        "job_id": str(job["job_id"]),
        "conversation_id": job["conversation_id"],
        "status": job["status"],
        "attempt": job["attempt"],
        "created_at": job["created_at"].isoformat() if job.get("created_at") else None,
        "completed_at": (job["completed_at"].isoformat()
                         if job.get("completed_at") else None),
        "duration_ms": job.get("duration_ms"),
        "error_code": job.get("error_code"),
        "error_message": job.get("error_message"),
    }


def spawn(job_id: str, runner) -> None:
    """Run `runner()` detached from the request that created it.

    A daemon thread, deliberately not FastAPI BackgroundTasks: those are awaited
    by Starlette as part of finishing the response, so they are still tied to
    the request's lifecycle and to whether that response completes. The point
    here is that the generation must outlive the request entirely.
    """
    def _wrap():
        t0 = time.time()
        mark_processing(job_id)
        log.info("chat_job started job_id=%s", job_id)
        try:
            runner()
        except Exception as exc:                        # noqa: BLE001
            # Last-resort net: the runner handles its own errors, so reaching
            # here means something unexpected. The job must still leave
            # 'processing', or the client polls forever.
            log.exception("chat_job crashed job_id=%s", job_id)
            mark_failed(job_id, "internal",
                        "Something went wrong while analyzing the data. "
                        "Please try again.",
                        int((time.time() - t0) * 1000))

    threading.Thread(target=_wrap, name=f"chatjob-{job_id[:8]}",
                     daemon=True).start()
