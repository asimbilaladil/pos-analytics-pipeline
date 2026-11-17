#!/usr/bin/env python3
"""Store-scoped HME backfill (migration 64): Mission Bend only, nothing else moves.

Run:  venv/bin/python tests/test_hme_store_scoped_backfill.py   (exit 0 = pass)

HME 1158614 (LCF Mission Bend, establishment 25) was invisible to the
integration account 2026-10-14..11-16. The ten other stores are loaded for
those dates; Mission Bend is not. The normal backfill would re-write the ten
stores, and v_hme_day_completeness_llm took the LATEST run per date as the
day's network verdict, so a Mission-Bend-only run could have turned the
unreconciled 2026-10-14/15 into reconciled days for every store.

Pinned here:
  A. migration 64: additive; the day view's output is unchanged for existing
     data; the rollback refuses while scoped runs exist and restores otherwise
  B. a scoped load writes ONLY Mission Bend: other stores' rows, goals,
     per-store results and every day verdict are byte-identical afterwards;
     2026-10-14 stays partial / unreconciled (the chat HME gate still blocks it)
  C. duplicate runs are idempotent (one row per store-date, same values)
  D. missing source data: nothing written; a failed scoped run never becomes
     the day's verdict and never fools the reconciliation retry
  E. scope guards: mapping must be active VERIFIED to the expected
     establishment; an unknown store in the extract still aborts
  F. the planner preserves existing history and is restartable; partial
     failures stay failed and are retried; the CLI refuses unsafe arguments
  G. the normal (unscoped) path is unchanged and works before migration 64
Database: everything runs in ONE session on TEMP copies of the HME tables and
views (pg_temp shadows public), inside a transaction that is rolled back.
Production objects are never altered or locked beyond a plain read.
"""
import copy
import datetime
import hashlib
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(".env", override=True)
import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402

import chat_sql as C  # noqa: E402
import hme_backfill as B  # noqa: E402
import hme_daily_ingest as I  # noqa: E402
import hme_load as L  # noqa: E402
import hme_reconciliation_retry as R  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


MB, EST = "1158614", 25
BASE = "/var/lib/laynes/hme/raw/2026-10-13/querydata-facts.json"     # last day MB loaded
with open(BASE) as f:
    BASE_DOC = json.load(f)
MIG = open("migrations/64_hme_store_scoped_runs.sql").read()
RB = open("docs/rollback/64_hme_store_scoped_runs_rollback.sql").read()
TABLES = ("hme_ingest_run", "hme_store_daily", "hme_outliers_daily", "hme_store_ingest_result",
          "hme_goal_history", "hme_goal_conflict")
VIEWS = ("v_hme_store_daily_llm", "v_hme_outliers_daily_llm", "v_hme_goal_history_llm",
         "v_hme_store_status_llm", "v_hme_day_completeness_llm")


def doc_for(day, *, drop=(), add_unknown=False):
    """The real 2026-10-13 extract (all 11 stores incl. Mission Bend) relabelled
    to another business date; optionally without some stores."""
    d = copy.deepcopy(BASE_DOC)
    d["business_date"] = d["requested_date"] = day
    for k in list(d["facts"]):
        rows = [r for r in d["facts"][k] if str(r["hme_store_number"]) not in drop]
        for r in rows:
            for key in ("business_date", "observed_for_date"):
                if key in r:
                    r[key] = day
        d["facts"][k] = rows
    if add_unknown:
        x = dict(d["facts"]["store_daily"][0], hme_store_number="999999")
        d["facts"]["store_daily"].append(x)
    d["freshness_ok"] = True
    return d


def sql_body(text):
    import re
    text = re.sub(r"--[^\n]*", "", text)
    return text.replace("BEGIN;", "").replace("COMMIT;", "")


class NoCommit:
    """The loader commits; in this sandbox nothing may be committed or rolled back."""
    def __init__(self, c):
        self._c = c

    def commit(self):
        pass

    def rollback(self):
        pass

    def __getattr__(self, k):
        return getattr(self._c, k)


def make_sandbox(real, apply_migration=True):
    """TEMP copies of the HME tables (with data) and views, in this session."""
    cur = real.cursor()
    viewdefs = {}
    for v in VIEWS:                         # read BEFORE shadowing: unqualified text
        cur.execute("SELECT pg_get_viewdef(%s::regclass, true)", ("public." + v,))
        viewdefs[v] = cur.fetchone()[0]
    for t in TABLES:
        cur.execute(f"CREATE TEMP TABLE {t} (LIKE public.{t} INCLUDING DEFAULTS "
                    f"INCLUDING CONSTRAINTS INCLUDING INDEXES)")
        cur.execute(f"INSERT INTO pg_temp.{t} SELECT * FROM public.{t}")
        # serial / identity columns: a temp sequence, never the production one
        cur.execute("""SELECT a.attname, a.attidentity <> '' AS ident,
                              pg_get_expr(d.adbin, d.adrelid) AS dflt
                         FROM pg_attribute a LEFT JOIN pg_attrdef d
                           ON d.adrelid = a.attrelid AND d.adnum = a.attnum
                        WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped""",
                    ("public." + t,))
        for col, ident, dflt in cur.fetchall():
            if ident or (dflt and "nextval" in dflt):
                cur.execute(f"SELECT COALESCE(max({col}), 0) + 1000000 FROM pg_temp.{t}")
                start = cur.fetchone()[0]
                cur.execute(f"CREATE TEMP SEQUENCE {t}_{col}_sbx START {start}")
                cur.execute(f"ALTER TABLE pg_temp.{t} ALTER COLUMN {col} "
                            f"SET DEFAULT nextval('pg_temp.{t}_{col}_sbx')")
    cur.execute("SELECT n.nspname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.oid = 'hme_ingest_run'::regclass")
    assert cur.fetchone()[0].startswith("pg_temp"), "sandbox does not shadow hme_ingest_run"
    for v in VIEWS:
        cur.execute(f"CREATE TEMP VIEW {v} AS {viewdefs[v]}")
    if apply_migration:
        cur.execute(sql_body(MIG).replace("CREATE OR REPLACE VIEW", "CREATE OR REPLACE TEMP VIEW"))
    return viewdefs


