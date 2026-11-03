"""Credential, audit and job storage for the MCP server (SQLite, own file).

Nothing here touches the Laynes PostgreSQL database: credentials are created by
a server-side CLI and live in a 0600 SQLite file under MCP_DATA_DIR. Only a
SHA-256 digest of each secret is stored.

Token format:   lmcp_<key_id>_<secret>
  key_id  12 hex chars (public identifier, safe to log)
  secret  43 url-safe chars = 256 bits from secrets.token_urlsafe(32)
The secret has full entropy, so a fast hash is appropriate (nothing to brute
force); lookup is by key_id and the digest is compared in constant time.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import settings

TOOLS = ("get_sales_guest_counts", "get_labor_metrics", "get_pnl_food_cost",
         "get_drive_thru_speed", "ask_laynes_question")

_TOKEN_RX = re.compile(r"^lmcp_([0-9a-f]{12})_([A-Za-z0-9_-]{43})$")
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
  key_id        TEXT PRIMARY KEY,
  label         TEXT NOT NULL,
  secret_sha256 TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  expires_at    TEXT,
  revoked_at    TEXT,
  revoked_reason TEXT,
  tools_json    TEXT NOT NULL,
  stores_json   TEXT NOT NULL,          -- JSON list of ints, or "*"
  rate_per_min  INTEGER NOT NULL,
  daily_asks    INTEGER NOT NULL,
  rotated_from  TEXT
);
CREATE TABLE IF NOT EXISTS usage_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key_id TEXT, ts TEXT NOT NULL, tool TEXT, outcome TEXT NOT NULL,
  duration_ms INTEGER, store_count INTEGER, range_days INTEGER
);
CREATE INDEX IF NOT EXISTS usage_key_ts ON usage_log(key_id, ts);
CREATE TABLE IF NOT EXISTS ask_jobs (
  job_id TEXT PRIMARY KEY,
  key_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  question_sha256 TEXT NOT NULL,
  scope_json TEXT NOT NULL,
  status TEXT NOT NULL,                 -- queued|processing|completed|failed
  model TEXT,
  answer TEXT, evidence_json TEXT, error_code TEXT, error_message TEXT,
  created_at TEXT NOT NULL, started_at TEXT, completed_at TEXT, duration_ms INTEGER
);
-- One logical submission per (credential, idempotency key). A FAILED job is
-- outside the index so an explicit retry may create a new attempt.
CREATE UNIQUE INDEX IF NOT EXISTS ask_jobs_idem ON ask_jobs(key_id, idempotency_key)
  WHERE status IN ('queued','processing','completed');
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect():
    d = settings.data_dir()
    os.makedirs(d, mode=0o700, exist_ok=True)
    path = settings.db_path()
    fresh = not os.path.exists(path)
    conn = sqlite3.connect(path, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    if fresh:
        os.chmod(path, 0o600)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.executescript(SCHEMA)
        yield conn
    finally:
        conn.close()


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


@dataclass(frozen=True)
class Principal:
    key_id: str
    label: str
    tools: frozenset
    stores: object                     # frozenset[int] or "*"
    rate_per_min: int
    daily_asks: int                   # 0 = unlimited

    @property
    def all_stores(self) -> bool:
        return self.stores == "*"


@dataclass
class KeyRecord:
    key_id: str
    label: str
    created_at: str
    expires_at: str | None
    revoked_at: str | None
    tools: list
    stores: object
    rate_per_min: int
    daily_asks: int
    rotated_from: str | None = None

    @property
    def status(self) -> str:
        if self.revoked_at:
            return "revoked"
        if self.expires_at and self.expires_at <= now_iso():
            return "expired"
        return "active"


def _row_to_record(r) -> KeyRecord:
    stores = json.loads(r["stores_json"])
    return KeyRecord(r["key_id"], r["label"], r["created_at"], r["expires_at"],
                     r["revoked_at"], json.loads(r["tools_json"]), stores,
                     r["rate_per_min"], r["daily_asks"], r["rotated_from"])


# ── administration (called only by the server-side CLI) ───────────────────
def create_key(label: str, tools, stores, *, expires_at: str | None = None,
               rate_per_min: int | None = None, daily_asks: int | None = None,
               rotated_from: str | None = None) -> tuple[str, KeyRecord]:
    """Returns (plaintext_token, record). The token is never stored."""
    label = (label or "").strip()
    if not label or len(label) > 120:
        raise ValueError("label is required (max 120 chars)")
    tools = sorted(set(tools))
    bad = [t for t in tools if t not in TOOLS]
    if not tools or bad:
        raise ValueError("tools must be a non-empty subset of: " + ", ".join(TOOLS)
                         + (f" (unknown: {bad})" if bad else ""))
    if stores != "*":
        stores = sorted({int(s) for s in stores})
        if not stores:
            raise ValueError("stores must list at least one id, or '*' for all")
    if expires_at:
        datetime.fromisoformat(expires_at)         # validate
    # None = not supplied (use the default). 0 is meaningful for daily_asks:
    # it means UNLIMITED. Negative values are never valid.
    if rate_per_min is None:
        rate_per_min = settings.DEFAULT_RATE_PER_MIN
    if isinstance(rate_per_min, bool) or not isinstance(rate_per_min, int) or rate_per_min < 1:
        raise ValueError("rate_per_min must be an integer of at least 1")
    if daily_asks is None:
        daily_asks = settings.DEFAULT_DAILY_ASKS
    if isinstance(daily_asks, bool) or not isinstance(daily_asks, int) or daily_asks < 0:
        raise ValueError("daily_asks must be 0 (unlimited) or a positive integer")
    key_id = secrets.token_hex(6)
    secret = secrets.token_urlsafe(32)
    token = f"lmcp_{key_id}_{secret}"
    with _lock, connect() as c:
        c.execute(
            "INSERT INTO api_keys (key_id,label,secret_sha256,created_at,expires_at,"
            "tools_json,stores_json,rate_per_min,daily_asks,rotated_from) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (key_id, label, _digest(secret), now_iso(), expires_at,
             json.dumps(tools), json.dumps(stores),
             rate_per_min, daily_asks, rotated_from))
        rec = _row_to_record(c.execute("SELECT * FROM api_keys WHERE key_id=?",
                                       (key_id,)).fetchone())
    return token, rec


def list_keys() -> list[KeyRecord]:
    with connect() as c:
        return [_row_to_record(r) for r in
                c.execute("SELECT * FROM api_keys ORDER BY created_at")]


def get_key(key_id: str) -> KeyRecord | None:
    with connect() as c:
        r = c.execute("SELECT * FROM api_keys WHERE key_id=?", (key_id,)).fetchone()
    return _row_to_record(r) if r else None


def revoke_key(key_id: str, reason: str = "") -> bool:
    with _lock, connect() as c:
        cur = c.execute(
            "UPDATE api_keys SET revoked_at=?, revoked_reason=? "
            "WHERE key_id=? AND revoked_at IS NULL",
            (now_iso(), reason[:200], key_id))
        return cur.rowcount == 1


def rotate_key(key_id: str, *, grace_hours: float = 0.0) -> tuple[str, KeyRecord]:
    """New credential with the same label/permissions; old one is revoked now,
    or after grace_hours (implemented as an expiry so both work during overlap)."""
    old = get_key(key_id)
    if old is None or old.status != "active":
        raise ValueError("only an active key can be rotated")
    token, new = create_key(old.label, old.tools, old.stores,
                            expires_at=old.expires_at, rate_per_min=old.rate_per_min,
                            daily_asks=old.daily_asks, rotated_from=key_id)
    if grace_hours and grace_hours > 0:
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) + timedelta(hours=grace_hours)
                  ).isoformat(timespec="seconds")
        with _lock, connect() as c:
            c.execute("UPDATE api_keys SET expires_at=? WHERE key_id=? AND "
                      "(expires_at IS NULL OR expires_at > ?)", (cutoff, key_id, cutoff))
    else:
        revoke_key(key_id, f"rotated to {new.key_id}")
    return token, new


# ── verification (called per request) ─────────────────────────────────────
_DUMMY = _digest("x" * 43)


def verify_token(token: str | None) -> Principal | None:
    """Principal for a valid, active, unexpired token; otherwise None.

    One code path and one comparison for every failure so the response does not
    reveal whether a key id exists, is revoked or is expired.
    """
    m = _TOKEN_RX.match(token or "")
    key_id, secret = (m.group(1), m.group(2)) if m else ("", "")
    row = None
    if m:
        with connect() as c:
            row = c.execute("SELECT * FROM api_keys WHERE key_id=?", (key_id,)).fetchone()
    expected = row["secret_sha256"] if row else _DUMMY
    ok = hmac.compare_digest(_digest(secret), expected) and row is not None and bool(m)
    if not ok:
        return None
    rec = _row_to_record(row)
    if rec.status != "active":
        return None
    return Principal(rec.key_id, rec.label, frozenset(rec.tools),
                     "*" if rec.stores == "*" else frozenset(rec.stores),
                     rec.rate_per_min, rec.daily_asks)


# ── audit / rate limiting ─────────────────────────────────────────────────
_windows: dict[str, list[float]] = {}
_win_lock = threading.Lock()


def rate_limited(key_id: str, per_min: int) -> bool:
    """True when this request exceeds the per-minute budget (and is not counted)."""
    now = time.monotonic()
    with _win_lock:
        w = [t for t in _windows.get(key_id, []) if now - t < 60.0]
        if len(w) >= per_min:
            _windows[key_id] = w
            return True
        w.append(now)
        _windows[key_id] = w
        return False


def audit(key_id: str | None, tool: str | None, outcome: str, *,
          duration_ms: int | None = None, store_count: int | None = None,
          range_days: int | None = None) -> None:
    """Sanitised usage record: no arguments, no question text, no secrets."""
    try:
        with connect() as c:
            c.execute("INSERT INTO usage_log (key_id,ts,tool,outcome,duration_ms,"
                      "store_count,range_days) VALUES (?,?,?,?,?,?,?)",
                      (key_id, now_iso(), tool, outcome, duration_ms, store_count,
                       range_days))
    except sqlite3.Error:
        pass            # auditing must never break a request


def usage_summary(key_id: str | None = None, limit: int = 20) -> list[dict]:
    with connect() as c:
        q = "SELECT * FROM usage_log " + ("WHERE key_id=? " if key_id else "") + \
            "ORDER BY id DESC LIMIT ?"
        args = ((key_id, limit) if key_id else (limit,))
        return [dict(r) for r in c.execute(q, args)]


def asks_today(key_id: str) -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    with connect() as c:
        return c.execute(
            "SELECT count(*) FROM ask_jobs WHERE key_id=? AND created_at >= ?",
            (key_id, today)).fetchone()[0]
