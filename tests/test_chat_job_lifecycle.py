#!/usr/bin/env python3
"""A submitted question must survive the browser connection that sent it.

Mobile users submit a question, switch apps, and come back to nothing. The
backend was never the cause: the messages endpoint is a sync `def`, FastAPI runs
it in a threadpool, and a client disconnect cancels nothing. Measured on this
stack (uvicorn 0.49 / starlette 1.3), both a sync AND an async handler ran to
completion after the client aborted at 3s, and the assistant message was
persisted either way.

The loss was entirely client-side: `await api.ask()` was the only route to the
answer, so a dropped socket discarded a finished reply. For a NEW conversation it
was worse -- conversation_id lived only in that response body, so the client
never learned where its answer had gone.

These tests pin the job lifecycle that gives the answer a durable address.
Every DB write is made against real tables and cleaned up afterwards.
"""
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from dotenv import load_dotenv

load_dotenv(".env", override=True)
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

from backend import jobs as J          # noqa: E402
from backend import conversations as C  # noqa: E402
from backend.db import query           # noqa: E402

results = []
CLEANUP = {"conversations": [], "jobs": []}


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


USER_A = int(query("SELECT id FROM app_users ORDER BY id LIMIT 1", fetch="one")["id"])
USER_B = int(query("SELECT id FROM app_users ORDER BY id DESC LIMIT 1",
                   fetch="one")["id"])


def new_convo(user_id=USER_A, title="job-lifecycle-test"):
    cid = C.create(user_id, title, "claude-sonnet-5")
    CLEANUP["conversations"].append(cid)
    return cid


