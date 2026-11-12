#!/usr/bin/env python3
"""Per-store HME ingestion: one absent store no longer refuses the whole day.

Run:  venv/bin/python tests/test_hme_partial_stores.py     (exit 0 = pass)
      HME_DOWNLOADER_DIR=<downloader checkout> to test an unmerged extractor.

2026-10/11 incident: from 2026-10-14 every HME day was refused because one
VERIFIED store (Mission Bend, HME 1158614) was absent from every report --
the HME account no longer returns it, even for dates it loaded before. The
guard treated one absent store as "the day cannot represent Laynes coverage",
retried a deterministic condition three times (37 min), and loaded nothing
for the other ten stores for four weeks.

These checks pin:
  1. one inaccessible store while the others succeed
  2. one unmapped location (Downtown Houston): SKIPPED, never guessed
  3. empty responses: no VERIFIED store at all still fails the day; a
     measure-less store row is dropped, never written over a valid row
  4. a failed date followed by successful dates (backfill keeps going, and
     keeps the failed date failed)
  5. safe retries and idempotent loads; 9. no duplicate rows
  6. partial-success reporting and exit codes (never a green partial)
  7. reconciliation failure: per store, and day-level
  8. interrupted backfill resumes without redoing loaded dates
  + the delayed reconciliation retry still works with a NO_ACCESS store
  + chat/MCP gate: per-store HME verdicts and explicit unavailable reasons
Database: the loader tests run inside ONE transaction that is rolled back --
migration 61's DDL is applied inside it, so production is never modified.
"""
import copy
import datetime
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(".env", override=True)
DOWNLOADER = os.environ.get("HME_DOWNLOADER_DIR", "/opt/hme-report-downloader")
sys.path.insert(0, DOWNLOADER)
from hme import guard  # noqa: E402
import chat_sql as C  # noqa: E402
import hme_backfill as B  # noqa: E402
import hme_daily_ingest as I  # noqa: E402
import hme_load as L  # noqa: E402
import hme_reconciliation_retry as R  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


MB = "1158614"          # Mission Bend (VERIFIED, establishment 25)
BASE = "/var/lib/laynes/hme/raw/2026-10-13/querydata-facts.json"
with open(BASE) as f:
    BASE_DOC = json.load(f)


def doc_without(*stores, reason="NOT_VISIBLE"):
    d = copy.deepcopy(BASE_DOC)
    for k in list(d["facts"]):
        d["facts"][k] = [r for r in d["facts"][k] if str(r["hme_store_number"]) not in stores]
    # source totals must describe what the source returned (it never saw them)
    for k, rows in d["facts"].items():
        tot = (d.get("source_totals") or {}).get(k)
        if tot and k == "store_daily":
            for col in ("total_orders", "regular_orders", "disastrous_orders"):
                if col in tot:
                    tot[col] = sum(r.get(col) or 0 for r in rows)
        if tot and k == "outliers_daily" and "car_departures" in tot:
            tot["car_departures"] = sum(r.get("car_departures") or 0 for r in rows)
    d["completeness"] = dict(d.get("completeness") or {})
    d["completeness"]["partial_store_policy"] = True
    d["completeness"]["missing_verified_detail"] = [
        {"hme_store_number": s, "hme_store_name": "x", "reason": reason} for s in stores]
    d["freshness_ok"] = True
    return d


MAPPING = [{"hme_store_number": n, "mapping_status": "VERIFIED", "revel_establishment_id": e}
           for n, e in [("108", 6), ("109", 7), ("00111", 14), ("115", 15), ("16", 20),
                        (MB, 25), ("15", 26), ("26", 32), ("1163022", 36), ("1", 40),
                        ("1171670", 54)]]
ESTS = [6, 7, 14, 15, 20, 25, 26, 32, 36, 40, 48, 54]

# ---------------------------------------------------------------------------
print("1/2. grading: one inaccessible store, one unmapped location")
inv = {m["hme_store_number"]: {"hme_store_name": "x", "mapping_status": "VERIFIED"} for m in MAPPING}
seen = {m["hme_store_number"] for m in MAPPING} - {MB}
try:
    guard.assess_completeness(seen, inv, context="t")
    check("default policy still refuses a missing VERIFIED store", False)
except guard.IncompleteSourceError:
    check("default policy still refuses a missing VERIFIED store", True)
det = guard.assess_completeness(seen, inv, context="t", allow_missing_verified=True,
                                visible_numbers=set(seen))
check("partial policy grades the absent store NOT_VISIBLE (not in any response)",
      det["missing_verified_detail"] == [{"hme_store_number": MB, "hme_store_name": "x",
                                          "reason": "NOT_VISIBLE"}])
