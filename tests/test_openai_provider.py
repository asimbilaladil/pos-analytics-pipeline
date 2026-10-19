#!/usr/bin/env python3
"""OpenAI provider alongside Claude: same loop, same gates, never empty.

Run:  venv/bin/python tests/test_openai_provider.py     (exit 0 = pass)

No network and no key: the REAL openai SDK runs against a mocked HTTP
transport, so every check sees the exact JSON body that would be sent to
api.openai.com and parses responses through the SDK's own types. SQL runs for
real, read-only, as the LLM role (laynes_ro) through chat_sql's validator and
scope gates -- the point is that an OpenAI tool call reaches the database by
exactly the same path as a Claude one.
"""
import json
import logging
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from dotenv import load_dotenv

load_dotenv(".env", override=True)
# Never reach api.openai.com from a test, whatever .env holds: the real key is
# dropped and any client that escapes the mocks hits an unroutable address.
os.environ["OPENAI_BASE_URL"] = "http://127.0.0.1:9/v1"
for _k in ("OPENAI_ENABLED", "OPENAI_API_KEY", "OPENAI_ALLOWED_MODELS",
           "OPENAI_DEFAULT_MODEL", "OPENAI_REASONING_EFFORT", "OPENAI_MAX_OUTPUT_TOKENS"):
    os.environ.pop(_k, None)

import httpx2  # noqa: E402
import openai  # noqa: E402

import chat_providers as P  # noqa: E402
import chat_sql as C  # noqa: E402
from backend import chat as chat_svc  # noqa: E402

results = []
FAKE_KEY = "test-placeholder-not-a-key"


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


def enable(models="gpt-6-luna,gpt-6-astra", default=None, **extra):
    os.environ.update(OPENAI_ENABLED="1", OPENAI_API_KEY=FAKE_KEY,
                      OPENAI_ALLOWED_MODELS=models)
    if default:
        os.environ["OPENAI_DEFAULT_MODEL"] = default
    os.environ.update(extra)
    P.reset_availability_cache()


def disable():
    for k in ("OPENAI_ENABLED", "OPENAI_API_KEY", "OPENAI_ALLOWED_MODELS",
              "OPENAI_DEFAULT_MODEL", "OPENAI_REASONING_EFFORT", "OPENAI_MAX_OUTPUT_TOKENS"):
        os.environ.pop(k, None)
    P.reset_availability_cache()


# ---------------------------------------------------------------------------
# Mock OpenAI HTTP API: scripted /responses replies, recorded request bodies.
class MockOpenAI:
    def __init__(self, script=(), available=("gpt-6-luna", "gpt-6-astra", "gpt-6-sol",
                                             "gpt-6.1-sol")):
        self.script, self.bodies, self.paths = list(script), [], []
        self.available = set(available)

    def handler(self, req):
        self.paths.append((req.method, req.url.path))
        if req.method == "GET" and "/models/" in req.url.path:
            mid = req.url.path.rsplit("/", 1)[1]
            if mid in self.available:
                return httpx2.Response(200, json={"id": mid, "object": "model",
                                                  "created": 1, "owned_by": "openai"})
            return httpx2.Response(404, json={"error": {"message": "no such model",
                                                        "type": "invalid_request_error"}})
        body = json.loads(req.content)
        self.bodies.append(body)
        nxt = self.script.pop(0) if self.script else R([msg("")])
        if isinstance(nxt, httpx2.Response):
            return nxt
        nxt = dict(nxt, model=body.get("model"))
        return httpx2.Response(200, json=nxt, headers={"x-request-id": "req_test"})

    def client(self, **_kw):
        return openai.OpenAI(api_key=FAKE_KEY, max_retries=0,
                             http_client=httpx2.Client(transport=httpx2.MockTransport(self.handler)))


_n = {"i": 0}


def _id(p):
    _n["i"] += 1
    return f"{p}_{_n['i']}"


def R(output, status="completed", incomplete=None, reasoning_tokens=7, cached_tokens=0):
    return {"id": _id("resp"), "object": "response", "created_at": 1, "status": status,
            "output": output, "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
            "incomplete_details": {"reason": incomplete} if incomplete else None, "error": None,
            "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
                      "input_tokens_details": {"cached_tokens": cached_tokens, "cache_write_tokens": 0},
                      "output_tokens_details": {"reasoning_tokens": reasoning_tokens}}}


def reasoning(enc="ENC"):
    return {"type": "reasoning", "id": _id("rs"), "summary": [], "encrypted_content": enc}


def fcall(call_id, name, args):
    return {"type": "function_call", "id": _id("fc"), "call_id": call_id, "name": name,
            "arguments": args if isinstance(args, str) else json.dumps(args),
            "status": "completed"}


def msg(t):
    return {"type": "message", "id": _id("msg"), "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": t, "annotations": []}]}


def sql_call(cid, sql, start=None, end=None, est=None):
    return fcall(cid, "run_sql", {"sql": sql, "establishment_id": est,
                                  "period_start": start, "period_end_exclusive": end})


_orig_client = P._openai_client


