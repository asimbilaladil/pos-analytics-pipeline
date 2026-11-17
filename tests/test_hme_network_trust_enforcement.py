#!/usr/bin/env python3
"""Unverified HME store-days can never reach a figure, whatever SQL is run.

Run (isolated test database ONLY -- see tests/isolated_db.py; migration 65
applied by tests/test_hme_store_day_trust.py):
    DB_HOST=127.0.0.1 DB_PORT=55432 DB_NAME=laynes_test DB_USER=... DB_PASS=... \\
    DB_RO_USER=laynes_ro DB_RO_PASS=... \\
        venv/bin/python tests/test_hme_network_trust_enforcement.py   (exit 0 = pass)

PR #37 final review. For a NETWORK scope an unverified store-day (Mission Bend
2026-11-03 / 11-06, PARTIAL / RECONCILIATION_MISMATCH while the network day
reconciled) was only a model-facing WARNING: the statement ran, so the model's
SQL could sum Mission Bend's row into a "verified network total", or filter to
Mission Bend alone under a declared network scope and read the row the
single-store gate refuses. Reproduced on the isolated DB before the fix.

Pinned (all read-only, as laynes_ro through chat_sql.run_sql):
  A. rewrite unit tests: every reference shape is confined; CTE name reuse is
     refused; non-HME SQL is untouched
  B. adversarial SQL (what an LLM might write) for network/multi-store/single
     store/date-range queries: Mission Bend 11-03/11-06 never contribute; every
     other store-day is returned with exactly its stored value
  C. fail closed: trust view unavailable -> the statement errors, no rows
  D. store-restricted MCP credentials still work and are still confined
  E. end-to-end answer loop with a mocked model that ignores the warning and
     calls the sum a "verified network total": the rows are removed before
     execution and the answer carries a deterministic server note; an answer
     not touching those dates gets no note
  F. network-day rules unchanged: 11-13 (network-unreconciled) still refused
"""
import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import isolated_db  # noqa: E402

isolated_db.require_isolated()
import chat_sql as C  # noqa: E402
import hme_trust_sql as H  # noqa: E402
import store_scope  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


MB, OTHER = 25, 6
BAD = {(MB, "2026-11-03"), (MB, "2026-11-06")}


def ro(sql, params=()):
    c = C._ro_conn()
    try:
        c.set_session(readonly=True)
        with c.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        c.close()


def raw_cars(where="TRUE", verified_only=False):
    """Ground truth computed directly (bypassing run_sql)."""
    return int(ro(f"""SELECT coalesce(sum(d.drive_thru_cars), 0)
                        FROM v_hme_store_daily_llm d
                        JOIN v_hme_store_day_trust_llm t USING (establishment_id, business_date)
                       WHERE {where} {"AND t.trust_status = 'VERIFIED'" if verified_only else ""}
                    """)[0][0])


def run(sql):
    return C.run_sql(sql)


def raises(fn):
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        return str(e)
    return None


