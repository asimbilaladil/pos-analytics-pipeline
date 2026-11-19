#!/usr/bin/env python3
"""Exit 5 (reconciliation pending scheduled retry) for the HME daily ingest.

Pure: no database, no network, no extractor. Every DB / extractor touchpoint
is monkeypatched, so this is safe to run anywhere.

What it proves
  1. The real 2026-11-18 shape (run 588: Trend lag at 115 Shepherd only)
     defers to the 10:15 retry, and the September Outliers-lag shape does too.
  2. Every other shape keeps exit 2 (fail closed): non-lag checks, mixed
     checks, FAILED / NO_ACCESS stores, run-id mismatch, a run the retry
     would refuse, past the deadline, wrong day, naive clock, errors.
  3. Nothing the database or the retry sees changes: classify_store_results
     still returns 2 for the same load (retry + backfill semantics intact),
     reconcile() is still exact-match, and the deferral is applied only by
     the scheduled main() path (never --date, never --dry-run).
  4. Units: the daily unit and the production drop-in add 5 and nothing
     else; 2 and 4 stay red; the retry unit and both timers are unchanged.
"""
import datetime
import os
import sys
import tempfile
import zoneinfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
for k in ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASS", "PGHOST", "PGPASSWORD"):
    os.environ.pop(k, None)

import hme_daily_ingest as I            # noqa: E402
import hme_load                         # noqa: E402
import hme_reconciliation_retry as R    # noqa: E402

CHI = zoneinfo.ZoneInfo("America/Chicago")
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not ok else ""))


def at(y, m, d, hh, mm):
    return datetime.datetime(y, m, d, hh, mm, tzinfo=CHI)


TREND = "trend_total_cars_matches_pa_total_orders_per_store"
OUTL = "pa_total_orders_vs_outliers_car_departures"


def checks(failed=(TREND,)):
    names = [OUTL, "store_rows_sum_vs_source_total", TREND,
             "regular_plus_disastrous_vs_total", "goal_d_matches_pa_threshold"]
    return [{"check": n, "ok": n not in failed, "observed": 1 if n in failed else 0,
             "expected": 0, "stores": ["115"] if n == TREND and n in failed else []}
            for n in names]


def stores(bad=(("PARTIAL", "RECONCILIATION_MISMATCH"),)):
    out = [{"revel_establishment_id": e, "hme_store_number": str(e),
            "status": "SUCCESS", "reason_code": "OK"}
           for e in (6, 7, 14, 20, 25, 26, 32, 36, 40, 54)]
    out.append({"revel_establishment_id": 48, "hme_store_number": None,
                "status": "SKIPPED", "reason_code": "UNMAPPED"})
    for i, (st, rc) in enumerate(bad):
        out.append({"revel_establishment_id": 15 + 100 * i, "hme_store_number": "115",
                    "status": st, "reason_code": rc})
    return out


def res588(**kw):
    r = {"run_id": 588, "status": "partial", "reconciliation_ok": False,
         "reconciliation": {"checks": checks(), "warnings": []},
         "store_results": stores(), "store_count": 11}
    r.update(kw)
    return r


def run588(**kw):
    r = {"run_id": 588, "status": "partial", "reconciliation_ok": False,
         "tenant_scope_ok": True, "store_count": 11, "verified_store_count": 11,
         "out_of_scope_store_count": 0,
         "completeness_detail": {"verified_observed_count": 11, "verified_expected_count": 11,
                                 "observed_store_count": 11, "expected_store_count": 34,
                                 "missing_mapping_statuses": ["OUT_OF_SCOPE"],
                                 "unexpected_store_count": 0, "no_access_verified_count": 0}}
    r.update(kw)
    return r


T = "2026-11-18"
MORNING = at(2026, 11, 19, 5, 38)          # run 588 actually finished 05:38:40 CST


def ap(res=None, run=None, target=T, now=MORNING):
    return I.assess_pending_retry(res588() if res is None else res,
                                  run588() if run is None else run, target=target, now=now)


print("1. lag shapes defer to the retry")
ok, why = ap()
check("run 588 shape (Trend lag, 115 Shepherd only) at 05:38 -> defer", ok, why)
ok, why = ap(res=res588(reconciliation={"checks": checks((OUTL,)), "warnings": []},
                        store_results=stores([("PARTIAL", "RECONCILIATION_MISMATCH")] * 11)))