def run(question, script, model="gpt-6-luna", history=(), attachments=None, mock=None):
    mock = mock or MockOpenAI(script)
    P._openai_client = mock.client
    try:
        return C.answer_question(list(history), question, model=model,
                                 attachments=attachments), mock
    finally:
        P._openai_client = _orig_client


def outputs_for(body):
    return [i for i in body["input"] if isinstance(i, dict) and i.get("type") == "function_call_output"]


PERIOD_SQL = ("SELECT period_label FROM v_ayg_business_periods_llm "
              "WHERE fiscal_year = 2026 AND period_number = 10")

# ---------------------------------------------------------------------------
print("1. Configuration: disabled unless explicitly enabled and configured")
disable()
check("default: OpenAI not usable, no OpenAI options, Claude default",
      not P.openai_config().usable and chat_svc.openai_models() == []
      and chat_svc.model_options()["default"] == chat_svc.DEFAULT_MODEL
      and all(o["provider"] == "anthropic" for o in chat_svc.model_options()["options"]))
os.environ.update(OPENAI_API_KEY=FAKE_KEY, OPENAI_ALLOWED_MODELS="gpt-6-luna")
check("key + models but OPENAI_ENABLED unset -> still disabled", not P.openai_config().usable)
disable()
os.environ.update(OPENAI_ENABLED="1", OPENAI_ALLOWED_MODELS="gpt-6-luna")
check("enabled without a key -> disabled", not P.openai_config().usable)
enable("gpt-6-luna, gpt-9-imaginary, gpt-6-astra", default="gpt-6-astra")
cfg = P.openai_config()
check("unprofiled model ids are never offered (fail closed)",
      cfg.allowed == ("gpt-6-luna", "gpt-6-astra") and cfg.rejected == ("gpt-9-imaginary",))
check("OPENAI_DEFAULT_MODEL honoured when allowed", cfg.default == "gpt-6-astra")
enable("gpt-6-luna", default="gpt-6-astra")
check("OPENAI_DEFAULT_MODEL outside the allowlist is ignored", P.openai_config().default == "gpt-6-luna")

print("2. Availability is verified against the key, not assumed")
enable("gpt-6-luna,gpt-6-astra")
m = MockOpenAI(available=("gpt-6-luna",))
P._openai_client = m.client
try:
    avail = chat_svc.openai_models()
    avail2 = chat_svc.openai_models()
finally:
    P._openai_client = _orig_client
check("a model the key cannot see is not offered", avail == ["gpt-6-luna"], str(avail))
check("availability uses models.retrieve only (metadata, no generation)",
      all(meth == "GET" and "/models/" in path for meth, path in m.paths), str(m.paths))
check("availability is cached (no second round of lookups)", avail2 == avail and len(m.paths) == 2)
opts = chat_svc.model_options()
check("/api/models: Claude first and default; OpenAI grouped with provider + label",
      opts["default"] == chat_svc.DEFAULT_MODEL and opts["options"][0]["provider"] == "anthropic"
      and {"id": "gpt-6-luna", "label": "GPT-6 Luna", "provider": "openai"} in opts["options"]
      and [p["enabled"] for p in opts["providers"] if p["id"] == "openai"] == [True])
check("/api/models carries no key or secret material",
      FAKE_KEY not in json.dumps(opts) and "api_key" not in json.dumps(opts))

print("2b. OpenAI outage never blocks /api/models (Claude users included)")
import threading  # noqa: E402
import time as _time  # noqa: E402


class SlowOpenAI(MockOpenAI):
    """models.retrieve hangs (an outage), then fails like a timeout would."""
    def __init__(self, delay):
        super().__init__()
        self.delay, self.lookups = delay, 0

    def handler(self, req):
        if "/models/" in req.url.path:
            self.lookups += 1
            _time.sleep(self.delay)
            return httpx2.Response(503, json={"error": {"message": "overloaded", "type": "x"}})
        return super().handler(req)


