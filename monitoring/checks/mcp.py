"""MCP: health endpoint and tool-execution outcomes.

Tool outcomes come from the MCP server's own usage_log (SQLite). The live file
is in WAL mode, where even a mode=ro reader may create/update the -shm index --
as root that could leave a root-owned file the MCP service cannot write. So the
monitor never opens the live database: it copies the db and -wal into a private
temp dir and reads the copy. 'partial' and 'unavailable' are data-trust outcomes (the
server correctly withheld unverified data) and are not errors; 'error' is.
A burst of 'auth_failed' means someone is calling with a bad or revoked key.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import urllib.error
import urllib.request
from datetime import timedelta

from ..model import FAIL, HIGH, OK, SRC_MCP, WARNING, CheckResult


def http_status(url: str, timeout: float) -> tuple[int | None, str | None]:
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "laynes-monitor"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, None
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {str(e)[:120]}"


def health(ctx) -> list[CheckResult]:
    url = ctx.cfg["mcp"]["health_url"]
    code, err = ctx.http(url, ctx.cfg["services"]["http_timeout_s"])
    base = dict(source=SRC_MCP, check="health", expected="HTTP 200")
    if code == 200:
        return [CheckResult(state=OK, title="MCP healthy", actual="200", **base)]
    return [CheckResult(state=FAIL, severity=HIGH, category="down",
                        title="MCP health check failing", actual=err or f"HTTP {code}",
                        actions=["`systemctl status laynes-mcp`; `journalctl -u laynes-mcp -n 200`.",
                                 "Restart only with approval and zero active ask jobs."], **base)]


def tool_errors(ctx) -> list[CheckResult]:
    c = ctx.cfg["mcp"]
    since = (ctx.now - timedelta(minutes=int(c["error_window_minutes"]))).isoformat(timespec="seconds")
    base_e = dict(source=SRC_MCP, check="tool_errors",
                  expected=f"< {c['error_threshold']} tool errors in {c['error_window_minutes']} min")
    base_a = dict(source=SRC_MCP, check="auth_failures",
                  expected=f"< {c['auth_failed_threshold']} auth failures in "
                           f"{c['error_window_minutes']} min")
    try:
        counts, tools = read_usage(c["usage_db"], since)
    except (sqlite3.Error, OSError) as e:
        return [CheckResult(state=FAIL, severity=WARNING, category="unreadable",
                            title="MCP usage log unreadable", actual=f"{type(e).__name__}: {e}",
                            actions=["Check the path and permissions of the MCP usage DB."],
                            **base_e)]
    out = []
    n_err = counts.get("error", 0)
    if n_err >= c["error_threshold"]:
        out.append(CheckResult(state=FAIL, severity=HIGH, category="tool_errors",
                               title="MCP tool execution errors", actual=f"{n_err} error(s)",
                               evidence={"tools": tools, "outcomes": counts},
                               actions=["`journalctl -u laynes-mcp --since -1h` for tracebacks."],
                               **base_e))
    else:
        out.append(CheckResult(state=OK, title="No MCP tool errors", evidence={"outcomes": counts},
                               **base_e))
    n_auth = counts.get("auth_failed", 0)
    if n_auth >= c["auth_failed_threshold"]:
        out.append(CheckResult(state=FAIL, severity=WARNING, category="auth_failures",
                               title="Many MCP authentication failures", actual=f"{n_auth}",
                               actions=["A client is using a wrong or revoked key; check which "
                                        "integration and rotate if unexpected."], **base_a))
    else:
        out.append(CheckResult(state=OK, title="MCP auth failures normal", actual=str(n_auth),
                               **base_a))
    return out


def read_usage(path: str, since: str) -> tuple[dict, list]:
    """Outcome counts since `since`, read from a private copy (see module doc)."""
    last = None
    for _attempt in range(2):          # a checkpoint mid-copy can tear the copy; retry once
        with tempfile.TemporaryDirectory(prefix="laynes-monitor-mcp-") as tmp:
            dst = os.path.join(tmp, "usage.sqlite3")
            shutil.copyfile(path, dst)
            if os.path.exists(path + "-wal"):
                shutil.copyfile(path + "-wal", dst + "-wal")
            try:
                db = sqlite3.connect(dst, timeout=5)
                try:
                    counts = dict(db.execute(
                        "SELECT outcome, COUNT(*) FROM usage_log WHERE ts >= ? GROUP BY outcome",
                        (since,)).fetchall())
                    tools = [t for (t,) in db.execute(
                        "SELECT DISTINCT tool FROM usage_log WHERE ts >= ? AND outcome = 'error'",
                        (since,)).fetchall()]
                    return counts, tools
                finally:
                    db.close()
            except sqlite3.Error as e:
                last = e
    raise last