def h(cur, sql, params=()):
    cur.execute(sql, params)
    return hashlib.sha256(json.dumps(cur.fetchall(), default=str).encode()).hexdigest()


def snapshot(cur):
    """Everything a Mission-Bend-only load must not change."""
    return {
        "other_store_daily": h(cur, "SELECT * FROM hme_store_daily WHERE hme_store_number <> %s "
                                    "ORDER BY hme_store_number, business_date", (MB,)),
        "other_outliers": h(cur, "SELECT * FROM hme_outliers_daily WHERE hme_store_number <> %s "
                                 "ORDER BY hme_store_number, business_date", (MB,)),
        "other_goals": h(cur, "SELECT * FROM hme_goal_history WHERE hme_store_number <> %s "
                              "ORDER BY hme_store_number, effective_from_date", (MB,)),
        "other_results": h(cur, "SELECT * FROM hme_store_ingest_result "
                                "WHERE revel_establishment_id IS DISTINCT FROM %s "
                                "ORDER BY run_id, revel_establishment_id, hme_store_number", (EST,)),
        "day_view": h(cur, "SELECT * FROM v_hme_day_completeness_llm ORDER BY business_date"),
        "network_runs": h(cur, "SELECT * FROM hme_ingest_run WHERE store_scope IS NULL ORDER BY run_id"),
        "other_status": h(cur, "SELECT * FROM v_hme_store_status_llm WHERE establishment_id <> %s "
                               "ORDER BY establishment_id", (EST,)),
        "mb_history_to_1013": h(cur, "SELECT * FROM hme_store_daily WHERE hme_store_number = %s "
                                     "AND business_date <= '2026-10-13' ORDER BY business_date", (MB,)),
    }