enable("gpt-6-luna")
slow = SlowOpenAI(3.0)
P._openai_client = slow.client
try:
    # Known-good but expired: served at once, refreshed in the background.
    with P._avail_lock:
        P._avail_cache["gpt-6-luna"] = (True, _time.monotonic() - 1)
    durs, outs = [], []

    def hit():
        t0 = _time.perf_counter()
        outs.append(chat_svc.model_options())
        durs.append(_time.perf_counter() - t0)
    ths = [threading.Thread(target=hit) for _ in range(8)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    check("stale cache + hanging OpenAI: 8 concurrent /api/models answer immediately",
          max(durs) < 0.5, f"max {max(durs):.2f}s")
    check("... with the last known answer (GPT still listed) and Claude intact",
          all("gpt-6-luna" in o["models"] and o["default"] == chat_svc.DEFAULT_MODEL
              and "claude-sonnet-5" in o["models"] for o in outs))
    P._avail_refresh["thread"].join(10)
    check("... and ONE background refresh (single-flight), not one per caller",
          slow.lookups == 1, str(slow.lookups))
    check("a failed refresh marks the model unavailable (no GPT offered during the outage)",
          chat_svc.openai_models() == [] and "gpt-6-luna" not in chat_svc.model_options()["models"])
    # Cold cache (nothing known) + hanging OpenAI: bounded wait, then Claude-only.
    P.reset_availability_cache()
    t0 = _time.perf_counter()
    cold = chat_svc.model_options()
    cold_s = _time.perf_counter() - t0
    check("cold cache + hanging OpenAI: bounded by the cold wait, Claude still offered",
          cold_s < P.AVAIL_COLD_WAIT_SECONDS + 0.5 and "gpt-6-luna" not in cold["models"]
          and cold["default"] == chat_svc.DEFAULT_MODEL, f"{cold_s:.2f}s")
    t0 = _time.perf_counter()
    again = chat_svc.model_options()
    check("... and callers after the cold wait do not wait again for the same refresh",
          _time.perf_counter() - t0 < 0.5 and "gpt-6-luna" not in again["models"])
    check("submitting a Claude model never consults OpenAI availability",
          chat_svc.resolve_model("claude-opus-5", strict=True) == "claude-opus-5")
finally:
    P.reset_availability_cache()
    P._openai_client = _orig_client
fast = MockOpenAI()
P._openai_client = fast.client
try:
    P.warm_availability()
    P._avail_refresh["thread"].join(10)
    check("startup warm-up fills the cache in the background (no request waits)",
          chat_svc.openai_models() == ["gpt-6-luna"] and len(fast.paths) == 1)
finally:
    P._openai_client = _orig_client
api_src = open("backend/api.py").read()
check("API startup warms OpenAI availability", "chat_svc.warm_models()" in api_src)

print("3. Model selection is validated server-side")
enable("gpt-6-luna,gpt-6-astra")
P._openai_client = MockOpenAI(available=("gpt-6-luna",)).client
try:
    chat_svc.openai_models()                       # luna available, astra not
finally:
    P._openai_client = _orig_client
check("Claude ids unchanged", chat_svc.resolve_model("claude-opus-5", strict=True) == "claude-opus-5")
check("unknown / retired Claude id -> default (unchanged behaviour)",
      chat_svc.resolve_model("claude-sonnet-4", strict=True) == chat_svc.DEFAULT_MODEL)
check("available OpenAI id accepted", chat_svc.resolve_model("gpt-6-luna", strict=True) == "gpt-6-luna")
for bad in ("gpt-6-astra", "gpt-4o", "gpt-9-imaginary"):
    try:
        chat_svc.resolve_model(bad, strict=True)
        ok = False
    except chat_svc.ModelUnavailable:
        ok = True
    check(f"unavailable OpenAI id {bad!r} is refused, never re-routed to Claude", ok)
check("non-strict paths (new conversation / import) fall back to the default",
      chat_svc.resolve_model("gpt-9-imaginary") == chat_svc.DEFAULT_MODEL)
check("provider_of", chat_svc.provider_of("gpt-6-luna") == "openai"
      and chat_svc.provider_of("claude-sonnet-5") == "anthropic")
disable()
try:
    C.answer_question([], "x", model="gpt-6-luna")
    ok, code = False, None
except P.ProviderError as e:
    ok, code = True, e.code
check("a job whose provider is disabled fails visibly (provider_disabled), never answered by Claude",
      ok and code == "provider_disabled")
ans, steps, _d, err = chat_svc.ask([], "x", "gpt-6-luna")
check("... surfaced as a user-safe answer + error code",
      "not enabled" in ans and chat_svc.failure_code(err) == "provider_disabled", f"{ans} | {err}")
enable("gpt-6-luna,gpt-6-astra,gpt-6-sol,gpt-6.1-sol")

# ---------------------------------------------------------------------------
print("4. Claude path unchanged")
prov = P.make_provider("claude-sonnet-5", [C.CHECK_DATA_TOOL, C.RUN_SQL_TOOL],
                       anthropic_timeout=C.ANTHROPIC_TIMEOUT_SECONDS,
                       anthropic_max_retries=C.ANTHROPIC_MAX_RETRIES)
check("Claude ids route to the Anthropic provider even with OpenAI enabled",
      isinstance(prov, P.ClaudeProvider))
calls = []


class FakeAnthropic:
    def __init__(self, script):
        self.script = list(script)
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kw):
        calls.append(kw)
        return self.script.pop(0)


def aresp(stop, *blocks):
    return SimpleNamespace(stop_reason=stop, content=list(blocks),
                           usage=SimpleNamespace(input_tokens=1, output_tokens=1))


_orig_anth = C.anthropic.Anthropic
C.anthropic.Anthropic = lambda **kw: FakeAnthropic([
    aresp("tool_use", SimpleNamespace(type="tool_use", id="t1", name="run_sql",
                                      input={"sql": PERIOD_SQL})),
    aresp("end_turn", SimpleNamespace(type="text", text="Claude answer."))])
try:
    r = C.answer_question([{"role": "user", "content": "earlier"},
                           {"role": "assistant", "content": "GPT wrote this earlier."}],
                          "Which period is P10?", model="claude-sonnet-5")
finally:
    C.anthropic.Anthropic = _orig_anth