check("Sep 2026 shape (Outliers lag, day-level check) -> defer", ok, why)
ok, why = ap(res=res588(reconciliation={"checks": checks((OUTL, TREND)), "warnings": []}))
check("both settling checks failed together -> defer", ok, why)
ok, _ = ap(now=at(2026, 11, 19, 9, 59))
check("09:59 Chicago (still before the 10:15 retry) -> defer", ok)
ok, _ = ap(target="2026-03-08", now=at(2026, 3, 9, 9, 59))
check("DST: morning after spring-forward, 09:59 CDT -> defer", ok)
ok, _ = ap(target="2026-11-01", now=at(2026, 11, 2, 5, 40))
check("DST: morning after fall-back, 05:40 CST -> defer", ok)
ok, _ = ap(now=datetime.datetime(2026, 11, 19, 11, 38, tzinfo=datetime.timezone.utc))
check("UTC-aware clock is converted to Chicago (11:38 UTC = 05:38 CST) -> defer", ok)

print("2. every other shape keeps exit 2 (fail closed)")
for name, kw in [
    ("goal_d mismatch (a goal change, not a lag)", dict(reconciliation={"checks": checks(("goal_d_matches_pa_threshold",)), "warnings": []})),
    ("store rows != source total", dict(reconciliation={"checks": checks(("store_rows_sum_vs_source_total",)), "warnings": []})),
    ("Trend lag + goal_d mismatch together", dict(reconciliation={"checks": checks((TREND, "goal_d_matches_pa_threshold")), "warnings": []})),
    ("an unknown failed check name", dict(reconciliation={"checks": checks() + [{"check": "new_check", "ok": False}], "warnings": []})),
    ("recon failed but no failed check identified", dict(reconciliation={"checks": checks(()), "warnings": []})),
    ("recon detail missing", dict(reconciliation=None)),
    ("a VERIFIED store FAILED/NO_ACCESS", dict(store_results=stores([("PARTIAL", "RECONCILIATION_MISMATCH"), ("FAILED", "NO_ACCESS")]))),
    ("a VERIFIED store FAILED/NO_DATA", dict(store_results=stores([("FAILED", "NO_DATA")]))),
    ("a store PARTIAL/FACT_SET_MISSING", dict(store_results=stores([("PARTIAL", "FACT_SET_MISSING")]))),
    ("reconciled partial (that is exit 3/4, not 5)", dict(reconciliation_ok=True)),
    ("reconciliation_ok unknown (None)", dict(reconciliation_ok=None)),
    ("status succeeded", dict(status="succeeded")),
    ("status failed", dict(status="failed")),
]:
    ok, why = ap(res=res588(**kw))
    check(f"{name} -> exit 2 kept", not ok, why)

for name, run in [
    ("latest network run is a different run_id", run588(run_id=589)),
    ("tenant_scope_ok false (retry never re-extracts)", run588(tenant_scope_ok=False)),
    ("run recorded as failed", run588(status="failed")),
    ("run already reconciled in the DB", run588(reconciliation_ok=True)),
    ("VERIFIED coverage 10/11", run588(completeness_detail=dict(run588()["completeness_detail"], verified_observed_count=10, missing_mapping_statuses=["OUT_OF_SCOPE", "VERIFIED"]))),
    ("store outside the allowlist present", run588(completeness_detail=dict(run588()["completeness_detail"], unexpected_store_count=1))),
]:
    ok, why = ap(run=run)
    check(f"{name} -> exit 2 kept", not ok, why)
ok, why = I.assess_pending_retry(res588(), None, target=T, now=MORNING)
check("no run record -> exit 2 kept", not ok, why)
ok, why = I.assess_pending_retry(None, run588(), target=T, now=MORNING)
check("no load result -> exit 2 kept", not ok, why)

for name, now, tgt in [
    ("10:00 Chicago (deadline)", at(2026, 11, 19, 10, 0), T),
    ("10:20 Chicago (retry already ran or is running)", at(2026, 11, 19, 10, 20), T),
    ("23:59 the same day", at(2026, 11, 19, 23, 59), T),
    ("a day later (no retry for this date any more)", at(2026, 11, 20, 5, 38), T),
    ("same day as the business date", at(2026, 11, 18, 5, 38), T),
]:
    ok, why = ap(now=now, target=tgt)
    check(f"{name} -> exit 2 kept", not ok, why)
ok, why = ap(now=datetime.datetime(2026, 11, 19, 5, 38))
check("naive clock -> exit 2 kept", not ok, why)

print("3. database-facing semantics unchanged")
check("classify_store_results still returns 2 for the run-588 load (retry/backfill unchanged)",
      I.classify_store_results(res588()) == I.EXIT_RECONCILIATION_FAILED)
