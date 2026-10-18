#!/usr/bin/env python3
"""Server side of chat on a phone that locks, sleeps, loses its network or is
killed while an answer is being generated.

Run:  venv/bin/python tests/test_chat_mobile_resume.py      (exit 0 = pass)

Reproduced 2026-10-18 in Chromium with the page frozen the way a locked phone
freezes it: once a POST is accepted the server job finishes with ZERO client
requests, so the server was never the cause. The failure was a send whose 202
the frozen page never read: the client reported an error, and its Retry -- the
same idempotency key -- arrived after the job had completed. Reuse covered only
ACTIVE jobs, so the resend made a second conversation and a second generation.

These checks run the real API on a private uvicorn server with a fake, slow
generation (no model calls), against real tables, cleaned up afterwards. The
startup lifespan is OFF: the job sweep must never touch production's jobs.
"""
import json
import os
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from dotenv import load_dotenv

load_dotenv(".env", override=True)
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import uvicorn  # noqa: E402

from backend import api as A            # noqa: E402
from backend import chat as chat_svc    # noqa: E402
from backend import conversations as C  # noqa: E402
from backend import jobs as J           # noqa: E402
from backend.db import query            # noqa: E402

results = []
GEN_SECONDS = 2.0
calls = {"n": 0}


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


def fake_ask(history, question, model, attachments=None):
    calls["n"] += 1
    time.sleep(GEN_SECONDS)
    return f"answer to: {question}", [], int(GEN_SECONDS * 1000), None


chat_svc.ask = fake_ask
chat_svc.assistant_configured = lambda: True
USER = int(query("SELECT id FROM app_users ORDER BY id LIMIT 1", fetch="one")["id"])
TOKEN = secrets.token_urlsafe(32)
query("INSERT INTO app_sessions (token, user_id, expires_at) VALUES (%s, %s, %s)",
      (TOKEN, USER, datetime.now(timezone.utc) + timedelta(hours=1)), fetch=None, commit=True)
CONV0 = int(query("SELECT coalesce(max(id), 0) AS m FROM chat_conversations", fetch="one")["m"])

with socket.socket() as _s:
    _s.bind(("127.0.0.1", 0))
    PORT = _s.getsockname()[1]
server = uvicorn.Server(uvicorn.Config(A.app, host="127.0.0.1", port=PORT,
                                       log_level="warning", lifespan="off"))
threading.Thread(target=server.run, daemon=True).start()
for _ in range(100):
    if server.started:
        break
    time.sleep(0.05)
BASE = f"http://127.0.0.1:{PORT}"