check("Claude loop call: same kwargs as before (model, 4096, system, both tools, messages)",
      sorted(calls[0]) == ["max_tokens", "messages", "model", "system", "tools"]
      and calls[0]["max_tokens"] == 4096
      and [t["name"] for t in calls[0]["tools"]] == ["check_data", "run_sql"]
      and calls[0]["tools"][0] is C.CHECK_DATA_TOOL)
check("Claude tool_result shape unchanged",
      calls[1]["messages"][-1]["role"] == "user"
      and calls[1]["messages"][-1]["content"][0]["type"] == "tool_result"
      and calls[1]["messages"][-1]["content"][0]["tool_use_id"] == "t1"
      and "is_error" not in calls[1]["messages"][-1]["content"][0])
check("Claude answers a conversation that GPT answered earlier (history is plain text)",
      r.answer == "Claude answer." and calls[0]["messages"][1] ==
      {"role": "assistant", "content": "GPT wrote this earlier."})
check("result records provider and usage", r.provider == "anthropic" and r.usage.get("calls") == 2)

# ---------------------------------------------------------------------------
print("5. OpenAI normal response")
r, m = run("Which period is P10?", [R([reasoning(), msg("P10 runs Sep 10 - Oct 7.")])])
b = m.bodies[0]
check("plain answer, one call", r.answer == "P10 runs Sep 10 - Oct 7." and len(m.bodies) == 1)
check("Responses API: stateless (store=false), encrypted reasoning requested",
      b["store"] is False and b["include"] == ["reasoning.encrypted_content"]
      and "previous_response_id" not in b)
check("instructions = the same system prompt Claude gets",
      C._system_static() in b["instructions"] and "FINANCIAL_WASTE_REVIEW" not in b["instructions"])
check("only the two application function tools; no hosted tools",
      [(t["type"], t["name"]) for t in b["tools"]] == [("function", "check_data"), ("function", "run_sql")]
      and b["tools"][1]["parameters"] == C.RUN_SQL_TOOL["input_schema"])
check("prompt cache key is static and carries no user data",
      b["prompt_cache_key"] == P.OPENAI_PROMPT_CACHE_KEY == "laynes-chat-v1")
check("loop call: low reasoning effort and the provider's own output budget",
      b["reasoning"] == {"effort": "low"} and b["max_output_tokens"] == P.OPENAI_LOOP_MAX_OUTPUT_TOKENS
      and "temperature" not in b)
check("result records provider and token usage",
      r.provider == "openai" and r.usage["input_tokens"] == 100 and r.usage["reasoning_tokens"] == 7)

print("6. Tool-call round trip (real SQL through the real gates)")
r, m = run("Which period is P10?", [
    R([reasoning("ENC-A"), sql_call("call_a", PERIOD_SQL)]),
    R([msg("It is P10.")])])
b2 = m.bodies[1]
outs = outputs_for(b2)
check("function call executed through run_sql and answered", r.answer == "It is P10."
      and any(s.get("sql") == PERIOD_SQL and s["row_count"] == 1 for s in r.steps))
check("function_call_output carries the SAME call_id and the rows",
      len(outs) == 1 and outs[0]["call_id"] == "call_a" and '"P10"' in outs[0]["output"])
kinds = [i.get("type") for i in b2["input"] if isinstance(i, dict) and "type" in i]
check("reasoning (encrypted) and function_call items are passed back verbatim, before the output",
      kinds == ["reasoning", "function_call", "function_call_output"]
      and any(i.get("encrypted_content") == "ENC-A" for i in b2["input"] if isinstance(i, dict))
      and all("status" not in i for i in b2["input"] if isinstance(i, dict) and i.get("type")))

print("7. Multiple tool calls in one response")
r, m = run("Which period is P10?", [
    R([reasoning(), sql_call("c1", PERIOD_SQL),
       fcall("c2", "check_data", {"establishment_id": None, "period_start": "2026-09-10",
                                   "period_end": "2026-10-08", "domains": ["hme"]}),
       sql_call("c3", "SELECT * FROM app_users")]),
    R([msg("done")])])
outs = outputs_for(m.bodies[1])
check("every call answered, in order, with its own call_id",
      [o["call_id"] for o in outs] == ["c1", "c2", "c3"])
check("check_data ran the real HME profile", '"domains":["hme"]' in outs[1]["output"]
      or '"hme"' in outs[1]["output"], outs[1]["output"][:120])
check("SECURITY: a forbidden relation is refused by the same validator (not executed)",
      outs[2]["output"].startswith(("ERROR", "REJECTED"))
      and any(s.get("sql") == "SELECT * FROM app_users" and s["error"] and s["row_count"] is None
              for s in r.steps), outs[2]["output"][:120])