det2 = guard.assess_completeness(seen, inv, context="t", allow_missing_verified=True,
                                 visible_numbers=set(seen) | {MB})
check("visible but without rows -> NO_DATA",
      det2["missing_verified_detail"][0]["reason"] == "NO_DATA")
d1 = doc_without(MB)
ok1, rec1 = L.reconcile(d1)
g = {r["revel_establishment_id"]: r for r in L.grade_stores(d1, MAPPING, ESTS, rec1)}
check("Mission Bend FAILED / NO_ACCESS", (g[25]["status"], g[25]["reason_code"]) == ("FAILED", "NO_ACCESS"))
check("Downtown Houston SKIPPED / UNMAPPED, no HME number guessed",
      (g[48]["status"], g[48]["reason_code"], g[48]["hme_store_number"]) == ("SKIPPED", "UNMAPPED", None))
others = [e for e in ESTS if e not in (25, 48)]
check("the ten other stores are graded on their own merits (SUCCESS or a named PARTIAL)",
      all(g[e]["status"] in ("SUCCESS", "PARTIAL") for e in others)
      and all(g[e]["reason_code"] in ("OK", "RECONCILIATION_MISMATCH", "FACT_SET_MISSING")
              for e in others), json.dumps({e: g[e]["status"] for e in others}))
check("day reconciliation is unaffected by the absent store (totals are of returned stores)",
      all(c["ok"] for c in rec1["checks"]
          if c["check"] in ("pa_total_orders_vs_outliers_car_departures",
                            "store_rows_sum_vs_source_total")) == all(
          c["ok"] for c in L.reconcile(copy.deepcopy(BASE_DOC))[1]["checks"]
          if c["check"] in ("pa_total_orders_vs_outliers_car_departures",
                            "store_rows_sum_vs_source_total")))

# ---------------------------------------------------------------------------
print("3. empty responses")
try:
    guard.assess_completeness(set(), inv, context="t", allow_missing_verified=True)
    check("no VERIFIED store at all is still an account-wide failure", False)
except guard.IncompleteSourceError as e:
    check("no VERIFIED store at all is still an account-wide failure", "account-wide" in str(e))
emp = copy.deepcopy(BASE_DOC)
sd = emp["facts"]["store_daily"]
victim = next(r for r in sd if str(r["hme_store_number"]) == "115")
for k in ("total_orders", "lane_total_avg_seconds", "lane_queue_avg_seconds"):
    victim[k] = None
dropped = L.drop_empty_rows(emp)
check("a measure-less store row is dropped before loading",
      dropped.get("store_daily") == ["115"]
      and not any(str(r["hme_store_number"]) == "115" for r in emp["facts"]["store_daily"]))

# ---------------------------------------------------------------------------
print("1/5/6/9. loader in a rolled-back transaction (migration 61 applied inside it)")
MIG = open("migrations/61_hme_store_ingest_result.sql").read()
MIG = MIG.replace("BEGIN;", "").replace("COMMIT;", "")
class NoCommit:
    """The loader commits; inside this test everything must roll back."""
    def __init__(self, c):
        self._c = c

    def commit(self):
        pass

    def __getattr__(self, k):
        return getattr(self._c, k)


