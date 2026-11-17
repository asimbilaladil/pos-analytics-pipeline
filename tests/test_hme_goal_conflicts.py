#!/usr/bin/env python3
"""Goal observations that fail their own reconciliation never alter goal history.

Run (isolated test database ONLY -- see tests/isolated_db.py):
    DB_HOST=127.0.0.1 DB_PORT=55432 DB_NAME=laynes_test DB_USER=... DB_PASS=... \\
        venv/bin/python tests/test_hme_goal_conflicts.py        (exit 0 = pass)

2026-11 finding: Mission Bend (HME 1158614, establishment 25) on 2026-11-03 and
2026-11-06 carried a Multi-report goal of A239/B251/C263/D289 while the same
day's Performance Analysis threshold (lane_total_goal_d_seconds) was 285. The
day's goal_d_matches_pa_threshold check FAILED, yet the loader still folded the
observation into hme_goal_history: the 285 period was closed at 11-02, a one-day
289 period opened, and the next day's 285 opened another period.

Pinned here, using the REAL 2026-11-03 / 11-06 extracts:
  1. a failing observation is recorded in hme_goal_conflict (reason
     observation_fails_pa_goal_d_check, with the covering period) and goal
     history is byte-identical afterwards
  2. the next day's valid observation (285) leaves history unchanged
  3. historical backfill: a failing observation dated INSIDE an open period
     (previously case D: close-and-open) changes nothing
  4. re-running the same day is idempotent (no duplicate conflict rows)
  5. a genuine goal change -- observation AND PA threshold both move -- still
     opens a new period (case D unchanged)
  6. an observation whose store has no PA threshold that day cannot be checked
     and keeps the pre-existing behaviour
  7. other stores' goal history is untouched throughout
  8. pure helper: goal_d_failed_stores reads only the failed goal check
Everything runs in one transaction that is rolled back.
"""
import copy
import datetime
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import isolated_db  # noqa: E402

isolated_db.require_isolated()
import hme_load as L  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


MB = "1158614"
RAW = "/var/lib/laynes/hme/raw"
NOV3 = json.load(open(f"{RAW}/2026-11-03/querydata-facts.dup-081033.json"))
NOV6 = json.load(open(f"{RAW}/2026-11-06/querydata-facts.dup-083718.json"))
BASE = json.load(open(f"{RAW}/2026-10-13/querydata-facts.json"))


def relabel(doc, day, goal=None, pa=None):
    d = copy.deepcopy(doc)
    d["business_date"] = d["requested_date"] = day
    for rows in d["facts"].values():
        for r in rows:
            for k in ("business_date", "observed_for_date"):
                if k in r:
                    r[k] = day
            if str(r["hme_store_number"]) == MB:
                if goal is not None and "goal_d_seconds" in r:
                    r.update(dict(zip(L.GOAL_COLS, goal)))
                if pa is not None and "lane_total_goal_d_seconds" in r:
                    r["lane_total_goal_d_seconds"] = pa
    d["freshness_ok"] = True
    return d


class NoCommit:
    def __init__(self, c):
        self._c = c

    def commit(self):
        pass

    def rollback(self):
        pass

    def __getattr__(self, k):
        return getattr(self._c, k)


def history(cur, sn=MB):
    cur.execute("""SELECT effective_from_date, effective_to_date, goal_a_seconds,
                          goal_b_seconds, goal_c_seconds, goal_d_seconds
                     FROM hme_goal_history WHERE hme_store_number = %s
                    ORDER BY effective_from_date""", (sn,))
    return [tuple(r) for r in cur.fetchall()]


def others_hash(cur):
    cur.execute("SELECT * FROM hme_goal_history WHERE hme_store_number <> %s "
                "ORDER BY hme_store_number, effective_from_date", (MB,))
    return hashlib.sha256(json.dumps(cur.fetchall(), default=str).encode()).hexdigest()


def conflicts(cur):
    cur.execute("""SELECT observed_date, observed_goal_d, existing_from, existing_to,
                          existing_goal_d, reason
                     FROM hme_goal_conflict WHERE hme_store_number = %s
                    ORDER BY observed_date, reason""", (MB,))
    return [tuple(r) for r in cur.fetchall()]


D = datetime.date
CLEAN = [(D(2026, 9, 8), None, 240, 255, 270, 285)]

# ── 8. pure helper ───────────────────────────────────────────────────────────
print("8. goal_d_failed_stores")
ok, recon = L.reconcile(L.scope_doc(relabel(NOV3, "2026-11-03"), [MB]))
check("real 11-03 extract scoped to Mission Bend fails goal_d_matches_pa_threshold",
      L.goal_d_failed_stores(recon) == {MB}, str(L.goal_d_failed_stores(recon)))
ok, recon = L.reconcile(L.scope_doc(relabel(BASE, "2026-10-13"), [MB]))
check("a reconciled day rejects nothing", ok and L.goal_d_failed_stores(recon) == set())
fake = {"checks": [{"check": "trend_total_cars_matches_pa_total_orders_per_store",
                    "ok": False, "stores": ["1"]},
                   {"check": "goal_d_matches_pa_threshold", "ok": True, "stores": []}]}