# ═══ A. rewrite unit tests ═════════════════════════════════════════════════════
print("A. rewrite")
plain = "SELECT count(*) FROM v_orders_classified WHERE business_date >= '2026-11-03'"
check("non-HME SQL is returned unchanged", H.confine_to_verified(plain) == plain)
status = "SELECT * FROM v_hme_day_completeness_llm WHERE business_date = '2026-11-03'"
check("HME status views are not rewritten", H.confine_to_verified(status) == status)
shapes = {
    "bare": "SELECT * FROM v_hme_store_daily_llm",
    "alias": "SELECT d.drive_thru_cars FROM v_hme_store_daily_llm d",
    "schema": "SELECT * FROM public.v_hme_store_daily_llm",
    "join": "SELECT 1 FROM v_hme_store_daily_llm d JOIN v_hme_outliers_daily_llm o "
            "USING (establishment_id, business_date)",
    "cte": "WITH x AS (SELECT * FROM v_hme_store_daily_llm) SELECT * FROM x",
    "union": "SELECT drive_thru_cars FROM v_hme_store_daily_llm UNION ALL "
             "SELECT drive_thru_cars FROM v_hme_store_daily_llm",
    "scalar": "SELECT (SELECT sum(drive_thru_cars) FROM v_hme_store_daily_llm) AS s",
    "exists": "SELECT 1 WHERE EXISTS (SELECT 1 FROM v_hme_store_daily_llm)",
    "lateral": "SELECT * FROM establishments e, LATERAL (SELECT drive_thru_cars FROM "
               "v_hme_store_daily_llm d WHERE d.establishment_id = e.id) z",
    "nested_join": "SELECT 1 FROM establishments e LEFT JOIN (v_hme_store_daily_llm d "
                   "JOIN v_hme_outliers_daily_llm o USING (establishment_id, business_date)) "
                   "ON d.establishment_id = e.id",
}
for name, q in shapes.items():
    out = H.confine_to_verified(q)
    n_fact = (out.count("public.v_hme_store_daily_llm AS _f")
              + out.count("public.v_hme_outliers_daily_llm AS _f"))
    n_src = q.count("v_hme_store_daily_llm") + q.count("v_hme_outliers_daily_llm")
    check(f"shape {name}: every fact-view read wrapped by the VERIFIED filter",
          n_fact == n_src and out.count("trust_status = 'VERIFIED'") == n_src
          and H.confine_to_verified(out) is not None, out)
err = raises(lambda: H.confine_to_verified(
    "WITH v_hme_store_daily_llm AS (SELECT 1 AS drive_thru_cars) "
    "SELECT * FROM v_hme_store_daily_llm"))
check("a WITH name reusing an HME data view's name is refused", err and "WITH name" in err, err)
check("run_sql refuses the CTE-name trick before execution",
      "WITH name" in (raises(lambda: run(
          "WITH v_hme_outliers_daily_llm AS (SELECT * FROM v_hme_store_daily_llm) "
          "SELECT count(*) FROM v_hme_outliers_daily_llm")) or ""))

# ═══ B. adversarial SQL ═══════════════════════════════════════════════════════
print("B. adversarial SQL through run_sql")
NOV3 = "business_date >= '2026-11-03' AND business_date < '2026-11-04'"
R1103 = "business_date BETWEEN '2026-11-01' AND '2026-11-08'"
truth_all = raw_cars(NOV3)
truth_ok = raw_cars(NOV3, verified_only=True)
mb_1103 = raw_cars(f"{NOV3} AND establishment_id = {MB}")
check("fixture: Mission Bend has a non-zero 11-03 row (else the test proves nothing)",
      mb_1103 > 0 and truth_all == truth_ok + mb_1103, f"{truth_all} = {truth_ok} + {mb_1103}")

r = run(f"SELECT sum(drive_thru_cars) AS network_cars FROM v_hme_store_daily_llm WHERE {NOV3}")
check("network SUM for 11-03 equals the VERIFIED subset, not the raw total",
      int(r["rows"][0]["network_cars"]) == truth_ok, f"{r['rows']} vs {truth_ok}")

r = run(f"SELECT establishment_id, sum(drive_thru_cars) c FROM v_hme_store_daily_llm "
        f"WHERE {NOV3} GROUP BY 1 ORDER BY 1")
got = {x["establishment_id"]: int(x["c"]) for x in r["rows"]}
want = {e: int(c) for e, c in ro(
    f"SELECT d.establishment_id, d.drive_thru_cars FROM v_hme_store_daily_llm d "
    f"JOIN v_hme_store_day_trust_llm t USING (establishment_id, business_date) "
    f"WHERE {NOV3.replace('business_date', 'd.business_date')} AND t.trust_status='VERIFIED'")}
check("per-store 11-03: Mission Bend absent, every other store exactly its stored value",
      MB not in got and got == want and len(got) >= 10, f"{len(got)} stores")

r = run(f"SELECT * FROM v_hme_store_daily_llm WHERE establishment_id = {MB} AND {NOV3}")
check("Mission Bend-only SQL (e.g. under a declared network scope): no 11-03 row",
      r["row_count"] == 0)