print("8. SQL security and trust gates apply unchanged")
for label, sql, start, end in (
        ("DML", "DELETE FROM chat_messages", None, None),
        ("multi-statement", "SELECT 1; SELECT 2", None, None),
        ("raw R365 table", "SELECT * FROM r365_financial_period_expense", None, None),
        ("pg catalog", "SELECT * FROM pg_shadow", None, None),
        ("unbounded R365 detail", "SELECT store_name, SUM(expense_amount) FROM "
         "v_r365_financial_transaction_llm GROUP BY store_name", "2026-09-10", "2026-10-08")):
    r, m = run("security probe", [R([sql_call("x", sql, start, end)]), R([msg("refused")])])
    st = [s for s in r.steps if s.get("sql") == sql]
    check(f"OpenAI-issued {label} is refused before execution",
          st and st[0]["error"] and st[0]["row_count"] is None, (st[0]["error"] or "")[:90] if st else "")

print("9. Malformed or truncated tool calls are never executed")
r, m = run("Which period is P10?", [R([fcall("bad", "run_sql", "{not json")]), R([msg("ok")])])
outs = outputs_for(m.bodies[1])
check("invalid JSON arguments -> error output, nothing executed",
      outs and outs[0]["call_id"] == "bad" and outs[0]["output"].startswith("ERROR")
      and not any(s.get("sql") for s in r.steps))
r, m = run("Which period is P10?", [R([fcall("u", "drop_tables", {})]), R([msg("ok")])])
check("unknown tool name -> error output (every call answered)",
      outputs_for(m.bodies[1])[0]["output"].startswith("ERROR: unknown tool"))
r, m = run("Which period is P10?", [
    R([reasoning(), sql_call("cut", PERIOD_SQL)], status="incomplete", incomplete="max_output_tokens"),
    R([msg("Synthesised.")])])
check("incomplete (max_output_tokens) mid tool call -> no SQL executed, forced synthesis",
      not any(s.get("sql") for s in r.steps) and r.answer == "Synthesised."
      and any(s.get("forced_synthesis") == "max_tokens" for s in r.steps))

# ---------------------------------------------------------------------------
print("10. Never-empty: reasoning-only, incomplete, empty synthesis")
r, m = run("Which period is P10?", [R([reasoning()], status="incomplete", incomplete="max_output_tokens"),
                                    R([msg("Final from evidence.")])])
fb = m.bodies[-1]
check("reasoning-only + max_output_tokens -> one forced synthesis",
      len(m.bodies) == 2 and r.answer == "Final from evidence.")
check("synthesis: tool_choice none, lowest effort for the model (luna: none), 4096 budget",
      fb["tool_choice"] == "none" and fb["reasoning"] == {"effort": "none"}
      and fb["max_output_tokens"] == P.OPENAI_FINAL_MAX_OUTPUT_TOKENS)
check("synthesis instruction forbids further checking",
      "You cannot run SQL any more" in fb["input"][-1]["content"])
r, m = run("Which period is P10?", [R([reasoning()], status="incomplete", incomplete="max_output_tokens"),
                                    R([msg("ok")])], model="gpt-6-astra")
check("astra (no 'none' support) synthesises at 'low', never an unsupported effort",
      m.bodies[-1]["reasoning"] == {"effort": "low"})
r, m = run("Which period is P10?", [R([reasoning()], status="incomplete", incomplete="max_output_tokens"),
                                    R([msg("ok")])], model="gpt-6.1-sol")
check("6.1 sol synthesises at 'low'", m.bodies[-1]["reasoning"] == {"effort": "low"})
r, m = run("Which period is P10?", [R([reasoning()]), R([msg("From evidence.")])])
check("completed with reasoning only (no text) is not an answer -> synthesis",
      r.answer == "From evidence." and len(m.bodies) == 2)
r, m = run("Which period is P10?", [R([reasoning()]), R([reasoning()]), R([msg("")])])
check("synthesis empty twice -> explicit fallback, never \"\"",
      r.answer == C.EMPTY_ANSWER_FALLBACK and len(m.bodies) == 3, r.answer)
r, m = run("Which period is P10?", [R([reasoning()], status="incomplete", incomplete="max_output_tokens"),
                                    R([msg("Partial report")], status="incomplete",
                                      incomplete="max_output_tokens")])
check("a synthesis cut at the limit says so",
      r.answer.startswith("Partial report") and "shortened at the length limit" in r.answer)
r, m = run("Which period is P10?", [R([msg("")], status="incomplete", incomplete="content_filter"),
                                    R([msg("")], status="incomplete", incomplete="content_filter"),
                                    R([msg("")], status="incomplete", incomplete="content_filter")])
check("content_filter incomplete -> fallback, never empty", r.answer == C.EMPTY_ANSWER_FALLBACK)
r, m = run("Which period is P10?",
           [R([sql_call(f"l{i}", PERIOD_SQL)]) for i in range(C.MAX_LOOPS)] + [R([msg("Loop answer.")])])
check("MAX_LOOPS unchanged and enforced for OpenAI -> synthesis from evidence",
      C.MAX_LOOPS == 6 and len(m.bodies) == C.MAX_LOOPS + 1 and r.answer == "Loop answer."
      and m.bodies[-1]["tool_choice"] == "none")
os.environ["OPENAI_REASONING_EFFORT"] = "none"
r, m = run("x", [R([msg("ok")])], model="gpt-6-astra")
check("an effort override the model does not support is ignored (astra stays 'low')",
      m.bodies[0]["reasoning"] == {"effort": "low"})
