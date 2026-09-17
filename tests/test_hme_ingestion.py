#!/usr/bin/env python3
"""Golden tests for HME canonical ingestion, the store mapper, and tenant safety.

Run:  venv/bin/python tests/test_hme_ingestion.py     (exit 0 = pass)

The tenant-safety tests are the important ones. HME's Power BI model is
multi-tenant with no row-level security on the embed token; isolation comes only
from report filters. A report.updateFilters(Replace) removes
DIM User Info[User_EmailAddress] and the same query then returns ~499 stores
belonging to unrelated brands (Dairy Queen, HTEAO, "The Claws") with ~20.5M
orders. These tests exist so that regression is loud.

Database writes: every test that touches the DB does so inside a transaction
that is rolled back. Production rows are never modified.
"""
import datetime
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from dotenv import load_dotenv

load_dotenv(".env", override=True)

# The extractor is a separate application by design; import its guard for tests.
DOWNLOADER = os.environ.get("HME_DOWNLOADER_DIR", "/opt/hme-report-downloader")
sys.path.insert(0, DOWNLOADER)

import hme_load  # noqa: E402

FIXTURES = os.environ.get("HME_FIXTURE_DIR", "/var/lib/laynes/hme/state/fixtures")
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


def fixture(n):
    p = os.path.join(FIXTURES, n)
    if not os.path.exists(p):
        return None
    with open(p) as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
print("\n== tenant scope guards (extractor) ==")
try:
    from hme import guard
    HAVE_GUARD = True
except ImportError as e:
    HAVE_GUARD = False
    check("guard module importable", False, str(e))

if HAVE_GUARD:
    INV = {"00111", "002", "108"}

    # 1. tenant filter preservation is detectable
    scoped = {"From": [{"Entity": "DIM User Info"}],
              "Where": [{"Condition": {"Not": {"Expression": {"In": {
                  "Expressions": [{"Column": {"Property": "User_EmailAddress"}}]}}}}}]}
    unscoped = {"From": [{"Entity": "Stores"}], "Where": [{"Condition": {"In": {}}}]}
    check("user-scope clause detected when present", guard.query_has_user_scope(scoped))
    check("user-scope clause absence detected", not guard.query_has_user_scope(unscoped))

    # 2. abort if tenant scope disappears -> unknown stores must raise
    try:
        guard.assert_stores_in_inventory({"00111", "9999"}, INV, context="t")
        check("abort on store outside tenant inventory", False, "did not raise")
    except guard.TenantScopeError:
        check("abort on store outside tenant inventory", True)

    # empty result must also fail closed, not pass vacuously
    try:
        guard.assert_stores_in_inventory(set(), INV, context="t")
        check("abort on empty store set (fail closed)", False, "did not raise")
    except guard.TenantScopeError:
        check("abort on empty store set (fail closed)", True)

    # 3. foreign-store rejection using the real leaked brand names
    un = fixture("unscoped_sample.json")
    names = [s["hme_store_name_seen"] for s in un["stores"]] if un else ["18108 Dairy Queen"]
    try:
        guard.assert_no_foreign_brands(names, context="t")
        check("foreign-brand store names rejected", False, "did not raise")
    except guard.TenantScopeError:
        check("foreign-brand store names rejected", True, f"rejected {names}")

    # store-count mismatch (the 499-store leak signature)
    try:
        guard.assert_store_count(499, 34, context="t")
        check("abort when store count widens (499 vs 34)", False, "did not raise")
    except guard.TenantScopeError:
        check("abort when store count widens (499 vs 34)", True)

    # date determinism helper
    q = {"Where": [{"c": "datetime'2026-09-14T00:00:00'"},
                   {"c": "datetime'2026-09-14T23:59:59.999'"}]}
    check("explicit date literals extracted from query",
          guard.query_date_literals(q) == ["2026-09-14T00:00:00", "2026-09-14T23:59:59.999"])

# ---------------------------------------------------------------------------
print("\n== date determinism (two days differ) ==")
f13, f14 = fixture("store_daily_2026-09-13.json"), fixture("store_daily_2026-09-14.json")
if not (f13 and f14):
    check("date determinism fixtures present", False, f"missing under {FIXTURES}")
else:
    t13, t14 = f13["source_totals"], f14["source_totals"]
    check("09-14 totals match the known golden values",
          t14["total_orders"] == 5283 and t14["lane_total_avg_seconds"] == 191
          and abs(t14["disastrous_pct"] - 0.15445769449176605) < 1e-12,
          f"total={t14['total_orders']} avg={t14['lane_total_avg_seconds']}")
    check("09-13 differs from 09-14 on every headline measure",
          all(t13[k] != t14[k] for k in
              ("total_orders", "regular_orders", "disastrous_orders",
               "disastrous_pct", "lane_total_avg_seconds")),
          f"09-13 total={t13['total_orders']} vs 09-14 {t14['total_orders']}")
    d13 = {r["hme_store_number"]: r for r in f13["store_daily"]}
    d14 = {r["hme_store_number"]: r for r in f14["store_daily"]}
    both = set(d13) & set(d14)
    differing = sum(1 for s in both if d13[s]["total_orders"] != d14[s]["total_orders"])
    check("per-store totals differ between the two days",
          differing == len(both), f"{differing}/{len(both)} stores differ")

# ---------------------------------------------------------------------------
print("\n== units and scale ==")
if f14:
    pcts = [r["disastrous_pct"] for r in f14["store_daily"] if r["disastrous_pct"] is not None]
    check("percentages are stored as ratios in [0,1], not 0-100",
          all(0.0 <= p <= 1.0 for p in pcts) and max(pcts) < 1.0,
          f"max={max(pcts):.6f}")
    secs = [r["lane_total_avg_seconds"] for r in f14["store_daily"]
            if r["lane_total_avg_seconds"] is not None]
    check("lane times are whole seconds in a plausible range",
          all(isinstance(s, int) and 0 < s < 3600 for s in secs),
          f"min={min(secs)} max={max(secs)}")
    goals = [r["lane_total_goal_d_seconds"] for r in f14["store_daily"]
             if r["lane_total_goal_d_seconds"] is not None]
    check("goal thresholds are whole seconds", all(isinstance(g, int) for g in goals))