# ═══ A + B + C + D + E: one sandbox with migration 64 ═══════════════════════
real = L.connect()
real.autocommit = False
conn = NoCommit(real)
try:
    print("A. migration 64 (on TEMP copies)")
    cur = real.cursor()
    up = MIG.upper()
    check("A additive only: no DROP / DELETE / UPDATE / INSERT / TRUNCATE / GRANT / REVOKE",
          not any(w in sql_body(MIG).upper() for w in
                  ("DROP ", "DELETE ", "UPDATE ", "INSERT ", "TRUNCATE", "GRANT ", "REVOKE ")))
    check("A the new column is nullable with no default (metadata-only add)",
          "ADD COLUMN IF NOT EXISTS STORE_SCOPE TEXT[];" in up)
    make_sandbox(real, apply_migration=True)
    cur.execute("SELECT * FROM public.v_hme_day_completeness_llm LIMIT 0")
    pub_cols = [d.name for d in cur.description]
    cur.execute("SELECT * FROM v_hme_day_completeness_llm LIMIT 0")
    new_cols = [d.name for d in cur.description]
    check("A day view keeps the same columns in the same order", pub_cols == new_cols)
    cur.execute("""SELECT count(*) FROM ((SELECT * FROM public.v_hme_day_completeness_llm
                                          EXCEPT SELECT * FROM v_hme_day_completeness_llm)
                                         UNION ALL (SELECT * FROM v_hme_day_completeness_llm
                                          EXCEPT SELECT * FROM public.v_hme_day_completeness_llm)) x""")
    check("A day view output identical to production for all existing data", cur.fetchone()[0] == 0)
    cur.execute("SELECT count(*) FROM hme_ingest_run WHERE store_scope IS NOT NULL")
    check("A every existing run is network-wide (store_scope NULL)", cur.fetchone()[0] == 0)
    try:
        cur.execute("SAVEPOINT ck")
        cur.execute("UPDATE hme_ingest_run SET store_scope = '{}' WHERE run_id = (SELECT max(run_id) FROM hme_ingest_run)")
        cur.execute("RELEASE SAVEPOINT ck")
        check("A CHECK rejects an empty store_scope", False)
    except psycopg2.errors.CheckViolation:
        cur.execute("ROLLBACK TO SAVEPOINT ck")
        check("A CHECK rejects an empty store_scope", True)

    print("B. scoped load writes only Mission Bend")
    before = snapshot(cur)
    cur.execute("SELECT status, reconciliation_ok FROM v_hme_day_completeness_llm WHERE business_date = '2026-10-14'")
    day14_before = cur.fetchone()
    res = L.load(doc_for("2026-10-14"), conn, only_stores=[MB])
    after = snapshot(cur)
    changed = [k for k in before if before[k] != after[k]]
    check("B nothing outside Mission Bend changed (rows, outliers, goals, results, "
          "day verdicts, network runs, other stores' status, MB history)", not changed, str(changed))
    cur.execute("SELECT store_scope, store_count, verified_store_count, status FROM hme_ingest_run WHERE run_id = %s",
                (res["run_id"],))
    sc, n, nv, st = cur.fetchone()
    check("B the run is recorded as store-scoped to Mission Bend only",
          sc == [MB] and n == 1 and nv == 1 and res["store_scope"] == [MB], str((sc, n, nv, st)))
    cur.execute("SELECT count(*), max(run_id) FROM hme_store_daily WHERE hme_store_number = %s "
                "AND business_date = '2026-10-14'", (MB,))
    cnt, rid = cur.fetchone()
    check("B Mission Bend has exactly one canonical row for the date, from this run",
          cnt == 1 and rid == res["run_id"])
    cur.execute("SELECT revel_establishment_id, hme_store_number, status FROM hme_store_ingest_result "
                "WHERE run_id = %s", (res["run_id"],))
    rr = cur.fetchall()
    check("B per-store result rows: Mission Bend only", [(r[0], r[1]) for r in rr] == [(EST, MB)], str(rr))
    cur.execute("SELECT status, reconciliation_ok FROM v_hme_day_completeness_llm WHERE business_date = '2026-10-14'")
    day14_after = cur.fetchone()
    check("B 2026-10-14 network verdict unchanged: still partial / NOT reconciled",
          day14_after == day14_before and day14_after[1] is False, str((day14_before, day14_after)))
    check("B the scoped run's own reconciliation is computed with the same checks",
          {c["check"] for c in res["reconciliation"]["checks"]} >= {"pa_total_orders_vs_outliers_car_departures"})
    dcur = real.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    hb = C._hme_block(dcur, EST, "2026-10-14", "2026-10-16")
    check("B chat HME gate for Mission Bend still lists 2026-10-14 as unreconciled",
          "2026-10-14" in [str(x) for x in (hb.get("unreconciled_days") or [])],
          str({k: hb.get(k) for k in ("unreconciled_days", "freshness_status")}))
    pd = {str(x["business_date"]): x for x in hb.get("per_day") or []}
    check("B chat HME per-day row for 2026-10-14 still reconciliation_ok = false",
          pd.get("2026-10-14", {}).get("reconciliation_ok") is False)
    dcur.execute("SELECT latest_business_date FROM v_hme_store_status_llm WHERE establishment_id = %s", (EST,))
    check("B Mission Bend's per-store freshness advances to the loaded date",
          str(dcur.fetchone()["latest_business_date"]) == "2026-10-14")
    # a reconciled network day: its verdict must not move either
    res16 = L.load(doc_for("2026-10-16"), conn, only_stores=[MB])
    check("B a second date (2026-10-16): still nothing outside Mission Bend changed",
          snapshot(cur)["day_view"] == before["day_view"]
          and snapshot(cur)["other_store_daily"] == before["other_store_daily"])

    print("C. duplicate runs")
    cur.execute("SELECT * FROM hme_store_daily WHERE hme_store_number = %s AND business_date = '2026-10-14'", (MB,))
    row1 = cur.fetchone()
    res_dup = L.load(doc_for("2026-10-14"), conn, only_stores=[MB])
    cur.execute("SELECT * FROM hme_store_daily WHERE hme_store_number = %s AND business_date = '2026-10-14'", (MB,))
    rows2 = cur.fetchall()
    cols = [d.name for d in cur.description]
    same = (len(rows2) == 1 and all(a == b for a, b, c in zip(row1, rows2[0], cols)
                                    if c not in ("run_id", "ingested_at")))
    check("C a duplicate run leaves ONE row per store-date with identical figures", same)
    cur.execute("SELECT count(*) FROM hme_outliers_daily WHERE hme_store_number = %s "
                "AND business_date = '2026-10-14'", (MB,))
    check("C ... and one outliers row", cur.fetchone()[0] == 1)
    check("C a duplicate run still changes nothing outside Mission Bend",
          {k: v for k, v in snapshot(cur).items() if k != "mb_history_to_1013"}
          == {k: v for k, v in before.items() if k != "mb_history_to_1013"})
    check("C each run is auditable (two scoped runs for the date)",
          res_dup["run_id"] != res["run_id"])

    print("D. missing source data")
    s0 = snapshot(cur)
    cur.execute("SELECT count(*) FROM hme_store_daily")
    n0 = cur.fetchone()[0]
    try:
        L.load(doc_for("2026-10-20", drop=(MB,)), conn, only_stores=[MB])
        check("D extract without Mission Bend -> StoreNotInExtract", False)
    except L.StoreNotInExtract:
        check("D extract without Mission Bend -> StoreNotInExtract", True)
    cur.execute("SELECT count(*) FROM hme_store_daily")
    check("D ... and nothing was written", cur.fetchone()[0] == n0 and snapshot(cur) == s0)
    emp = doc_for("2026-10-21")
    for r in emp["facts"]["store_daily"]:
        if str(r["hme_store_number"]) == MB:
            for k in ("total_orders", "lane_total_avg_seconds", "lane_queue_avg_seconds"):
                r[k] = None
    for fs in ("outliers_daily", "trend_store_daily", "goal_observations"):
        emp["facts"][fs] = [r for r in emp["facts"][fs] if str(r["hme_store_number"]) != MB]
    try:
        L.load(emp, conn, only_stores=[MB])
        check("D a measure-less Mission Bend row is absence, not data", False)
    except L.StoreNotInExtract:
        check("D a measure-less Mission Bend row is absence, not data", True)
    fid = L.record_failed_run(conn, "2026-10-14", status="failed", reason="scoped store absent",
                              store_scope=[MB])
    cur.execute("SELECT store_scope FROM hme_ingest_run WHERE run_id = %s", (fid,))
    check("D a failed scoped attempt is recorded as scoped", cur.fetchone()[0] == [MB])
    lr = R.latest_run(conn, datetime.date(2026, 10, 14))
    cur.execute("SELECT max(run_id) FROM hme_ingest_run WHERE business_date = '2026-10-14' AND store_scope IS NULL")
    net_rid = cur.fetchone()[0]
    check("D the reconciliation retry ignores scoped runs (sees the network run, still unreconciled)",
          lr and lr["run_id"] == net_rid and lr["reconciliation_ok"] is False, str(lr))
    check("D day verdicts unchanged after a failed scoped attempt", snapshot(cur)["day_view"] == before["day_view"])

    print("E. scope guards")
    cur.execute("SELECT hme_store_number FROM hme_store_mapping WHERE active AND mapping_status = 'OUT_OF_SCOPE' LIMIT 1")
    oos = cur.fetchone()
    if oos:
        try:
            L.load(doc_for("2026-10-22"), conn, only_stores=[oos[0]])
            check("E a non-VERIFIED store cannot be a load scope", False)
        except L.LoadAborted as e:
            check("E a non-VERIFIED store cannot be a load scope", "VERIFIED" in str(e))
    try:
        L.load(doc_for("2026-10-22"), conn, only_stores=["424242"])
        check("E an unmapped store number cannot be a load scope", False)
    except L.LoadAborted as e:
        check("E an unmapped store number cannot be a load scope", "VERIFIED" in str(e))
    try:
        L.load(doc_for("2026-10-22", add_unknown=True), conn, only_stores=[MB])
        check("E an unknown store in the extract still aborts a scoped load (tenant check first)", False)
    except L.LoadAborted as e:
        check("E an unknown store in the extract still aborts a scoped load (tenant check first)",
              "not in hme_store_mapping" in str(e))
    for args, ok in (((MB, 25), True), ((MB, 54), False), (("424242", 25), False)):
        try:
            B.verify_store_scope(real, *args)
            got = True
        except B.ScopeError:
            got = False
        check(f"E verify_store_scope{args} -> {'accepted' if ok else 'refused'}", got == ok)
    if oos:
        try:
            B.verify_store_scope(real, oos[0], 25)
            check("E verify_store_scope refuses a non-VERIFIED store", False)
        except B.ScopeError:
            check("E verify_store_scope refuses a non-VERIFIED store", True)

    print("F. planner: history preserved, restartable")
    ck = {"version": B.BACKFILL_VERSION, "dates": {}}
    rows, todo = B.store_plan(real, MB, datetime.date(2026, 10, 12), datetime.date(2026, 10, 18), ck,
                              today=datetime.date(2026, 10, 18))
    acts = {r["date"]: r["action"] for r in rows}
    check("F existing Mission Bend history (10-12, 10-13) is never re-written",
          acts["2026-10-12"].startswith("skip: store already") and acts["2026-10-13"].startswith("skip: store already"))
    check("F dates loaded in this sandbox (10-14, 10-16) are now skipped too",
          acts["2026-10-14"].startswith("skip: store already") and acts["2026-10-16"].startswith("skip: store already"))
    check("F missing dates are planned (10-15, 10-17); today is never planned",
          todo == [datetime.date(2026, 10, 15), datetime.date(2026, 10, 17)]
          and acts["2026-10-18"].startswith("skip: belongs to the daily/retry cycle"), str(todo))
    _, todo_inc = B.store_plan(real, MB, datetime.date(2026, 10, 13), datetime.date(2026, 10, 14), ck,
                               today=datetime.date(2026, 10, 18), include_loaded=True)
    check("F --include-loaded is the only way to reload a date the store has",
          todo_inc == [datetime.date(2026, 10, 13), datetime.date(2026, 10, 14)])