r = run(f"SELECT business_date, drive_thru_cars FROM v_hme_store_daily_llm "
        f"WHERE establishment_id = {MB} AND {R1103} ORDER BY 1")
dates = [x["business_date"][:10] for x in r["rows"]]
check("Mission Bend 11-01..11-08: 6 verified days, 11-03 and 11-06 absent",
      dates == ["2026-11-01", "2026-11-02", "2026-11-04", "2026-11-05", "2026-11-07",
                "2026-11-08"], str(dates))

r = run(f"SELECT sum(drive_thru_cars) s FROM v_hme_store_daily_llm "
        f"WHERE establishment_id IN ({OTHER}, {MB}) AND {R1103}")
check("multi-store range: sum equals the verified store-days only",
      int(r["rows"][0]["s"]) == raw_cars(f"establishment_id IN ({OTHER},{MB}) AND {R1103}",
                                         verified_only=True))

tricks = {
    "CTE": f"WITH x AS (SELECT * FROM v_hme_store_daily_llm WHERE {NOV3}) "
           f"SELECT sum(drive_thru_cars) s FROM x",
    "subquery": f"SELECT sum(drive_thru_cars) s FROM (SELECT * FROM v_hme_store_daily_llm "
                f"WHERE {NOV3}) q",
    "scalar subquery": f"SELECT (SELECT sum(drive_thru_cars) FROM v_hme_store_daily_llm "
                       f"WHERE {NOV3}) s",
    "UNION of store sets": f"SELECT sum(c) s FROM (SELECT drive_thru_cars c FROM "
                           f"v_hme_store_daily_llm WHERE {NOV3} AND establishment_id = {MB} "
                           f"UNION ALL SELECT drive_thru_cars FROM v_hme_store_daily_llm "
                           f"WHERE {NOV3} AND establishment_id <> {MB}) u",
    "schema-qualified": f"SELECT sum(drive_thru_cars) s FROM public.v_hme_store_daily_llm "
                        f"WHERE {NOV3}",
    "join to establishments": f"SELECT sum(d.drive_thru_cars) s FROM establishments e JOIN "
                              f"v_hme_store_daily_llm d ON d.establishment_id = e.id "
                              f"WHERE d.{NOV3.replace('AND business_date', 'AND d.business_date')}",
    "window function": f"SELECT DISTINCT sum(drive_thru_cars) OVER () s FROM "
                       f"v_hme_store_daily_llm WHERE {NOV3}",
    "ignores trust view filter": f"SELECT sum(d.drive_thru_cars) s FROM v_hme_store_daily_llm d "
                                 f"LEFT JOIN v_hme_store_day_trust_llm t USING "
                                 f"(establishment_id, business_date) WHERE "
                                 f"d.{NOV3.replace('AND business_date', 'AND d.business_date')} "
                                 f"AND (t.trust_status IS NULL OR t.trust_status <> 'nope')",
}
for name, q in tricks.items():
    r = run(q)
    check(f"trick '{name}': 11-03 network sum is the verified subset",
          int(r["rows"][0]["s"]) == truth_ok, f"{r['rows']} vs {truth_ok}")

r = run(f"SELECT establishment_id, business_date FROM v_hme_outliers_daily_llm "
        f"WHERE establishment_id = {MB} AND {R1103}")
od = {x["business_date"][:10] for x in r["rows"]}
check("outliers view: Mission Bend 11-03/11-06 absent too",
      not od & {"2026-11-03", "2026-11-06"}, str(sorted(od)))

r = run("SELECT sum(drive_thru_cars) s FROM v_hme_store_daily_llm "
        "WHERE business_date BETWEEN '2026-11-09' AND '2026-11-12'")
check("fully verified dates 11-09..11-12: figure identical to the raw total",
      int(r["rows"][0]["s"]) == raw_cars("business_date BETWEEN '2026-11-09' AND '2026-11-12'"))
