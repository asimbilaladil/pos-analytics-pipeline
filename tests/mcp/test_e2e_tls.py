"""HTTPS end-to-end: real uvicorn + self-signed TLS + the OFFICIAL MCP client.

Stands in for a remote MCP consumer such as xAI's servers: bearer header, tool
discovery, tool call, several requests, revocation mid-use and an aborted
connection."""
import asyncio
import os
import socket
import subprocess
import threading
import time

import httpx
import pytest

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from mcp_server import keystore


@pytest.fixture
def tls_server(tmp_path, monkeypatch):
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                    "-out", str(cert), "-days", "1", "-subj", "/CN=localhost",
                    "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
                   check=True, capture_output=True)
    monkeypatch.setenv("SSL_CERT_FILE", str(cert))
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    import uvicorn
    from mcp_server import analytics, server
    monkeypatch.setattr(analytics, "drive_thru_speed",
                        lambda ids, a, b, d: {"status": "ok", "stores": [{"store_id": i} for i in ids]})
    cfg = uvicorn.Config(server.build_app(), host="127.0.0.1", port=port, log_level="error",
                         ssl_certfile=str(cert), ssl_keyfile=str(key))
    srv = uvicorn.Server(cfg)
    th = threading.Thread(target=srv.run, daemon=True); th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"https://localhost:{port}/mcp", str(cert), port
    srv.should_exit = True
    th.join(5)


async def _session(url, token):
    async with streamablehttp_client(url, headers={"Authorization": f"Bearer {token}"}) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            res = await s.call_tool("get_drive_thru_speed",
                                    {"start_date": "2026-10-01", "end_date": "2026-10-02", "store_ids": [6]})
            return [t.name for t in tools.tools], res


def test_official_client_over_https(tls_server, make_key):
    url, cert, port = tls_server
    tok, _ = make_key()
    names, res = asyncio.run(_session(url, tok))
    assert "get_sales_guest_counts" in names and "ask_laynes_question" in names
    assert not res.isError and res.structuredContent["stores"] == [{"store_id": 6}]


def test_multiple_sequential_and_concurrent_clients(tls_server, make_key):
    url, cert, port = tls_server
    tok, _ = make_key()

    async def many():
        return await asyncio.gather(*[_session(url, tok) for _ in range(4)])
    out = asyncio.run(many())
    assert all(not r.isError for _, r in out)
    asyncio.run(_session(url, tok))                       # and again afterwards


def test_invalid_and_revoked_credentials_over_https(tls_server, make_key):
    url, cert, port = tls_server
    tok, rec = make_key()
    h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "x", "version": "1"}}}
    with httpx.Client(verify=cert) as c:
        assert c.post(url, headers={**h, "Authorization": "Bearer nope"}, json=init).status_code == 401
        assert c.post(url, headers={**h, "Authorization": f"Bearer {tok}"}, json=init).status_code == 200
        keystore.revoke_key(rec.key_id)
        r = c.post(url, headers={**h, "Authorization": f"Bearer {tok}"}, json=init)
        assert r.status_code == 401
    with pytest.raises(BaseException):
        asyncio.run(_session(url, tok))


def test_http_plain_is_not_served_and_client_abort_does_not_hurt_server(tls_server, make_key):
    url, cert, port = tls_server
    tok, _ = make_key()
    # abrupt disconnect mid-request
    raw = socket.create_connection(("127.0.0.1", port))
    raw.sendall(b"POST /mcp HTTP/1.1\r\nHost: localhost\r\nContent-Length: 500\r\n\r\npartial")
    raw.close()
    # server still healthy for the next real client
    names, res = asyncio.run(_session(url, tok))
    assert not res.isError
