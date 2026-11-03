import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest
from conftest import HDR, INIT, auth, call, rpc

from mcp_server import admin, keystore, settings


def post(client, tok, body=INIT):
    return client.post("/mcp", headers=auth(tok) if tok else HDR, json=body)


def test_valid_key(client, make_key):
    tok, _ = make_key()
    assert post(client, tok).status_code == 200


def test_missing_wrong_malformed_keys_all_401_identically(client, make_key):
    tok, rec = make_key()
    wrong_secret = f"lmcp_{rec.key_id}_" + "A" * 43
    unknown_id = "lmcp_" + "0" * 12 + "_" + "A" * 43
    bodies = set()
    for t in (None, "garbage", wrong_secret, unknown_id, "lmcp_short"):
        r = post(client, t)
        assert r.status_code == 401, t
        assert r.headers["www-authenticate"].startswith("Bearer")
        bodies.add(r.text)
    assert len(bodies) == 1                  # no oracle: same body for every failure


def test_basic_scheme_rejected(client, make_key):
    tok, _ = make_key()
    r = client.post("/mcp", headers={**HDR, "Authorization": f"Basic {tok}"}, json=INIT)
    assert r.status_code == 401


def test_revoked_key_rejected_immediately(client, make_key):
    tok, rec = make_key()
    assert post(client, tok).status_code == 200
    assert keystore.revoke_key(rec.key_id, "test")
    assert post(client, tok).status_code == 401
    assert keystore.revoke_key(rec.key_id) is False          # idempotent


def test_expired_key_rejected(client, make_key):
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    tok, rec = make_key(expires_at=past)
    assert keystore.get_key(rec.key_id).status == "expired"
    assert post(client, tok).status_code == 401
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    tok2, _ = make_key(expires_at=future)
    assert post(client, tok2).status_code == 200


def test_rotation_revokes_old_and_issues_new(client, make_key):
    old, rec = make_key()
    new, rec2 = keystore.rotate_key(rec.key_id)
    assert rec2.key_id != rec.key_id and rec2.rotated_from == rec.key_id
    assert rec2.tools == rec.tools and rec2.stores == rec.stores
    assert post(client, old).status_code == 401
    assert post(client, new).status_code == 200
    with pytest.raises(ValueError):
        keystore.rotate_key(rec.key_id)                       # revoked keys cannot rotate


def test_rotation_with_grace_keeps_old_key_until_expiry(client, make_key):
    old, rec = make_key()
    new, _ = keystore.rotate_key(rec.key_id, grace_hours=2)
    assert post(client, old).status_code == 200 and post(client, new).status_code == 200
    assert keystore.get_key(rec.key_id).expires_at is not None


def test_unauthorized_tool_denied_by_default(client, make_key):
    tok, _ = make_key(tools=["get_drive_thru_speed"])
    res = call(client, tok, "get_labor_metrics", {"start_date": "2026-10-01", "end_date": "2026-10-02"})
    assert res["isError"] and "not permitted" in res["content"][0]["text"]


def test_unauthorized_store_denied_not_silently_dropped(client, make_key, monkeypatch):
    from mcp_server import analytics
    monkeypatch.setattr(analytics, "drive_thru_speed", lambda *a: pytest.fail("must not run"))
    tok, _ = make_key(stores=[6])
    res = call(client, tok, "get_drive_thru_speed",
               {"start_date": "2026-10-01", "end_date": "2026-10-02", "store_ids": [6, 7]})
    assert res["isError"] and "not permitted" in res["content"][0]["text"]
    # an id that does not exist at all is indistinguishable from a forbidden one
    res2 = call(client, tok, "get_drive_thru_speed",
                {"start_date": "2026-10-01", "end_date": "2026-10-02", "store_ids": [99999]})
    assert res2["content"][0]["text"] == res["content"][0]["text"]


def test_aggregate_request_never_widens_beyond_grant(client, make_key, monkeypatch):
    """Omitting store_ids (the 'network' request) must only ever cover granted stores."""
    from mcp_server import analytics
    seen = {}
    monkeypatch.setattr(analytics, "sales_guest_counts",
                        lambda ids, s, e, g: seen.setdefault("ids", ids) and {"status": "ok"})
    tok, _ = make_key(stores=[6, 14])
    call(client, tok, "get_sales_guest_counts", {"start_date": "2026-10-01", "end_date": "2026-10-02"})
    assert seen["ids"] == [6, 14]


def test_all_stores_grant_requires_explicit_star(make_key, monkeypatch):
    from mcp_server import security
    tok, _ = make_key(stores="*")
    p = keystore.verify_token(tok)
    assert p.all_stores
    assert set(security.resolve_stores(p, None)) == set(security.known_store_ids())
    with pytest.raises(ValueError):
        keystore.create_key("x", ["get_labor_metrics"], [])