r, m = run("x", [R([msg("ok")])], model="gpt-6-luna")
check("... and applied where supported", m.bodies[0]["reasoning"] == {"effort": "none"})
os.environ.pop("OPENAI_REASONING_EFFORT")

# ---------------------------------------------------------------------------
print("11. API failures: recoverable, user-safe, no secrets")


def err(status, message):
    return httpx2.Response(status, json={"error": {"message": message, "type": "x", "code": "x"}},
                           headers={"x-request-id": "req_err"})


leak = "Incorrect API key provided: sk-proj-abcd****wxyz"
for status, code in ((401, "provider_auth"), (403, "provider_auth"), (429, "provider_rate_limited"),
                     (404, "model_unavailable"), (400, "model_unavailable"), (500, "provider_error")):
    mock = MockOpenAI([err(status, leak)])
    P._openai_client = mock.client
    try:
        ans, steps, _d, e = chat_svc.ask([], "Which period is P10?", "gpt-6-luna")
    finally:
        P._openai_client = _orig_client
    check(f"HTTP {status} -> {code}, safe message, no key fragment logged",
          chat_svc.failure_code(e) == code and "sk-" not in e and "abcd" not in e
          and "sk-" not in ans and "OpenAI" in ans and "request_id=req_err" in e, f"{e} | {ans[:60]}")


class Boom:
    def __init__(self, exc):
        self.exc = exc

    def client(self, **_kw):
        def h(req):
            raise self.exc
        return openai.OpenAI(api_key=FAKE_KEY, max_retries=0,
                             http_client=httpx2.Client(transport=httpx2.MockTransport(h)))


for exc, code in ((httpx2.ReadTimeout("t"), "provider_timeout"),
                  (httpx2.ConnectError("c"), "provider_unreachable")):
    P._openai_client = Boom(exc).client
    try:
        ans, steps, _d, e = chat_svc.ask([], "Which period is P10?", "gpt-6-luna")
    finally:
        P._openai_client = _orig_client
    check(f"{type(exc).__name__} -> {code}", chat_svc.failure_code(e) == code, e)
r_fail = R([], status="failed")
r_fail["error"] = {"code": "server_error", "message": "boom"}
mock = MockOpenAI([r_fail])
P._openai_client = mock.client
try:
    ans, steps, _d, e = chat_svc.ask([], "Which period is P10?", "gpt-6-luna")
finally:
    P._openai_client = _orig_client
check("status=failed body -> provider_error, readable message",
      chat_svc.failure_code(e) == "provider_error" and "OpenAI" in ans, e)
check("SDK retry bound is the same as Claude's (1 retry, 90 s)",
      P.OPENAI_MAX_RETRIES == C.ANTHROPIC_MAX_RETRIES and P.OPENAI_TIMEOUT_SECONDS == 90)
check("redact() strips key-shaped strings", "sk-" not in P.redact("x sk-proj-abcdEFGH1234 y"))

# ---------------------------------------------------------------------------
print("12. Cross-provider continuity and attachments")
hist = [{"role": "user", "content": "What was P9 electricity?"},
        {"role": "assistant", "content": "Claude said: P9 electricity was ..."}]
r, m = run("And P10?", [R([msg("GPT continues.")])], history=hist)
check("OpenAI receives Claude's earlier answers as plain role/content items",
      m.bodies[0]["input"][:2] == hist and m.bodies[0]["input"][2] == {"role": "user", "content": "And P10?"})
att = [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
       {"type": "document", "title": "a.pdf", "source": {"type": "base64",
                                                         "media_type": "application/pdf", "data": "BBBB"}},
       {"type": "document", "title": "n.csv", "source": {"type": "text", "media_type": "text/plain",
                                                         "data": "a,b\n1,2"}}]
r, m = run("Read these", [R([msg("ok")])], attachments=att)
parts = m.bodies[0]["input"][-1]["content"]
check("attachments converted to Responses input parts (image / file / text) + question last",
      [p["type"] for p in parts] == ["input_image", "input_file", "input_text", "input_text"]
      and parts[0]["image_url"] == "data:image/png;base64,AAAA"
      and parts[1]["file_data"] == "data:application/pdf;base64,BBBB"
      and "a,b" in parts[2]["text"] and parts[3]["text"] == "Read these")