finally:
    real.rollback()
    real.close()

# planner + run(): partial failures, resume, idempotence (pure, no DB)
D5 = [datetime.date(2026, 10, 14) + datetime.timedelta(days=i) for i in range(5)]
seen = []


def fake(outcomes):
    def fn(d):
        seen.append(d)
        o = outcomes.get(d, "ok")
        if o == "missing":
            return I.EXIT_HARD_FAILURE, None, "load aborted: scoped store(s) ['1158614'] returned no rows"
        if o == "boom":
            raise RuntimeError("chromium crashed")
        return (I.EXIT_OK, {"run_id": 1, "status": "succeeded", "reconciliation_ok": True,
                            "rows_written": 4, "store_results": [
                                {"revel_establishment_id": EST, "status": "SUCCESS"}]}, "succeeded")
    return fn


with tempfile.TemporaryDirectory() as td:
    ckp = os.path.join(td, "ck.json")
    ck = B.load_checkpoint(ckp)
    B.run(D5, ingest_fn=fake({"2026-10-15": "missing", "2026-10-17": "boom"}),
          ck=ck, ck_path=ckp, pause=0, fh=None)
    st = {d: ck["dates"][d.isoformat()]["state"] for d in D5}
    check("F a missing/crashed date does not stop the others",
          [st[d] for d in D5] == ["loaded", "failed", "loaded", "failed", "loaded"], str(st))
    check("F overall exit is a failure while any date failed", B.overall_exit(ck, D5) == I.EXIT_HARD_FAILURE)
    ck2 = B.load_checkpoint(ckp)
    todo2 = [d for d in D5 if (ck2["dates"].get(d.isoformat()) or {}).get("state") != "loaded"]
    seen.clear()
    B.run(todo2, ingest_fn=fake({}), ck=ck2, ck_path=ckp, pause=0, fh=None)
    check("F resume retries only the failed dates", seen == [D5[1].isoformat(), D5[3].isoformat()], str(seen))
    check("F after resume every date is loaded, exit 0",
          B.overall_exit(B.load_checkpoint(ckp), D5) == I.EXIT_OK)
    check("F attempts are counted per date",
          B.load_checkpoint(ckp)["dates"]["2026-10-15"]["attempts"] == 2)
    seen.clear()
    ck3 = B.load_checkpoint(ckp)
    todo3 = [d for d in D5 if (ck3["dates"].get(d.isoformat()) or {}).get("state") != "loaded"]
    B.run(todo3, ingest_fn=fake({}), ck=ck3, ck_path=ckp, pause=0, fh=None)
    check("F a third run is a no-op (idempotent)", seen == [] and todo3 == [])

