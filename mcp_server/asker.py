"""Tool 5: ask_laynes_question -- a bounded bridge to the existing chat backend.

No second analytics engine: the answer comes from backend.chat.ask ->
chat_sql.answer_question, so the AST validator, relation allowlist, laynes_ro
role, A12/domain trust gates, the 3-query financial-waste-review budget and
never-empty answer handling all apply unchanged.

What this module adds
  * Store isolation in the DATA layer: the worker thread activates a
    store_scope policy for the credential's stores, so every SQL statement the
    model writes is rewritten to read only those stores (see store_scope.py).
  * Its own job table (SQLite). It never writes chat_jobs / conversations and
    never uses a personal user's session, so it cannot touch, duplicate or
    orphan website jobs, and no job can belong to another credential.
  * Idempotency: one logical submission per (credential, idempotency key).
  * Deadlines: wait up to MCP_ASK_WAIT_SECONDS inline; beyond that return a
    pending job reference the SAME credential can poll via the `job_id` input.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from datetime import datetime, timezone

import store_scope

from . import keystore, security, settings
from .keystore import Principal
from .security import ToolInputError, chicago_today, parse_range, resolve_stores

_GLOBAL_SLOTS = threading.BoundedSemaphore(3)       # concurrent model runs, process-wide

_BLOCKED_RX = re.compile(
    r"(?i)(?<!\w)("
    r"drop\s+(table|view|database|schema|index|user|role|column)|truncate\s+table|"
    r"alter\s+(table|user|role|database|system)|insert\s+into|update\s+\w+\s+set|"
    r"delete\s+from|grant\s+(select|insert|update|delete|all|usage|access)|revoke\s+\w+|"
    r"create\s+(table|user|role|database|index|view)|"
    r"run[_ ]sql|execute\s+(raw\s+)?sql|raw\s+sql|"
    r"(run|open|get|spawn)\s+(a\s+)?(shell|terminal|bash)|bash\s+command|sudo|ssh|\.env|"
    r"api[_ -]?key|access[_ -]?token|bearer|jwt|session\s+cookie|passw(or)?d|app_users|"
    r"admin(istrator)?\s+(panel|account|user|password)|"
    r"other\s+users?'?s?\s+(chat|conversation|message)s?|conversation\s+history|"
    r"(reset|change)\s+(my|the|a)\s+password|add\s+(a\s+)?user|delete\s+(a\s+)?user"
    r")(?!\w)")


def screen_question(q: str) -> str:
    """Deterministic pre-screen. Defence in depth only: the real controls are the
    SQL validator, the relation allowlist and the store policy."""
    if not isinstance(q, str) or not q.strip():
        raise ToolInputError("question is required")
    q = "".join(ch for ch in q if ch == "\n" or ch >= " ").strip()
    if len(q) > settings.ASK_MAX_QUESTION_CHARS:
        raise ToolInputError(
            f"question is too long (max {settings.ASK_MAX_QUESTION_CHARS} characters)")
    if _BLOCKED_RX.search(q):
        raise ToolInputError(
            "this tool answers read-only business-analytics questions only; it cannot "
            "run SQL, change data, manage users or credentials, or read conversations")
    return q


def _scope_note(names: dict, start, end, restricted: bool) -> str:
    stores = ", ".join(f"{n} (id {i})" for i, n in sorted(names.items()))
    note = (f"[API request from an automated client. Permitted stores: {stores}. "
            "Answer ONLY about these stores and say so if asked about others; the data "
            "layer enforces this. ")
    if start:
        note += f"Focus period (inclusive, America/Chicago): {start} to {end}. "
    note += "Never reveal credentials, SQL text, or other users' data.] "
    return note


def _now():
    return keystore.now_iso()


def _public(row, *, include_answer=True) -> dict:
    out = {"job_id": row["job_id"], "status": row["status"],
           "created_at": row["created_at"], "completed_at": row["completed_at"],
           "duration_ms": row["duration_ms"], "model": row["model"]}
    if row["status"] == "completed" and include_answer:
        out["answer"] = row["answer"]
        out["evidence"] = json.loads(row["evidence_json"] or "{}")
    if row["status"] == "failed":
        out["error_code"] = row["error_code"]
        out["error_message"] = row["error_message"]
    return out


def _fetch(job_id: str, key_id: str):
    with keystore.connect() as c:
        return c.execute("SELECT * FROM ask_jobs WHERE job_id=? AND key_id=?",
                         (job_id, key_id)).fetchone()


def sweep_interrupted() -> int:
    """At startup: no job started by a previous process can still be running."""
    with keystore.connect() as c:
        cur = c.execute(
            "UPDATE ask_jobs SET status='failed', error_code='interrupted', "
            "error_message='The service restarted while this answer was being "
            "generated. Please ask again.', completed_at=? "
            "WHERE status IN ('queued','processing')", (_now(),))
        return cur.rowcount


def _evidence(steps, model, provider_name) -> dict:
    import chat_sql
    queries, checks = [], []
    for st in steps or []:
        if st.get("check"):
            checks.append({"domains": st.get("domains"), "status": st.get("status"),
                           "error": bool(st.get("error"))})
        elif st.get("sql"):
            try:
                rels = sorted(chat_sql._relations_in(st["sql"]))
            except Exception:                                   # noqa: BLE001
                rels = []
            queries.append({"relations": rels, "row_count": st.get("row_count"),
                            "trust_gate": st.get("gate"), "domains": st.get("domains"),
                            "rejected": bool(st.get("error"))})
    return {"sources": sorted({r for q in queries for r in q["relations"]}),
            "queries": queries, "freshness_checks": checks,
            "model": model, "provider": provider_name,
            "note": "SQL text is not returned; relations and gate verdicts are."}


def _run_job(job_id: str, question: str, model: str, allowed, t0: float) -> None:
    from backend import chat as chat_svc
    tok = store_scope.activate(allowed) if allowed is not None else None
    try:
        with keystore.connect() as c:
            c.execute("UPDATE ask_jobs SET status='processing', started_at=? "
                      "WHERE job_id=? AND status='queued'", (_now(), job_id))
        try:
            answer, steps, dur, error = chat_svc.ask([], question, model)
        except Exception:                                       # noqa: BLE001
            answer, steps, dur, error = "", [], int((time.time() - t0) * 1000), "internal"
        with keystore.connect() as c:
            if error:
                code = chat_svc.failure_code(error) if error != "internal" else "internal"
                c.execute("UPDATE ask_jobs SET status='failed', error_code=?, "
                          "error_message=?, completed_at=?, duration_ms=? WHERE job_id=?",
                          (code, (answer or "The request could not be completed.")[:500],
                           _now(), dur, job_id))
            else:
                ev = _evidence(steps, model, chat_svc.provider_of(model))
                c.execute("UPDATE ask_jobs SET status='completed', answer=?, "
                          "evidence_json=?, completed_at=?, duration_ms=? WHERE job_id=?",
                          (answer, json.dumps(ev), _now(), dur, job_id))
    except Exception:                                           # noqa: BLE001
        try:
            with keystore.connect() as c:
                c.execute("UPDATE ask_jobs SET status='failed', error_code='internal', "
                          "error_message='Something went wrong. Please try again.', "
                          "completed_at=? WHERE job_id=? AND status IN "
                          "('queued','processing')", (_now(), job_id))
        except Exception:                                       # noqa: BLE001
            pass
    finally:
        if tok is not None:
            store_scope.deactivate(tok)
        _GLOBAL_SLOTS.release()


def _wait(job_id: str, key_id: str, seconds: float):
    deadline = time.monotonic() + seconds
    while True:
        row = _fetch(job_id, key_id)
        if row["status"] in ("completed", "failed") or time.monotonic() >= deadline:
            return row
        time.sleep(0.25)


def ask(p: Principal, question, store_ids, start_date, end_date, idempotency_key,
        job_id, wait_seconds: float | None = None) -> dict:
    wait = settings.ASK_WAIT_SECONDS if wait_seconds is None else wait_seconds

    # Retrieval of an earlier job: ownership is part of the lookup, so another
    # credential's id is indistinguishable from an id that does not exist.
    if job_id:
        row = _fetch(str(job_id), p.key_id)
        if row is None:
            raise ToolInputError("no such job for this credential")
        # The stored scope must still lie inside the credential's CURRENT grant
        # (a key narrowed after submission must not read the wider answer).
        stored = set(json.loads(row["scope_json"]).get("stores") or [])
        granted = set(security.known_store_ids()) if p.all_stores else set(p.stores)
        if not stored <= granted:
            raise ToolInputError("no such job for this credential")
        if row["status"] in ("queued", "processing"):
            row = _wait(row["job_id"], p.key_id, min(wait, 25))
        return _result(row)

    q = screen_question(question)
    start = end = None
    if start_date or end_date:
        if not (start_date and end_date):
            raise ToolInputError("provide both start_date and end_date, or neither")
        s, e = parse_range(start_date, end_date, max_days=366)
        start, end = s.isoformat(), e.isoformat()
    # Effective store scope. Always resolved against the credential's grant.
    ids = resolve_stores(p, store_ids)
    import chat_sql
    names = {i: chat_sql.ESTABLISHMENTS.get(i, f"store {i}") for i in ids}
    # Unrestricted only when the credential holds every store AND the caller did
    # not narrow the scope. Anything else is confined in the data layer.
    all_known = set(chat_sql.ESTABLISHMENTS)
    allowed = None if (p.all_stores and set(ids) == all_known) else ids
    if allowed is not None and (chat_sql.is_financial_waste_review(q)
                                or chat_sql.is_financial_waste_review(
                                    _scope_note(names, start, end, True) + q)):
        raise ToolInputError(
            "the financial waste review compares stores across the network and is "
            "only available to a credential with access to every store")

    from backend import chat as chat_svc
    if not chat_svc.assistant_configured():
        raise ToolInputError("the analytics assistant is not configured on this server")
    model = chat_svc.resolve_model(settings.ASK_MODEL)

    scope = {"stores": ids, "start": start, "end": end}
    q_sha = hashlib.sha256(q.encode()).hexdigest()
    idem = (idempotency_key or "").strip()
    if idem and not re.fullmatch(r"[A-Za-z0-9._:-]{8,100}", idem):
        raise ToolInputError("idempotency_key must be 8-100 chars of [A-Za-z0-9._:-]")
    if not idem:
        # Absent a client key, collapse an immediate retry of the same question
        # (10-minute bucket) onto one job instead of paying twice.
        bucket = int(time.time() // 600)
        idem = "auto-" + hashlib.sha256(
            f"{q_sha}|{json.dumps(scope, sort_keys=True)}|{bucket}".encode()).hexdigest()[:32]

    with keystore.connect() as c:
        existing = c.execute(
            "SELECT * FROM ask_jobs WHERE key_id=? AND idempotency_key=? "
            "AND status IN ('queued','processing','completed') ORDER BY created_at DESC "
            "LIMIT 1", (p.key_id, idem)).fetchone()
    if existing:
        if existing["question_sha256"] != q_sha:
            raise ToolInputError("idempotency_key was already used for a different question")
        row = existing
        if row["status"] in ("queued", "processing"):
            row = _wait(row["job_id"], p.key_id, wait)
        return _result(row, reused=True)

    if keystore.asks_today(p.key_id) >= p.daily_asks:
        raise ToolInputError("daily question limit reached for this credential")
    with keystore.connect() as c:
        active = c.execute("SELECT count(*) FROM ask_jobs WHERE key_id=? AND status IN "
                           "('queued','processing')", (p.key_id,)).fetchone()[0]
    if active >= settings.ASK_MAX_CONCURRENT:
        raise ToolInputError("too many questions are still running for this credential; "
                             "retrieve them with job_id or wait")
    if not _GLOBAL_SLOTS.acquire(blocking=False):
        raise ToolInputError("the assistant is busy; try again shortly")

    new_id = str(uuid.uuid4())
    try:
        with keystore.connect() as c:
            c.execute(
                "INSERT INTO ask_jobs (job_id,key_id,idempotency_key,question_sha256,"
                "scope_json,status,model,created_at) VALUES (?,?,?,?,?,'queued',?,?)",
                (new_id, p.key_id, idem, q_sha, json.dumps(scope), model, _now()))
    except Exception:                                           # noqa: BLE001
        _GLOBAL_SLOTS.release()
        # Lost a race with an identical submission: return the winner's job.
        with keystore.connect() as c:
            row = c.execute("SELECT * FROM ask_jobs WHERE key_id=? AND idempotency_key=? "
                            "AND status IN ('queued','processing','completed') "
                            "ORDER BY created_at DESC LIMIT 1", (p.key_id, idem)).fetchone()
        if row is None:
            raise ToolInputError("could not create the job") from None
        return _result(_wait(row["job_id"], p.key_id, wait), reused=True)

    prompt = _scope_note(names, start, end, allowed is not None) + q
    threading.Thread(target=_run_job, name=f"mcpask-{new_id[:8]}", daemon=True,
                     args=(new_id, prompt, model, allowed, time.time())).start()
    return _result(_wait(new_id, p.key_id, wait))


def _result(row, reused: bool = False) -> dict:
    out = _public(row)
    out["idempotent_replay"] = reused
    if row["status"] in ("queued", "processing"):
        out["status"] = "pending"
        out["retry_after_seconds"] = 10
        out["message"] = ("The answer is still being generated. Call ask_laynes_question "
                          "again with this job_id to retrieve it; the job keeps running "
                          "and is not restarted.")
    out["wait_limit_seconds"] = settings.ASK_WAIT_SECONDS
    return out
