"""The bridge to the analytics assistant.

This module deliberately contains NO analytics. It calls
chat_sql.answer_question, which is the same entry point the Streamlit app used,
so the A12 reconciliation gate, the AST validator, the relation allowlist and
the laynes_ro role all apply unchanged. The browser sends a question string and
receives rendered markdown -- never SQL, never a connection, never a key.
"""

from __future__ import annotations

import os
import re
import time

import chat_providers as providers
import chat_sql

# The Claude ids offered in the picker. Kept here rather than in the client so
# the browser cannot ask for an arbitrary model. OpenAI ids come from server
# configuration (OPENAI_ALLOWED_MODELS), filtered to models with a verified
# parameter profile and confirmed available to the configured key.
AVAILABLE_MODELS = [
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-haiku-4-5-20251001",
]
DEFAULT_MODEL = chat_sql.MODEL


class ModelUnavailable(Exception):
    """An explicitly requested model this server will not run right now."""


def provider_of(model: str | None) -> str:
    return (providers.PROVIDER_OPENAI if providers.is_openai_model(model)
            else providers.PROVIDER_ANTHROPIC)


def _claude_label(model: str) -> str:
    bare = re.sub(r"-\d{8}$", "", model)
    m = (re.match(r"^claude-([a-z]+)-((?:\d+-)*\d+)$", bare)
         or re.match(r"^claude-((?:\d+-)*\d+)-([a-z]+)$", bare))
    if not m:
        return model
    fam, ver = (m.group(2), m.group(1)) if m.group(1)[0].isdigit() else (m.group(1), m.group(2))
    return f"{fam.capitalize()} {ver.replace('-', '.')}"


def model_label(model: str) -> str:
    prof = providers.OPENAI_MODEL_PROFILES.get(model)
    return prof.label if prof else _claude_label(model)


def openai_models() -> list[str]:
    """Configured, profiled AND verified available -- [] when disabled."""
    return providers.openai_available_models()


def available_models() -> list[str]:
    return list(AVAILABLE_MODELS) + openai_models()


def model_options() -> dict:
    """What the picker may offer. Claude stays the default for everyone."""
    oa = openai_models()
    opts = [{"id": m, "label": model_label(m), "provider": providers.PROVIDER_ANTHROPIC}
            for m in AVAILABLE_MODELS]
    opts += [{"id": m, "label": model_label(m), "provider": providers.PROVIDER_OPENAI}
             for m in oa]
    return {
        "models": [o["id"] for o in opts],
        "default": DEFAULT_MODEL,
        "options": opts,
        "providers": [
            {"id": providers.PROVIDER_ANTHROPIC,
             "label": providers.PROVIDER_LABELS[providers.PROVIDER_ANTHROPIC],
             "enabled": anthropic_configured()},
            {"id": providers.PROVIDER_OPENAI,
             "label": providers.PROVIDER_LABELS[providers.PROVIDER_OPENAI],
             "enabled": bool(oa)},
        ],
    }


def resolve_model(requested: str | None, strict: bool = False) -> str:
    """Only ids we publish are accepted.

    An unknown or retired Claude id falls back to the default, as it always
    has (old conversations keep working). An OpenAI id that is not offered
    right now is NEVER silently answered by Claude: with strict=True (a
    question being submitted) it raises ModelUnavailable instead.
    """
    if requested in AVAILABLE_MODELS:
        return requested
    if providers.is_openai_model(requested):
        if requested in openai_models():
            return requested
        if strict:
            raise ModelUnavailable(requested)
    return DEFAULT_MODEL


def anthropic_configured() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def assistant_configured() -> bool:
    """Some provider can answer (gates /api/status and every submission)."""
    return anthropic_configured() or providers.openai_config().usable


def provider_configured(model: str) -> bool:
    """The provider behind this (already resolved) model has credentials."""
    if provider_of(model) == providers.PROVIDER_OPENAI:
        return providers.openai_config().usable
    return anthropic_configured()


_FAIL_CODE_RX = re.compile(r"^ProviderError\[([a-z_]+)\]")


def failure_code(error: str | None) -> str:
    """chat_jobs.error_code for a failed generation."""
    if error and error.startswith("ChatTimeout"):
        return "timeout"
    m = _FAIL_CODE_RX.match(error or "")
    return m.group(1) if m else "generation_error"


def ask(history, question: str, model: str, attachments: list[dict] | None = None):
    """Returns (answer, steps, duration_ms, error). Never raises to the caller."""
    started = time.time()
    error = None
    try:
        result = chat_sql.answer_question(history, question, model=model,
                                          attachments=attachments)
        answer, steps = result.answer, result.steps
    except chat_sql.ChatTimeout as exc:
        # The application gives up on its OWN deadline (240 s), comfortably
        # inside nginx's 300 s proxy_read_timeout, so the browser receives this
        # JSON answer instead of nginx's HTML 504 -- which the UI could not
        # parse and which left the spinner running indefinitely.
        error = f"ChatTimeout: {exc}"
        answer = "That request took too long. Please try again."
        steps = []
    except providers.ProviderError as exc:
        # Provider failures carry their own user-safe text. The logged detail
        # is class / status / request id only -- never the provider's error
        # body, which can echo a masked key or request content.
        error = providers.safe_error(exc)
        answer = exc.user_message
        steps = []
    except Exception as exc:                      # noqa: BLE001
        # The class name is safe to record; the message may carry internals, so
        # it is logged server-side and never returned to the browser verbatim.
        error = providers.redact(f"{type(exc).__name__}: {exc}")
        answer = ("Something went wrong while analyzing the data. "
                  "Please try again.")
        steps = []
    return answer, steps, int((time.time() - started) * 1000), error