for argv, name in ((["--only-store", MB, "--start", "2026-10-14", "--end", "2026-10-15"],
                    "--only-store without --expect-establishment"),
                   (["--plan", "--start", "2026-10-14", "--end", "2026-10-15"], "--plan without --only-store")):
    try:
        B.main(argv)
        check(f"F CLI refuses {name}", False)
    except SystemExit as e:
        check(f"F CLI refuses {name}", e.code == 2)
check("F CLI refuses a wrong establishment before taking the lock (exit 1)",
      B.main(["--only-store", MB, "--expect-establishment", "54", "--start", "2026-10-14",
              "--end", "2026-10-15", "--plan"]) == I.EXIT_HARD_FAILURE)

# ═══ G. normal path unchanged; works BEFORE migration 64; rollback ═════════
print("G. normal path before migration 64; rollback script")
real = L.connect()
real.autocommit = False
conn = NoCommit(real)
try:
    cur = real.cursor()
    make_sandbox(real, apply_migration=False)
    cur.execute("SELECT count(*) FROM information_schema.columns WHERE table_name = 'hme_ingest_run' "
                "AND column_name = 'store_scope' AND table_schema LIKE 'pg_temp%%'")
    check("G sandbox without migration 64 has no store_scope column", cur.fetchone()[0] == 0)
    r0 = L.load(doc_for("2099-01-02"), conn, allow_partial_stores=True)
    n_doc = len({str(r["hme_store_number"]) for rows in doc_for("2099-01-02")["facts"].values() for r in rows})
    check("G a normal network load works on a database WITHOUT migration 64",
          r0["store_scope"] is None and r0["store_count"] == n_doc and n_doc >= 11, str((r0["store_count"], n_doc)))
    fid0 = L.record_failed_run(conn, "2099-01-03", status="failed", reason="x")
    check("G a normal failed-run record works without migration 64", fid0 is not None)
    lr0 = R.latest_run(conn, datetime.date(2099, 1, 2))
    check("G the retry's run lookup works without migration 64", lr0 and lr0["run_id"] == r0["run_id"])
    try:
        cur.execute("SAVEPOINT s")
        L.load(doc_for("2099-01-04"), conn, only_stores=[MB])
        check("G a scoped load is impossible without migration 64 (fails closed)", False)
    except psycopg2.errors.UndefinedColumn:
        cur.execute("ROLLBACK TO SAVEPOINT s")
        check("G a scoped load is impossible without migration 64 (fails closed)", True)
    cur.execute(sql_body(MIG).replace("CREATE OR REPLACE VIEW", "CREATE OR REPLACE TEMP VIEW"))
    cur.execute("SELECT * FROM v_hme_day_completeness_llm WHERE business_date = '2099-01-02'")
    check("G after migration 64 the network run is the day's verdict", cur.fetchone() is not None)
    rbsql = sql_body(RB).replace("CREATE OR REPLACE VIEW", "CREATE OR REPLACE TEMP VIEW")
    cur.execute("SAVEPOINT rb")
    cur.execute(rbsql)
    cur.execute("SELECT count(*) FROM information_schema.columns WHERE table_name = 'hme_ingest_run' "
                "AND column_name = 'store_scope' AND table_schema LIKE 'pg_temp%%'")
    check("G rollback with no scoped runs removes the column", cur.fetchone()[0] == 0)
    cur.execute("SELECT count(*) FROM ((SELECT * FROM public.v_hme_day_completeness_llm "
                "EXCEPT SELECT * FROM v_hme_day_completeness_llm WHERE business_date < '2099-01-01')) x")
    check("G ... and restores the production day view output", cur.fetchone()[0] == 0)
    cur.execute("ROLLBACK TO SAVEPOINT rb")
    L.load(doc_for("2099-01-05"), conn, only_stores=[MB])
    try:
        cur.execute("SAVEPOINT rb2")
        cur.execute(rbsql)
        check("G rollback REFUSES while store-scoped runs exist", False)
    except psycopg2.errors.RaiseException as e:
        cur.execute("ROLLBACK TO SAVEPOINT rb2")
        check("G rollback REFUSES while store-scoped runs exist", "store-scoped" in str(e))