def call(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json",
                                          "Cookie": f"laynes_session={TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, None


def jobs_for(key):
    return query("SELECT * FROM chat_jobs WHERE user_id=%s AND idempotency_key=%s ORDER BY id",
                 (USER, key), fetch="all") or []


def roles(cid):
    return [r["role"] for r in query("SELECT role FROM chat_messages WHERE conversation_id=%s "
                                     "ORDER BY id", (cid,), fetch="all") or []]


def wait_done(key, limit=30):
    t = time.time()
    while time.time() - t < limit:            # database only: no client request
        js = jobs_for(key)
        if js and all(j["status"] in ("completed", "failed") for j in js):
            return js
        time.sleep(0.2)
    return jobs_for(key)


def new_convs():
    return [r["id"] for r in query("SELECT id FROM chat_conversations WHERE user_id=%s AND id > %s "
                                   "ORDER BY id", (USER, CONV0), fetch="all") or []]


Q = "mobile resume test question"
try:
    # ---------------------------------------------------------------------
    print("A/C. POST -> 202, then zero client requests")
    key = f"t-a-{uuid.uuid4()}"
    st, acc = call("POST", "/api/conversations/0/messages", {"question": Q, "idempotency_key": key})
    check("A. 202 with conversation_id, job_id, status, user_message_id",
          st == 202 and {"conversation_id", "job_id", "status", "user_message_id"} <= set(acc or {}),
          str(acc)[:120])
    js = wait_done(key)
    check("A. with no polling at all the job still completes",
          len(js) == 1 and js[0]["status"] == "completed", str([j["status"] for j in js]))
    check("A. job went through processing (started_at) and finished (completed_at)",
          js[0]["started_at"] and js[0]["completed_at"] and js[0]["started_at"] <= js[0]["completed_at"])
    cid_a = acc["conversation_id"]
    check("F. reopening later shows the question and the answer exactly once",
          roles(cid_a) == ["user", "assistant"], str(roles(cid_a)))

    # ---------------------------------------------------------------------
    print("J. a completed job always has its persisted assistant message")
    m = query("SELECT id, role, created_at FROM chat_messages WHERE id=%s",
              (js[0]["assistant_message_id"],), fetch="one")
    check("J. assistant_message_id points at a stored assistant message",
          m and m["role"] == "assistant")
    check("J. the message was stored before the job was marked completed",
          m and m["created_at"] <= js[0]["completed_at"])
    src = open("backend/api.py").read()
    run = src[src.index("    def _run():"):src.index("jobs_svc.spawn(job_id, _run)")]
    check("J. code order: add assistant message -> mark_completed",
          run.index('add_message_returning_id(cid, "assistant"') < run.index("mark_completed"))

    # ---------------------------------------------------------------------
    print("B/H. client vanishes before reading the 202 (socket closed)")
    key = f"t-b-{uuid.uuid4()}"
    body = json.dumps({"question": Q, "idempotency_key": key}).encode()
    s = socket.create_connection(("127.0.0.1", PORT))
    s.sendall((f"POST /api/conversations/0/messages HTTP/1.1\r\nHost: x\r\n"
               f"Content-Type: application/json\r\nCookie: laynes_session={TOKEN}\r\n"
               f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode() + body)
    time.sleep(0.3)
    s.close()
    js = wait_done(key)
    check("B. disconnect after the request reached the server: job completes",
          len(js) == 1 and js[0]["status"] == "completed", str([j["status"] for j in js]))

    # ---------------------------------------------------------------------
    print("G. lost acknowledgement -> resend with the SAME key")
    n0, c0 = calls["n"], set(new_convs())
    st, again = call("POST", "/api/conversations/0/messages", {"question": Q, "idempotency_key": key})
    check("G. resend after completion returns the SAME completed job",
          st == 202 and again["job_id"] == str(js[0]["job_id"]) and again["status"] == "completed",
          str(again)[:120])
    check("G. ... in the job's own conversation (the client never learned it)",
          again["conversation_id"] == js[0]["conversation_id"])
    check("G. ... with no new conversation, message or generation",
          set(new_convs()) == c0 and calls["n"] == n0 and roles(again["conversation_id"]) == ["user", "assistant"])
    st, ex = call("POST", f"/api/conversations/{cid_a}/messages",
                  {"question": "follow-up", "idempotency_key": (k2 := f"t-g2-{uuid.uuid4()}")})
    wait_done(k2)
    st, ex2 = call("POST", f"/api/conversations/{cid_a}/messages",
                   {"question": "follow-up", "idempotency_key": k2})
    check("G. existing conversation: resend after completion adds nothing",
          ex2["job_id"] == ex["job_id"] and roles(cid_a) == ["user", "assistant", "user", "assistant"],
          str(roles(cid_a)))

    key = f"t-g3-{uuid.uuid4()}"
    st, first = call("POST", "/api/conversations/0/messages", {"question": Q, "idempotency_key": key})
    st, mid = call("POST", "/api/conversations/0/messages", {"question": Q, "idempotency_key": key})
    js = wait_done(key)
    check("G. resend while still processing returns the same job and conversation",
          mid["job_id"] == first["job_id"] and mid["conversation_id"] == first["conversation_id"]
          and len(js) == 1)
    check("G. ... and leaves no empty conversation behind",
          all(roles(c) for c in new_convs()), str({c: roles(c) for c in new_convs()}))

    key = f"t-g4-{uuid.uuid4()}"
    outs = []

    def fire():
        outs.append(call("POST", "/api/conversations/0/messages", {"question": Q, "idempotency_key": key})[1])
    ths = [threading.Thread(target=fire) for _ in range(4)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    js = wait_done(key)
    check("G. four concurrent identical sends -> one job, one conversation, no error",
          len(js) == 1 and len({o["job_id"] for o in outs if o}) == 1
          and len({o["conversation_id"] for o in outs if o}) == 1 and all(outs),
          str([(o or {}).get("conversation_id") for o in outs]))
    check("G. ... no empty conversation left by the losers",
          all(roles(c) for c in new_convs()))

    key = f"t-g5-{uuid.uuid4()}"
    st, f1 = call("POST", "/api/conversations/0/messages", {"question": Q, "idempotency_key": key})
    wait_done(key)
    J.mark_failed(f1["job_id"], "generation_error", "failed for the test")
    st, f2 = call("POST", "/api/conversations/0/messages", {"question": Q, "idempotency_key": key})
    js = wait_done(key)
    check("G. after a FAILED job the same key starts a new attempt (Retry)",
          f2["job_id"] != f1["job_id"] and len(js) == 2 and js[-1]["attempt"] == 2)
    n0 = calls["n"]
    st, d1 = call("POST", f"/api/conversations/{cid_a}/messages", {"question": "same words"})
    time.sleep(GEN_SECONDS + 1)
    st, d2 = call("POST", f"/api/conversations/{cid_a}/messages", {"question": "same words"})
    time.sleep(GEN_SECONDS + 1)
    check("G. without a client key, asking the same words again is a new question",
          d1["job_id"] != d2["job_id"] and calls["n"] == n0 + 2)

    # ---------------------------------------------------------------------
    print("K. API restart while processing")
    cid_k = C.create(USER, "restart-test", "m")
    young = J.create_or_reuse(conversation_id=cid_k, user_id=USER, question="q", model="m",
                              idempotency_key=f"t-k-{uuid.uuid4()}", user_message_id=None)[0]
    J.mark_processing(str(young["job_id"]))
    boot = datetime.now(timezone.utc)
    time.sleep(0.05)
    later = J.create_or_reuse(conversation_id=cid_k, user_id=USER, question="q2", model="m",
                              idempotency_key=f"t-k2-{uuid.uuid4()}", user_message_id=None)[0]
    J.sweep_interrupted(conversation_id=cid_k)
    check("K. the old age-only sweep left a seconds-old orphan 'processing'",
          J.get(str(young["job_id"]), USER)["status"] == "processing")
    J.sweep_interrupted(started_before=boot, conversation_id=cid_k)
    y, lt = J.get(str(young["job_id"]), USER), J.get(str(later["job_id"]), USER)
    check("K. startup sweep fails every job created before the process started",
          y["status"] == "failed" and y["error_code"] == "interrupted", y["status"])
    check("K. ... with the Retry message, and leaves newer jobs alone",
          "try again" in (y["error_message"] or "") and lt["status"] == "queued")
    J.mark_failed(str(later["job_id"]), "test", "cleanup")
    check("K. startup wiring passes the process start time",
          "sweep_interrupted(started_before=_PROCESS_STARTED_AT)" in src)

    # ---------------------------------------------------------------------
    print("9. absence of polling never fails a job")
    jsrc = open("backend/jobs.py").read()
    check("9. no code path ties job state to polls or heartbeats",
          "last_poll" not in jsrc and "heartbeat" not in jsrc.lower().replace("never", ""))
    check("14. lifecycle logs: created / started (execution_source) / completed / failed / reattach",
          "chat_job created" in jsrc and "execution_source=detached_thread" in jsrc
          and "chat_job completed" in jsrc and "chat_job failed" in jsrc and "chat_job reattach" in src)
finally:
    server.should_exit = True
    for cid in new_convs():
        C.delete(cid, USER)
    query("DELETE FROM app_sessions WHERE token=%s", (TOKEN,), fetch=None, commit=True)

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
