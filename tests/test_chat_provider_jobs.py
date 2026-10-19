#!/usr/bin/env python3
"""Provider selection through the persistent chat-job API (PR #24 guarantees).

Run:  venv/bin/python tests/test_chat_provider_jobs.py      (exit 0 = pass)

The real API on a private uvicorn server (lifespan OFF: the startup sweep must
never touch production's jobs), real tables, a fake slow generation that
records which model each job ran with -- no model calls. Everything it creates
is deleted afterwards.
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
# Never reach api.openai.com from a test, whatever .env holds: the real key is
# dropped and any client that escapes the mocks hits an unroutable address.
os.environ["OPENAI_BASE_URL"] = "http://127.0.0.1:9/v1"
os.environ.setdefault("ANTHROPIC_API_KEY", "x")
for _k in ("OPENAI_ENABLED", "OPENAI_API_KEY", "OPENAI_ALLOWED_MODELS"):
    os.environ.pop(_k, None)

import uvicorn  # noqa: E402

import chat_providers as P              # noqa: E402
from backend import api as A            # noqa: E402
from backend import chat as chat_svc    # noqa: E402
from backend import conversations as C  # noqa: E402
from backend.db import query            # noqa: E402

results = []
GEN_SECONDS = 2.0
ran = []                                  # (question, model) per generation


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


FAIL_NEXT = {"error": None}


def fake_ask(history, question, model, attachments=None):
    ran.append((question, model))
    time.sleep(GEN_SECONDS)
    if FAIL_NEXT["error"]:
        code, FAIL_NEXT["error"] = FAIL_NEXT["error"], None
        return ("OpenAI is limiting requests right now. Please try again in a minute, "
                "or switch to Claude.", [], 10, f"ProviderError[{code}] RateLimitError status_code=429")
    return f"answer to: {question}", [], int(GEN_SECONDS * 1000), None


chat_svc.ask = fake_ask
OPENAI_ON = {"on": False}
chat_svc.openai_models = lambda: ["gpt-6-luna"] if OPENAI_ON["on"] else []
_cfg = P.openai_config


def fake_cfg():
    c = _cfg()
    return c.__class__(enabled=OPENAI_ON["on"], has_key=OPENAI_ON["on"],
                       allowed=("gpt-6-luna",) if OPENAI_ON["on"] else (), rejected=(),
                       default="gpt-6-luna" if OPENAI_ON["on"] else None,
                       loop_effort=None, loop_max_output_tokens=12000)


P.openai_config = fake_cfg
if P.openai is None:                       # usable needs the SDK importable
    P.openai = object()

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
        raw = e.read()
        return e.code, (json.loads(raw) if raw else None)


def jobs_for(key):
    return query("SELECT * FROM chat_jobs WHERE user_id=%s AND idempotency_key=%s ORDER BY id",
                 (USER, key), fetch="all") or []


def wait_done(key, limit=30):
    t = time.time()
    while time.time() - t < limit:
        js = jobs_for(key)
        if js and all(j["status"] in ("completed", "failed") for j in js):
            return js
        time.sleep(0.2)
    return jobs_for(key)


def new_convs():
    return [r["id"] for r in query("SELECT id FROM chat_conversations WHERE user_id=%s AND id > %s "
                                   "ORDER BY id", (USER, CONV0), fetch="all") or []]


def ask(cid, q, model, key=None):
    return call("POST", f"/api/conversations/{cid}/messages",
                {"question": q, "model": model,
                 "idempotency_key": key or f"t-p-{uuid.uuid4()}"})


try:
    print("1. /api/models")
    st, mo = call("GET", "/api/models")
    check("OpenAI off: Claude only, Claude default, OpenAI provider disabled",
          st == 200 and mo["default"] == chat_svc.DEFAULT_MODEL
          and all(o["provider"] == "anthropic" for o in mo["options"])
          and {p["id"]: p["enabled"] for p in mo["providers"]}["openai"] is False, str(mo)[:160])
    OPENAI_ON["on"] = True
    st, mo = call("GET", "/api/models")
    check("OpenAI on: GPT option offered with label/provider; Claude still the default",
          {"id": "gpt-6-luna", "label": "GPT-6 Luna", "provider": "openai"} in mo["options"]
          and mo["default"] == chat_svc.DEFAULT_MODEL and "gpt-6-luna" in mo["models"])

    print("2. A job keeps the provider it was submitted with")
    k1 = f"t-p1-{uuid.uuid4()}"
    st, a1 = ask(0, "provider question one", "gpt-6-luna", k1)
    check("OpenAI submission -> 202 promptly", st == 202 and a1["status"] == "queued")
    cid = a1["conversation_id"]
    time.sleep(0.3)
    # The user flips the picker to Claude and asks again while job 1 runs.
    k2 = f"t-p2-{uuid.uuid4()}"
    st, a2 = ask(cid, "provider question two", "claude-sonnet-5", k2)
    j1, j2 = wait_done(k1), wait_done(k2)
    check("job 1 ran on OpenAI, job 2 on Claude (switching never re-routes a running job)",
          j1[0]["model"] == "gpt-6-luna" and j2[0]["model"] == "claude-sonnet-5"
          and ("provider question one", "gpt-6-luna") in ran
          and ("provider question two", "claude-sonnet-5") in ran, str(ran[-2:]))
    st, d = call("GET", f"/api/conversations/{cid}")
    am = [m for m in d["messages"] if m["role"] == "assistant"]
    check("each answer carries its own model and provider",
          [(m["model"], m["provider"]) for m in am]
          == [("gpt-6-luna", "openai"), ("claude-sonnet-5", "anthropic")], str(am))
    check("user messages carry no model; history intact (u, u, a, a or u, a, u, a)",
          all(m["model"] is None for m in d["messages"] if m["role"] == "user")
          and len(d["messages"]) == 4)

    print("3. Lost acknowledgement / resend with the same key")
    n0 = len(ran)
    st, again = ask(0, "provider question one", "gpt-6-luna", k1)
    check("resend after completion -> same completed job, no new generation",
          again["job_id"] == a1["job_id"] and again["status"] == "completed" and len(ran) == n0)
    OPENAI_ON["on"] = False
    st, again2 = ask(0, "provider question one", "gpt-6-luna", k1)
    check("... even after OpenAI was disabled (lookup comes before validation)",
          st == 202 and again2["job_id"] == a1["job_id"] and len(ran) == n0, str((st, again2)))

    print("4. Validation of a NEW submission")
    c0 = set(new_convs())
    st, rej = ask(0, "should be refused", "gpt-6-luna")
    check("OpenAI disabled: new GPT submission -> 400, not answered by Claude",
          st == 400 and "not available" in (rej or {}).get("detail", "") and len(ran) == n0)
    check("... and nothing was created", set(new_convs()) == c0)
    st, rej = ask(0, "should be refused", "gpt-9-imaginary")
    check("unknown GPT id -> 400", st == 400)
    kf = f"t-p4-{uuid.uuid4()}"
    st, fb = ask(0, "legacy model id", "claude-sonnet-4", kf)
    wait_done(kf)
    check("retired Claude id -> default Claude model (unchanged behaviour)",
          st == 202 and jobs_for(kf)[0]["model"] == chat_svc.DEFAULT_MODEL)

    print("5. Provider failure -> recoverable terminal state, answer persisted first")
    OPENAI_ON["on"] = True
    FAIL_NEXT["error"] = "provider_rate_limited"
    kr = f"t-p5-{uuid.uuid4()}"
    st, fr = ask(0, "rate limited question", "gpt-6-luna", kr)
    js = wait_done(kr)
    check("failed with the provider's code and a user-safe message (no endless spinner)",
          js[0]["status"] == "failed" and js[0]["error_code"] == "provider_rate_limited"
          and "OpenAI" in js[0]["error_message"], str(js[0]["error_code"]))
    st, job = call("GET", f"/api/chat/jobs/{fr['job_id']}")
    check("poll shows failed + message", job["status"] == "failed" and "OpenAI" in job["error_message"])
    st, rr = ask(0, "rate limited question", "gpt-6-luna", kr)
    js = wait_done(kr)
    check("Retry with the same key starts attempt 2 on the SAME provider",
          rr["job_id"] != fr["job_id"] and len(js) == 2 and js[-1]["attempt"] == 2
          and js[-1]["model"] == "gpt-6-luna" and js[-1]["status"] == "completed")

    print("6. Mobile: socket closed before the 202 is read, concurrent duplicates")
    kb = f"t-p6-{uuid.uuid4()}"
    body = json.dumps({"question": "sleep test", "model": "gpt-6-luna",
                       "idempotency_key": kb}).encode()
    s = socket.create_connection(("127.0.0.1", PORT))
    s.sendall((f"POST /api/conversations/0/messages HTTP/1.1\r\nHost: x\r\n"
               f"Content-Type: application/json\r\nCookie: laynes_session={TOKEN}\r\n"
               f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode() + body)
    time.sleep(0.3)
    s.close()
    js = wait_done(kb)
    check("disconnect before the ack: the OpenAI job still completes, server-owned",
          len(js) == 1 and js[0]["status"] == "completed" and js[0]["model"] == "gpt-6-luna")
    n1 = len(ran)
    st, re = ask(0, "sleep test", "gpt-6-luna", kb)
    check("wake + resend: same job, its conversation, no second generation",
          re["job_id"] == str(js[0]["job_id"]) and re["conversation_id"] == js[0]["conversation_id"]
          and len(ran) == n1)
    kc = f"t-p7-{uuid.uuid4()}"
    outs = []

    def fire():
        outs.append(ask(0, "dup test", "gpt-6-luna", kc)[1])
    ths = [threading.Thread(target=fire) for _ in range(4)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    js = wait_done(kc)
    check("four concurrent sends with an OpenAI model -> one job, one generation",
          len(js) == 1 and len({o["job_id"] for o in outs if o}) == 1
          and sum(1 for q, _m in ran if q == "dup test") == 1)

    print("7. Wiring")
    src = open("backend/api.py").read()
    ia = src.index("prior = jobs_svc.find_for_key")
    ib = src.index("chat_svc.resolve_model(model, strict=True)")
    check("resend lookup precedes model validation", ia < ib)
    run_src = src[src.index("    def _run():"):src.index("jobs_svc.spawn(job_id, _run)")]
    check("assistant answer saved before the job is marked completed/failed",
          run_src.index('add_message_returning_id(cid, "assistant"') < run_src.index("mark_completed")
          and run_src.index('add_message_returning_id(cid, "assistant"') < run_src.index("mark_failed"))
    check("generation uses the model fixed at submission (closure), logged with its provider",
          "chat_svc.ask(history, question, model, attachments)" in run_src
          and "provider=%s model=%s" in run_src)
finally:
    server.should_exit = True
    for c in new_convs():
        C.delete(c, USER)
    query("DELETE FROM app_sessions WHERE token=%s", (TOKEN,), fetch=None, commit=True)

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