finally:
    real.rollback()
    real.close()

# ingest_date: the normal path calls the loader exactly as before; a scoped
# run passes the scope to the loader and to the failed-run record.
calls = []
_load, _rfr, _ext = L.load, L.record_failed_run, I.run_extractor
with tempfile.TemporaryDirectory() as td:
    fp = os.path.join(td, "f.json")
    with open(fp, "w") as f:
        json.dump(doc_for("2026-10-20"), f)
    I.run_extractor = lambda d, env, fh: (0, fp)
    L.load = lambda doc, conn, dry_run=False, allow_partial_stores=False: calls.append("legacy") or (_ for _ in ()).throw(L.LoadAborted("stop"))
    L.record_failed_run = lambda conn, d, **kw: calls.append(("rfr", sorted(kw))) or 1
    L_connect = L.connect
    L.connect = lambda: type("X", (), {"rollback": lambda s: None, "close": lambda s: None, "closed": True})()
    try:
        I.ingest_date("2026-10-20", env={}, fh=None, max_attempts=1, backoff=[0])
        check("G normal ingest_date calls a legacy-signature loader (no only_stores kwarg)",
              calls[0] == "legacy" and "store_scope" not in calls[1][1], str(calls))
        calls.clear()
        def scoped_load(doc, conn, dry_run=False, allow_partial_stores=False, only_stores=None):
            calls.append(("scoped", only_stores))
            raise L.StoreNotInExtract("absent")
        L.load = scoped_load
        code, res_, why = I.ingest_date("2026-10-20", env={}, fh=None, max_attempts=1, backoff=[0],
                                        only_stores=[MB])
        check("G scoped ingest_date passes the scope to the loader and the failed-run record",
              calls[0] == ("scoped", [MB]) and "store_scope" in calls[1][1] and code == I.EXIT_HARD_FAILURE,
              str(calls))
    finally:
        L.load, L.record_failed_run, I.run_extractor, L.connect = _load, _rfr, _ext, L_connect

# ═══ V. pre-PR verification (review points 1-5) ═════════════════════════════
print("V. pre-PR verification")
import re  # noqa: E402
import subprocess  # noqa: E402
import importlib.util  # noqa: E402


