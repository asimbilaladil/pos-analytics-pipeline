"""Configuration, read from the environment at call time (never cached secrets)."""

from __future__ import annotations

import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def data_dir() -> str:
    # Credential hashes, audit log and job state. Owned by the service user, 0700.
    return os.environ.get("MCP_DATA_DIR", "/var/lib/laynes-mcp")


def db_path() -> str:
    return os.path.join(data_dir(), "mcp.sqlite3")


def allowed_origins() -> list[str]:
    """Browser Origins accepted. Non-browser clients send none (and are allowed)."""
    raw = os.environ.get("MCP_ALLOWED_ORIGINS", "https://intelligence.aygchicken.com")
    return [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]


def allowed_hosts() -> list[str]:
    raw = os.environ.get(
        "MCP_ALLOWED_HOSTS",
        "intelligence.aygchicken.com,127.0.0.1:*,localhost:*")
    return [h.strip() for h in raw.split(",") if h.strip()]


# Request limits (overridable per key via the CLI for rate_per_min / daily_asks).
MAX_RANGE_DAYS = _int("MCP_MAX_RANGE_DAYS", 62)
MAX_STORES_PER_CALL = _int("MCP_MAX_STORES", 12)
MAX_BODY_BYTES = _int("MCP_MAX_BODY_BYTES", 65536)
TOOL_TIMEOUT_SECONDS = _int("MCP_TOOL_TIMEOUT_SECONDS", 110)
STATEMENT_TIMEOUT_MS = _int("MCP_STATEMENT_TIMEOUT_MS", 15000)
DEFAULT_RATE_PER_MIN = _int("MCP_DEFAULT_RATE_PER_MIN", 30)
DEFAULT_DAILY_ASKS = _int("MCP_DEFAULT_DAILY_ASKS", 40)
ASK_WAIT_SECONDS = _int("MCP_ASK_WAIT_SECONDS", 90)   # inline wait before pending
ASK_MAX_QUESTION_CHARS = _int("MCP_ASK_MAX_QUESTION_CHARS", 1500)
ASK_MAX_CONCURRENT = _int("MCP_ASK_MAX_CONCURRENT", 2)  # running jobs per key
JOB_RETENTION_DAYS = _int("MCP_JOB_RETENTION_DAYS", 14)
# ask_laynes_question model: None = chat_sql's existing default provider/model.
ASK_MODEL = os.environ.get("MCP_ASK_MODEL") or None

DATA_START = "2026-01-01"          # earliest date any source holds
