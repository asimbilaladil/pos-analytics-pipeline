#!/usr/bin/env python3
"""The Mission Bend goal-history correction scripts commit ONLY on -v apply=1.

Run (isolated test database ONLY -- see tests/isolated_db.py; needs psql):
    DB_HOST=127.0.0.1 DB_PORT=55432 DB_NAME=laynes_test DB_USER=... DB_PASS=... \\
        venv/bin/python tests/test_hme_goal_correction_sql.py     (exit 0 = pass)

Regression (PR #37 review): the scripts used  \\if :{?apply}  which tests whether
the variable is DEFINED, so  psql -v apply=0  would have COMMITTED. Pinned here
for both docs/corrections/2026-11-17_mission_bend_goal_history.sql and its
_rollback.sql:
  1. unset and apply=0 are dry runs: nothing persists (history, backup table,
     audit rows all unchanged), even with a stray -v do_commit=true
  2. every other value (true, yes, on, 01, ' 1', 2, empty) aborts non-zero
     before any change
  3. only apply=1 commits; the backup table and the two audit rows exist ONLY
     after that; other stores' history is untouched
  4. re-applying is refused; the rollback (apply=1) restores the original rows
     byte-for-byte and marks the audit rows REVERTED
This test COMMITS to the isolated database, then removes what it created and
asserts the database is back to its starting state.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(__file__))
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
import isolated_db  # noqa: E402

isolated_db.require_isolated()

FIX = os.path.join(ROOT, "docs/corrections/2026-11-17_mission_bend_goal_history.sql")
UNDO = os.path.join(ROOT, "docs/corrections/2026-11-17_mission_bend_goal_history_rollback.sql")
MB = "1158614"
BACKUP = "hme_goal_history_correction_20261117"
CORRECTED = "2026-09-08,,285,2"
INVALID = ["true", "TRUE", "yes", "on", "01", " 1", "1 ", "2", "", "-1"]

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


def psql(script, *vars_):
    env = {**os.environ, "PGPASSWORD": os.environ["DB_PASS"]}
    cmd = ["psql", "-X", "-q", "-h", os.environ["DB_HOST"], "-p", os.environ["DB_PORT"],
           "-U", os.environ["DB_USER"], "-d", os.environ["DB_NAME"]]
    for v in vars_:
        cmd += ["-v", v]
    p = subprocess.run(cmd + ["-f", script], env=env, capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def state():
    """Everything the scripts could change, as comparable values."""
    with isolated_db.rw() as conn, conn.cursor() as cur:
        cur.execute("""SELECT string_agg(concat_ws(',', effective_from_date,
                                 coalesce(effective_to_date::text, ''), goal_d_seconds,
                                 observed_run_id), '|' ORDER BY effective_from_date),
                              md5(string_agg(row(g.*)::text, '|' ORDER BY effective_from_date))
                         FROM hme_goal_history g WHERE hme_store_number = %s""", (MB,))
        mb, mb_md5 = cur.fetchone()
        cur.execute("""SELECT md5(string_agg(row(g.*)::text, '|'
                                  ORDER BY hme_store_number, effective_from_date))
                         FROM hme_goal_history g WHERE hme_store_number <> %s""", (MB,))
        others = cur.fetchone()[0]
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (BACKUP,))
        backup = cur.fetchone()[0]
        cur.execute("""SELECT count(*), count(*) FILTER (WHERE resolution_note LIKE '%%REVERTED%%')
                         FROM hme_goal_conflict WHERE hme_store_number = %s
                          AND reason = 'observation_fails_pa_goal_d_check'
                          AND observed_date IN ('2026-11-03', '2026-11-06')""", (MB,))
        audit, reverted = cur.fetchone()
    conn.close()
    return {"mb": mb, "mb_md5": mb_md5, "others": others, "backup": backup,
            "audit": audit, "reverted": reverted}


def cleanup():
    """Return the isolated DB to its pre-test state (restore from backup if the
    correction is still applied, then drop the backup and the audit rows)."""
    with isolated_db.rw() as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (BACKUP,))
        if cur.fetchone()[0]:
            cur.execute("""SELECT string_agg(concat_ws(',', effective_from_date,
                                     coalesce(effective_to_date::text, ''), goal_d_seconds,
                                     observed_run_id), '|' ORDER BY effective_from_date)
                             FROM hme_goal_history WHERE hme_store_number = %s""", (MB,))
            if cur.fetchone()[0] == CORRECTED:
                cur.execute("DELETE FROM hme_goal_history WHERE hme_store_number = %s", (MB,))
                cur.execute(f"""INSERT INTO hme_goal_history (hme_store_number,
                                  effective_from_date, effective_to_date, goal_a_seconds,
                                  goal_b_seconds, goal_c_seconds, goal_d_seconds,
                                  observed_run_id, recorded_at)
                                SELECT hme_store_number, effective_from_date, effective_to_date,
                                       goal_a_seconds, goal_b_seconds, goal_c_seconds,
                                       goal_d_seconds, observed_run_id, recorded_at
                                  FROM {BACKUP}""")
            cur.execute(f"DROP TABLE {BACKUP}")
        cur.execute("""DELETE FROM hme_goal_conflict WHERE hme_store_number = %s
                         AND reason = 'observation_fails_pa_goal_d_check'
                         AND observed_date IN ('2026-11-03', '2026-11-06')""", (MB,))
    conn.close()


start = state()
if start["backup"] or start["audit"] or start["mb"] == CORRECTED:
    sys.exit(f"REFUSING: isolated DB is not in the pre-correction state: {start}")
print(f"start: MB history {start['mb']}")

try:
    print("== correction script: dry runs and invalid values change nothing")
    for label, vars_ in [("unset", []), ("apply=0", ["apply=0"]),
                         ("apply=0 + stray do_commit=true", ["apply=0", "do_commit=true"]),
                         ("stray do_commit=true only", ["do_commit=true"])]:
        rc, out = psql(FIX, *vars_)
        check(f"correction {label}: dry run, exit 0", rc == 0 and "DRY RUN" in out
              and "APPLY" not in out, f"rc={rc}")
        check(f"correction {label}: no persistent change", state() == start)
    for v in INVALID:
        rc, out = psql(FIX, f"apply={v}")
        check(f"correction apply={v!r}: aborts non-zero before BEGIN",
              rc != 0 and "invalid -v apply value" in out and "BEFORE" not in out, f"rc={rc}")
        check(f"correction apply={v!r}: no persistent change", state() == start)

    print("== rollback script before any correction: never commits")
    for vars_ in ([], ["apply=0"], ["apply=1"]):
        rc, out = psql(UNDO, *vars_)
        check(f"rollback {vars_ or 'unset'} with no backup: refused",
              rc != 0 and "no backup table" in out, f"rc={rc}")
        check(f"rollback {vars_ or 'unset'} with no backup: no change", state() == start)
    for v in ("true", "yes", "01"):
        rc, out = psql(UNDO, f"apply={v}")
        check(f"rollback apply={v!r}: aborts at the gate",
              rc != 0 and "invalid -v apply value" in out, f"rc={rc}")

    print("== correction apply=1 commits")
    rc, out = psql(FIX, "apply=1")
    applied = state()
    check("correction apply=1: exit 0 and committed", rc == 0 and "APPLY: committing" in out,
          f"rc={rc}")
    check("correction apply=1: Mission Bend is one open 285 period", applied["mb"] == CORRECTED,
          applied["mb"])
    check("correction apply=1: backup table created", applied["backup"] is True)
    check("correction apply=1: two audit rows created", applied["audit"] == 2
          and applied["reverted"] == 0)
    check("correction apply=1: other stores untouched", applied["others"] == start["others"])

    rc, out = psql(FIX, "apply=1")
    check("correction re-applied: refused, nothing changes",
          rc != 0 and "precondition:" in out and state() == applied, f"rc={rc}")

    print("== rollback script: dry runs and invalid values change nothing")
    for vars_ in ([], ["apply=0"], ["apply=0", "do_commit=true"]):
        rc, out = psql(UNDO, *vars_)
        check(f"rollback {vars_ or 'unset'}: dry run, exit 0",
              rc == 0 and "DRY RUN" in out and "APPLY" not in out, f"rc={rc}")
        check(f"rollback {vars_ or 'unset'}: no persistent change", state() == applied)
    for v in INVALID:
        rc, out = psql(UNDO, f"apply={v}")
        check(f"rollback apply={v!r}: aborts non-zero", rc != 0
              and "invalid -v apply value" in out and state() == applied, f"rc={rc}")

    print("== rollback apply=1 restores the original rows exactly")
    rc, out = psql(UNDO, "apply=1")
    undone = state()
    check("rollback apply=1: exit 0 and committed", rc == 0 and "APPLY: committing" in out,
          f"rc={rc}")
    check("rollback apply=1: Mission Bend rows byte-identical to the start",
          undone["mb"] == start["mb"] and undone["mb_md5"] == start["mb_md5"])
    check("rollback apply=1: audit rows kept and marked REVERTED",
          undone["audit"] == 2 and undone["reverted"] == 2)
    check("rollback apply=1: other stores untouched", undone["others"] == start["others"])
finally:
    cleanup()

end = state()
check("cleanup: isolated DB back to its starting state", end == start)

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