# ---------------------------------------------------------------------------
print("13. R365 financial questions through OpenAI (electricity, gas, water, rent, R&M)")
P9S, P10S, P10E = "2026-08-13", "2026-09-10", "2026-10-08"
for cat in ("ELECTRICITY", "GAS", "WATER", "RENT", "REPAIRS_MAINTENANCE"):
    # The prompt's own few-shot shape: the period is bounded by period_start,
    # matching the declared scope the gate reconciles.
    sql = ("SELECT store_name, expense_amount, has_posting FROM v_r365_financial_period_llm "
           f"WHERE analytics_category = '{cat}' AND fiscal_year = 2026 AND period_number = 10 "
           f"AND period_start = '{P10S}' ORDER BY has_posting DESC, expense_amount DESC")
    r, m = run(f"How much {cat.lower()} did each store spend in P10?", [
        R([reasoning(), fcall("chk", "check_data", {"establishment_id": None, "period_start": P10S,
                                                     "period_end": P10E,
                                                     "domains": ["r365_financial"]}),
           sql_call("q", sql, P10S, P10E)]),
        R([msg(f"{cat} answer.")])])
    outs = outputs_for(m.bodies[1])
    st = [s for s in r.steps if s.get("sql") == sql]
    check(f"{cat}: gated (r365_financial) and executed as laynes_ro",
          st and not st[0]["error"] and st[0]["row_count"] > 0
          and st[0].get("domains") == ["r365_financial"], str(st[0] if st else r.steps)[:160])
    payload = json.loads(outs[1]["output"])
    if "r365_financial_note" in payload:
        check(f"{cat}: missing-posting wording travels with the rows (not $0)",
              "NO POSTING is not $0" in payload["r365_financial_note"])
DETAIL = ("SELECT store_name, accounting_date, transaction_type, expense_amount "
          "FROM v_r365_financial_transaction_llm WHERE analytics_category = 'ELECTRICITY' "
          f"AND accounting_date >= '{P9S}' AND accounting_date < '{P10E}' ORDER BY accounting_date")
r, m = run("Show the electricity transactions in P9 and P10", [
    R([sql_call("d", DETAIL, P9S, P10E)]), R([msg("Detail answer.")])])
st = [s for s in r.steps if s.get("sql") == DETAIL]
out = json.loads(outputs_for(m.bodies[1])[0]["output"])
check("transaction drill-down: date-bounded detail executes",
      st and not st[0]["error"] and st[0]["row_count"] > 0)
check("transaction drill-down: detail rule (no posting != $0) attached",
      "NO POSTING" in out.get("r365_financial_note", ""), out.get("r365_financial_note", "")[:80])

print("14. Revel and HME through OpenAI")
r, m = run("How many drive-thru cars at all stores yesterday?", [
    R([fcall("h", "check_data", {"establishment_id": None, "period_start": P10S,
                                  "period_end": P10E, "domains": ["hme"]})]),
    R([msg("HME answer.")])])
hme = json.loads(outputs_for(m.bodies[1])[0]["output"])
check("HME check_data returns the HME-only profile (not blocked by Revel)",
      hme.get("domains") == ["hme"] and "analysis_permitted" in hme, str(hme.get("domains")))
r, m = run("Net sales by store in P10", [
    R([fcall("rv", "check_data", {"establishment_id": None, "period_start": P10S,
                                   "period_end": P10E, "domains": ["revel"]})]),
    R([msg("Revel answer.")])])
rv = json.loads(outputs_for(m.bodies[1])[0]["output"])
check("Revel check_data runs the reconciliation gate", "reconciliation" in rv
      and rv["reconciliation"]["status"] in ("PASS", "FAIL", "WARN"), str(rv.get("reconciliation", {}))[:80])
UNSCOPED = "SELECT count(*) FROM orders_v2"
r, m = run("How many orders?", [R([sql_call("o", UNSCOPED)]), R([msg("x")])])
st = [s for s in r.steps if s.get("sql") == UNSCOPED]
check("Revel transactional SQL without a declared scope is rejected (A12 gate)",
      st and st[0]["error"] and st[0]["row_count"] is None, (st[0]["error"] or "")[:90] if st else "")

# ---------------------------------------------------------------------------
print("15. FINANCIAL_WASTE_REVIEW through OpenAI: 3-query budget, targets, blocked evidence")
EXACT = ("10 numbers were lackluster at Best help me analyze the financial data to find "
         "financial waste. I think the team is working on food and labor so focus your "
         "attention on everything else we’re looking for utilities. We’re looking at "
         "any other supplies. Maintenance do a deep dive at every category across every "
         "store ask me any questions as necessary to clarify Store FootPrinter any other "
         "information that you would need we want you to be a McKenzie cost cutting "
         "specialist so we can create 2 to 3 points of margin.")
w = C.fin_review_window(EXACT)
TPL = C.fin_review_queries(w)
s, e = w["start"], w["end_exclusive"]
r, m = run(EXACT, [R([reasoning(), sql_call("q1", TPL["Q1"], s, e), sql_call("q2", TPL["Q2"], s, e)]),
                   R([sql_call("q3", PERIOD_SQL), sql_call("q4", PERIOD_SQL)]),
                   R([msg("Review answer.")])])
executed = [st for st in r.steps if st.get("sql") and not st.get("error")]
skipped = [st for st in r.steps if st.get("error") == "not executed: review tool budget reached"]
check("review: at most 3 run_sql executed, 4th refused", len(executed) == 3 and len(skipped) == 1)
check("review: then one synthesis call that cannot call tools",
      len(m.bodies) == 3 and m.bodies[-1]["tool_choice"] == "none"
      and C.FIN_REVIEW_SYNTHESIS_RULES in m.bodies[-1]["input"][-1]["content"])
check("review: plan in the instructions of every call",
      all("FINANCIAL_WASTE_REVIEW" in bd["instructions"] for bd in m.bodies))