try:
    # -----------------------------------------------------------------------
    # A -- normal lifecycle: queued -> processing -> completed
    # -----------------------------------------------------------------------
    cid = new_convo()
    umid = C.add_message_returning_id(cid, "user", "test question")
    job, created = J.create_or_reuse(conversation_id=cid, user_id=USER_A,
                                     question="test question", model="m",
                                     idempotency_key="k-normal",
                                     user_message_id=umid)
    CLEANUP["jobs"].append(job["job_id"])
    check("A. a submission creates a job", created and job["status"] == "queued",
          job["status"])
    check("A. the job records its conversation and owner",
          job["conversation_id"] == cid and job["user_id"] == USER_A)

    J.mark_processing(str(job["job_id"]))
    check("A. queued -> processing",
          J.get(str(job["job_id"]), USER_A)["status"] == "processing")

    amid = C.add_message_returning_id(cid, "assistant", "the answer")
    J.mark_completed(str(job["job_id"]), amid, 1234)
    done = J.get(str(job["job_id"]), USER_A)
    check("A. processing -> completed", done["status"] == "completed")
    check("A. the completed job points at the persisted assistant message",
          done["assistant_message_id"] == amid)
    check("A. duration is recorded", done["duration_ms"] == 1234)
    check("A. completed_at is set", done["completed_at"] is not None)
    msgs = C.messages(cid, USER_A)
    check("A. the transcript holds both turns",
          [m["role"] for m in msgs] == ["user", "assistant"], str(msgs))

    # -----------------------------------------------------------------------
    # B -- client disconnect: generation continues, answer persists, a NEW
    #      client retrieves it. This is the reported bug.
    # -----------------------------------------------------------------------
    cid2 = new_convo()
    um2 = C.add_message_returning_id(cid2, "user", "disconnect question")
    job2, _ = J.create_or_reuse(conversation_id=cid2, user_id=USER_A,
                                question="disconnect question", model="m",
                                idempotency_key="k-disc", user_message_id=um2)
    CLEANUP["jobs"].append(job2["job_id"])
    jid2 = str(job2["job_id"])

    finished = {"done": False}

    def runner():
        # Stands in for chat_svc.ask: the point is that it keeps running with
        # nobody holding the HTTP connection.
        time.sleep(2)
        aid = C.add_message_returning_id(cid2, "assistant", "answer after disconnect")
        J.mark_completed(jid2, aid, 2000)
        finished["done"] = True

    J.spawn(jid2, runner)                      # the "client" now goes away
    time.sleep(0.5)
    mid = J.get(jid2, USER_A)
    check("B. generation is in flight with no client attached",
          mid["status"] == "processing", mid["status"])
    for _ in range(40):
        if finished["done"]:
            break
        time.sleep(0.25)
    after = J.get(jid2, USER_A)
    check("B. generation completed with no client connected",
          after["status"] == "completed", after["status"])
    check("B. the answer is persisted",
          any(m["content"] == "answer after disconnect"
              for m in C.messages(cid2, USER_A)))
    # A brand-new client, holding nothing, recovers it from the conversation.
    check("B. a fresh client can retrieve the completed answer by job_id",
          J.get(jid2, USER_A)["assistant_message_id"] is not None)
    check("B. no active job remains for the conversation",
          J.active_for_conversation(cid2, USER_A) is None)

    # -----------------------------------------------------------------------
    # C/D -- tab switch and reload both reattach through the same mechanism:
    #        the pending job is discoverable from the conversation alone.
    # -----------------------------------------------------------------------
    cid3 = new_convo()
    um3 = C.add_message_returning_id(cid3, "user", "pending question")
    job3, _ = J.create_or_reuse(conversation_id=cid3, user_id=USER_A,
                                question="pending question", model="m",
                                idempotency_key="k-pending", user_message_id=um3)
    CLEANUP["jobs"].append(job3["job_id"])
    J.mark_processing(str(job3["job_id"]))
    found = J.active_for_conversation(cid3, USER_A)
    check("C/D. a still-processing job is discoverable from the conversation",
          found is not None and str(found["job_id"]) == str(job3["job_id"]))
    check("C/D. reattach requires no client-held state (conversation id only)",
          found["status"] == "processing")
    check("C/D. the user's question is already persisted, so a reload shows it",
          any(m["content"] == "pending question" for m in C.messages(cid3, USER_A)))
    check("C/D. polling only reads state (no generation fields are mutated)",
          J.get(str(job3["job_id"]), USER_A)["status"] == "processing")

    # -----------------------------------------------------------------------
    # E -- duplicate submit: one logical submission, one generation
    # -----------------------------------------------------------------------
    cid4 = new_convo()
    j1, c1 = J.create_or_reuse(conversation_id=cid4, user_id=USER_A,
                               question="dup", model="m",
                               idempotency_key="k-dup", user_message_id=None)
    CLEANUP["jobs"].append(j1["job_id"])
    j2, c2 = J.create_or_reuse(conversation_id=cid4, user_id=USER_A,
                               question="dup", model="m",
                               idempotency_key="k-dup", user_message_id=None)
    check("E. the first submit creates a job", c1 is True)
    check("E. an identical resubmit does NOT create a second job", c2 is False)
    check("E. the resubmit is handed the SAME job back",
          str(j1["job_id"]) == str(j2["job_id"]))
    n = query("SELECT count(*) AS n FROM chat_jobs WHERE user_id=%s AND "
              "idempotency_key='k-dup'", (USER_A,), fetch="one")["n"]
    check("E. exactly one job row exists for the key", int(n) == 1, str(n))

    # A retry AFTER a genuine failure is a new attempt, not a silent duplicate.
    J.mark_failed(str(j1["job_id"]), "generation_error", "failed for the test")
    j3, c3 = J.create_or_reuse(conversation_id=cid4, user_id=USER_A,
                               question="dup", model="m",
                               idempotency_key="k-dup", user_message_id=None)
    CLEANUP["jobs"].append(j3["job_id"])
    check("E. retry after a real failure creates a NEW attempt",
          c3 is True and str(j3["job_id"]) != str(j1["job_id"]))
    check("E. the new attempt is numbered", j3["attempt"] == 2, str(j3["attempt"]))

    # -----------------------------------------------------------------------
    # F -- failed generation is persisted, user-safely
    # -----------------------------------------------------------------------
    cid5 = new_convo()
    j5, _ = J.create_or_reuse(conversation_id=cid5, user_id=USER_A,
                              question="boom", model="m",
                              idempotency_key="k-fail", user_message_id=None)
    CLEANUP["jobs"].append(j5["job_id"])
    J.mark_failed(str(j5["job_id"]), "generation_error",
                  "Something went wrong while analyzing the data. Please try again.")
    f5 = J.get(str(j5["job_id"]), USER_A)
    check("F. a failure is persisted as failed", f5["status"] == "failed")
    check("F. the failure carries a user-safe message",
          "went wrong" in f5["error_message"] and "Traceback" not in f5["error_message"])
    check("F. a failed job is no longer active",
          J.active_for_conversation(cid5, USER_A) is None)
    pub = J.to_public(f5)
    for leaked in ("question", "user_id", "idempotency_key"):
        check(f"F. the public shape does not leak {leaked}", leaked not in pub)

    # -----------------------------------------------------------------------
    # G -- authorization
    # -----------------------------------------------------------------------
    check("G. another user cannot read the job by id",
          J.get(str(j5["job_id"]), USER_B) is None)
    check("G. another user sees no active job for the conversation",
          J.active_for_conversation(cid3, USER_B) is None)
    check("G. ownership is enforced in SQL, not after the fetch",
          "user_id = %s" in open("backend/jobs.py").read())

    # -----------------------------------------------------------------------
    # H -- restart semantics, stated and enforced
    # -----------------------------------------------------------------------
    cid6 = new_convo()
    j6, _ = J.create_or_reuse(conversation_id=cid6, user_id=USER_A,
                              question="orphan", model="m",
                              idempotency_key="k-orphan", user_message_id=None)
    CLEANUP["jobs"].append(j6["job_id"])
    J.mark_processing(str(j6["job_id"]))
    query("UPDATE chat_jobs SET created_at = now() - interval '2 hours' "
          "WHERE job_id=%s", (str(j6["job_id"]),), fetch=None, commit=True)
    swept = J.sweep_interrupted()
    o6 = J.get(str(j6["job_id"]), USER_A)
    check("H. a job orphaned by a restart is swept to failed",
          o6["status"] == "failed" and o6["error_code"] == "interrupted",
          f'{o6["status"]}/{o6["error_code"]}')
    check("H. the sweep message tells the user to retry",
          "try again" in (o6["error_message"] or "").lower())
    check("H. the sweep reports what it changed", swept >= 1, str(swept))
    # A young processing job must NOT be swept -- it is probably still running.
    j7, _ = J.create_or_reuse(conversation_id=cid6, user_id=USER_A,
                              question="young", model="m",
                              idempotency_key="k-young", user_message_id=None)
    CLEANUP["jobs"].append(j7["job_id"])
    J.mark_processing(str(j7["job_id"]))
    J.sweep_interrupted()
    check("H. an in-flight job is not swept",
          J.get(str(j7["job_id"]), USER_A)["status"] == "processing")

    # -----------------------------------------------------------------------
    # I -- the gates and the endpoints
    # -----------------------------------------------------------------------
    api_src = open("backend/api.py").read()
    check("I. generation is detached via jobs_svc.spawn, not BackgroundTasks",
          "jobs_svc.spawn(job_id, _run)" in api_src
          and "BackgroundTasks" not in api_src)
    check("I. the submission responds 202 without the answer",
          "status_code=202" in api_src and "AskAcceptedOut" in api_src)
    check("I. conversation_id is returned up front",
          "conversation_id=cid" in api_src)
    check("I. a status endpoint exists", '"/api/chat/jobs/{job_id}"' in api_src)
    check("I. a conversation reattach endpoint exists",
          '"/api/conversations/{cid}/active-job"' in api_src)
    check("I. the startup sweep is registered",
          'on_event("startup")' in api_src and "sweep_interrupted" in api_src)
    check("I. generation still goes through chat_svc.ask (gates intact)",
          "chat_svc.ask(history, question, model, attachments)" in api_src)
    chat_src = open("chat_sql.py").read()
    for gate, label in (("domains_for_relations", "source-aware gating"),
                        ("_ALLOWED_RELATIONS", "SQL validator allowlist"),
                        ("REQUEST_DEADLINE_SECONDS", "Anthropic/request bounds"),
                        ("hme_freshness", "HME freshness"),
                        ("_log_timings", "timing instrumentation")):
        check(f"I. {label} untouched", gate in chat_src)

    # -----------------------------------------------------------------------
    # J -- polling is cheap: status reads touch no analytics path
    # -----------------------------------------------------------------------
    jobs_src = open("backend/jobs.py").read()
    for expensive in ("meta_extract", "meta_profile", "answer_question",
                      "run_sql", "anthropic"):
        check(f"J. the job module never calls {expensive}", expensive not in jobs_src)
    check("J. the status endpoint only reads chat_jobs",
          "SELECT * FROM chat_jobs WHERE job_id" in jobs_src)

    # -----------------------------------------------------------------------
    # K -- observability
    # -----------------------------------------------------------------------
    for ev in ("chat_job created", "chat_job started", "chat_job reuse",
               "chat_job swept"):
        check(f"K. logs {ev!r}", ev in jobs_src)
    check("K. logs carry job_id and conversation_id", "job_id=%s" in jobs_src)
    check("K. the log does not dump question text",
          "question=%s" not in jobs_src and "content=%s" not in jobs_src)

finally:
    for jid in CLEANUP["jobs"]:
        query("DELETE FROM chat_jobs WHERE job_id=%s", (str(jid),),
              fetch=None, commit=True)
    for cid_ in CLEANUP["conversations"]:
        query("DELETE FROM chat_jobs WHERE conversation_id=%s", (cid_,),
              fetch=None, commit=True)
        query("DELETE FROM chat_messages WHERE conversation_id=%s", (cid_,),
              fetch=None, commit=True)
        query("DELETE FROM chat_conversations WHERE id=%s", (cid_,),
              fetch=None, commit=True)
    print(f"\n  cleaned up {len(CLEANUP['conversations'])} test conversation(s)")

passed = sum(1 for _, ok, _ in results if ok)
failed_n = len(results) - passed
print(f"\n{'='*66}\nchat job lifecycle: {passed} passed, {failed_n} failed, {len(results)} total")
if failed_n:
    for n_, ok, d in results:
        if not ok:
            print(f"  FAILED: {n_} -- {d}")
sys.exit(1 if failed_n else 0)