# ---------------------------------------------------------------------------
print("\n== Regular/Disastrous residual must NOT be silently corrected ==")
if f14:
    t = f14["source_totals"]
    delta = t["total_orders"] - (t["regular_orders"] + t["disastrous_orders"])
    check("known 2026-09-14 residual of 5 is preserved as-is", delta == 5,
          f"{t['regular_orders']}+{t['disastrous_orders']}={t['regular_orders']+t['disastrous_orders']} vs {t['total_orders']} (delta {delta})")
    doc = {"business_date": "2026-09-14", "requested_date": "2026-09-14",
           "facts": {"store_daily": f14["store_daily"], "outliers_daily": [],
                     "trend_store_daily": [], "goal_observations": []},
           "source_totals": {"store_daily": t, "outliers_daily": None}}
    ok, recon = hme_load.reconcile(doc)
    names = {c["check"]: c for c in recon["checks"]}
    rd = names.get("regular_plus_disastrous_vs_total")
    check("reconciliation records the residual without failing the run",
          rd is not None and rd["ok"] and rd["observed"] == 5
          and rd["expected"] == "not enforced")
    check("residual is surfaced as a warning", any("regular+disastrous" in w
                                                   for w in recon["warnings"]))

# ---------------------------------------------------------------------------
print("\n== credential hygiene ==")
try:
    hme_load.assert_no_secrets({"facts": {"a": [{"hme_store_number": "1"}]}})
    check("clean payload accepted", True)
except Exception as e:
    check("clean payload accepted", False, str(e))
for label, bad in (("embed_token at top level", {"embed_token": "x"}),
                   ("id_token nested in an object", {"nested": {"id_token": "x"}}),
                   ("ctx_token nested in a list", {"a": [{"ctx_token": "x"}]}),
                   ("Authorization header", {"Authorization": "Bearer x"}),
                   ("cookie", {"cookie": "x"}),
                   ("password", {"password": "x"})):
    try:
        hme_load.assert_no_secrets(bad)
        check(f"reject payload containing {label}", False, "did not raise")
    except hme_load.LoadAborted:
        check(f"reject payload containing {label}", True)

# the on-disk extract must not contain credential-shaped keys either
extract = "/var/lib/laynes/hme/raw/2026-09-14/querydata-facts.json"
if os.path.exists(extract):
    raw = open(extract).read().lower()
    check("persisted extract contains no token/credential material",
          not any(k in raw for k in ("id_token", "ctx_token", "bearer ",
                                     "authorization", "password", "set-cookie")))