check("review: food and labor excluded in the plan", "FOOD (all Cost of Goods Sold accounts"
      in m.bodies[0]["instructions"] and "LABOR (Labor accounts and 5500 payroll accounts)"
      in m.bodies[0]["instructions"])
check("review: answer carries the SQL targets and the not-confirmed-savings sentence",
      all(x in r.answer for x in ("$41,786.26", "$83,572.52", "$125,358.78", "working hypothesis",
                                  "It is not confirmed savings.")), r.answer[-200:])
Q3_BAD = TPL["Q3"].replace("AND (<PAIRS>)", "").replace(
    f"WHERE accounting_date >= '{s}' AND accounting_date < '{e}'\n", "WHERE TRUE\n")
r, m = run(EXACT, [R([sql_call("q1", TPL["Q1"], s, e), sql_call("q2", TPL["Q2"], s, e)]),
                   R([sql_call("q3", Q3_BAD, s, e)]),
                   R([reasoning()], status="incomplete", incomplete="max_output_tokens"),
                   R([msg("Review from Q1 and Q2; drill-down not completed.")])])
done = [st for st in r.steps if st.get("sql") and not st.get("error")]
bad = [st for st in r.steps if st.get("sql") == Q3_BAD]
synth = [bd for bd in m.bodies if bd.get("tool_choice") == "none"]
sent = json.dumps(synth[0]["input"]) if synth else ""
check("blocked: unbounded Q3 rejected by the gate, Q1 + Q2 kept as evidence",
      len(done) == 2 and bad and bad[0]["error"] and "one_margin_point" in sent
      and "scenario_outliers_to_median_excl_rent" in sent)
check("rejected Q3 counts against the budget -> review synthesis with the review rules "
      "(same path as Claude)",
      synth and C.FIN_REVIEW_SYNTHESIS_RULES in synth[0]["input"][-1]["content"])
check("blocked: reasoning-only synthesis retried once, then a visible answer with targets",
      len(synth) == 2 and r.answer.startswith("Review from Q1 and Q2") and "$83,572.52" in r.answer)

HVAC_BOUNDED = ("SELECT store_name, SUM(expense_amount) AS total FROM v_r365_financial_transaction_llm "
                f"WHERE gl_account_number = '6000-14' AND accounting_date >= '{s}' "
                f"AND accounting_date < '{e}' GROUP BY store_name")
HVAC_UNBOUNDED = ("SELECT store_name, SUM(expense_amount) AS total FROM "
                  "v_r365_financial_transaction_llm WHERE gl_account_number = '6000-14' "
                  "GROUP BY store_name")
r, m = run("Which stores spent most on HVAC repairs in P9 and P10?", [
    R([fcall("c1", "check_data", {"establishment_id": None, "period_start": s, "period_end": e,
                                   "domains": ["r365_financial_detail"]}),
       sql_call("b", HVAC_BOUNDED, s, e)]),
    R([sql_call("u", HVAC_UNBOUNDED, s, e)]),
    R([reasoning()], status="incomplete", incomplete="max_output_tokens"),
    R([msg("Answer from the bounded HVAC rows.")])])
synth = [bd for bd in m.bodies if bd.get("tool_choice") == "none"]
verdict = synth[0]["input"][-1]["content"] if synth else ""
rej = [st for st in r.steps if st.get("sql") == HVAC_UNBOUNDED]
check("blocked path: unbounded drill-down rejected, never executed",
      rej and "explicit date bounds" in rej[0]["error"] and rej[0]["row_count"] is None)
check("blocked path: verdict authoritative, source PASSED, earlier rows kept",
      "DATA TRUST VERDICT (authoritative, do not re-check)" in verdict
      and '"source_trusted":true' in verdict and "failed its trust gate" not in verdict
      and "LCF Missouri City" in json.dumps(synth[0]["input"]))
check("blocked path: same helper -> retry once -> visible answer",
      len(synth) == 2 and r.answer == "Answer from the bounded HVAC rows."
      and any(st.get("forced_synthesis") == "blocked" for st in r.steps))

# ---------------------------------------------------------------------------
print("16. Observability: one payload-free timing line with provider, model and usage")
records = []


class H(logging.Handler):
    def emit(self, rec):
        records.append(rec.getMessage())


C.log.addHandler(H())
C.log.setLevel(logging.INFO)
r, m = run("Which period is P10?", [R([sql_call("a", PERIOD_SQL)]),
                                    R([msg("x")], cached_tokens=60)])
line = [x for x in records if x.startswith("chat_timing")][-1]
check("cached input tokens are accounted (cost is not overstated)",
      r.usage["cached_tokens"] == 60 and "cached_tokens=60" in line, str(r.usage))
check("timing line names provider, model and token usage",
      "provider=openai" in line and "model=gpt-6-luna" in line and "input_tokens=200" in line
      and "reasoning_tokens=14" in line and "openai.responses.create[0]" in line, line[:200])
check("timing line carries no SQL, rows, question or key",
      PERIOD_SQL not in line and "P10" not in line and FAKE_KEY not in line)

disable()
failed = [x for x in results if not x[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
