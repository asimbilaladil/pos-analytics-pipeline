"""Focused isolation checks driving the REAL chat_sql.answer_question loop with a
scripted provider (no network, no database): one permitted request and a series of
cross-store attacks, a waste review, and job-result binding."""
import json

import pytest
from conftest import auth, rpc

import chat_providers as cp
import chat_sql
import store_scope
from mcp_server import asker, keystore, security
from mcp_server.security import ToolInputError

META_OK = {"analysis_permitted": True, "blocking_reasons": [], "warnings": [],
           "domains": ["revel"], "reconciliation": {"status": "PASS"}, "_cache": "computed"}


class Script:
    name = "anthropic"
    call_label = final_label = "scripted"
    usage = {}

    def __init__(self, calls):
        self.calls = list(calls)
        self.results = []

    def start(self, h, q, a):
        return []

    def turn(self, system, messages):
        if not self.calls:
            return cp.Turn("end_turn", "done")
        i, inp = self.calls.pop(0)
        return cp.Turn("tool_use", "", [cp.ToolCall(f"c{i}", "run_sql", inp)])

    def final(self, system, messages):
        return cp.Turn("end_turn", "Final answer.")

    def add_assistant(self, m, t):
        pass

    def add_tool_results(self, m, results):
        self.results.extend(results)


class Rec:
    """Fake DB connection that records executed SQL and returns one row."""
    executed = []

    def __init__(self): pass
    def set_session(self, **k): pass
    def rollback(self): pass
    def close(self): pass
    def cursor(self, **k): return self
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, *a): Rec.executed.append(sql)
    def fetchall(self): return [{"establishment_id": 6, "n": 1}]


@pytest.fixture
def env(monkeypatch):
    Rec.executed = []
    monkeypatch.setattr(chat_sql, "_ro_conn", lambda: Rec())
    monkeypatch.setattr(chat_sql, "meta_profile",
                        lambda est, a, b, domains=None, cache=None: (
                            store_scope.check_declared_scope(est) or dict(META_OK)))

    def run(calls, allowed, question="Katy sales?"):
        prov = Script(calls)
        monkeypatch.setattr(cp, "make_provider", lambda *a, **k: prov)
        tok = store_scope.activate(allowed) if allowed else None
        try:
            res = chat_sql.answer_question([], question, model="claude-sonnet-5")
        finally:
            if tok:
                store_scope.deactivate(tok)
        return res, prov
    return run


def sql_call(i, sql, est, a="2026-10-01", b="2026-10-08"):
    return (i, {"sql": sql, "establishment_id": est, "period_start": a, "period_end_exclusive": b})


BASE = ("SELECT sum(total_revenue) FROM features_daily_summary_v2 WHERE establishment_id = {e} "
        "AND date >= '2026-10-01' AND date < '2026-10-08'")


def data_sql():
    return [s for s in Rec.executed if "features_daily_summary_v2" in s or "orders_v2" in s]


def test_permitted_store_request_runs_confined(env):
    res, prov = env([sql_call(1, BASE.format(e=6), 6)], [6])
    assert res.answer == "done" and prov.results[0][2] is False
    assert len(data_sql()) == 1 and "establishment_id IN (6)" in data_sql()[0]


@pytest.mark.parametrize("declared,sql", [
    (7, BASE.format(e=7)),                                           # other store, declared
    (None, BASE.format(e=6).replace("establishment_id = 6 AND", "")),   # "network" scope
    (6, BASE.format(e="7")),                                         # declared 6, filters 7
    (6, BASE.format(e="ANY(ARRAY[6,7,14])")),                        # array trick
])
def test_cross_store_attacks_never_reach_the_database(env, declared, sql):
    res, prov = env([sql_call(1, sql, declared)], [6])
    assert prov.results[0][2] is True                                 # tool error
    assert data_sql() == []                                           # nothing executed


def test_unfiltered_subquery_attack_is_confined_by_rewrite(env):
    sql = ("SELECT (SELECT sum(total_revenue) FROM features_daily_summary_v2 "
           "WHERE date >= '2026-10-01') AS everyone, f.establishment_id "
           "FROM features_daily_summary_v2 f WHERE f.establishment_id = 6 "
           "AND f.date >= '2026-10-01' AND f.date < '2026-10-08'")
    env([sql_call(1, sql, 6)], [6])
    ex = data_sql()[0]
    assert ex.count("establishment_id IN (6)") == 2                   # BOTH scans confined


@pytest.mark.parametrize("sql", ["SELECT * FROM v_identity_profile",
                                 "SELECT * FROM app_users", "DELETE FROM orders_v2",
                                 "SELECT pg_read_file('/etc/passwd')"])