r = run(f"SELECT count(*) n FROM v_hme_store_daily_llm WHERE {R1103}")
n_ok = ro(f"SELECT count(*) FROM v_hme_store_day_trust_llm WHERE {R1103} "
          "AND trust_status = 'VERIFIED'")[0][0]
check("11-01..11-08: exactly the VERIFIED store-day rows are visible (2 removed)",
      r["rows"][0]["n"] == n_ok and ro(f"SELECT count(*) FROM v_hme_store_daily_llm "
                                       f"WHERE {R1103}")[0][0] - n_ok == 2)

# ═══ C. fail closed ═══════════════════════════════════════════════════════════
print("C. fail closed")
orig_tv = H.TRUST_VIEW
H.TRUST_VIEW = "v_hme_store_day_trust_missing"
try:
    err = raises(lambda: run(f"SELECT sum(drive_thru_cars) FROM v_hme_store_daily_llm "
                             f"WHERE {NOV3}"))
    check("trust view unavailable: the statement errors, nothing is returned",
          err is not None and "does not exist" in err, err)
    check("trust view unavailable: non-HME SQL still runs",
          run("SELECT count(*) n FROM establishments")["row_count"] == 1)
finally:
    H.TRUST_VIEW = orig_tv

# ═══ D. store-restricted MCP credential ═══════════════════════════════════════
print("D. store-restricted credential")
tok = store_scope.activate([OTHER, MB])
try:
    r = run(f"SELECT establishment_id, sum(drive_thru_cars) c FROM v_hme_store_daily_llm "
            f"WHERE {NOV3} GROUP BY 1 ORDER BY 1")
    check("restricted {6,25}: only store 6 on 11-03 (store scope AND trust applied)",
          [x["establishment_id"] for x in r["rows"]] == [OTHER], str(r["rows"]))
    r = run(f"SELECT count(*) n FROM v_hme_store_day_trust_llm WHERE {NOV3}")
    check("restricted credential may read the trust view, confined to its stores",
          r["rows"][0]["n"] == 2)
finally:
    store_scope.deactivate(tok)

# ═══ E. end-to-end answer loop, model ignores the warning ═════════════════════
print("E. answer loop with a mocked model")


def text(t):
    return SimpleNamespace(type="text", text=t)


def resp(stop, *blocks):
    return SimpleNamespace(stop_reason=stop, content=list(blocks),
                           usage=SimpleNamespace(input_tokens=1, output_tokens=1))


def sql_call(i, sql, est, start, end):
    return SimpleNamespace(type="tool_use", id=f"s{i}", name="run_sql",
                           input={"sql": sql, "establishment_id": est,
                                  "period_start": start, "period_end_exclusive": end})


class FakeClient:
    def __init__(self, script):
        self.script, self.calls = list(script), []
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kw):
        self.calls.append(kw)
        return self.script.pop(0) if self.script else resp("end_turn", text("done"))


def answer(question, script):
    fake = FakeClient(script)
    orig = C.anthropic.Anthropic
    C.anthropic.Anthropic = lambda **kw: fake
    try:
        return C.answer_question([], question), fake
    finally:
        C.anthropic.Anthropic = orig


def tool_payloads(fake):
    out = []
    for call in fake.calls:
        for m in call["messages"]:
            if m["role"] == "user" and isinstance(m["content"], list):
                for c in m["content"]:
                    if isinstance(c, dict) and c.get("type") == "tool_result":
                        out.append(c["content"])
    return out


UNSAFE = f"SELECT sum(drive_thru_cars) AS network_cars FROM v_hme_store_daily_llm WHERE {NOV3}"
r, f = answer("Total drive-thru cars across all stores on Nov 3 2026?", [
    resp("tool_use", sql_call(1, UNSAFE, None, "2026-11-03", "2026-11-04")),
    resp("end_turn", text("Verified network total: 99999 cars across all 11 stores."))])