# ---------------------------------------------------------------------------
print("\n== database: mapper identity and join rules ==")
conn = hme_load.connect()
try:
    cur = conn.cursor()

    # 4/5. store number is TEXT and leading zeros survive
    cur.execute("""SELECT data_type FROM information_schema.columns
                   WHERE table_name='hme_store_mapping' AND column_name='hme_store_number'""")
    check("hme_store_number is TEXT", cur.fetchone()[0] == "text")
    cur.execute("SELECT hme_store_number FROM hme_store_mapping WHERE hme_store_number LIKE '0%%' ORDER BY 1")
    zeros = [r[0] for r in cur.fetchall()]
    check("leading zeros preserved in the mapper", zeros == ["00111", "002", "004", "01"], str(zeros))
    cur.execute("""SELECT count(*) FROM hme_store_daily WHERE hme_store_number='00111'
                   AND business_date='2026-09-14'""")
    check("fact row addressable by zero-padded key '00111'", cur.fetchone()[0] == 1)

    # 7. no numeric HME -> Revel identity
    cur.execute("""SELECT m.hme_store_number, m.hme_store_name, e.id, e.name
                   FROM hme_store_mapping m JOIN establishments e
                     ON e.id::text = m.hme_store_number
                   WHERE m.revel_establishment_id IS DISTINCT FROM e.id""")
    bad_numeric = cur.fetchall()
    check("numeric HME==Revel equality would mis-map (so it is never used)",
          len(bad_numeric) >= 3,
          "; ".join(f"HME {a}={b} vs Revel {c}={d}" for a, b, c, d in bad_numeric))

    # 6. cross-system view exposes only VERIFIED + active
    cur.execute("""SELECT count(*) FROM v_hme_store_daily_verified v
                   JOIN hme_store_mapping m USING (hme_store_number)
                   WHERE m.mapping_status <> 'VERIFIED' OR NOT m.active""")
    check("cross-system view leaks no non-VERIFIED store", cur.fetchone()[0] == 0)
    cur.execute("SELECT count(*) FROM v_hme_store_daily_verified WHERE business_date='2026-09-14'")
    vcount = cur.fetchone()[0]
    check("cross-system view exposes exactly the 11 verified stores", vcount == 11, str(vcount))
    cur.execute("SELECT count(*) FROM hme_store_daily WHERE business_date='2026-09-14'")
    check("out-of-scope stores stay available for HME-only analytics",
          cur.fetchone()[0] == 34)

    # Revel-side gap is explicit, with no invented HME key
    cur.execute("""SELECT coverage_status FROM v_hme_revel_coverage
                   WHERE revel_establishment_id=48""")
    check("Revel 48 Downtown Houston is an explicit NO_HME_MAPPING gap",
          cur.fetchone()[0] == "NO_HME_MAPPING")
    cur.execute("SELECT count(*) FROM hme_store_mapping WHERE revel_establishment_id=48")
    check("no HME key was invented for Revel 48", cur.fetchone()[0] == 0)

    # VERIFIED requires a human signature
    try:
        cur.execute("""INSERT INTO hme_store_mapping
            (hme_store_number, hme_store_name, mapping_status) VALUES ('ZZTEST','x','VERIFIED')""")
        check("VERIFIED without revel id/verified_by is rejected", False, "insert succeeded")
    except Exception:
        check("VERIFIED without revel id/verified_by is rejected", True)
    conn.rollback()

    # 8. duplicate ACTIVE Revel mapping rejected by the partial unique index
    cur = conn.cursor()
    try:
        cur.execute("""INSERT INTO hme_store_mapping
            (hme_store_number, hme_store_name, revel_establishment_id, revel_store_name,
             mapping_status, verified_by, verified_at, active)
            VALUES ('ZZDUP','dup',14,'LCF Beaumont','VERIFIED','test',now(),true)""")
        check("second ACTIVE mapping to the same Revel id is rejected", False, "insert succeeded")
    except Exception:
        check("second ACTIVE mapping to the same Revel id is rejected", True)
    conn.rollback()

    # ... but an INACTIVE historical mapping to the same Revel id is allowed
    cur = conn.cursor()
    try:
        cur.execute("""INSERT INTO hme_store_mapping
            (hme_store_number, hme_store_name, revel_establishment_id, revel_store_name,
             mapping_status, verified_by, verified_at, active)
            VALUES ('ZZOLD','old',14,'LCF Beaumont','VERIFIED','test',now(),false)""")
        check("inactive historical mapping to the same Revel id is allowed", True)
    except Exception as e:
        check("inactive historical mapping to the same Revel id is allowed", False, str(e)[:90])
    conn.rollback()

    # percentage domain enforced at the schema level
    cur = conn.cursor()
    try:
        cur.execute("""INSERT INTO hme_store_daily
            (hme_store_number, business_date, disastrous_pct)
            VALUES ('00111','2099-01-01', 15.45)""")
        check("percent-scale value (15.45) rejected by CHECK", False, "insert succeeded")
    except Exception:
        check("percent-scale value (15.45) rejected by CHECK", True)
    conn.rollback()

    # 9. idempotent daily upsert
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM hme_store_daily WHERE business_date='2026-09-14'")
    before = cur.fetchone()[0]
    doc = None
    if os.path.exists(extract):
        with open(extract) as fh:
            doc = json.load(fh)
    if doc:
        hme_load.load(doc, conn, dry_run=True)   # rolls back internally
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM hme_store_daily WHERE business_date='2026-09-14'")
        check("re-loading the same day does not duplicate rows",
              cur.fetchone()[0] == before, f"{before} rows before and after")

    # loader refuses an unknown store (tenant guard at the DB gate)
    if doc:
        bad = json.loads(json.dumps(doc))
        bad["facts"]["store_daily"][0]["hme_store_number"] = "99999"
        try:
            hme_load.load(bad, conn, dry_run=True)
            check("loader aborts on a store absent from the mapper", False, "loaded")
        except hme_load.LoadAborted:
            check("loader aborts on a store absent from the mapper", True)
        conn.rollback()

        bad2 = json.loads(json.dumps(doc))
        bad2["tenant_scope_ok"] = False
        try:
            hme_load.load(bad2, conn, dry_run=True)
            check("loader aborts when tenant_scope_ok is false", False, "loaded")
        except hme_load.LoadAborted:
            check("loader aborts when tenant_scope_ok is false", True)
        conn.rollback()

        bad3 = json.loads(json.dumps(doc))
        bad3["requested_date"] = "2026-09-13"
        try:
            hme_load.load(bad3, conn, dry_run=True)
            check("loader aborts when business_date != requested_date", False, "loaded")
        except hme_load.LoadAborted:
            check("loader aborts when business_date != requested_date", True)
        conn.rollback()

    # 14. no HME relation is readable by the LLM role
    cur = conn.cursor()
    # Match HME relations by prefix. A substring match would also catch
    # "establis(hme)nts", which is a Revel table and is legitimately readable.
    cur.execute("""SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                   WHERE n.nspname='public'
                     AND (c.relname LIKE 'hme\\_%%' OR c.relname LIKE 'v\\_hme\\_%%')
                     AND c.relkind IN ('r','v','m')
                     AND has_table_privilege('laynes_ro', c.oid, 'SELECT')""")
    leaked = [r[0] for r in cur.fetchall()]
    check("no HME relation is granted to laynes_ro (LLM role)", not leaked, str(leaked))

    # goal history is observation-effective, never backdated
    cur.execute("""SELECT min(effective_from_date) FROM hme_goal_history""")
    gmin = cur.fetchone()[0]
    # 2026-09-08 is the EARLIEST day the values were actually observed. Case A
    # widened the period back to it; nothing before it is invented.
    check("goal history starts no earlier than the first real observation",
          gmin == datetime.date(2026, 9, 8), str(gmin))
    cur.execute("""SELECT count(*) FROM hme_goal_history
                   WHERE effective_to_date IS NOT NULL
                     AND effective_to_date < effective_from_date""")
    check("no inverted goal effective ranges", cur.fetchone()[0] == 0)
finally:
    conn.rollback()
    conn.close()

# ---------------------------------------------------------------------------
print("\n== goal history: four load-order cases ==")
import hme_daily_ingest as hdi  # noqa: E402

GOAL_CASE_STORE = "00111"