def test_aggregate_views_admin_tables_and_writes_refused(env, sql):
    res, prov = env([sql_call(1, sql, 6)], [6])
    assert prov.results[0][2] is True and data_sql() == []


def test_waste_review_cannot_start_under_a_policy(env):
    tok = store_scope.activate([6])
    try:
        with pytest.raises(store_scope.StoreScopeError):
            chat_sql.fin_review_window("financial waste review P10")
        with pytest.raises(store_scope.StoreScopeError):
            chat_sql.answer_question([], "Do a financial waste review for P10", model="claude-sonnet-5")
    finally:
        store_scope.deactivate(tok)
    assert Rec.executed == []


@pytest.mark.parametrize("q", ["Where are we wasting money? financial waste review",
                               "give me cost-cutting ideas", "how can we reduce our expenses",
                               "which stores have margin improvement opportunities",
                               "operating expense review please"])
def test_restricted_key_waste_questions_rejected_before_any_run(make_key, monkeypatch, q):
    from backend import chat as chat_svc
    monkeypatch.setattr(chat_svc, "assistant_configured", lambda: True)
    monkeypatch.setattr(chat_svc, "ask", lambda *a, **k: pytest.fail("must not run"))
    tok, _ = make_key(tools=["ask_laynes_question"], stores=[6])
    with pytest.raises(ToolInputError) as e:
        asker.ask(keystore.verify_token(tok), q, None, None, None, None, None)
    assert "every store" in str(e.value)


def test_all_store_key_keeps_the_waste_review(make_key, monkeypatch):
    from backend import chat as chat_svc
    monkeypatch.setattr(chat_svc, "assistant_configured", lambda: True)
    monkeypatch.setattr(chat_svc, "ask", lambda h, q, m, a=None: ("ok", [], 1, None))
    tok, _ = make_key(tools=["ask_laynes_question"], stores="*")
    r = asker.ask(keystore.verify_token(tok), "financial waste review P10", None, None, None, None, None)
    assert r["status"] == "completed"
    assert chat_sql.FIN_REVIEW_SQL_BUDGET == 3


# ── job results stay bound to the originating credential ─────────────────────
@pytest.fixture
def done_job(client, make_key, monkeypatch):
    from backend import chat as chat_svc
    monkeypatch.setattr(chat_svc, "assistant_configured", lambda: True)
    monkeypatch.setattr(chat_svc, "ask", lambda h, q, m, a=None: ("Katy answer", [], 1, None))
    tok, rec = make_key(tools=["ask_laynes_question"], stores=[6])
    r = asker.ask(keystore.verify_token(tok), "Katy sales?", None, None, None, None, None)
    return tok, rec, r["job_id"]


def fetch(client, tok, job_id):
    return client.post("/mcp", headers=auth(tok), json=rpc("tools/call", {
        "name": "ask_laynes_question", "arguments": {"job_id": job_id}}))


def test_owner_can_fetch_over_http(client, done_job):
    tok, _, jid = done_job
    r = fetch(client, tok, jid)
    assert r.status_code == 200 and r.json()["result"]["structuredContent"]["answer"] == "Katy answer"


def test_revoked_credential_cannot_fetch_its_saved_result(client, done_job):
    tok, rec, jid = done_job
    keystore.revoke_key(rec.key_id)
    assert fetch(client, tok, jid).status_code == 401


def test_rotated_or_other_credentials_cannot_fetch(client, done_job, make_key):
    tok, rec, jid = done_job
    other, _ = make_key(tools=["ask_laynes_question"], stores=[6])
    res = fetch(client, other, jid).json()["result"]
    assert res["isError"] and "answer" not in json.dumps(res)
    new, _ = keystore.rotate_key(rec.key_id)                          # new key id => new owner
    assert fetch(client, new, jid).json()["result"]["isError"]


def test_narrowed_grant_hides_a_previously_wider_job(make_key, monkeypatch):
    from backend import chat as chat_svc
    monkeypatch.setattr(chat_svc, "assistant_configured", lambda: True)
    monkeypatch.setattr(chat_svc, "ask", lambda h, q, m, a=None: ("wide", [], 1, None))
    tok, rec = make_key(tools=["ask_laynes_question"], stores=[6, 7])
    p = keystore.verify_token(tok)
    jid = asker.ask(p, "sales?", None, None, None, None, None)["job_id"]
    narrowed = keystore.Principal(p.key_id, p.label, p.tools, frozenset({6}), 10, 10)
    with pytest.raises(ToolInputError):
        asker.ask(narrowed, None, None, None, None, None, jid)
