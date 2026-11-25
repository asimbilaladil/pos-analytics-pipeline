"""AI Chat: stuck/failed jobs, provider errors, SQL errors and trust-guard blocks.

Privacy: these checks read counts, error codes and short error classes only.
No question text, answer text or conversation content is selected, stored or
sent anywhere.

Error classes in chat_query_log.steps[*].error:
  * trust gate ("did not pass the ... trust gate"): AI Chat CORRECTLY refused to
    answer from unverified data. Reported as a WARNING tally that points at the
    data incidents; never as a chat failure.
  * scope discipline ("must carry explicit date bounds", "must declare
    period_start"): the model's SQL was rejected by the validator and it
    usually retries. Counted, not alerted.
  * anything else: a real SQL / execution error.
"""
from __future__ import annotations

from ..model import FAIL, HIGH, OK, SRC_CHAT, WARNING, CheckResult
from ..pg import rows

_STUCK_SQL = """
SELECT COUNT(*) AS n, MIN(created_at) AS oldest
FROM chat_jobs
WHERE status IN ('queued', 'processing')
  AND created_at < now() - make_interval(mins => %(mins)s)
"""

_FAILED_JOBS_SQL = """
SELECT status, COALESCE(error_code, '') AS error_code, COUNT(*) AS n
FROM chat_jobs
WHERE created_at >= now() - make_interval(mins => %(mins)s)
  AND status NOT IN ('queued', 'processing', 'completed')
GROUP BY 1, 2
"""

_PROVIDER_SQL = """
SELECT COUNT(*) AS n,
       COALESCE(substring(MAX(error) from '^[A-Za-z]+Error\\[[a-z_]+\\]'), '') AS sample
FROM chat_query_log
WHERE asked_at >= now() - make_interval(mins => %(mins)s) AND error IS NOT NULL
"""

_STEP_ERRORS_SQL = """
SELECT CASE
         WHEN s->>'error' ILIKE '%%trust gate%%' THEN 'trust_gate'
         WHEN s->>'error' ILIKE '%%explicit date bounds%%'
           OR s->>'error' ILIKE '%%must declare period_start%%' THEN 'scope_discipline'
         ELSE 'sql_error'
       END AS kind,
       COUNT(*) AS n,
       MAX(left(s->>'error', 80)) AS sample
FROM chat_query_log q,
     jsonb_array_elements(CASE WHEN jsonb_typeof(q.steps) = 'array' THEN q.steps
                               ELSE '[]'::jsonb END) s
WHERE q.asked_at >= now() - make_interval(mins => %(mins)s)
  AND s->>'error' IS NOT NULL
GROUP BY 1
"""


def stuck_jobs(ctx) -> list[CheckResult]:
    mins = int(ctx.cfg["chat"]["stuck_job_minutes"])
    r = rows(ctx.pg(), _STUCK_SQL, {"mins": mins})[0]
    base = dict(source=SRC_CHAT, check="stuck_jobs", expected=f"no job queued/processing > {mins} min")
    if r["n"]:
        return [CheckResult(
            state=FAIL, severity=HIGH, category="stuck_jobs",
            title="AI Chat jobs stuck in queue/processing", actual=f"{r['n']} job(s)",
            evidence={"oldest_created_at": r["oldest"].isoformat() if r["oldest"] else None},
            actions=["Check `journalctl -u laynes-api --since -1h` for the job worker.",
                     "Do not restart laynes-api while jobs are processing unless they are "
                     "confirmed stuck; a restart marks in-flight jobs interrupted."], **base)]
    return [CheckResult(state=OK, title="No stuck chat jobs", **base)]


def errors(ctx) -> list[CheckResult]:
    c = ctx.cfg["chat"]
    mins = int(c["error_window_minutes"])
    pg = ctx.pg()
    out: list[CheckResult] = []

    failed = rows(pg, _FAILED_JOBS_SQL, {"mins": mins})
    n_failed = sum(r["n"] for r in failed)
    base = dict(source=SRC_CHAT, check="failed_jobs", expected=f"no failed jobs in {mins} min")
    if n_failed:
        out.append(CheckResult(
            state=FAIL, severity=HIGH, category="job_failures",
            title="AI Chat jobs failed", actual=f"{n_failed} in the last {mins} min",
            evidence={"by_status": [{k: r[k] for k in ("status", "error_code", "n")}
                                    for r in failed]},
            actions=["Check `journalctl -u laynes-api` around the failures.",
                     "chat_jobs.error_code tells provider errors from internal ones."], **base))
    else:
        out.append(CheckResult(state=OK, title="No failed chat jobs", **base))

    prov = rows(pg, _PROVIDER_SQL, {"mins": mins})[0]
    base = dict(source=SRC_CHAT, check="provider_errors",
                expected=f"< {c['provider_error_threshold']} provider errors in {mins} min")
    if prov["n"] >= c["provider_error_threshold"]:
        out.append(CheckResult(
            state=FAIL, severity=WARNING, category="provider_errors",
            title="AI Chat model-provider errors", actual=f"{prov['n']} in the last {mins} min",
            evidence={"sample_class": prov["sample"]},
            actions=["Usually provider rate limits (429) or outages; check the provider "
                     "status page. No data problem."], **base))
    else:
        out.append(CheckResult(state=OK, title="Provider errors below threshold",
                               actual=str(prov["n"]), **base))

    kinds = {r["kind"]: r for r in rows(pg, _STEP_ERRORS_SQL, {"mins": mins})}
    sqlerr = kinds.get("sql_error")
    base = dict(source=SRC_CHAT, check="sql_errors",
                expected=f"< {c['sql_error_threshold']} SQL errors in {mins} min")
    if sqlerr and sqlerr["n"] >= c["sql_error_threshold"]:
        out.append(CheckResult(
            state=FAIL, severity=WARNING, category="sql_errors",
            title="AI Chat SQL errors", actual=f"{sqlerr['n']} in the last {mins} min",
            evidence={"sample": sqlerr["sample"],
                      "scope_discipline_rejections": (kinds.get("scope_discipline") or {}).get("n", 0)},
            actions=["Read chat_query_log.steps errors for the window (SQL only; do not copy "
                     "questions or answers into tickets).",
                     "A missing view/column after a deploy is the usual cause."], **base))
    else:
        out.append(CheckResult(state=OK, title="SQL errors below threshold",
                               actual=str((sqlerr or {}).get("n", 0)), **base))

    hours = int(c["trust_block_window_hours"])
    tb = {r["kind"]: r for r in rows(pg, _STEP_ERRORS_SQL, {"mins": hours * 60})}.get("trust_gate")
    base = dict(source=SRC_CHAT, check="trust_gate_blocks",
                expected="answers not blocked by unverified data")
    if tb and tb["n"]:
        out.append(CheckResult(
            state=FAIL, severity=WARNING, category="trust_gate",
            title="AI Chat refused queries on unverified data",
            actual=f"{tb['n']} blocked queries in {hours}h", evidence={"sample": tb["sample"]},
            actions=["The guard worked as designed. The cause is the matching Revel / HME / "
                     "R365 data incident -- fix or explain that, not the chat."], **base))
    else:
        out.append(CheckResult(state=OK, title="No trust-gate blocks", **base))
    return out