real = L.connect()
conn = NoCommit(real)
try:
    cur = conn.cursor()
    cur.execute("SELECT to_regclass('hme_store_ingest_result')")
    if cur.fetchone()[0] is None:
        cur.execute(MIG)
    cur.execute("""SELECT total_orders FROM hme_store_daily
                    WHERE hme_store_number = %s AND business_date = '2026-10-13'""", (MB,))
    mb_before = cur.fetchone()
    try:
        L.load(doc_without(MB), conn, dry_run=False, allow_partial_stores=False)
        check("loader default policy refuses a missing VERIFIED store", False)
    except L.LoadAborted:
        check("loader default policy refuses a missing VERIFIED store", True)
    unexplained = doc_without(MB)
    unexplained["completeness"]["missing_verified_detail"] = []
    try:
        L.load(unexplained, conn, allow_partial_stores=True)
        check("partial policy still refuses an UNEXPLAINED missing VERIFIED store", False)
    except L.LoadAborted:
        check("partial policy still refuses an UNEXPLAINED missing VERIFIED store", True)

    r1 = L.load(doc_without(MB), conn, allow_partial_stores=True)
    cur.execute("SELECT count(*) FROM hme_store_daily WHERE business_date = '2026-10-13'")
    n1 = cur.fetchone()[0]
    st = {x["revel_establishment_id"]: x for x in r1["store_results"]}
    check("partial day loads: run status 'partial', never 'succeeded'", r1["status"] == "partial")
    check("12 per-store results recorded for the run",
          len(r1["store_results"]) == 12)
    cur.execute("SELECT count(*), count(DISTINCT revel_establishment_id) FROM hme_store_ingest_result "
                "WHERE run_id = %s", (r1["run_id"],))
    check("...and persisted in hme_store_ingest_result", cur.fetchone() == (12, 12))
    cur.execute("""SELECT total_orders FROM hme_store_daily
                    WHERE hme_store_number = %s AND business_date = '2026-10-13'""", (MB,))
    check("Mission Bend's existing valid row is untouched (absence is not written)",
          cur.fetchone() == mb_before)
    check("exit code 4 (red) for a day with a FAILED store; never 0",
          I.classify_store_results(r1) == I.EXIT_PARTIAL_STORES
          or I.classify_store_results(r1) == I.EXIT_RECONCILIATION_FAILED)
    r2 = L.load(doc_without(MB), conn, allow_partial_stores=True)
    cur.execute("SELECT count(*) FROM hme_store_daily WHERE business_date = '2026-10-13'")
    n2 = cur.fetchone()[0]
    check("re-running the same day is idempotent (no duplicate store-day rows)", n1 == n2, f"{n1} vs {n2}")
    cur.execute("""SELECT count(*) FROM (SELECT hme_store_number, business_date, count(*)
                     FROM hme_store_daily WHERE business_date = '2026-10-13'
                    GROUP BY 1, 2 HAVING count(*) > 1) x""")
    check("no duplicate (store, date) rows", cur.fetchone()[0] == 0)
    check("each retry is its own run with its own results (audit kept)",
          r2["run_id"] != r1["run_id"] and len(r2["store_results"]) == 12)
    # retry eligibility with a NO_ACCESS store (latest run for the date = r2)
    cur.execute("""UPDATE hme_ingest_run SET reconciliation_ok = false WHERE run_id = %s""",
                (r2["run_id"],))
    run = R.latest_run(conn, "2026-10-13")
    el, why = R.assess_retry_eligibility(run)
    check("delayed reconciliation retry still eligible when the only missing VERIFIED store is NO_ACCESS",
          el, why)
    # empty row over a valid row
    cur.execute("""SELECT total_orders FROM hme_store_daily
                    WHERE hme_store_number = '115' AND business_date = '2026-10-13'""")
    shep_before = cur.fetchone()
    e2 = doc_without(MB)
    for r in e2["facts"]["store_daily"]:
        if str(r["hme_store_number"]) == "115":
            for k in ("total_orders", "lane_total_avg_seconds", "lane_queue_avg_seconds"):
                r[k] = None
    r3 = L.load(e2, conn, allow_partial_stores=True)
    cur.execute("""SELECT total_orders FROM hme_store_daily
                    WHERE hme_store_number = '115' AND business_date = '2026-10-13'""")
    check("an empty response never overwrites a valid historical row",
          cur.fetchone() == shep_before and shep_before is not None)
    s3 = {x["revel_establishment_id"]: x for x in r3["store_results"]}
    check("...and that store is reported PARTIAL / FACT_SET_MISSING, not SUCCESS",
          (s3[15]["status"], s3[15]["reason_code"]) == ("PARTIAL", "FACT_SET_MISSING"))
finally:
    real.rollback()
    real.close()

# ---------------------------------------------------------------------------
print("6/7. reconciliation failures and exit codes")
d7 = doc_without(MB)
tr = d7["facts"]["trend_store_daily"]
target = next(r for r in tr if str(r["hme_store_number"]) == "115")
target["total_cars"] = (target["total_cars"] or 0) + 7
ok7, rec7 = L.reconcile(d7)
g7 = {r["revel_establishment_id"]: r for r in L.grade_stores(d7, MAPPING, ESTS, rec7)}
check("per-store mismatch: that store PARTIAL / RECONCILIATION_MISMATCH",
      (g7[15]["status"], g7[15]["reason_code"]) == ("PARTIAL", "RECONCILIATION_MISMATCH")
      and g7[15]["detail"]["store_check_failed"])
check("reconciliation failure -> exit 2 (delayed retry path), never 0",
      I.classify_store_results({"status": "partial", "reconciliation_ok": False,
                                "store_results": list(g7.values())}) == I.EXIT_RECONCILIATION_FAILED)
dd = doc_without(MB)
dd["source_totals"]["store_daily"]["total_orders"] += 5
okd, recd = L.reconcile(dd)
gd = L.grade_stores(dd, MAPPING, ESTS, recd)
check("day-level check failure: every returned store is PARTIAL, none SUCCESS",
      not okd and not any(r["status"] == "SUCCESS" for r in gd))