check("exit codes distinct: 0 1 2 3 4 5 75",
      (I.EXIT_OK, I.EXIT_HARD_FAILURE, I.EXIT_RECONCILIATION_FAILED, I.EXIT_ACCEPTED_PARTIAL,
       I.EXIT_PARTIAL_STORES, I.EXIT_RECONCILIATION_PENDING_RETRY, I.EXIT_NOOP) == (0, 1, 2, 3, 4, 5, 75))
check("retry module does not use exit 5", not hasattr(R, "EXIT_RECONCILIATION_PENDING_RETRY")
      and "PENDING" not in open("hme_reconciliation_retry.py").read())


def doc(pa, trend):
    return {"facts": {"store_daily": [{"hme_store_number": "115", "total_orders": pa,
                                       "regular_orders": pa, "disastrous_orders": 0,
                                       "lane_total_goal_d_seconds": 285}],
                      "outliers_daily": [], "goal_observations": [],
                      "trend_store_daily": [{"hme_store_number": "115", "total_cars": trend}]},
            "source_totals": {"store_daily": {"total_orders": pa, "regular_orders": pa,
                                              "disastrous_orders": 0},
                              "outliers_daily": {"car_departures": pa}}}


ok, _ = hme_load.reconcile(doc(317, 316))
check("reconcile() still exact: a 1-car (0.3%) Trend gap still FAILS", not ok)
ok, _ = hme_load.reconcile(doc(292, 272))
check("reconcile(): the 11-18 Shepherd gap (292 vs 272) FAILS", not ok)
ok, _ = hme_load.reconcile(doc(292, 292))
check("reconcile(): equal counts pass", ok)

print("4. only the scheduled main() path defers; errors keep exit 2")
calls = {"defer": 0}
_orig = dict(acquire=I.acquire_lock, env=I.load_env_file, inv=I.refresh_tenant_inventory,
             ing=I.ingest_date, conn=hme_load.connect, latest=R.latest_run, logdir=I.LOG_DIR,
             flock=I.fcntl.flock, timer=I.retry_timer_active)


class _Conn:
    closed = False

    def close(self):
        self.closed = True


try:
    I.LOG_DIR = tempfile.mkdtemp(prefix="hme-pending-test-")
    I.acquire_lock = lambda fh: open(os.devnull, "w")
    I.load_env_file = lambda p: {}
    I.refresh_tenant_inventory = lambda c, fh: 34
    I.ingest_date = lambda target, **kw: (I.EXIT_RECONCILIATION_FAILED, res588(run_id=calls.get("rid", 588)), "partial")
    hme_load.connect = lambda: _Conn()
    R.latest_run = lambda conn, target: run588()
    I.fcntl.flock = (lambda f, op: None)
    I.retry_timer_active = lambda: True

    def run_main(argv, now):
        real_dt = I.datetime.datetime

        class _DT(real_dt):
            @classmethod
            def now(cls, tz=None):
                return now.astimezone(tz) if tz else now.replace(tzinfo=None)
        I.datetime.datetime = _DT
        sys.argv = ["hme_daily_ingest.py"] + argv
        try:
            return I.main()
        finally:
            I.datetime.datetime = real_dt

    check("scheduled run at 05:38 with a Trend lag -> main() returns 5",
          run_main([], MORNING) == I.EXIT_RECONCILIATION_PENDING_RETRY)
    check("explicit --date run -> main() keeps 2",
          run_main(["--date", T], MORNING) == I.EXIT_RECONCILIATION_FAILED)
    check("--dry-run -> main() keeps 2",
          run_main(["--dry-run"], MORNING) == I.EXIT_RECONCILIATION_FAILED)
    check("scheduled run finishing at 10:05 -> main() keeps 2",
          run_main([], at(2026, 11, 19, 10, 5)) == I.EXIT_RECONCILIATION_FAILED)

    I.retry_timer_active = lambda: False
    check("retry timer not active -> main() keeps 2",
          run_main([], MORNING) == I.EXIT_RECONCILIATION_FAILED)
    I.retry_timer_active = lambda: True
    n_conn = {"n": 0}

    def boom():                     # main()'s inventory connection works; the
        n_conn["n"] += 1            # deferral's own connection then fails
        if n_conn["n"] > 1:
            raise RuntimeError("db unavailable")
        return _Conn()
    hme_load.connect = boom
    check("DB error while assessing -> main() keeps 2 (fail closed)",
          run_main([], MORNING) == I.EXIT_RECONCILIATION_FAILED)
    hme_load.connect = lambda: _Conn()
    R.latest_run = lambda conn, target: None
    check("no run row found -> main() keeps 2", run_main([], MORNING) == I.EXIT_RECONCILIATION_FAILED)
    R.latest_run = lambda conn, target: run588()
    I.ingest_date = lambda target, **kw: (I.EXIT_OK, {"status": "succeeded"}, "succeeded")
    check("clean day -> main() returns 0, untouched", run_main([], MORNING) == I.EXIT_OK)
    I.ingest_date = lambda target, **kw: (I.EXIT_PARTIAL_STORES, res588(reconciliation_ok=True), "partial")
    check("exit 4 (partial stores) is never rewritten", run_main([], MORNING) == I.EXIT_PARTIAL_STORES)
    I.ingest_date = lambda target, **kw: (I.EXIT_HARD_FAILURE, None, "extractor rc=1")
    check("exit 1 (hard failure) is never rewritten", run_main([], MORNING) == I.EXIT_HARD_FAILURE)
    logs = open(os.path.join(I.LOG_DIR, f"daily-ingest-{T}.log")).read()
    check("the log states exit 5 and that the run stays unreconciled/UNVERIFIED",
          "exit 5: reconciliation pending scheduled retry" in logs and "UNVERIFIED" in logs)
    check("the log states why exit 2 was kept", "exit 2 kept:" in logs)
    check("the log names the inactive retry timer", "hme-reconciliation-retry.timer is not active" in logs)