check("other failed checks do not reject goal observations",
      L.goal_d_failed_stores(fake) == set())

real = isolated_db.rw()
conn = NoCommit(real)
cur = real.cursor()
try:
    # Start from the history the correction would restore: one open 285 period.
    cur.execute("DELETE FROM hme_goal_history WHERE hme_store_number = %s", (MB,))
    cur.execute("""INSERT INTO hme_goal_history (hme_store_number, effective_from_date,
                       goal_a_seconds, goal_b_seconds, goal_c_seconds, goal_d_seconds,
                       observed_run_id) VALUES (%s,'2026-09-08',240,255,270,285,2)""", (MB,))
    cur.execute("DELETE FROM hme_goal_conflict WHERE hme_store_number = %s", (MB,))
    other0 = others_hash(cur)

    print("1. real 2026-11-03 observation (D=289 vs PA 285)")
    res = L.load(relabel(NOV3, "2026-11-03"), conn, only_stores=[MB])
    check("history unchanged", history(cur) == CLEAN, str(history(cur)))
    check("goal_stats: 1 rejected, counted as a conflict, nothing opened",
          res["goal_stats"]["rejected_failed_check"] == 1
          and res["goal_stats"]["conflicts"] == 1 and res["goal_stats"]["opened"] == 0,
          str(res["goal_stats"]))
    c = conflicts(cur)
    check("conflict recorded with observed 289, covering period 285 from 09-08, reason",
          c == [(D(2026, 11, 3), 289, D(2026, 9, 8), None, 285,
                 L.GOAL_FAILED_CHECK_REASON)], str(c))
    check("run still PARTIAL (store-level reconciliation unchanged)",
          res["status"] == "partial" and not res["reconciliation_ok"])
    cur.execute("SELECT status, reason_code FROM hme_store_ingest_result "
                "WHERE run_id = %s AND hme_store_number = %s", (res["run_id"], MB))
    check("per-store result still PARTIAL / RECONCILIATION_MISMATCH",
          cur.fetchone() == ("PARTIAL", "RECONCILIATION_MISMATCH"))
    cur.execute("SELECT run_id FROM hme_goal_conflict WHERE hme_store_number=%s "
                "AND observed_date='2026-11-03'", (MB,))
    check("conflict carries the run id (provenance)", cur.fetchone()[0] == res["run_id"])

    print("2. next day valid observation (11-04, D=285)")
    res = L.load(relabel(BASE, "2026-11-04"), conn, only_stores=[MB])
    check("valid 285 observation: unchanged, history still one open period",
          res["goal_stats"]["unchanged"] == 1 and history(cur) == CLEAN, str(res["goal_stats"]))

    print("   real 2026-11-06 observation")
    L.load(relabel(NOV6, "2026-11-06"), conn, only_stores=[MB])
    check("11-06 rejected too; history unchanged", history(cur) == CLEAN)
    check("two audit rows (11-03, 11-06)",
          [x[0] for x in conflicts(cur)] == [D(2026, 11, 3), D(2026, 11, 6)])

    print("3. historical backfill inside the open period")
    res = L.load(relabel(NOV3, "2026-10-20"), conn, only_stores=[MB])
    check("failing observation dated 10-20 changes nothing (was close-and-open)",
          history(cur) == CLEAN and res["goal_stats"]["opened"] == 0, str(history(cur)))

    print("4. idempotent re-run")
    n0 = len(conflicts(cur))
    L.load(relabel(NOV3, "2026-11-03"), conn, only_stores=[MB])
    check("no duplicate conflict rows, history unchanged",
          len(conflicts(cur)) == n0 and history(cur) == CLEAN)

    print("5. genuine change: goal AND PA threshold move to 300 on 11-20")
    res = L.load(relabel(BASE, "2026-11-20", goal=(250, 265, 280, 300), pa=300),
                 conn, only_stores=[MB])
    h = history(cur)
    check("case D still opens a new period",
          res["goal_stats"]["opened"] == 1 and h == [
              (D(2026, 9, 8), D(2026, 11, 19), 240, 255, 270, 285),
              (D(2026, 11, 20), None, 250, 265, 280, 300)], str(h))
    check("genuine change passes reconciliation", res["reconciliation_ok"])

    print("6. no PA threshold that day: cannot be checked, old behaviour kept")
    d = relabel(BASE, "2026-11-25", goal=(250, 265, 280, 300))
    for r in d["facts"]["store_daily"]:
        if str(r["hme_store_number"]) == MB:
            r["lane_total_goal_d_seconds"] = None
    res = L.load(d, conn, only_stores=[MB])
    check("observation applied as before (unchanged vs open 300 period)",
          res["goal_stats"]["unchanged"] == 1 and res["goal_stats"]["rejected_failed_check"] == 0)

    print("7. other stores")
    check("other stores' goal history byte-identical", others_hash(cur) == other0)
finally:
    real.rollback()
    real.close()

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