check("summary groups stores by status",
      L.summarise_results(list(g.values()))["FAILED"] == [25]
      and L.summarise_results(list(g.values()))["SKIPPED"] == [48])
check("a clean day is exit 0", I.classify_store_results(
    {"status": "succeeded", "reconciliation_ok": True, "store_results": []}) == I.EXIT_OK)
check("exit 4 is not in the unit's SuccessExitStatus (it alerts)",
      " 4" not in open("systemd/hme-daily-ingest.service").read().split("SuccessExitStatus=")[1].split("\n")[0]
      if os.path.exists("systemd/hme-daily-ingest.service") else True)

# ---------------------------------------------------------------------------
print("4/8. backfill: failed date then successes; interruption and resume")
D = [datetime.date(2026, 10, 14) + datetime.timedelta(days=i) for i in range(4)]
calls = []


def fake_ingest(outcomes):
    def fn(d):
        calls.append(d)
        o = outcomes.get(d, "ok")
        if o == "fail":
            return I.EXIT_HARD_FAILURE, None, "extractor rc=1"
        if o == "boom":
            raise RuntimeError("chromium crashed")
        return (I.EXIT_PARTIAL_STORES,
                {"run_id": 1, "status": "partial", "reconciliation_ok": True, "rows_written": 40,
                 "store_results": [{"revel_establishment_id": 25, "status": "FAILED"}]}, "partial")
    return fn


with tempfile.TemporaryDirectory() as td:
    ckp = os.path.join(td, "ck.json")
    ck = B.load_checkpoint(ckp)
    today = D[-1] + datetime.timedelta(days=1)
    todo = B.plan(D[0], D[-1], ck, today=today)
    B.run(todo, ingest_fn=fake_ingest({"2026-10-14": "fail", "2026-10-16": "boom"}),
          ck=ck, ck_path=ckp, pause=0, fh=None)
    states = {d: ck["dates"][d.isoformat()]["state"] for d in D}
    check("a failed date does not stop later dates", states[D[1]] == "loaded" and states[D[3]] == "loaded")
    check("failed and crashed dates stay 'failed' (never skipped over)",
          states[D[0]] == "failed" and states[D[2]] == "failed")
    check("overall exit is a failure while any date failed",
          B.overall_exit(ck, D) == I.EXIT_HARD_FAILURE)
    ck2 = B.load_checkpoint(ckp)
    check("resume plans ONLY the failed dates", B.plan(D[0], D[-1], ck2, today=today) == [D[0], D[2]])
    calls.clear()
    B.run(B.plan(D[0], D[-1], ck2, today=today), ingest_fn=fake_ingest({}), ck=ck2,
          ck_path=ckp, pause=0, fh=None)
    check("resumed run re-did only the failed dates", calls == ["2026-10-14", "2026-10-16"])
    check("all loaded, some stores failed -> exit 4 (not green)",
          B.overall_exit(B.load_checkpoint(ckp), D) == I.EXIT_PARTIAL_STORES)
    check("attempt counts kept per date", B.load_checkpoint(ckp)["dates"]["2026-10-14"]["attempts"] == 2)
    check("plan never includes today or later",
          B.plan(D[0], D[-1] + datetime.timedelta(days=5), {"dates": {}}, today=today) == D)

# ---------------------------------------------------------------------------
print("orchestrator: no retry burn on a loaded partial day")
ext_calls = []
_re, _ld = I.run_extractor, L.load
with tempfile.TemporaryDirectory() as td:
    fp = os.path.join(td, "querydata-facts.json")
    dd = doc_without(MB)
    dd["business_date"] = dd["requested_date"] = "2026-10-13"
    json.dump(dd, open(fp, "w"))
    I.run_extractor = lambda date, env, fh: (ext_calls.append(date), (0, fp))[1]
    L.load = lambda doc, conn, dry_run=False, allow_partial_stores=False: {
        "run_id": 9, "status": "partial", "store_count": 10, "rows_received": 1,
        "rows_written": 1, "goal_stats": {}, "reconciliation_ok": True,
        "reconciliation": {"checks": [], "warnings": []}, "completeness": {},
        "store_results": L.grade_stores(doc, MAPPING, ESTS, L.reconcile(doc)[1])}
    _conn = L.connect
    L.connect = lambda: type("X", (), {"closed": False, "close": lambda s: None,
                                       "rollback": lambda s: None})()
    try:
        code, res, why = I.ingest_date("2026-10-13", env={}, fh=None, max_attempts=3, backoff=[0, 0, 0])
    finally:
        I.run_extractor, L.load, L.connect = _re, _ld, _conn