finally:
    I.acquire_lock, I.load_env_file = _orig["acquire"], _orig["env"]
    I.refresh_tenant_inventory, I.ingest_date = _orig["inv"], _orig["ing"]
    hme_load.connect, R.latest_run, I.LOG_DIR = _orig["conn"], _orig["latest"], _orig["logdir"]
    I.fcntl.flock = _orig["flock"]
    I.retry_timer_active = _orig["timer"]

_run = I.subprocess.run
try:
    def _fake(rc):
        return lambda *a, **k: type("P", (), {"returncode": rc})()
    I.subprocess.run = _fake(0)
    check("retry_timer_active: systemctl rc 0 -> True", I.retry_timer_active() is True)
    I.subprocess.run = _fake(3)
    check("retry_timer_active: rc 3 (inactive) -> False", I.retry_timer_active() is False)

    def _raise(*a, **k):
        raise FileNotFoundError("systemctl")
    I.subprocess.run = _raise
    check("retry_timer_active: systemctl missing -> False", I.retry_timer_active() is False)
finally:
    I.subprocess.run = _run

print("5. systemd units and timers")


def sxs(path):
    lines = [l for l in open(path).read().splitlines() if l.startswith("SuccessExitStatus=")]
    return lines, set(" ".join(l.split("=", 1)[1] for l in lines).split())


lines, codes = sxs("systemd/hme-daily-ingest.service")
check("daily unit: SuccessExitStatus declared once, exactly {0,3,5,75}",
      len(lines) == 1 and codes == {"0", "3", "5", "75"}, lines)
check("daily unit: 2 and 4 still red", not ({"2", "4"} & codes))
dl, dcodes = sxs("systemd/dropins/hme-daily-ingest-20-pending-retry.conf")
check("drop-in: adds exactly 5 (live unit's 0 3 75 + 5 == repo unit)",
      dcodes == {"5"} and {"0", "3", "75"} | dcodes == codes, dl)
check("drop-in: no ExecStart / timer / environment override",
      all(l.startswith(("#", "[Service]", "SuccessExitStatus=")) or not l.strip()
          for l in open("systemd/dropins/hme-daily-ingest-20-pending-retry.conf").read().splitlines()))
_, rcodes = sxs("systemd/hme-reconciliation-retry.service")
check("retry unit unchanged: {0,3,75}; its exit 2 stays red", rcodes == {"0", "3", "75"})
check("daily timer unchanged: 05:30 America/Chicago",
      "OnCalendar=*-*-* 05:30:00 America/Chicago" in open("systemd/hme-daily-ingest.timer").read())
rt = open("systemd/hme-reconciliation-retry.timer").read()
check("retry timer unchanged: 10:15 America/Chicago", "OnCalendar=*-*-* 10:15:00 America/Chicago" in rt)
check("defer deadline (10:00) precedes the retry elapse (10:15)",
      I.RETRY_DEFER_DEADLINE < datetime.time(10, 15))

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed" + (f"; FAILED: {failed}" if failed else ""))
sys.exit(1 if failed else 0)