def test_no_default_permissions(make_key):
    with pytest.raises(ValueError):
        keystore.create_key("x", [], [6])
    with pytest.raises(ValueError):
        keystore.create_key("x", ["run_sql"], [6])
    with pytest.raises(ValueError):
        keystore.create_key("", ["get_labor_metrics"], [6])


def test_only_hash_is_stored_and_secret_never_persisted(make_key, tmp_path):
    tok, rec = make_key()
    secret = tok.split("_", 2)[2]
    blob = b"".join(open(os.path.join(settings.data_dir(), f), "rb").read()
                    for f in os.listdir(settings.data_dir()))
    assert secret.encode() not in blob and tok.encode() not in blob
    row = sqlite3.connect(settings.db_path()).execute(
        "select secret_sha256 from api_keys where key_id=?", (rec.key_id,)).fetchone()
    assert len(row[0]) == 64 and row[0] != secret
    st = os.stat(settings.db_path()).st_mode & 0o777
    assert st == 0o600
    assert os.stat(settings.data_dir()).st_mode & 0o777 == 0o700


def test_tokens_have_high_entropy_and_are_unique(make_key):
    toks = {make_key()[0] for _ in range(20)}
    assert len(toks) == 20
    assert all(len(t.split("_", 2)[2]) == 43 for t in toks)


def test_rate_limit_returns_429_and_is_per_key(client, make_key):
    a, _ = make_key(rate_per_min=3)
    b, _ = make_key(rate_per_min=3)
    codes = [post(client, a).status_code for _ in range(5)]
    assert codes[:3] == [200] * 3 and codes[3:] == [429, 429]
    assert post(client, b).status_code == 200            # other key unaffected


def test_usage_audit_is_sanitised(client, make_key, monkeypatch):
    from mcp_server import analytics
    monkeypatch.setattr(analytics, "drive_thru_speed", lambda *a: {"status": "ok"})
    tok, rec = make_key()
    call(client, tok, "get_drive_thru_speed", {"start_date": "2026-10-01", "end_date": "2026-10-02"})
    post(client, "bad-token")
    rows = keystore.usage_summary(None, 50)
    outcomes = {r["outcome"] for r in rows}
    assert {"ok", "auth_failed"} <= outcomes
    dump = json.dumps(rows)
    assert tok not in dump and "2026-10-01" not in dump       # no secrets, no arguments
    assert any(r["key_id"] == rec.key_id and r["tool"] == "get_drive_thru_speed" for r in rows)


def test_admin_cli_refuses_to_print_secret_to_pipe_and_revokes(capsys, tmp_path):
    rc = admin.main(["create", "--label", "ci", "--tools", "get_labor_metrics", "--stores", "6"])
    out = capsys.readouterr()
    assert rc == 2 and "lmcp_" not in out.out + out.err
    assert all(k.status == "revoked" for k in keystore.list_keys())


def test_admin_cli_secret_file_is_0600_and_list_hides_secrets(capsys, tmp_path):
    f = tmp_path / "secret.txt"
    assert admin.main(["create", "--label", "Masroor Grok Bot", "--tools", "all",
                       "--stores", "6,7", "--write-secret-file", str(f)]) == 0
    capsys.readouterr()
    assert os.stat(f).st_mode & 0o777 == 0o600
    token = f.read_text().strip()
    assert token.startswith("lmcp_")
    admin.main(["list"])
    listing = capsys.readouterr().out
    assert token not in listing and token.split("_", 2)[2] not in listing
    assert "secret_sha256" not in listing and "Masroor Grok Bot" in listing
    kid = json.loads(listing.splitlines()[0])["key_id"]
    assert admin.main(["revoke", kid]) == 0
    assert keystore.verify_token(token) is None
    # never overwrites a secret file, and never changes any key when it cannot deliver
    tok2, rec2 = keystore.create_key("again", ["get_labor_metrics"], [6])
    before = [(k.key_id, k.status) for k in keystore.list_keys()]
    assert admin.main(["rotate", rec2.key_id, "--write-secret-file", str(f)]) == 2
    assert [(k.key_id, k.status) for k in keystore.list_keys()] == before
    assert keystore.verify_token(tok2) is not None            # old key still works
    assert f.read_text().strip() == token                     # file untouched


def test_no_http_route_manages_keys(client, make_key):
    tok, _ = make_key()
    for path in ("/admin", "/keys", "/mcp/keys", "/api/keys", "/mcp/admin"):
        for m in ("GET", "POST"):
            assert client.request(m, path, headers=auth(tok), json={}).status_code == 404


def test_tools_list_shows_only_permitted_tools(client, make_key):
    tok, _ = make_key(tools=["get_labor_metrics", "get_drive_thru_speed"])
    names = {t["name"] for t in client.post("/mcp", headers=auth(tok),
                                            json=rpc("tools/list")).json()["result"]["tools"]}
    assert names == {"get_labor_metrics", "get_drive_thru_speed"}
    full, _ = make_key()
    assert len(client.post("/mcp", headers=auth(full), json=rpc("tools/list")).json()["result"]["tools"]) == 5
