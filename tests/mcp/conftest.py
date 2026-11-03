"""Isolated test infrastructure for the MCP server.

* A fresh SQLite data dir per test (never /var/lib, never the Laynes database).
* chat_sql._ro_conn is replaced with a function that FAILS, so a test that
  accidentally reaches for PostgreSQL breaks loudly instead of touching data.
  Business data comes from fixtures patched into analytics._query/_gate.
"""
import os
import sys
import tempfile

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
os.environ.setdefault("MCP_ALLOWED_HOSTS", "testserver,127.0.0.1:*,localhost:*")
# Make sure no real credential can leak in from the developer's environment.
for k in ("DB_PASS", "DB_USER", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
    os.environ.pop(k, None)
os.environ["DB_RO_USER"] = "unit-test-no-db"
os.environ["DB_RO_PASS"] = "unit-test-no-db"
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-not-real-0000000000000000"

import chat_sql  # noqa: E402
from mcp_server import keystore  # noqa: E402


def _no_db(*a, **k):
    raise AssertionError("test attempted a real database connection")


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_DATA_DIR", str(tmp_path / "mcpdata"))
    monkeypatch.setattr(chat_sql, "_ro_conn", _no_db)
    keystore._windows.clear()
    yield


@pytest.fixture
def make_key():
    def _mk(tools=("get_sales_guest_counts", "get_labor_metrics", "get_pnl_food_cost",
                   "get_drive_thru_speed", "ask_laynes_question"),
            stores=(6, 7), **kw):
        return keystore.create_key("test key", list(tools), stores if stores == "*" else list(stores), **kw)
    return _mk


HDR = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def rpc(method, params=None, id_=1):
    m = {"jsonrpc": "2.0", "method": method}
    if id_ is not None:
        m["id"] = id_
    if params is not None:
        m["params"] = params
    return m


INIT = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                          "clientInfo": {"name": "pytest", "version": "1"}})


@pytest.fixture
def client():
    from starlette.testclient import TestClient
    from mcp_server import server
    app = server.build_app()
    with TestClient(app) as c:
        yield c


def auth(token, **extra):
    return {**HDR, "Authorization": f"Bearer {token}", **extra}


def call(client, token, name, args, id_=2):
    r = client.post("/mcp", headers=auth(token), json=rpc("tools/call", {"name": name, "arguments": args}, id_))
    assert r.status_code == 200, r.text
    return r.json()["result"]
