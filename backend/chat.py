"""The bridge to the analytics assistant.

This module deliberately contains NO analytics. It calls
chat_sql.answer_question, which is the same entry point the Streamlit app used,
so the A12 reconciliation gate, the AST validator, the relation allowlist and
the laynes_ro role all apply unchanged. The browser sends a question string and
receives rendered markdown -- never SQL, never a connection, never a key.
"""

from __future__ import annotations

import os
import time

import chat_sql

# The ids offered in the picker. Kept here rather than in the client so the
# browser cannot ask for an arbitrary model.
AVAILABLE_MODELS = [
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-haiku-4-5-20251001",
]
DEFAULT_MODEL = chat_sql.MODEL


def resolve_model(requested: str | None) -> str:
    """Only ids we publish are accepted; anything else falls back to default."""
    return requested if requested in AVAILABLE_MODELS else DEFAULT_MODEL


def assistant_configured() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def ask(history, question: str, model: str):
    """Returns (answer, steps, duration_ms, error). Never raises to the caller."""
    started = time.time()
    error = None
    try:
        result = chat_sql.answer_question(history, question, model=model)
        answer, steps = result.answer, result.steps
    except Exception as exc:                      # noqa: BLE001
        # The class name is safe to record; the message may carry internals, so
        # it is logged server-side and never returned to the browser verbatim.
        error = f"{type(exc).__name__}: {exc}"
        answer = ("Something went wrong reaching the assistant. "
                  "Please try again in a moment.")
        steps = []
    return answer, steps, int((time.time() - started) * 1000), error
