#!/usr/bin/env python3
"""The spinner must never outlive a request.

On 2026-09-25 a question sat on "Analyzing your data..." indefinitely. The
backend ran past nginx's 300 s proxy_read_timeout, nginx synthesised an HTML
504, and the browser had no JSON to parse.

NOTE ON SCOPE: this repository has no JavaScript test runner (no vitest/jest in
frontend/package.json), and adding one is outside this fix. These are static
guarantees over the source plus a build check -- they prove the error paths
exist and are wired, not that a rendered DOM behaves. That gap is stated in the
report rather than papered over.
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


client = open("frontend/src/api/client.ts").read()
chat = open("frontend/src/pages/Chat.tsx").read()

# --- every terminal path clears the loading state -------------------------
send = chat[chat.index("const send = async"):chat.index("const retry =")]
check("send() clears the pending/spinner state in a finally block",
      "finally {" in send and "setPending(null)" in send.split("finally {")[1])
check("the only setPending(null) on the send path is unconditional",
      send.count("setPending(null)") == 1)
check("send() catches errors rather than letting them escape", "catch (e)" in send)

# --- each response shape maps to a message --------------------------------
check("a transport failure is named for the user",
      "Cannot reach the server" in client)
check("an aborted (too slow) request is distinguished from being offline",
      "AbortError" in client and "took too long" in client)
check("a client-side ceiling exists for ask calls", "ASK_TIMEOUT_MS" in client)

m = re.search(r"ASK_TIMEOUT_MS\s*=\s*([0-9_]+)", client)
ask_ms = int(m.group(1).replace("_", "")) if m else 0
check("the client ceiling sits between the backend deadline and nginx",
      240_000 < ask_ms < 300_000, f"{ask_ms} ms")

check("a non-JSON error body (nginx HTML 502/504) falls back to a message",
      "isJson" in client and "httpFallback" in client)
check("a 2xx with an unreadable body raises instead of returning null",
      "unreadable response" in client)
check("5xx maps to a retry-able message",
      "The server had a problem" in client)
check("401 is surfaced as a session expiry", "session has expired" in client)
check("401 signs the user out rather than looping", "onSignedOut()" in chat)

# --- the question survives a failure --------------------------------------
check("a failed question is preserved", "setFailed({ question" in chat)
check("the preserved question is shown back to the user",
      "Your question was kept" in chat)
check("a retry action exists", "const retry =" in chat and ">Retry<" in chat)
check("retry re-sends the preserved question",
      "send(q)" in chat)
check("retry is not offered when files cannot be re-sent",
      "retryable: files.length === 0" in chat and "failed.retryable &&" in chat)
check("a successful send clears the failed state", "setFailed(null)" in chat)
check("dismissing the banner clears both error and failed state",
      "setError(null); setFailed(null)" in chat)

# --- no backend internals leak to the user --------------------------------
for leak in ("Traceback", "psycopg2", "anthropic", "SELECT "):
    check(f"the UI never renders {leak!r}", leak not in client and leak not in chat)

# --- the backend answers before nginx can time out ------------------------
import chat_sql  # noqa: E402
check("the backend deadline is under nginx's proxy_read_timeout",
      chat_sql.REQUEST_DEADLINE_SECONDS < 300)
nginx = open("systemd/nginx-intelligence.conf").read()
m = re.search(r"proxy_read_timeout\s+(\d+)", nginx)
check("nginx's /api timeout is unchanged at 300s (not raised as the fix)",
      m and int(m.group(1)) == 300, m.group(1) if m else "not found")

# --- it still compiles ----------------------------------------------------
r = subprocess.run(["npm", "run", "build"], cwd="frontend",
                   capture_output=True, text=True, timeout=600)
check("the frontend builds after the changes", r.returncode == 0,
      (r.stderr or r.stdout)[-200:] if r.returncode else "")

passed = sum(1 for _, ok, _ in results if ok)
failed = len(results) - passed
print(f"\n{'='*66}\nfrontend error-handling: {passed} passed, {failed} failed, {len(results)} total")
if failed:
    for n, ok, d in results:
        if not ok:
            print(f"  FAILED: {n} -- {d}")
sys.exit(1 if failed else 0)
