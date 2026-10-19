"""Model providers for the analytics assistant: Anthropic Claude and OpenAI.

A provider is ONLY the transport to a model. Everything that decides what data
may be read -- the check_data / run_sql tools, the pglast validator, the
relation allowlist, the laynes_ro role, enforce_scope and the domain trust
gates, FINANCIAL_WASTE_REVIEW's 3-query budget, the blocked-path verdict and
forced synthesis -- lives in chat_sql.answer_question and runs unchanged for
both providers. Neither provider gets a database tool of its own, a hosted
tool (web search, code execution, file search) or any route to SQL except the
two function tools that chat_sql executes itself.

The loop talks to a provider through five operations:

    start(history, question, attachments) -> transcript (provider-shaped list)
    turn(system, transcript)              -> Turn  (tools on)
    final(system, transcript)             -> Turn  (tools off, low/no reasoning)
    add_assistant(transcript, turn)
    add_tool_results(transcript, [(call_id, content, is_error), ...])

A plain {"role": "user", "content": str} item is valid for both APIs, so the
shared forced-synthesis helper appends its instruction the same way for both.

ClaudeProvider issues exactly the calls answer_question made before this
module existed (same kwargs, same message shapes).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field

import anthropic

try:                                  # optional: Claude must keep working without it
    import openai
except ImportError:                   # pragma: no cover - exercised by config tests
    openai = None

log = logging.getLogger("laynes.chat")

PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OPENAI = "openai"
PROVIDER_LABELS = {PROVIDER_ANTHROPIC: "Claude — Anthropic",
                   PROVIDER_OPENAI: "GPT — OpenAI"}

# Same bounds and rationale as the Anthropic client (see chat_sql: 240 s request
# deadline, nginx 300 s): one hung call must stay recoverable inside the
# deadline, and one SDK retry absorbs a transient 429/5xx. Retries are of a
# single failed HTTP call -- never of a completed model/tool step.
OPENAI_TIMEOUT_SECONDS = float(os.getenv("CHAT_OPENAI_TIMEOUT_SECONDS", "90"))
OPENAI_MAX_RETRIES = int(os.getenv("CHAT_OPENAI_MAX_RETRIES", "1"))
CLAUDE_MAX_TOKENS = 4096


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict
    bad_args: str | None = None       # set when the arguments were not a JSON object


@dataclass
class Turn:
    stop: str                         # "tool_use" | "end_turn" | "max_tokens" | other
    text: str
    calls: list[ToolCall] = field(default_factory=list)
    raw: object = None
    truncated: bool = False           # output budget ran out (visible text may be partial)


class ProviderError(Exception):
    """A provider failed in a way the user should hear about plainly."""

    def __init__(self, code: str, user_message: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code
        self.user_message = user_message


def _int(v) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


def _add_usage(usage: dict, input_tokens=0, output_tokens=0, reasoning_tokens=0,
               cached_tokens=0):
    """Per-request token totals for the timing log. cached_tokens is the part
    of input_tokens served from the provider's prompt cache (billed lower), so
    cost can be estimated from the log without overstating it."""
    usage["input_tokens"] = usage.get("input_tokens", 0) + _int(input_tokens)
    usage["output_tokens"] = usage.get("output_tokens", 0) + _int(output_tokens)
    if _int(reasoning_tokens):
        usage["reasoning_tokens"] = usage.get("reasoning_tokens", 0) + _int(reasoning_tokens)
    usage["cached_tokens"] = usage.get("cached_tokens", 0) + _int(cached_tokens)
    usage["calls"] = usage.get("calls", 0) + 1


# ── Anthropic ────────────────────────────────────────────────────────────────
class ClaudeProvider:
    name = PROVIDER_ANTHROPIC
    call_label = "anthropic.messages.create"
    final_label = "anthropic.forced_synthesis"

    def __init__(self, model: str, tools: list[dict], timeout: float, max_retries: int):
        self.model = model
        self.tools = tools
        self.usage: dict = {}
        # Explicit bounds: the SDK defaults (600 s read, 2 retries) can outlive
        # nginx several times over without the application noticing.
        self.client = anthropic.Anthropic(  # ANTHROPIC_API_KEY from env
            timeout=timeout, max_retries=max_retries)

    def start(self, history, question, attachments):
        messages: list = [{"role": m["role"], "content": m["content"]} for m in history]
        if attachments:
            messages.append({"role": "user", "content": [
                *attachments, {"type": "text", "text": question}]})
        else:
            messages.append({"role": "user", "content": question})
        return messages

    def _record(self, resp):
        u = getattr(resp, "usage", None)
        if u is not None:
            # Anthropic reports cache reads separately from input_tokens.
            _add_usage(self.usage, getattr(u, "input_tokens", 0),
                       getattr(u, "output_tokens", 0),
                       cached_tokens=getattr(u, "cache_read_input_tokens", 0))

    @staticmethod
    def _text(resp) -> str:
        return "".join(b.text for b in resp.content if b.type == "text")

    def turn(self, system, messages) -> Turn:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=CLAUDE_MAX_TOKENS,
            system=system,
            tools=self.tools,
            messages=messages,
        )
        self._record(resp)
        calls = [ToolCall(b.id, b.name, dict(b.input)) for b in resp.content
                 if b.type == "tool_use"]
        return Turn(stop=str(resp.stop_reason), text=self._text(resp), calls=calls,
                    raw=resp, truncated=resp.stop_reason == "max_tokens")

    def final(self, system, messages) -> Turn:
        # WITHOUT tools and without thinking, so the whole output budget goes to
        # the answer.
        resp = self.client.messages.create(model=self.model, max_tokens=CLAUDE_MAX_TOKENS,
                                           system=system, messages=messages,
                                           thinking={"type": "disabled"})
        self._record(resp)
        return Turn(stop=str(resp.stop_reason), text=self._text(resp), raw=resp,
                    truncated=resp.stop_reason == "max_tokens")

    def add_assistant(self, messages, turn: Turn) -> None:
        messages.append({"role": "assistant", "content": turn.raw.content})

    def add_tool_results(self, messages, results) -> None:
        out = []
        for call_id, content, is_error in results:
            r = {"type": "tool_result", "tool_use_id": call_id, "content": content}
            if is_error:
                r["is_error"] = True
            out.append(r)
        messages.append({"role": "user", "content": out})


# ── OpenAI ───────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class OpenAIModelProfile:
    """What one OpenAI model accepts. A model without a profile is never
    offered: reasoning-effort support differs per model (an unsupported value
    is an HTTP 400), so guessing would turn into failed jobs."""
    label: str
    efforts: tuple
    final_effort: str                 # lowest supported: the synthesis call
    loop_effort: str = "low"


# Verified 2026-10-19 against developers.openai.com (models pages, reasoning
# guide, deployment checklist) and openai-python 3.35.1 (ResponsesModel,
# ReasoningEffort): GPT-6 Astra and GPT-6.1 Sol support low..max, NOT none or
# minimal; GPT-6 Sol and GPT-6 Luna also support none. All support function
# calling through the Responses API. Availability to THIS key is verified at
# run time (models.retrieve), not assumed from this table.
OPENAI_MODEL_PROFILES: dict[str, OpenAIModelProfile] = {
    "gpt-6-astra": OpenAIModelProfile("GPT-6 Astra",
                                      ("low", "medium", "high", "xhigh", "max"), "low"),
    "gpt-6.1-sol": OpenAIModelProfile("GPT-6.1 Sol",
                                      ("low", "medium", "high", "xhigh", "max"), "low"),
    "gpt-6-sol": OpenAIModelProfile("GPT-6 Sol",
                                    ("none", "low", "medium", "high", "xhigh", "max"), "none"),
    "gpt-6-luna": OpenAIModelProfile("GPT-6 Luna",
                                     ("none", "low", "medium", "high", "xhigh", "max"), "none"),
}

# The tool loop's own output budget (max_output_tokens counts reasoning too).
# Provider-specific, not the Claude budget: OpenAI reasoning models spend part
# of it before any visible text. An exhausted budget is still caught and sent
# to forced synthesis, so this bounds cost rather than hiding failures.
OPENAI_LOOP_MAX_OUTPUT_TOKENS = 12000
OPENAI_PROMPT_CACHE_KEY = "laynes-chat-v1"
OPENAI_FINAL_MAX_OUTPUT_TOKENS = 4096


def _env_bool(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class OpenAIConfig:
    enabled: bool
    has_key: bool
    allowed: tuple                    # configured AND profiled, in configured order
    rejected: tuple                   # configured but unknown -> never offered
    default: str | None
    loop_effort: str | None           # override; validated per model
    loop_max_output_tokens: int

    @property
    def usable(self) -> bool:
        return self.enabled and self.has_key and openai is not None and bool(self.allowed)


def openai_config() -> OpenAIConfig:
    """Read at call time, so tests (and a restart with a new env) see changes."""
    raw = [m.strip() for m in os.getenv("OPENAI_ALLOWED_MODELS", "").split(",") if m.strip()]
    allowed = tuple(dict.fromkeys(m for m in raw if m in OPENAI_MODEL_PROFILES))
    rejected = tuple(m for m in raw if m not in OPENAI_MODEL_PROFILES)
    default = os.getenv("OPENAI_DEFAULT_MODEL", "").strip() or None
    if default not in allowed:
        default = allowed[0] if allowed else None
    try:
        budget = int(os.getenv("OPENAI_MAX_OUTPUT_TOKENS", "") or OPENAI_LOOP_MAX_OUTPUT_TOKENS)
    except ValueError:
        budget = OPENAI_LOOP_MAX_OUTPUT_TOKENS
    budget = max(2048, min(budget, 32000))
    return OpenAIConfig(
        enabled=_env_bool("OPENAI_ENABLED"),
        has_key=bool(os.getenv("OPENAI_API_KEY")),
        allowed=allowed, rejected=rejected, default=default,
        loop_effort=os.getenv("OPENAI_REASONING_EFFORT", "").strip() or None,
        loop_max_output_tokens=budget)


def is_openai_model(model: str | None) -> bool:
    """Shape test for a requested id: never silently answered by Claude."""
    return bool(model) and (model in OPENAI_MODEL_PROFILES or model.startswith(("gpt-", "o1", "o3", "o4")))


# Availability of each allowlisted model to THIS key, checked with
# models.retrieve (metadata only, not billed).
#
# The check NEVER runs on a request path once anything is known: /api/models
# (every page load, Claude users included) and submissions read the cache, and
# an expired entry is refreshed by ONE background thread while the last known
# answer is served. Measured before this design: with OpenAI hanging, every
# page load blocked 5 s once a minute and N concurrent loads made N lookups.
# Only a cold cache (nothing known yet; warm_availability() runs at API
# startup) waits, at most AVAIL_COLD_WAIT_SECONDS, for that same refresh.
_AVAIL_OK_TTL = 600.0
_AVAIL_FAIL_TTL = 60.0
AVAIL_CHECK_TIMEOUT_SECONDS = 5.0
AVAIL_COLD_WAIT_SECONDS = 2.0
_avail_lock = threading.Lock()
_avail_cache: dict[str, tuple[bool, float]] = {}
_avail_refresh: dict = {"thread": None, "done": threading.Event()}


def _openai_client(timeout=None, max_retries=None):
    return openai.OpenAI(  # OPENAI_API_KEY from env; never logged
        timeout=OPENAI_TIMEOUT_SECONDS if timeout is None else timeout,
        max_retries=OPENAI_MAX_RETRIES if max_retries is None else max_retries)


def _refresh_availability(models: tuple, done: threading.Event) -> None:
    try:
        client = _openai_client(timeout=AVAIL_CHECK_TIMEOUT_SECONDS, max_retries=0)
        for m in models:
            try:
                client.models.retrieve(m)
                ok = True
            except Exception as exc:                  # noqa: BLE001
                ok = False
                log.warning("openai_model_unavailable model=%s reason=%s", m, safe_error(exc))
            with _avail_lock:
                _avail_cache[m] = (ok, time.monotonic()
                                   + (_AVAIL_OK_TTL if ok else _AVAIL_FAIL_TTL))
    except Exception as exc:                          # noqa: BLE001
        log.warning("openai_availability_refresh_failed reason=%s", safe_error(exc))
        with _avail_lock:
            for m in models:
                _avail_cache[m] = (False, time.monotonic() + _AVAIL_FAIL_TTL)
    finally:
        done.set()


def _start_refresh(models: tuple) -> threading.Event:
    """Single-flight: at most one refresh thread at a time."""
    with _avail_lock:
        t = _avail_refresh["thread"]
        if t is not None and t.is_alive():
            return _avail_refresh["done"]
        done = threading.Event()
        t = threading.Thread(target=_refresh_availability, args=(models, done),
                             name="openai-availability", daemon=True)
        _avail_refresh.update(thread=t, done=done)
        t.start()
        return done


def openai_available_models(cfg: OpenAIConfig | None = None) -> list[str]:
    cfg = cfg or openai_config()
    if not cfg.usable:
        return []
    now = time.monotonic()
    with _avail_lock:
        known = {m: _avail_cache.get(m) for m in cfg.allowed}
    missing = [m for m, v in known.items() if v is None]
    if missing or any(v[1] <= now for v in known.values() if v):
        done = _start_refresh(cfg.allowed)
        if missing:                                   # cold: bounded wait, once
            done.wait(AVAIL_COLD_WAIT_SECONDS)
            with _avail_lock:
                for m in missing:
                    # Still unknown: record "unavailable, already stale" so
                    # later callers serve it at once instead of waiting again;
                    # the in-flight refresh overwrites it when it finishes.
                    _avail_cache.setdefault(m, (False, time.monotonic()))
                known = {m: _avail_cache.get(m) for m in cfg.allowed}
    # Unknown (cold wait expired) counts as unavailable for now.
    return [m for m, v in known.items() if v and v[0]]


def warm_availability() -> None:
    """Start the first check in the background (called at API startup)."""
    cfg = openai_config()
    if cfg.usable:
        _start_refresh(cfg.allowed)


def reset_availability_cache() -> None:
    with _avail_lock:
        _avail_cache.clear()
    t = _avail_refresh["thread"]
    if t is not None:
        t.join(AVAIL_CHECK_TIMEOUT_SECONDS * 4 + 1)


def _openai_tools(tools: list[dict]) -> list[dict]:
    """The SAME two tool schemas, in Responses function-tool shape. Not strict:
    optional arguments stay optional; chat_sql validates every argument itself."""
    return [{"type": "function", "name": t["name"], "description": t["description"],
             "parameters": t["input_schema"], "strict": False} for t in tools]


def _openai_attachment(block: dict) -> dict | None:
    src = block.get("source") or {}
    if block.get("type") == "image" and src.get("type") == "base64":
        return {"type": "input_image",
                "image_url": f"data:{src['media_type']};base64,{src['data']}"}
    if block.get("type") == "document" and src.get("type") == "base64":
        return {"type": "input_file", "filename": block.get("title") or "file.pdf",
                "file_data": f"data:{src['media_type']};base64,{src['data']}"}
    if block.get("type") == "document" and src.get("type") == "text":
        return {"type": "input_text",
                "text": f"[Attached file: {block.get('title') or 'file'}]\n{src['data']}"}
    return None


class OpenAIProvider:
    """OpenAI Responses API, stateless (store=False): the full transcript --
    including encrypted reasoning items and function calls -- is sent back on
    every call, so nothing about the user's data is retained server-side for
    conversation state, and no previous_response_id ties a job to OpenAI."""
    name = PROVIDER_OPENAI
    call_label = "openai.responses.create"
    final_label = "openai.forced_synthesis"

    def __init__(self, model: str, tools: list[dict], cfg: OpenAIConfig | None = None,
                 client=None):
        cfg = cfg or openai_config()
        if model not in OPENAI_MODEL_PROFILES:
            raise ProviderError("model_unavailable",
                                "The selected OpenAI model is not available.")
        self.model = model
        self.profile = OPENAI_MODEL_PROFILES[model]
        self.loop_effort = (cfg.loop_effort if cfg.loop_effort in self.profile.efforts
                            else self.profile.loop_effort)
        self.loop_budget = cfg.loop_max_output_tokens
        self.tools = _openai_tools(tools)
        self.usage: dict = {}
        self.client = client or _openai_client()

    def start(self, history, question, attachments):
        items: list = [{"role": m["role"], "content": m["content"]} for m in history]
        if attachments:
            parts = [p for p in (_openai_attachment(b) for b in attachments) if p]
            items.append({"role": "user",
                          "content": [*parts, {"type": "input_text", "text": question}]})
        else:
            items.append({"role": "user", "content": question})
        return items

    @staticmethod
    def _instructions(system) -> str:
        if isinstance(system, str):
            return system
        return "\n\n".join(b["text"] for b in system)

    def _call(self, system, items, *, final: bool):
        kw = dict(model=self.model, instructions=self._instructions(system), input=items,
                  tools=self.tools, store=False, include=["reasoning.encrypted_content"],
                  # Same static key for every chat request: the ~26k-token
                  # system prompt + tools prefix is shared, so routing them
                  # together raises prompt-cache hits. Carries no user data.
                  prompt_cache_key=OPENAI_PROMPT_CACHE_KEY)
        if final:
            # No tool may be called: the model can only write the answer, with
            # the lowest reasoning effort this model accepts.
            kw.update(tool_choice="none", max_output_tokens=OPENAI_FINAL_MAX_OUTPUT_TOKENS,
                      reasoning={"effort": self.profile.final_effort})
        else:
            kw.update(max_output_tokens=self.loop_budget,
                      reasoning={"effort": self.loop_effort}, parallel_tool_calls=True)
        try:
            resp = self.client.responses.create(**kw)
        except Exception as exc:                      # noqa: BLE001
            raise map_openai_error(exc) from exc
        u = getattr(resp, "usage", None)
        if u is not None:
            det = getattr(u, "output_tokens_details", None)
            idet = getattr(u, "input_tokens_details", None)
            _add_usage(self.usage, getattr(u, "input_tokens", 0), getattr(u, "output_tokens", 0),
                       getattr(det, "reasoning_tokens", 0) if det is not None else 0,
                       getattr(idet, "cached_tokens", 0) if idet is not None else 0)
        return resp

    @staticmethod
    def _parse(resp, final: bool) -> Turn:
        status = getattr(resp, "status", None)
        output = getattr(resp, "output", None)
        if not isinstance(output, list):
            raise ProviderError("provider_bad_response",
                                "OpenAI returned a response the assistant could not read. "
                                "Please try again, or switch to Claude.",
                                f"output={type(output).__name__} status={status}")
        texts, calls = [], []
        for item in output:
            t = getattr(item, "type", None)
            if t == "message":
                for c in getattr(item, "content", None) or []:
                    ct = getattr(c, "type", None)
                    if ct == "output_text" and getattr(c, "text", None):
                        texts.append(c.text)
                    elif ct == "refusal" and getattr(c, "refusal", None):
                        texts.append(c.refusal)
            elif t == "function_call":
                try:
                    args = json.loads(getattr(item, "arguments", "") or "{}")
                    bad = None if isinstance(args, dict) else "arguments must be a JSON object"
                except (TypeError, ValueError):
                    args, bad = {}, "arguments were not valid JSON"
                calls.append(ToolCall(item.call_id, item.name,
                                      args if isinstance(args, dict) else {}, bad))
            # reasoning items carry no visible text: never an answer.
        text = "".join(texts)
        reason = getattr(getattr(resp, "incomplete_details", None), "reason", None)
        if status == "incomplete":
            # A call cut off mid-arguments must not be executed: treat the turn
            # like Claude's max_tokens and let forced synthesis answer.
            stop = "max_tokens" if reason == "max_output_tokens" else f"incomplete:{reason}"
            return Turn(stop=stop, text=text, raw=resp, truncated=True)
        if status == "completed":
            if calls and not final:
                return Turn(stop="tool_use", text=text, calls=calls, raw=resp)
            return Turn(stop="end_turn", text=text, raw=resp)
        err = getattr(resp, "error", None)
        raise ProviderError("provider_error",
                            "OpenAI could not complete this answer. Please try again, "
                            "or switch to Claude.",
                            f"status={status} code={getattr(err, 'code', None)}")

    def turn(self, system, items) -> Turn:
        return self._parse(self._call(system, items, final=False), final=False)

    def final(self, system, items) -> Turn:
        return self._parse(self._call(system, items, final=True), final=True)

    def add_assistant(self, items, turn: Turn) -> None:
        # Reasoning (encrypted), message and function_call items go back
        # verbatim, as the stateless Responses pattern requires; status is an
        # output-only field.
        for item in turn.raw.output:
            d = item.to_dict(mode="json") if hasattr(item, "to_dict") else dict(item)
            d.pop("status", None)
            items.append(d)

    def add_tool_results(self, items, results) -> None:
        for call_id, content, _is_error in results:
            # No is_error flag in this API: the content itself starts with
            # ERROR / REJECTED BEFORE EXECUTION / NOT EXECUTED.
            items.append({"type": "function_call_output", "call_id": call_id,
                          "output": content})


# ── errors: user-safe text, log-safe detail ──────────────────────────────────
_KEY_RX = re.compile(r"(sk-[A-Za-z0-9_\-*]{4,}|Bearer\s+\S+)")


def redact(text: str) -> str:
    return _KEY_RX.sub("[redacted]", text or "")


def safe_error(exc: BaseException) -> str:
    """Class, status and request id only. Provider error bodies can echo a
    (masked) key or request content, so they are never logged."""
    if isinstance(exc, ProviderError):
        cause = exc.__cause__
        return f"ProviderError[{exc.code}]" + (f" <- {safe_error(cause)}" if cause else
                                               f" {redact(str(exc))[:200]}")
    parts = [type(exc).__name__]
    for attr in ("status_code", "request_id"):
        v = getattr(exc, attr, None)
        if v is not None:
            parts.append(f"{attr}={v}")
    return " ".join(parts)


def map_openai_error(exc: BaseException) -> ProviderError:
    if isinstance(exc, ProviderError):
        return exc
    if openai is None:
        return ProviderError("provider_error", "OpenAI is not available.", safe_error(exc))
    if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return ProviderError("provider_auth",
                             "OpenAI is not available right now (the server's OpenAI "
                             "access was refused). Please switch to Claude.", safe_error(exc))
    if isinstance(exc, openai.RateLimitError):
        return ProviderError("provider_rate_limited",
                             "OpenAI is limiting requests right now. Please try again in "
                             "a minute, or switch to Claude.", safe_error(exc))
    if isinstance(exc, openai.APITimeoutError):
        return ProviderError("provider_timeout",
                             "OpenAI took too long to respond. Please try again, or "
                             "switch to Claude.", safe_error(exc))
    if isinstance(exc, openai.APIConnectionError):
        return ProviderError("provider_unreachable",
                             "The server could not reach OpenAI. Please try again, or "
                             "switch to Claude.", safe_error(exc))
    if isinstance(exc, (openai.NotFoundError, openai.BadRequestError)):
        return ProviderError("model_unavailable",
                             "The selected OpenAI model rejected the request or is not "
                             "available. Please choose another model.", safe_error(exc))
    if isinstance(exc, openai.APIStatusError):
        return ProviderError("provider_error",
                             "OpenAI returned an error. Please try again, or switch to "
                             "Claude.", safe_error(exc))
    return ProviderError("provider_bad_response",
                         "OpenAI returned a response the assistant could not read. "
                         "Please try again, or switch to Claude.", safe_error(exc))


def make_provider(model: str, tools: list[dict], *, anthropic_timeout: float,
                  anthropic_max_retries: int):
    if is_openai_model(model):
        cfg = openai_config()
        if not cfg.usable or model not in cfg.allowed:
            # Never fall back to Claude: the job keeps the provider it was
            # submitted with, and fails visibly if that provider is gone.
            raise ProviderError("provider_disabled",
                                "OpenAI is not enabled on this server. Please choose a "
                                "Claude model.")
        return OpenAIProvider(model, tools, cfg)
    return ClaudeProvider(model, tools, anthropic_timeout, anthropic_max_retries)