check("a loaded day with a NO_ACCESS store is extracted ONCE (no 3x retry, no 37 min)",
      ext_calls == ["2026-10-13"], str(ext_calls))
check("...and exits 4 or 2, never 0", code in (I.EXIT_PARTIAL_STORES, I.EXIT_RECONCILIATION_FAILED))
check("extractor is invoked with --partial-stores",
      "--partial-stores" in open("hme_daily_ingest.py").read() and I.PARTIAL_STORES)

# ---------------------------------------------------------------------------
print("chat / MCP: per-store HME verdicts")
base_h = {"available": True, "per_day": [{"business_date": "2026-11-10"}],
          "latest_business_date": "2026-11-11", "freshness_status": "CURRENT",
          "unreconciled_days": [], "incomplete_days": []}
net = dict(base_h, missing_verified_stores=[], unavailable_stores_in_scope=[
    {"establishment_id": 25, "store_name": "LCF Mission Bend",
     "missing_loaded_dates": ["2026-10-14", "2026-11-10"], "latest_business_date": "2026-10-13",
     "latest_result_reason": "NO_ACCESS"}])
f, w = C._hme_gate(net, "2026-10-14", "2026-11-11")
check("network scope: an absent store does NOT fail the gate", not f, str(f))
check("...it is named with its reason, and the network-total warning is given",
      any("LCF Mission Bend" in x and "NO_ACCESS" in x and "network total" in x for x in w))
single = dict(base_h, missing_verified_stores=["2026-11-09"])
f2, _ = C._hme_gate(single, "2026-11-09", "2026-11-11")
check("single store missing its own loaded day: still a hard FAIL (unchanged)", bool(f2))
mb = {"available": False, "latest_business_date": "2026-11-11", "freshness_status": "CURRENT",
      "mapped_store_coverage": {"revel_establishments_in_scope": 1, "unmapped_establishments": []},
      "store_status": [{"establishment_id": 25, "store_name": "LCF Mission Bend",
                        "hme_mapping_status": "VERIFIED", "latest_business_date": "2026-10-13",
                        "latest_result_date": "2026-11-11", "latest_result_status": "FAILED",
                        "latest_result_reason": "NO_ACCESS"}]}
rs = C.hme_unavailable_reason(mb, "2026-11-01", "2026-11-12")
check("Mission Bend: 'HME NO ACCESS', mapped, with its own latest date -- never 'unmapped'",
      rs.startswith("HME NO ACCESS") and "IS mapped" in rs and "2026-10-13" in rs
      and "NOT MAPPED" not in rs, rs)
dt = dict(mb, mapped_store_coverage={"revel_establishments_in_scope": 1,
                                     "unmapped_establishments": [48]},
          store_status=[{"establishment_id": 48, "hme_mapping_status": "UNMAPPED"}])
check("Downtown Houston: 'HME NOT MAPPED'",
      C.hme_unavailable_reason(dt, "2026-11-01", "2026-11-12").startswith("HME NOT MAPPED"))
check("status view is allowlisted as an ungated reference relation",
      "v_hme_store_status_llm" in C._ALLOWED_RELATIONS
      and "v_hme_store_status_llm" in C._REFERENCE_RELATIONS)
import mcp_server.analytics as A  # noqa: E402
check("MCP drive-thru mapping comes from the maintained mapping, not 'has rows'",
      "v_hme_store_status_llm" in A._HME_MAPPED_SQL and "VERIFIED" in A._HME_MAPPED_SQL)

# A broken HME read must never break a Revel answer (read-only, live).
_hb = C._hme_block


def _broken(cur, *a, **k):
    cur.execute("SELECT 1 FROM hme_view_that_does_not_exist")


C._hme_block = _broken
try:
    m = C.meta_extract(15, "2026-11-04", "2026-11-05")
    check("Revel profile survives a failed HME read (savepoint), HME reported unknown",
          m.get("reconciliation", {}).get("status") in ("PASS", "WARN", "FAIL")
          and (m.get("hme") or {}).get("available") is None, str(m.get("hme"))[:120])
except Exception as e:  # noqa: BLE001
    check("Revel profile survives a failed HME read (savepoint), HME reported unknown",
          False, f"{type(e).__name__}: {str(e)[:120]}")
finally:
    C._hme_block = _hb

passed = sum(ok for _, ok, _ in results)
print(f"\nHME partial stores: {passed} passed, {len(results) - passed} failed, {len(results)} total")
sys.exit(0 if passed == len(results) else 1)