payload = json.loads(next(p for p in tool_payloads(f) if "network_cars" in p))
check("unsafe network SQL executed only over verified rows",
      int(payload["rows"][0]["network_cars"]) == truth_ok, str(payload["rows"]))
check("tool result tells the model the rows were removed (verified subset)",
      "VERIFIED SUBSET" in payload.get("hme_trust_note", "")
      and "LCF Mission Bend" in payload["hme_trust_note"])
check("step log records the excluded store-day",
      any((st.get("hme_store_days_excluded") or [{}])[0].get("business_date") == "2026-11-03"
          for st in r.steps))
check("final answer carries the deterministic server note even though the model "
      "called it a verified network total",
      r.answer.endswith(C.hme_exclusion_note(
          [s for st in r.steps for s in (st.get("hme_store_days_excluded") or [])]))
      and C.HME_SERVER_EXCLUSION_PREFIX in r.answer and "2026-11-03" in r.answer
      and "not a complete network total" in r.answer, r.answer[-300:])

BIG = (f"SELECT establishment_id, business_date, drive_thru_cars, repeat('x', 1500) AS pad "
       f"FROM v_hme_store_daily_llm WHERE {R1103}")
r, f = answer("All stores Nov 1-8, every row", [
    resp("tool_use", sql_call(1, BIG, None, "2026-11-01", "2026-11-09")),
    resp("end_turn", text("Here are the rows."))])
p = json.loads(next(p for p in tool_payloads(f) if "truncated to 200 rows" in p))
check("truncated (>60 KB) tool payload still carries the trust note",
      "VERIFIED SUBSET" in p.get("hme_trust_note", "")
      and not any(x["establishment_id"] == MB and x["business_date"][:10] in
                  ("2026-11-03", "2026-11-06") for x in p["rows"])
      and C.HME_SERVER_EXCLUSION_PREFIX in r.answer)

MBONLY = f"SELECT * FROM v_hme_store_daily_llm WHERE establishment_id = {MB} AND {NOV3}"
r, f = answer("Mission Bend drive-thru on Nov 3?", [
    resp("tool_use", sql_call(1, MBONLY, None, "2026-11-03", "2026-11-04")),
    resp("end_turn", text("No data."))])
p = json.loads(next(p for p in tool_payloads(f) if '"row_count"' in p))
check("Mission Bend-only SQL under a NETWORK scope: 0 rows, note appended",
      p["row_count"] == 0 and C.HME_SERVER_EXCLUSION_PREFIX in r.answer)

r, f = answer("Mission Bend drive-thru on Nov 3?", [
    resp("tool_use", sql_call(1, MBONLY, MB, "2026-11-03", "2026-11-04")),
    resp("end_turn", text("Mission Bend 11-03 is unverified."))])
check("Mission Bend single-store scope: still refused before execution (unchanged)",
      any("store-level reconciliation NOT verified" in (st.get("error") or "")
          for st in r.steps), str([st.get("error") for st in r.steps]))

SAFE = ("SELECT sum(drive_thru_cars) AS cars FROM v_hme_store_daily_llm "
        "WHERE business_date >= '2026-11-09' AND business_date < '2026-11-13'")
r, f = answer("Total cars Nov 9-12?", [
    resp("tool_use", sql_call(1, SAFE, None, "2026-11-09", "2026-11-13")),
    resp("end_turn", text("Total: N cars."))])
check("verified-only period: no exclusion, no note, answer unchanged",
      r.answer == "Total: N cars." and not any(st.get("hme_store_days_excluded")
                                               for st in r.steps), r.answer)

# ═══ F. network rules unchanged ═══════════════════════════════════════════════
print("F. network rules unchanged")
err = raises(lambda: C.enforce_scope(
    "SELECT sum(drive_thru_cars) FROM v_hme_store_daily_llm WHERE business_date >= "
    "'2026-11-13' AND business_date < '2026-11-14'", None, "2026-11-13", "2026-11-14", {}))
check("network-unreconciled 11-13 is still refused before execution",
      err and "HME cross-source reconciliation failed" in err, err)

failed = [x for x in results if not x[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