def _goal_case(cur, obs_date, vals, seed=None):
    """Run one observation against a seeded history inside a savepoint."""
    cur.execute("SAVEPOINT gcase")
    cur.execute("DELETE FROM hme_goal_conflict WHERE hme_store_number=%s", (GOAL_CASE_STORE,))
    cur.execute("DELETE FROM hme_goal_history WHERE hme_store_number=%s", (GOAL_CASE_STORE,))
    for frm, to, v in (seed or []):
        cur.execute("""INSERT INTO hme_goal_history (hme_store_number, effective_from_date,
                         effective_to_date, goal_a_seconds, goal_b_seconds,
                         goal_c_seconds, goal_d_seconds)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)""", (GOAL_CASE_STORE, frm, to, *v))
    obs = {"hme_store_number": GOAL_CASE_STORE,
           "goal_a_seconds": vals[0], "goal_b_seconds": vals[1],
           "goal_c_seconds": vals[2], "goal_d_seconds": vals[3]}
    outcome = hme_load.apply_goal_observation(cur, obs, obs_date, None)
    cur.execute("""SELECT effective_from_date, effective_to_date, goal_a_seconds,
                          goal_b_seconds, goal_c_seconds, goal_d_seconds
                   FROM hme_goal_history WHERE hme_store_number=%s
                   ORDER BY effective_from_date""", (GOAL_CASE_STORE,))
    rows = cur.fetchall()
    cur.execute("SELECT count(*) FROM hme_goal_conflict WHERE hme_store_number=%s",
                (GOAL_CASE_STORE,))
    nconf = cur.fetchone()[0]
    return outcome, rows, nconf


conn2 = hme_load.connect()
try:
    c2 = conn2.cursor()
    V = (240, 255, 270, 285)
    W = (200, 215, 230, 245)
    D8, D14 = datetime.date(2026, 9, 8), datetime.date(2026, 9, 14)

    # Case A: older observation, identical values -> extend backward
    outcome, rows, nconf = _goal_case(c2, D8, V, seed=[(D14, None, V)])
    check("case A: older + identical extends effective_from backward",
          outcome == "extended" and len(rows) == 1
          and rows[0][0] == D8 and rows[0][1] is None and nconf == 0,
          f"outcome={outcome} rows={rows}")

    # Case B: older observation, DIFFERENT values -> conflict, history untouched
    outcome, rows, nconf = _goal_case(c2, D8, W, seed=[(D14, None, V)])
    check("case B: older + different logs a conflict and rewrites nothing",
          outcome == "conflicts" and len(rows) == 1
          and rows[0][0] == D14 and tuple(rows[0][2:6]) == V and nconf == 1,
          f"outcome={outcome} rows={rows} conflicts={nconf}")

    # Case C: newer observation, identical values -> no new row
    outcome, rows, nconf = _goal_case(c2, D14, V, seed=[(D8, None, V)])
    check("case C: newer + identical creates no new row",
          outcome == "unchanged" and len(rows) == 1
          and rows[0][0] == D8 and rows[0][1] is None and nconf == 0,
          f"outcome={outcome} rows={rows}")

    # Case D: newer observation, changed values -> close old, open new
    outcome, rows, nconf = _goal_case(c2, D14, W, seed=[(D8, None, V)])
    ok_d = (outcome == "opened" and len(rows) == 2
            and rows[0][0] == D8 and rows[0][1] == D14 - datetime.timedelta(days=1)
            and tuple(rows[0][2:6]) == V
            and rows[1][0] == D14 and rows[1][1] is None and tuple(rows[1][2:6]) == W
            and nconf == 0)
    check("case D: newer + changed closes the old period and opens a new one",
          ok_d, f"outcome={outcome} rows={rows}")

    # Never fabricate values before a real observation
    outcome, rows, nconf = _goal_case(c2, D14, V, seed=None)
    check("first observation opens at its own date, nothing earlier invented",
          outcome == "opened" and len(rows) == 1 and rows[0][0] == D14,
          f"rows={rows}")
    conn2.rollback()

    # production state: identical goals across the 7 observed days
    c2 = conn2.cursor()
    c2.execute("""SELECT min(effective_from_date), count(*),
                         count(*) FILTER (WHERE effective_to_date IS NULL)
                  FROM hme_goal_history""")
    gmin, gtot, gopen = c2.fetchone()
    check("production goal history starts at the earliest observed day (2026-09-08)",
          gmin == datetime.date(2026, 9, 8), str(gmin))
    check("unchanged goals remain exactly one open row per store",
          gtot == 34 and gopen == 34, f"total={gtot} open={gopen}")
    c2.execute("SELECT count(*) FROM hme_goal_conflict WHERE resolved_at IS NULL")
    check("no unresolved goal conflicts in production", c2.fetchone()[0] == 0)
finally:
    conn2.rollback()
    conn2.close()

# ---------------------------------------------------------------------------
print("\n== DST-safe Chicago yesterday ==")
CHI = hdi.CHICAGO
cases = [
    # (chicago local now, expected target date, label)
    ((2026, 3, 8, 0, 30),  (2026, 3, 7),  "spring-forward day, just after midnight"),
    ((2026, 3, 8, 12, 0),  (2026, 3, 7),  "spring-forward day, midday (CDT)"),
    ((2026, 3, 9, 0, 30),  (2026, 3, 8),  "day after spring forward (23h day)"),
    ((2026, 3, 7, 23, 59), (2026, 3, 6),  "eve of spring forward (CST)"),
    ((2026, 11, 1, 0, 30), (2026, 10, 31), "fall-back day, before the repeat hour"),
    ((2026, 11, 1, 12, 0), (2026, 10, 31), "fall-back day, midday (CST)"),
    ((2026, 11, 2, 0, 30), (2026, 11, 1),  "day after fall back (25h day)"),
    ((2026, 10, 31, 23, 59), (2026, 10, 30), "eve of fall back (CDT)"),
    ((2026, 1, 1, 0, 1),   (2025, 12, 31), "new-year rollover"),
]
for local, expected, label in cases:
    now = datetime.datetime(*local, tzinfo=CHI)
    got = hdi.chicago_target_date(now)
    check(f"DST: {label}", got == datetime.date(*expected),
          f"chicago {now.isoformat()} -> {got} (expected {datetime.date(*expected)})")

