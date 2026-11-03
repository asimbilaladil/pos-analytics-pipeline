import json

import pytest
from conftest import HDR, INIT, auth, call, rpc


def test_initialize_and_discovery(client, make_key):
    tok, _ = make_key()
    r = client.post("/mcp", headers=auth(tok), json=INIT)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    res = r.json()["result"]
    assert res["protocolVersion"] == "2025-06-18"
    assert res["serverInfo"]["name"] == "laynes-intelligence"
    assert "tools" in res["capabilities"]
    r = client.post("/mcp", headers=auth(tok), json=rpc("tools/list"))
    tools = {t["name"]: t for t in r.json()["result"]["tools"]}
    assert set(tools) == {"get_sales_guest_counts", "get_labor_metrics", "get_pnl_food_cost",
                          "get_drive_thru_speed", "ask_laynes_question"}
    for t in tools.values():
        assert t["inputSchema"]["type"] == "object"
        assert t.get("description")


@pytest.mark.parametrize("version", ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"])
def test_supported_protocol_versions_negotiate_to_themselves(client, make_key, version):
    tok, _ = make_key()
    body = rpc("initialize", {"protocolVersion": version, "capabilities": {},
                              "clientInfo": {"name": "t", "version": "1"}})
    r = client.post("/mcp", headers=auth(tok), json=body)
    assert r.json()["result"]["protocolVersion"] == version


def test_unknown_protocol_version_gets_server_latest(client, make_key):
    tok, _ = make_key()
    body = rpc("initialize", {"protocolVersion": "1999-01-01", "capabilities": {},
                              "clientInfo": {"name": "t", "version": "1"}})
    r = client.post("/mcp", headers=auth(tok), json=body)
    assert r.json()["result"]["protocolVersion"] in (
        "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")


def test_tools_call_returns_structured_content(client, make_key, monkeypatch):
    from mcp_server import analytics
    monkeypatch.setattr(analytics, "drive_thru_speed",
                        lambda ids, s, e, d: {"status": "ok", "stores": [{"store_id": ids[0]}]})
    tok, _ = make_key()
    res = call(client, tok, "get_drive_thru_speed",
               {"start_date": "2026-10-01", "end_date": "2026-10-02", "store_ids": [6]})
    assert res.get("isError") in (False, None)
    assert res["structuredContent"]["status"] == "ok"
    assert json.loads(res["content"][0]["text"])["stores"][0]["store_id"] == 6


def test_unknown_tool_and_bad_arguments(client, make_key):
    tok, _ = make_key()
    r = client.post("/mcp", headers=auth(tok), json=rpc("tools/call", {"name": "run_sql", "arguments": {"sql": "select 1"}}))
    body = r.json()
    assert "error" in body or body["result"].get("isError")
    res = call(client, tok, "get_drive_thru_speed", {"start_date": "yesterday", "end_date": "x"})
    assert res["isError"] is True


def test_notification_is_accepted_without_body(client, make_key):
    tok, _ = make_key()
    r = client.post("/mcp", headers=auth(tok), json=rpc("notifications/initialized", id_=None))
    assert r.status_code == 202


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
def test_invalid_methods_do_not_succeed(client, make_key, method):
    tok, _ = make_key()
    r = client.request(method, "/mcp", headers=auth(tok))
    assert r.status_code == 405


def test_get_stream_not_offered_in_stateless_mode(client, make_key):
    tok, _ = make_key()
    r = client.get("/mcp", headers={**auth(tok), "Accept": "text/event-stream"})
    assert r.status_code == 405 and r.headers["allow"] == "POST"


def test_malformed_json_is_rejected(client, make_key):
    tok, _ = make_key()
    r = client.post("/mcp", headers=auth(tok), content=b"{not json")
    assert r.status_code in (400, 422) or "error" in r.json()


def test_wrong_content_type_is_rejected(client, make_key):
    tok, _ = make_key()
    r = client.post("/mcp", headers={**auth(tok), "Content-Type": "text/plain"}, content=b"hello")
    assert r.status_code in (400, 415)


def test_missing_accept_header_is_rejected(client, make_key):
    tok, _ = make_key()
    r = client.post("/mcp", headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}, json=INIT)
    assert r.status_code in (200, 406)


def test_bad_origin_is_forbidden_even_with_valid_key(client, make_key):
    tok, _ = make_key()
    r = client.post("/mcp", headers=auth(tok, Origin="https://evil.example"), json=INIT)
    assert r.status_code == 403


def test_allowed_and_absent_origin_pass(client, make_key):
    tok, _ = make_key()
    ok = client.post("/mcp", headers=auth(tok, Origin="https://intelligence.aygchicken.com"), json=INIT)
    assert ok.status_code == 200
    assert client.post("/mcp", headers=auth(tok), json=INIT).status_code == 200


def test_bad_origin_checked_before_authentication(client):
    r = client.post("/mcp", headers={**HDR, "Origin": "https://evil.example"}, json=INIT)
    assert r.status_code == 403


def test_unknown_paths_404_and_health_is_data_free(client):
    assert client.get("/sse").status_code == 404
    assert client.get("/api/anything").status_code == 404
    h = client.get("/healthz")
    assert h.status_code == 200 and h.json() == {"status": "ok"}


def test_oversized_body_rejected(client, make_key):
    tok, _ = make_key()
    r = client.post("/mcp", headers=auth(tok), content=b"x" * 70000)
    assert r.status_code == 413


def test_tool_schemas_accept_no_sql_url_path_or_shell(client, make_key):
    tok, _ = make_key()
    tools = client.post("/mcp", headers=auth(tok), json=rpc("tools/list")).json()["result"]["tools"]
    banned = {"sql", "query", "table", "url", "path", "file", "command", "shell", "cmd",
              "relation", "database", "connection"}
    for t in tools:
        props = set(t["inputSchema"].get("properties", {}))
        assert not (props & banned), (t["name"], props & banned)
    assert "run_sql" not in {t["name"] for t in tools}
