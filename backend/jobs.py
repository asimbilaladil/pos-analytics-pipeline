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

Mobile sleep (2026-10-18)
-------------------------
A locked phone freezes the page -- JS, timers and fetch -- often while the
submission itself is in flight, so the 202 is never read. The job still runs
(it never depended on the client), but the client used to report a failure
and its Retry resent the key after the job had COMPLETED, which the active-
only reuse treated as a new submission: a second conversation and a second
generation. A client-supplied key now identifies ONE logical submission for
good: find_for_key() hands back its active or completed job, so a resend is
a lookup. Only a FAILED job may be retried under the same key.

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


def find_for_key(user_id: int, idempotency_key: str,
                 include_completed: bool = True) -> dict | None:
    """The job a resend of this submission must land on, or None.

    Active jobs are always reused. A completed job is reused when the key is
    the client's own per-submission key (include_completed): resending after a
    lost acknowledgement must find the answer, not generate it again. A failed
    job is never reused, so Retry after a genuine failure starts a new attempt.
    """
    statuses = list(_ACTIVE) + (["completed"] if include_completed else [])
    job = _row(
        "SELECT * FROM chat_jobs WHERE user_id = %s AND idempotency_key = %s "
        "ORDER BY id DESC LIMIT 1", (user_id, idempotency_key))
    if job and job["status"] in statuses:
        log.info("chat_job reuse job_id=%s conversation_id=%s status=%s",
                 job["job_id"], job["conversation_id"], job["status"])
        return dict(job)
    return None


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
    # Two identical submissions racing (a double tap, or a resend crossing the
    # original on a flaky network) both pass the SELECT above. The partial
    # unique index lets exactly one INSERT win; the loser reads the winner's
    # row instead of surfacing an IntegrityError as a 500.
    job = query(
        "INSERT INTO chat_jobs (job_id, conversation_id, user_id, user_message_id, "
        "status, idempotency_key, question, model, attempt) "
        "VALUES (%s,%s,%s,%s,'queued',%s,%s,%s,%s) "
        "ON CONFLICT (user_id, idempotency_key) "
        "WHERE status IN ('queued', 'processing') DO NOTHING RETURNING *",
        (str(uuid.uuid4()), conversation_id, user_id, user_message_id,
         idempotency_key, question, model, attempt),
        fetch="one", commit=True)
    if job is None:
        winner = _row(
            "SELECT * FROM chat_jobs WHERE user_id = %s AND idempotency_key = %s "
            "AND status = ANY(%s) ORDER BY id DESC LIMIT 1",
            (user_id, idempotency_key, list(_ACTIVE)))
        if winner:
            log.info("chat_job reuse job_id=%s conversation_id=%s status=%s (race)",
                     winner["job_id"], winner["conversation_id"], winner["status"])
            return dict(winner), False
        raise RuntimeError("chat job insert conflicted but no active job was found")
    log.info("chat_job created job_id=%s conversation_id=%s attempt=%s model=%s",
             job["job_id"], conversation_id, attempt, model)
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
    # Logged HERE rather than at the call site so the terminal marker cannot be
    # lost by a caller forgetting it -- which is exactly what happened once.
    log.info("chat_job completed job_id=%s assistant_message_id=%s duration_ms=%s",
             job_id, assistant_message_id, duration_ms)


def mark_failed(job_id: str, code: str, message: str,
                duration_ms: int | None = None) -> None:
    """message must already be user-safe; provider internals stay in the log."""
    query("UPDATE chat_jobs SET status='failed', error_code=%s, error_message=%s, "
          "duration_ms=%s, completed_at=now() WHERE job_id=%s",
          (code, message[:500], duration_ms, job_id), fetch=None, commit=True)
    # error_code only: the message is user-facing text, not a diagnostic, and
    # the real detail is already in the service log from chat_svc.
    log.info("chat_job failed job_id=%s error_code=%s duration_ms=%s",
             job_id, code, duration_ms)


def sweep_interrupted(started_before=None, conversation_id=None) -> int:
    """Fail jobs this process cannot possibly still be running.

    Called at startup, and safe to call repeatedly. Without it a job orphaned by
    a restart would poll as 'processing' forever and the spinner would never
    clear -- the exact failure mode this whole change exists to remove.

    started_before: this process's start time. laynes-api runs ONE uvicorn
    worker and jobs run in its threads, so at startup every active job created
    before the process existed is dead -- however young. Without it only jobs
    older than STUCK_AFTER_SECONDS were swept, so a restart inside a job's
    first 10 minutes left it 'processing' forever. Only server conditions fail a
    job here; client polling (or its absence) never does.

    conversation_id: limit the sweep to one conversation (used by tests on the
    shared database, so they can never touch a real in-flight job).
    """
    if started_before is not None:
        cond, arg = "created_at < %s", started_before
    else:
        cond, arg = "created_at < now() - (%s || ' seconds')::interval", STUCK_AFTER_SECONDS
    scope, extra = ("", ()) if conversation_id is None else (" AND conversation_id = %s", (conversation_id,))
    rows = query(
        "UPDATE chat_jobs SET status='failed', error_code='interrupted', "
        "error_message='The server restarted while this answer was being "
        "generated. Please try again.', completed_at=now() "
        f"WHERE status = ANY(%s) AND {cond}{scope} "
        "RETURNING job_id", (list(_ACTIVE), arg, *extra),
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
        # execution_source: generation belongs to this server thread from here
        # on; no client request, socket or poll is involved in finishing it.
        log.info("chat_job started job_id=%s execution_source=detached_thread", job_id)
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