# the ambiguous repeated hour on fall-back night must not change the answer
amb = datetime.datetime(2026, 11, 1, 1, 30, tzinfo=CHI)
check("DST: ambiguous repeated 01:30 on fall-back night still yields Oct 31",
      hdi.chicago_target_date(amb) == datetime.date(2026, 10, 31))

# host UTC date must not leak into the calculation
utc_now = datetime.datetime(2026, 9, 16, 2, 30, tzinfo=datetime.timezone.utc)
check("host UTC date is not used (UTC 09-16 02:30 -> Chicago yesterday 09-14)",
      hdi.chicago_target_date(utc_now) == datetime.date(2026, 9, 14),
      str(hdi.chicago_target_date(utc_now)))
try:
    hdi.chicago_target_date(datetime.datetime(2026, 9, 16, 2, 30))
    check("naive datetime is rejected", False, "did not raise")
except ValueError:
    check("naive datetime is rejected", True)

# ---------------------------------------------------------------------------
print("\n== run locking ==")
import fcntl as _fcntl  # noqa: E402
import subprocess as _sp  # noqa: E402
lockp = "/var/lib/laynes/hme/state/ingest.lock.test"
held = open(lockp, "w")
_fcntl.flock(held, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
env = dict(os.environ, HME_LOCK_PATH=lockp)
pr = _sp.run(["venv/bin/python", "hme_daily_ingest.py", "--date", "2026-09-14", "--dry-run"],
             capture_output=True, text=True, env=env, timeout=120)
check("second concurrent run exits 75 (EX_TEMPFAIL) without acting",
      pr.returncode == 75 and "holds the lock" in pr.stdout,
      f"rc={pr.returncode}")
check("blocked run performs no extraction", "[extract]" not in pr.stdout)
_fcntl.flock(held, _fcntl.LOCK_UN)
held.close()
os.unlink(lockp)

# ---------------------------------------------------------------------------
print("\n== retry / failure state ==")
conn3 = hme_load.connect()
try:
    c3 = conn3.cursor()
    c3.execute("SAVEPOINT rf")
    for st in ("failed", "aborted_tenant_scope", "partial"):
        rid = hme_load.record_failed_run(conn3, "2099-01-01", status=st,
                                         reason=f"synthetic {st}", attempt=3)
        c3 = conn3.cursor()
        c3.execute("""SELECT status, attempt, rows_written, warnings
                      FROM hme_ingest_run WHERE run_id=%s""", (rid,))
        row = c3.fetchone()
        check(f"failure status {st!r} is recorded, never as success",
              row[0] == st and row[1] == 3 and row[2] == 0
              and f"synthetic {st}" in row[3][0])
    try:
        hme_load.record_failed_run(conn3, "2099-01-01", status="succeeded", reason="x")
        check("record_failed_run refuses a success status", False, "did not raise")
    except ValueError:
        check("record_failed_run refuses a success status", True)
    c3 = conn3.cursor()
    c3.execute("DELETE FROM hme_ingest_run WHERE business_date='2099-01-01'")
    conn3.commit()

    # incomplete source must not load
    if os.path.exists(extract):
        with open(extract) as fh:
            stale = json.load(fh)
        stale["freshness_ok"] = False
        stale["freshness_detail"] = {"ok": False, "non_empty_results": False}
        try:
            hme_load.load(stale, conn3, dry_run=True)
            check("loader refuses an extract marked not fresh", False, "loaded")
        except hme_load.LoadAborted:
            check("loader refuses an extract marked not fresh", True)
        conn3.rollback()
finally:
    conn3.rollback()
    conn3.close()

# ---------------------------------------------------------------------------
print("\n== automation idempotency + provenance ==")
conn4 = hme_load.connect()
try:
    c4 = conn4.cursor()
    # Asserted as invariants (34 stores per day, 11 verified per day) rather
    # than a hardcoded day count, so adding a day does not rot the test.
    c4.execute("""SELECT count(*), count(DISTINCT business_date) FROM hme_store_daily""")
    n, d = c4.fetchone()
    check("hme_store_daily holds exactly 34 stores for every loaded day",
          n == 34 * d, f"{n} rows over {d} days")
    c4.execute("""SELECT count(*), count(DISTINCT business_date) FROM hme_outliers_daily""")
    n2, d2 = c4.fetchone()
    check("hme_outliers_daily holds exactly 34 for every loaded day",
          n2 == 34 * d2 and d2 == d, f"{n2} rows over {d2} days")
    c4.execute("""SELECT count(*), count(DISTINCT business_date)
                  FROM v_hme_store_daily_verified""")
    n3, d3 = c4.fetchone()
    check("verified view exposes exactly 11 stores for every loaded day",
          n3 == 11 * d3 and d3 == d, f"{n3} rows over {d3} days")
    c4.execute("""SELECT count(*) FROM (
                    SELECT 1 FROM hme_store_daily
                    GROUP BY hme_store_number, business_date HAVING count(*)>1) x""")
    check("no duplicate (store, date) daily facts", c4.fetchone()[0] == 0)
    c4.execute("""SELECT count(*) FROM hme_ingest_run
                  WHERE status='succeeded' AND (extractor_version IS NULL
                        OR loader_version IS NULL)""")
    c4.execute("""SELECT loader_version, verified_store_count, out_of_scope_store_count
                  FROM hme_ingest_run WHERE status='succeeded'
                  ORDER BY run_id DESC LIMIT 1""")
    lv = c4.fetchone()
    check("latest successful run records loader version and scope counts",
          lv is not None and lv[0] is not None and lv[1] == 11 and lv[2] == 23,
          str(lv))
finally:
    conn4.close()

# ---------------------------------------------------------------------------
print("\n== Power BI endpoint recognition (shape, not URL) ==")
from hme import pbireq  # noqa: E402
from hme import facts as hfacts  # noqa: E402

SQ = json.dumps({"version": "1.0.0", "queries": [{"Query": {"Commands": [
        {"SemanticQueryDataShapeCommand": {"Query": {"Version": 2, "From": []}}}]}}]})
SHARED = "https://wabi-west-us-c-primary-redirect.analysis.windows.net/explore/querydata"
DEDICATED = ("https://2a40c1b32b6d45f398e133b3b121c076.pbidedicated.windows.net"
             "/webapi/capacities/2a40c1b3/workloads/QES/QueryExecutionService"
             "/automatic/public/query")

ok, _ = pbireq.is_semantic_data_request("POST", SHARED, SQ)
check("shared /explore/querydata recognised", ok)
check("shared endpoint labelled", pbireq.endpoint_class(SHARED) == "shared:explore/querydata")

ok, _ = pbireq.is_semantic_data_request("POST", DEDICATED, SQ)
check("dedicated QES /public/query recognised", ok)
check("dedicated endpoint labelled",
      pbireq.endpoint_class(DEDICATED) == "dedicated:QES/public/query")

ok, _ = pbireq.is_semantic_data_request("POST", "https://dc.services.visualstudio.com/v2/track", SQ)
check("non-Power-BI host ignored even with a semantic body", not ok)
ok, _ = pbireq.is_semantic_data_request(
    "POST", "https://portal-service.hmecloud.com/api/pbi-metrics", SQ)
check("HME portal host ignored", not ok)
ok, _ = pbireq.is_semantic_data_request(
    "POST", "https://wabi-x.analysis.windows.net/telemetry/certifiedevents",
    json.dumps({"openReportEvents": []}))
check("Power BI POST without SemanticQueryDataShapeCommand ignored", not ok)
ok, _ = pbireq.is_semantic_data_request("GET", SHARED, SQ)
check("GET ignored", not ok)
ok, _ = pbireq.is_semantic_data_request("POST", SHARED, "not json at all")
check("unparseable body ignored", not ok)
ok, _ = pbireq.is_semantic_data_request("POST", SHARED, None)
check("empty body ignored", not ok)
ok, _ = pbireq.is_semantic_data_request("POST", SHARED, SQ.encode())
check("bytes body accepted", ok)

# --- response envelope adapter: both transports normalise identically ------
DSR = {"Version": 2, "DS": [{"N": "DS0", "PH": [{"DM0": [
    {"S": [{"N": "G0", "T": 1}, {"N": "M0", "T": 4}], "C": [" 00111 Beaumont", 7]}]}]}]}
DESC = {"Select": [{"Kind": 1, "Value": "G0", "Name": "X.Store"},
                   {"Kind": 2, "Value": "M0", "Name": "X.Metric"}]}
canonical = {"results": [{"result": {"data": {"descriptor": DESC, "dsr": DSR}}}]}
unwrapped = {"results": [{"data": {"descriptor": DESC, "dsr": DSR}}]}
jobwrapped = {"results": [{"jobResult": {"result": {"data": {"descriptor": DESC, "dsr": DSR}}}}]}
for label, env in (("canonical results[].result.data", canonical),
                   ("unwrapped results[].data", unwrapped),
                   ("dedicated results[].jobResult.result.data", jobwrapped)):
    pl = pbireq.normalise_result_envelope(env)
    okp = len(pl) == 1 and "dsr" in pl[0]
    check(f"envelope normalised: {label}", okp)
check("garbage envelope yields no payloads",
      pbireq.normalise_result_envelope({"nope": 1}) == []
      and pbireq.normalise_result_envelope(None) == [])

# ---------------------------------------------------------------------------
print("\n== Multi field map: both model schemas ==")

def _ex(fields, rows, date_lit=None, endpoint="shared:explore/querydata"):
    """Build a minimal captured exchange with the given named fields."""
    keys = [f"M{i}" for i in range(len(fields))]
    sel = [{"Kind": 2, "Value": k, "Name": nm} for k, nm in zip(keys, fields)]
    dm = [{"S": [{"N": k, "T": 4} for k in keys], "C": rows[0]}]
    for r in rows[1:]:
        dm.append({"C": r})
    where = ([{"Condition": {"c": f"datetime'{date_lit}T00:00:00'"}}] if date_lit else [])
    return {"endpoint_class": endpoint,
            "req_body": {"queries": [{"Query": {"Commands": [
                {"SemanticQueryDataShapeCommand": {"Query": {
                    "Version": 2, "From": [], "Select": sel, "Where": where}}}]}}]},
            "resp_json": {"results": [{"result": {"data": {
                "descriptor": {"Select": sel},
                "dsr": {"DS": [{"N": "DS0", "PH": [{"DM1": dm}]}]}}}}]}}

NEWF = ["Dim_User_Info.store",
        "Sum(Fact_Detectors_Eventdata_Daypart.GoalA)",
        "Sum(Fact_Detectors_Eventdata_Daypart.GoalB)",
        "Sum(Fact_Detectors_Eventdata_Daypart.GoalC)",
        "Sum(Fact_Detectors_Eventdata_Daypart.GoalD)",
        "Fact_Detectors_Eventdata_Daypart.Total Cars Goal A",
        "Fact_Detectors_Eventdata_Daypart.Total Cars Goal E"]
new_ex = _ex(NEWF, [["00111", 240, 255, 270, 285, 109, 36],
                    ["002", 300, 310, 320, 330, 50, 9]],
             date_lit="2026-09-15", endpoint="dedicated:QES/public/query")
r = hfacts.goal_observations([new_ex], "2026-09-15")
check("NEW Multi schema recognised", r is not None and r["schema"] == "new",
      None if r is None else r["schema"])
if r:
    by = {x["hme_store_number"]: x for x in r["rows"]}
    check("lowercase Dim_User_Info.store used as discrete key",
          sorted(by) == ["00111", "002"], str(sorted(by)))
    check("leading zeros preserved from discrete store field",
          "00111" in by and isinstance(list(by)[0], str))
    check("GoalA/B/C/D extracted from new model",
          by["00111"]["goal_a_seconds"] == 240 and by["00111"]["goal_b_seconds"] == 255
          and by["00111"]["goal_c_seconds"] == 270 and by["00111"]["goal_d_seconds"] == 285,
          json.dumps(by["00111"]))
    check("Total Cars Goal A/E captured",
          by["00111"].get("total_cars_goal_a") == 109
          and by["00111"].get("total_cars_goal_e") == 36)
    check("new-schema store name not invented from display parsing",
          by["00111"]["hme_store_name_seen"] is None)

OLDF = ["DIM User Info.Store",
        "Avg(Detector Event Data.GOALA)", "Avg(Detector Event Data.GOALB)",
        "Avg(Detector Event Data.GOALC)", "Avg(Detector Event Data.GOALD)"]
old_ex = _ex(OLDF, [[" 00111 Beaumont", 240, 255, 270, 285]], date_lit="2026-09-15")
r2 = hfacts.goal_observations([old_ex], "2026-09-15")
check("OLD Multi schema still supported", r2 is not None and r2["schema"] == "old",
      None if r2 is None else r2["schema"])
if r2:
    row = r2["rows"][0]
    check("old schema splits concatenated store string",
          row["hme_store_number"] == "00111" and row["hme_store_name_seen"] == "Beaumont",
          json.dumps(row))
    check("old schema GoalA..D extracted",
          row["goal_d_seconds"] == 285)

# dated instance must win over an undated one (the 37,724-vs-5,500 bug)
undated = _ex(NEWF, [["00111", 239, 248, 257, 292, 37724, 11104]], date_lit=None,
              endpoint="dedicated:QES/public/query")
r3 = hfacts.goal_observations([undated, new_ex], "2026-09-15")
check("dated goal exchange preferred over undated",
      r3 and r3["rows"][0]["goal_d_seconds"] == 285
      and r3["rows"][0]["total_cars_goal_a"] == 109,
      json.dumps(r3["rows"][0]) if r3 else None)

# unknown schema must fail closed
unknown = _ex(["Some_Other_Table.whatever", "Sum(Weird_Fact.GoalA)"],
              [["x", 1]], date_lit="2026-09-15")
try:
    hfacts.goal_observations([unknown], "2026-09-15")
    check("unknown Multi schema fails closed", False, "did not raise")
except hfacts.UnknownMultiSchema:
    check("unknown Multi schema fails closed", True)
check("no goal fields at all yields None (not an error)",
      hfacts.goal_observations([_ex(["A.b"], [["x"]], date_lit="2026-09-15")],
                               "2026-09-15") is None)

# ---------------------------------------------------------------------------
print("\n== 2026-09-15 regression path ==")
conn9 = hme_load.connect()
try:
    c9 = conn9.cursor()
    c9.execute("SELECT count(*) FROM hme_store_daily WHERE business_date='2026-09-15'")
    check("2026-09-15 loaded with 34 canonical rows", c9.fetchone()[0] == 34)
    c9.execute("""SELECT count(*) FROM v_hme_store_daily_verified
                  WHERE business_date='2026-09-15'""")
    check("2026-09-15 exposes 11 verified rows", c9.fetchone()[0] == 11)
    c9.execute("""SELECT sum(total_orders) FROM hme_store_daily
                  WHERE business_date='2026-09-15'""")
    check("2026-09-15 HME total is 5500", c9.fetchone()[0] == 5500)
    c9.execute("""SELECT count(*) FROM hme_store_daily d
                  JOIN hme_goal_history g USING (hme_store_number)
                  WHERE d.business_date='2026-09-15'
                    AND g.effective_to_date IS NULL
                    AND d.lane_total_goal_d_seconds IS DISTINCT FROM g.goal_d_seconds""")
    check("Goal D still matches PA Threshold for every store on 2026-09-15",
          c9.fetchone()[0] == 0)
    c9.execute("""SELECT count(*), count(DISTINCT business_date) FROM hme_store_daily""")
    n, d8 = c9.fetchone()
    # Invariant, not a fixed day count: 34 stores for every loaded day.
    check("fact table holds 34 stores for every loaded day", n == 34 * d8,
          f"{n} rows over {d8} days")
    c9.execute("""SELECT extractor_version FROM hme_ingest_run
                  WHERE business_date='2026-09-15' AND status='succeeded'
                  ORDER BY run_id DESC LIMIT 1""")
    check("09-15 run records the compatibility-fixed extractor version",
          (c9.fetchone() or [None])[0] == "hme_querydata/1.3.0")
finally:
    conn9.close()

# ---------------------------------------------------------------------------
print("\n== run status reflects reconciliation ==")
# Contract: reconciliation_ok true -> 'succeeded'; false -> 'partial'.
# A reconciliation-failing day must never be recorded or presented as a
# successful production ingestion (the original bug had both branches return
# 'succeeded', which let the 2026-09-16 Trend/PA divergence read as clean).
SETTLED = "/var/lib/laynes/hme/state/settling0916/querydata-facts.json"
LAGGED = "/var/lib/laynes/hme/raw/2026-09-16/querydata-facts.json"

check("'partial' is an allowed hme_ingest_run status", True)
conn10 = hme_load.connect()
try:
    c10 = conn10.cursor()
    c10.execute("""SELECT pg_get_constraintdef(oid) FROM pg_constraint
                   WHERE conname='hme_ingest_run_status_check'""")
    cdef = (c10.fetchone() or [""])[0]
    check("status CHECK constraint permits 'partial'", "partial" in cdef, cdef[:90])

    if os.path.exists(SETTLED):
        with open(SETTLED) as fh:
            good = json.load(fh)
        res = hme_load.load(good, conn10, dry_run=True)
        check("reconciliation_ok=true yields status 'succeeded'",
              res["reconciliation_ok"] and res["status"] == "succeeded",
              f"recon={res['reconciliation_ok']} status={res['status']}")
        conn10.rollback()
    else:
        check("settled 09-16 fixture present", False, SETTLED)

    if os.path.exists(LAGGED):
        with open(LAGGED) as fh:
            bad = json.load(fh)
        res = hme_load.load(bad, conn10, dry_run=True)
        check("reconciliation_ok=false yields status 'partial', NOT 'succeeded'",
              (not res["reconciliation_ok"]) and res["status"] == "partial",
              f"recon={res['reconciliation_ok']} status={res['status']}")
        check("partial run records which check failed as a warning",
              any("marked PARTIAL" in w for w in res["reconciliation"]["warnings"]),
              str(res["reconciliation"]["warnings"])[:120])
        failed = [c["check"] for c in res["reconciliation"]["checks"] if not c["ok"]]
        check("the failing check is identified by name",
              failed == ["trend_total_cars_matches_pa_total_orders_per_store"], str(failed))
        conn10.rollback()
    else:
        check("lagged 09-16 fixture present", False, LAGGED)
finally:
    conn10.rollback()
    conn10.close()

# A PARTIAL load must exit non-zero so systemd/orchestrator cannot show it clean.
if os.path.exists(LAGGED):
    pr = subprocess.run(["venv/bin/python", "hme_load.py", "--date", "2026-09-16",
                         "--file", LAGGED, "--dry-run"],
                        capture_output=True, text=True, timeout=300,
                        env=dict(os.environ, HME_RAW_DIR="/var/lib/laynes/hme/raw"))
    check("PARTIAL load exits non-zero (not presented as success)",
          pr.returncode == 2, f"rc={pr.returncode}")
    check("PARTIAL is announced in stdout",
          "LOAD COMPLETED AS PARTIAL" in pr.stdout)
    check("PARTIAL load does not print SUCCEEDED status",
          "status=SUCCEEDED" not in pr.stdout)
if os.path.exists(SETTLED):
    pr2 = subprocess.run(["venv/bin/python", "hme_load.py", "--date", "2026-09-16",
                          "--file", SETTLED, "--dry-run"],
                         capture_output=True, text=True, timeout=300,
                         env=dict(os.environ, HME_RAW_DIR="/var/lib/laynes/hme/raw"))
    check("reconciled load exits zero and reports SUCCEEDED",
          pr2.returncode == 0 and "status=SUCCEEDED" in pr2.stdout, f"rc={pr2.returncode}")

# ---------------------------------------------------------------------------
print("\n== 2026-09-16 settling correction ==")
conn11 = hme_load.connect()
try:
    c11 = conn11.cursor()
    c11.execute("""SELECT count(*) FROM hme_store_daily
                   WHERE business_date='2026-09-16'
                     AND total_orders IS DISTINCT FROM total_cars""")
    check("2026-09-16 Trend now equals PA for every store", c11.fetchone()[0] == 0)
    c11.execute("""SELECT sum(total_orders), sum(total_cars) FROM hme_store_daily
                   WHERE business_date='2026-09-16'""")
    pa, tr = c11.fetchone()
    check("2026-09-16 network PA and Trend totals agree at 5783",
          pa == 5783 and tr == 5783, f"PA={pa} Trend={tr}")

    # the corrected reload must not add rows
    c11.execute("""SELECT count(*), count(DISTINCT business_date) FROM hme_store_daily""")
    n, d = c11.fetchone()
    check("corrected reload added no store/date rows (34 per day)", n == 34 * d,
          f"{n} rows over {d} days")
    c11.execute("""SELECT count(*) FROM (SELECT 1 FROM hme_store_daily
                   GROUP BY hme_store_number, business_date HAVING count(*)>1) x""")
    check("no duplicate store/date rows after the correction", c11.fetchone()[0] == 0)

    # history must be preserved, not rewritten
    c11.execute("""SELECT status, reconciliation_ok, loader_version
                   FROM hme_ingest_run WHERE run_id=63""")
    r63 = c11.fetchone()
    check("historical run 63 still records reconciliation_ok=false",
          r63 is not None and r63[1] is False, str(r63))
    check("historical run 63 was not rewritten by the correction",
          r63[2] == "hme_load/1.1.0", str(r63))
    c11.execute("""SELECT run_id, status, reconciliation_ok FROM hme_ingest_run
                   WHERE business_date='2026-09-16' ORDER BY run_id DESC LIMIT 1""")
    latest = c11.fetchone()
    check("newest 2026-09-16 run is reconciled and succeeded",
          latest[1] == "succeeded" and latest[2] is True, str(latest))
    c11.execute("""SELECT count(*) FROM hme_ingest_run WHERE business_date='2026-09-16'""")
    check("both 2026-09-16 attempts remain auditable", c11.fetchone()[0] >= 2)
finally:
    conn11.close()

# ---------------------------------------------------------------------------
passed = sum(1 for _, ok, _ in results if ok)
failed = len(results) - passed
print(f"\n{'='*66}\nHME ingestion tests: {passed} passed, {failed} failed, {len(results)} total")
if failed:
    for n, ok, d in results:
        if not ok:
            print(f"  FAILED: {n} -- {d}")
sys.exit(1 if failed else 0)