def git_show(ref_path):
    r = subprocess.run(["git", "show", ref_path], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def view_stmts(sql_text):
    body = re.sub(r"--[^\n]*", "", sql_text)
    return [f"CREATE OR REPLACE TEMP VIEW {n} AS{sel}" for n, sel in re.findall(
        r"CREATE OR REPLACE VIEW\s+(\w+)\s+AS(.*?);\s*(?=\n(?:COMMENT|CREATE|COMMIT|GRANT|BEGIN|ALTER|$))",
        body + "\n", re.S | re.I)]


# V1: independent of PR #34 (migrations 62/63), in either order
m62 = git_show("origin/fix/financial-reporting-accuracy:migrations/62_financial_reporting_accuracy.sql")
m63 = git_show("origin/fix/financial-reporting-accuracy:migrations/63_financial_reporting_grants.sql")
check("V1 PR #34 migrations readable from its pushed branch (worktree untouched)", bool(m62 and m63))
objs64 = set(re.findall(r"\b(hme_\w+|v_hme_\w+|r365_\w+|v_r365_\w+|v_revel_\w+|v_orders\w*)\b", sql_body(MIG)))
objs62 = set(re.findall(r"\b(hme_\w+|v_hme_\w+|r365_\w+|v_r365_\w+|v_revel_\w+|v_orders\w*|payments_v2)\b",
                        sql_body(m62 or "") + sql_body(m63 or "")))
check("V1 migration 64 and migrations 62/63 touch disjoint objects",
      objs64 and objs62 and not (objs64 & objs62)
      and all(o.startswith(("hme_", "v_hme_")) for o in objs64), str(sorted(objs64 & objs62)))
for order in ("64 then 62", "62 then 64"):
    real = L.connect()
    real.autocommit = False
    try:
        cur = real.cursor()
        make_sandbox(real, apply_migration=False)
        steps = ([sql_body(MIG).replace("CREATE OR REPLACE VIEW", "CREATE OR REPLACE TEMP VIEW")]
                 + view_stmts(m62 or ""))
        if order == "62 then 64":
            steps = steps[1:] + steps[:1]
        for st in steps:
            cur.execute(st)
        cur.execute("SELECT count(*) FROM v_hme_day_completeness_llm")
        a = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM v_r365_store_period_llm WHERE forecast_days IS NOT NULL")
        b = cur.fetchone()[0]
        check(f"V1 both migrations apply cleanly together ({order})", a > 0 and b > 0)
    finally:
        real.rollback()
        real.close()
check("V1 migration 64 alone was applied to today's schema (62/63 NOT applied) in A-G above",
      all(ok for n, ok, _ in results if n.startswith("A ")))

real = L.connect()
real.autocommit = False
conn = NoCommit(real)
try:
    cur = real.cursor()
    dcur = real.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    make_sandbox(real, apply_migration=True)
    view_all = lambda: (cur.execute("SELECT * FROM v_hme_day_completeness_llm ORDER BY business_date")
                        or cur.fetchall())
    day = lambda d: (cur.execute("SELECT status, reconciliation_ok, observed_store_count FROM "
                                 "v_hme_day_completeness_llm WHERE business_date = %s", (d,)) or cur.fetchone())
    elig = lambda d: R.assess_retry_eligibility(R.latest_run(conn, datetime.date.fromisoformat(d)))

    # V3: a Mission-Bend-only run cannot upgrade a network verdict -- even if
    # it is forced to look perfect.
    v_before = view_all()
    e14, e15 = elig("2026-10-14"), elig("2026-10-15")
    r14 = L.load(doc_for("2026-10-14"), conn, only_stores=[MB])
    r15 = L.load(doc_for("2026-10-15"), conn, only_stores=[MB])
    cur.execute("UPDATE hme_ingest_run SET status = 'succeeded', reconciliation_ok = true, "
                "store_count = 11, verified_store_count = 11 WHERE run_id IN (%s, %s)",
                (r14["run_id"], r15["run_id"]))
    check("V3 even 'perfect' scoped runs on 10-14/10-15 leave every day verdict unchanged",
          view_all() == v_before)
    check("V3 2026-10-14 and 2026-10-15 remain NOT reconciled at network level",
          day("2026-10-14")[1] is False and day("2026-10-15")[1] is False)
    hb = C._hme_block(dcur, EST, "2026-10-14", "2026-10-16")
    check("V3 the chat HME gate still reports 10-14 and 10-15 as unreconciled for Mission Bend",
          {"2026-10-14", "2026-10-15"} <= {str(x) for x in (hb.get("unreconciled_days") or [])},
          str(hb.get("unreconciled_days")))
    hbn = C._hme_block(dcur, None, "2026-10-14", "2026-10-16")
    check("V3 ... and for the network", {"2026-10-14", "2026-10-15"}
          <= {str(x) for x in (hbn.get("unreconciled_days") or [])})

    # V2: scheduled jobs keep working with scoped runs present
    # The retry derives coverage from loaded rows, so after a backfill Mission
    # Bend counts as observed (10/11 + 1 NO_ACCESS -> 11/11). The DECISION is
    # unchanged either way; a scoped run can only add rows, so it can never
    # make an eligible day ineligible or the reverse.
    check("V2 retry eligibility DECISION for 10-14/10-15 is unchanged with scoped runs present",
          elig("2026-10-14")[0] == e14[0] and elig("2026-10-15")[0] == e15[0],
          str((e14, elig("2026-10-14"))))
    check("V2 ... only its coverage count now includes Mission Bend's loaded rows (10/11 -> 11/11)",
          "10/11" in e14[1] and "11/11" in elig("2026-10-14")[1])
    rn = L.load(doc_for("2026-10-14"), conn, allow_partial_stores=True)         # a later NETWORK run
    cur.execute("SELECT store_scope FROM hme_ingest_run WHERE run_id = %s", (rn["run_id"],))
    check("V2 a daily/retry-style network run after scoped runs is recorded network-wide",
          cur.fetchone()[0] is None)
    n_doc = len({str(r["hme_store_number"]) for rows in doc_for("2026-10-14")["facts"].values() for r in rows})
    check("V2 ... and becomes the day's verdict (not the scoped runs)", day("2026-10-14")[2] == n_doc)
    check("V2 the retry sees that network run as the latest", R.latest_run(conn, datetime.date(2026, 10, 14))["run_id"] == rn["run_id"])
    r14b = L.load(doc_for("2026-10-14"), conn, only_stores=[MB])
    check("V2 a scoped run AFTER the network run still does not replace it",
          R.latest_run(conn, datetime.date(2026, 10, 14))["run_id"] == rn["run_id"] and day("2026-10-14")[2] == n_doc)
    check("V2 the daily ingest never reads hme_ingest_run (only the mapping)",
          "FROM hme_ingest_run" not in open("hme_daily_ingest.py").read())

    # V4: rollback can never reinterpret scoped runs as network runs
    old_src = git_show("a86366d:hme_reconciliation_retry.py")
    with tempfile.TemporaryDirectory() as td:
        fp = os.path.join(td, "old_retry.py")
        open(fp, "w").write(old_src)
        spec = importlib.util.spec_from_file_location("old_retry", fp)
        old_R = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(old_R)
    check("V4 (risk being guarded) the OLD retry would take a newer scoped run as the latest",
          old_R.latest_run(conn, datetime.date(2026, 10, 14))["run_id"] == r14b["run_id"])
    check("V4 ... but the OLD retry only ever examines Chicago yesterday",
          "target = args.date or ingest.chicago_target_date().isoformat()" in old_src)
    ck = {"version": B.BACKFILL_VERSION, "dates": {}}
    yday = I.chicago_target_date()
    rows, todo = B.store_plan(real, MB, yday - datetime.timedelta(days=3), yday, ck)
    acts = {r["date"]: r["action"] for r in rows}
    check("V4 the planner never schedules Chicago yesterday (the retry's date)",
          yday not in todo and acts[yday.isoformat()].startswith("skip: belongs to the daily/retry cycle"),
          str(acts))
    cur.execute("SELECT max(business_date) FROM hme_ingest_run WHERE store_scope IS NULL")
    last_net = cur.fetchone()[0]
    _, todo_future = B.store_plan(real, MB, last_net + datetime.timedelta(days=1),
                                  last_net + datetime.timedelta(days=3), ck,
                                  today=last_net + datetime.timedelta(days=10))
    check("V4 the planner never schedules a day with no network-wide run yet", todo_future == [])
    rb = sql_body(RB).replace("CREATE OR REPLACE VIEW", "CREATE OR REPLACE TEMP VIEW")
    try:
        cur.execute("SAVEPOINT v4")
        cur.execute(rb)
        check("V4 schema rollback refuses while scoped runs exist", False)
    except psycopg2.errors.RaiseException:
        cur.execute("ROLLBACK TO SAVEPOINT v4")
        check("V4 schema rollback refuses while scoped runs exist", True)
    cur.execute("SELECT count(*) FROM hme_ingest_run WHERE store_scope IS NOT NULL")
    check("V4 ... and the scoped runs keep their scope", cur.fetchone()[0] >= 3)
finally:
    real.rollback()
    real.close()

# V5: the 33-day backfill is checkpointed and resumes safely after interruption
D33 = [datetime.date(2026, 10, 14) + datetime.timedelta(days=i) for i in range(33)]
check("V5 the plan range is exactly 33 days (2026-10-14..11-15)",
      D33[0] == datetime.date(2026, 10, 14) and D33[-1] == datetime.date(2026, 11, 15))
with tempfile.TemporaryDirectory() as td:
    ckp = os.path.join(td, "backfill-store-1158614-2026-10-14-2026-11-15.json")
    ck = B.load_checkpoint(ckp)
    done = []

    def interrupt_at(n):
        def fn(d):
            if len(done) == n:
                raise KeyboardInterrupt          # operator Ctrl-C / SIGINT
            done.append(d)
            return (I.EXIT_OK, {"run_id": len(done), "status": "succeeded", "reconciliation_ok": True,
                                "rows_written": 4, "store_results": [{"revel_establishment_id": EST,
                                                                      "status": "SUCCESS"}]}, "succeeded")
        return fn
    try:
        B.run(D33, ingest_fn=interrupt_at(10), ck=ck, ck_path=ckp, pause=0, fh=None)
        check("V5 an interrupt stops the run", False)
    except KeyboardInterrupt:
        check("V5 an interrupt stops the run", True)
    ck2 = B.load_checkpoint(ckp)
    loaded = [d for d in D33 if (ck2["dates"].get(d.isoformat()) or {}).get("state") == "loaded"]
    check("V5 the 10 dates completed before the interrupt are checkpointed as loaded",
          loaded == D33[:10] and D33[10].isoformat() not in ck2["dates"])
    check("V5 the checkpoint is written atomically (no temp file left behind)",
          not os.path.exists(ckp + ".tmp") and oct(os.stat(ckp).st_mode & 0o777) == "0o600")
    todo = [d for d in D33 if (ck2["dates"].get(d.isoformat()) or {}).get("state") != "loaded"]
    done.clear()
    B.run(todo, ingest_fn=interrupt_at(10 ** 6), ck=ck2, ck_path=ckp, pause=0, fh=None)
    check("V5 resume processes exactly the 23 remaining dates, the interrupted one first",
          done == [d.isoformat() for d in D33[10:]])
    check("V5 after resume all 33 dates are loaded and exit is 0",
          B.overall_exit(B.load_checkpoint(ckp), D33) == I.EXIT_OK)
    bad = os.path.join(td, "other.json")
    json.dump({"version": B.BACKFILL_VERSION, "store_scope": ["115"], "dates": {}}, open(bad, "w"))
    check("V5 a checkpoint belonging to another store scope is refused",
          B.main(["--only-store", MB, "--expect-establishment", "25", "--start", "2026-10-14",
                  "--end", "2026-11-15", "--checkpoint", bad, "--plan"]) == I.EXIT_HARD_FAILURE)
    check("V5 the store-scoped checkpoint name cannot collide with the network backfill's",
          "backfill-store-" in open("hme_backfill.py").read())
check("V5 every date is loaded under the shared ingest lock (acquire_lock) and the mapping is "
      "re-verified under it", "lock = ingest.acquire_lock(fh)" in open("hme_backfill.py").read()
      and "verify_store_scope(conn, store, a.expect_establishment)   # again, under the lock"
      in open("hme_backfill.py").read())

passed = sum(ok for _, ok, _ in results)
print(f"\nHME store-scoped backfill: {passed} passed, {len(results) - passed} failed, {len(results)} total")
sys.exit(0 if passed == len(results) else 1)
