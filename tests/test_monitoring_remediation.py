#!/usr/bin/env python3
"""Regression tests for the PR #38 review findings (FIX 1-7, 9).

Run:  venv/bin/python tests/test_monitoring_remediation.py      (exit 0 = pass)

Pure: no PostgreSQL, no network, no systemd. Stores are in-memory SQLite or
files in a private temporary directory; notification channels are fakes or the
real Telegram/e-mail adapters with their transport (urlopen / smtplib.SMTP)
replaced, so nothing is ever sent.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
from datetime import datetime, timedelta, timezone

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

from monitoring import cli, engine, notify  # noqa: E402
from monitoring import config as mcfg  # noqa: E402
from monitoring import summary  # noqa: E402
from monitoring.checks import CheckSpec, infra, registry, revel, selfmon  # noqa: E402
from monitoring.config import CHICAGO  # noqa: E402
from monitoring.model import (ACCEPTED, FAIL, HIGH, OK, PENDING, WARNING,  # noqa: E402
                              CheckResult, dedup_key)
from monitoring.store import Store, StoreSchemaError  # noqa: E402

results: list[tuple[str, bool]] = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {str(detail)[:400]}" if not ok and detail else ""))


T0 = datetime(2026, 11, 25, 16, 0, tzinfo=timezone.utc)
TMP = tempfile.mkdtemp(prefix="monitor-remediation-")
os.chmod(TMP, 0o755)          # so the unprivileged-process test can reach the store dir
for k in ("MONITOR_TELEGRAM_BOT_TOKEN", "MONITOR_TELEGRAM_CHAT_ID", "MONITOR_SMTP_USER",
          "MONITOR_SMTP_PASS"):
    os.environ.pop(k, None)


class FakeChannel(notify.Channel):
    name = "fake"

    def __init__(self, cfg, fail_times=0, err="HTTPError: 502", delay=0.0):
        super().__init__(cfg)
        self.sent, self.fail_times, self.err, self.delay = [], fail_times, err, delay
        self.calls = 0
        self.lock = threading.Lock()

    def send(self, body, subject):
        with self.lock:
            self.calls += 1
            n = self.calls
        if self.delay:
            time.sleep(self.delay)
        if n <= self.fail_times:
            raise notify.DeliveryError(self.err)
        with self.lock:
            self.sent.append(body)


def make_cfg(**over):
    base = {"notify": {"channels": ["fake"], "dry_run": False},
            "monitor": {"state_db": ":memory:"}}
    for k, v in over.items():
        base.setdefault(k, {}).update(v)
    return mcfg._merge(mcfg.DEFAULTS, base)


TIMER_OK = {"LoadState": "loaded", "ActiveState": "active", "UnitFileState": "enabled"}


def systemctl(states=None):
    states = states or {}
    return lambda unit: states.get(unit, TIMER_OK)


def run(cfg, store, now, specs, tier="data", channel=None, **ctx):
    ch = channel or FakeChannel(cfg)
    ctx.setdefault("systemctl", systemctl())
    out = engine.run_tier(cfg, tier, store=store, now=now, specs=specs, ctx_kwargs=ctx,
                          channels={"fake": ch, "log": notify.LogChannel(cfg)})
    return out, ch


def spec(fn, name="t.check", tiers=("data",), **kw):
    return CheckSpec(name, tiers, fn, **kw)


def ok_check(ctx):
    return [CheckResult(source="revel", check="t", state=OK, title="fine", store_id=1)]


def fail_check(ctx):
    return [CheckResult(source="revel", check="t", state=FAIL, severity=HIGH, title="bad",
                        store_id=1, actions=["look"])]


def boom(ctx):
    raise RuntimeError("synthetic crash")


def active(store):
    return {r["dedup_key"]: dict(r) for r in store.active_incidents()}


def run_row(store, run_id):
    return dict(store.db.execute("SELECT * FROM monitor_runs WHERE id = ?", (run_id,)).fetchone())


# ═══════════════════════════════════════════════════════════════════════════
print("FIX 1. monitor execution status is honest")
cfg = make_cfg()
st = Store(":memory:")
out, _ = run(cfg, st, T0, [spec(ok_check)])
r = run_row(st, out["run_id"])
check("all checks pass -> SUCCESS, ok=1, exit 0",
      out["status"] == "SUCCESS" and out["ok"] and r["status"] == "SUCCESS" and r["ok"] == 1
      and cli.EXIT_CODES[out["status"]] == 0, r)
first_ok = out["run_id"]

out, _ = run(cfg, st, T0 + timedelta(minutes=15), [spec(ok_check), spec(boom, name="t.boom")])
r = run_row(st, out["run_id"])
check("one check crashes -> DEGRADED, ok=0, exit 1",
      out["status"] == "DEGRADED" and r["ok"] == 0 and r["status"] == "DEGRADED"
      and cli.EXIT_CODES[out["status"]] == 1 and "t.boom" in r["error"], r)
check("last_run(ok_only) never returns a DEGRADED run",
      st.last_run("data", ok_only=True)["id"] == first_ok)
check("the crash keeps its own check_error incident",
      dedup_key("monitor", "check_error", None, None, "t.boom") in active(st))

st2 = Store(":memory:")
out, _ = run(cfg, st2, T0, [spec(fail_check)], channel=FakeChannel(cfg, fail_times=99))
r = run_row(st2, out["run_id"])
check("data-quality FAIL alone is not a monitor fault, but a failed delivery is: DEGRADED",
      out["status"] == "DEGRADED" and "delivery" in r["error"] and out["crashed"] == [], r)
st3 = Store(":memory:")
out, _ = run(cfg, st3, T0, [spec(fail_check)])
check("data-quality FAIL with delivery OK -> SUCCESS (incidents are not run failures)",
      out["status"] == "SUCCESS" and len(active(st3)) == 1)


def needs_db(ctx):
    ctx.pg()
    return []


def broken():
    raise OSError("could not connect")


st4 = Store(":memory:")
out, _ = run(cfg, st4, T0, [spec(needs_db, needs_pg=True)], pg_connect=broken)
check("PostgreSQL unavailable -> DEGRADED, never SUCCESS",
      out["status"] == "DEGRADED" and st4.last_run("data", ok_only=True) is None
      and "postgres unavailable" in run_row(st4, out["run_id"])["error"])


def explode_store(ctx):
    ctx.store.db.execute("DROP TABLE latest_results")   # the pass itself cannot complete
    return [CheckResult(source="revel", check="t", state=OK, title="x")]


st5 = Store(":memory:")
try:
    run(cfg, st5, T0, [spec(explode_store)])
    raised = False
except Exception:  # noqa: BLE001
    raised = True
r = dict(st5.db.execute("SELECT * FROM monitor_runs ORDER BY id DESC LIMIT 1").fetchone())
check("aborted pass -> FAILED recorded (exit 2), exception propagates",
      raised and r["status"] == "FAILED" and r["ok"] == 0 and r["finished_at"], r)

# Repeated failures -> the OTHER tier's heartbeat detects the missing success,
# and only a SUCCESS clears it.
st = Store(":memory:")
hb_spec = spec(selfmon.heartbeat, name="monitor.heartbeat", tiers=("health", "data"))
run(cfg, st, T0, [spec(ok_check)], tier="data")
run(cfg, st, T0, [hb_spec], tier="health")
hb_key = dedup_key("monitor", "heartbeat", None, None, "data")
for k in range(1, 7):     # data keeps crashing every 15 min for 90 min
    run(cfg, st, T0 + timedelta(minutes=15 * k), [spec(boom, name="t.boom")], tier="data")
    run(cfg, st, T0 + timedelta(minutes=15 * k), [hb_spec], tier="health")
a = active(st)
check("repeated DEGRADED data runs -> heartbeat incident (no SUCCESS for > 45 min)",
      hb_key in a and a[hb_key]["category"] == "monitor_stale"
      and "DEGRADED" in a[hb_key]["actual"], a.get(hb_key))
run(cfg, st, T0 + timedelta(minutes=110), [spec(boom, name="t.boom")], tier="data")
run(cfg, st, T0 + timedelta(minutes=111), [hb_spec], tier="health")
check("another unsuccessful run does NOT clear the stale-monitor incident", hb_key in active(st))
ov = summary.overview(st, T0 + timedelta(minutes=111), cfg)
check("dashboard agrees with the run record: data tier STALE, last attempt DEGRADED",
      ov["monitor"]["tiers"]["data"]["state"] == "STALE"
      and ov["monitor"]["tiers"]["data"]["last_attempt_status"] == "DEGRADED", ov["monitor"])
run(cfg, st, T0 + timedelta(minutes=120), [spec(ok_check)], tier="data")
run(cfg, st, T0 + timedelta(minutes=121), [hb_spec], tier="health")
check("recovery: heartbeat clears only after a SUCCESSFUL data run", hb_key not in active(st))

# CLI summary: exit code == recorded status == dashboard
sdb = os.path.join(TMP, "summary.db")
scfg = make_cfg(monitor={"state_db": sdb}, notify={"channels": ["log"], "dry_run": True})
rc = cli._summary(scfg, __import__("logging").getLogger("t"))
s = Store(sdb)
last = s.last_attempt("summary")
check("summary (dry-run) -> SUCCESS, exit 0", rc == 0 and last["status"] == "SUCCESS", dict(last))
scfg = make_cfg(monitor={"state_db": sdb}, notify={"channels": ["telegram"], "dry_run": False})
rc = cli._summary(scfg, __import__("logging").getLogger("t"))
last = s.last_attempt("summary")
ov = summary.overview(s, datetime.now(timezone.utc), scfg)
check("summary with a failing channel -> DEGRADED recorded, exit 1, dashboard shows DEGRADED",
      rc == 1 and last["status"] == "DEGRADED"
      and ov["monitor"]["tiers"]["summary"]["last_attempt_status"] == "DEGRADED", dict(last))
s.close()

# crash-notify: deduplicated incident, resolved by the tier's next SUCCESS
cdb = os.path.join(TMP, "crash.db")
ccfg = make_cfg(monitor={"state_db": cdb}, notify={"channels": ["log"], "dry_run": True})
for _ in range(3):
    cli._crash_notify(ccfg, "laynes-monitor-data.service", __import__("logging").getLogger("t"))
s = Store(cdb)
ukey = dedup_key("monitor", "unit_failed", None, None, "laynes-monitor-data.service")
n_unit = s.db.execute("SELECT COUNT(*) FROM notifications WHERE kind = 'OPENED'").fetchone()[0]
check("crash-notify x3 -> ONE unit_failed incident, ONE alert", ukey in active(s) and n_unit == 1,
      n_unit)
run(ccfg, s, datetime.now(timezone.utc), [spec(ok_check)], tier="data")
check("next SUCCESSFUL data pass resolves the unit_failed incident", ukey not in active(s))
s.close()

# ═══════════════════════════════════════════════════════════════════════════
print("FIX 2. tiers that never start are detected")
cfg = make_cfg()


def hb(store, now, tier="health", states=None, c=None, pg_connect=None):
    ctx = engine.Context(c or cfg, store, now, tier, systemctl=systemctl(states),
                         pg_connect=pg_connect)
    return {r.qualifier: r for r in selfmon.heartbeat(ctx)}


st = Store(":memory:")
st.start_run("health", T0, "x")          # the very first run of a fresh install
h = hb(st, T0)
check("fresh install: other tiers PENDING within the grace (no false alert)",
      h["data"].state == PENDING and h["summary"].state == PENDING, h)
check("missing history is never OK", all(r.state != OK for r in h.values()))
h = hb(st, T0 + timedelta(minutes=61))
check("after the grace, a data tier that never ran -> HIGH never_ran",
      h["data"].state == FAIL and h["data"].severity == HIGH and h["data"].category == "never_ran",
      h["data"])
check("summary judged on its own limit (still PENDING at 61 min)", h["summary"].state == PENDING)
h = hb(st, T0 + timedelta(hours=26))
check("summary never ran after 26h -> HIGH never_ran", h["summary"].category == "never_ran")
check("health tier checked independently (by the data tier)",
      hb(st, T0 + timedelta(minutes=61), tier="data")["health"].state == FAIL)

st = Store(":memory:")
for tier in ("health", "data", "summary"):
    rid = st.start_run(tier, T0 - timedelta(minutes=5), "x")
    st.finish_run(rid, T0 - timedelta(minutes=5), "SUCCESS")
st.db.execute("UPDATE monitor_runs SET started_at = ?", ((T0 - timedelta(days=2)).isoformat(),))
rid = st.start_run("health", T0 - timedelta(minutes=5), "x")
st.finish_run(rid, T0 - timedelta(minutes=5), "SUCCESS")
h = hb(st, T0)
check("all tiers recent and timers active -> OK", h["data"].state == OK and h["summary"].state == OK, h)
h = hb(st, T0, states={"laynes-monitor-data.timer": {"LoadState": "loaded", "ActiveState": "inactive",
                                                     "UnitFileState": "disabled"}})
check("disabled timer -> HIGH timer_disabled even though the last run was recent",
      h["data"].state == FAIL and h["data"].category == "timer_disabled", h["data"])
h = hb(st, T0, states={"laynes-monitor-summary.timer": {"LoadState": "not-found"}})
check("missing timer unit -> HIGH timer_missing", h["summary"].category == "timer_missing")
h = hb(st, T0, states={"laynes-monitor-data.timer": {"LoadState": "loaded", "ActiveState": "inactive",
                                                     "UnitFileState": "enabled"}})
check("stopped (inactive) timer -> HIGH timer_inactive", h["data"].category == "timer_inactive")
h = hb(st, T0, c=make_cfg(monitor={"disabled_tiers": ["summary"]}),
       states={"laynes-monitor-summary.timer": {"LoadState": "not-found"}})
check("deliberately disabled tier -> ACCEPTED (shown, never alerted)", h["summary"].state == ACCEPTED)
st.start_run("data", T0 - timedelta(minutes=50), "x")     # hung: never finished
h = hb(st, T0 + timedelta(minutes=50))
check("stalled tier (run never finished) -> HIGH stalled", h["data"].category == "stalled", h["data"])
rid = st.start_run("data", T0 + timedelta(minutes=55), "x")
st.finish_run(rid, T0 + timedelta(minutes=56), "SUCCESS")
check("recovery: a SUCCESSFUL data run -> OK", hb(st, T0 + timedelta(minutes=57))["data"].state == OK)
h = hb(st, T0 + timedelta(minutes=57), pg_connect=broken)
check("heartbeat works while PostgreSQL is down (SQLite + systemctl only)",
      h["data"].state == OK and "summary" in h)


def raising_systemctl(unit):
    raise FileNotFoundError("systemctl")


ctx = engine.Context(cfg, st, T0 + timedelta(minutes=57), "health", systemctl=raising_systemctl)
h = {r.qualifier: r for r in selfmon.heartbeat(ctx)}
check("systemctl unavailable -> timer state unknown in evidence, run history still judged",
      h["data"].state == OK and "error" in h["data"].evidence["timer"])
check("heartbeat runs in BOTH health and data tiers",
      [s.tiers for s in registry() if s.name == "monitor.heartbeat"][0] == ("health", "data"))

# ═══════════════════════════════════════════════════════════════════════════
print("FIX 3. SQLite permissions and dashboard access")
sub = os.path.join(TMP, "state")
db = os.path.join(sub, "monitor.db")
s = Store(db)
s.begin()
s.open_incident(CheckResult(source="revel", check="t", state=FAIL, severity=HIGH, title="x",
                            store_id=1), T0)
s.commit()
mode = lambda p: stat.S_IMODE(os.stat(p).st_mode)  # noqa: E731
check("store directory created 0750", mode(sub) == 0o750, oct(mode(sub)))
check("store file 0640 (never world-readable)", mode(db) == 0o640, oct(mode(db)))
side = [p for p in (db + "-wal", db + "-shm") if os.path.exists(p)]
check("-wal / -shm inherit 0640", side and all(mode(p) == 0o640 for p in side),
      {p: oct(mode(p)) for p in side})
check("WAL journal mode", s.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal")
iid = s.history()[0]["id"]

ro = Store.open_readonly(db)
check("read-only open reads incidents", len(ro.history()) == 1)
try:
    ro.db.execute("UPDATE incidents SET title = 'tampered'")
    ro_write = True
except sqlite3.OperationalError:
    ro_write = False
check("read-only open cannot write (SQLite mode=ro)", not ro_write)
ro.close()
try:
    Store.open_readonly(os.path.join(TMP, "absent.db"))
    missing = None
except FileNotFoundError:
    missing = "FileNotFoundError"
check("read-only open never creates a missing store", missing and not os.path.exists(
    os.path.join(TMP, "absent.db")))
old = os.path.join(TMP, "old.db")
sqlite3.connect(old).execute("CREATE TABLE incidents (id TEXT)").connection.close()
try:
    Store.open_readonly(old)
    schema_err = False
except StoreSchemaError:
    schema_err = True
check("dashboard refuses an unknown schema version (no guessing, no migration)", schema_err)
check("…and did not migrate it", sqlite3.connect(old).execute("PRAGMA user_version").fetchone()[0] == 0)

# The API routes against this store
cfg_path = os.path.join(TMP, "monitor.toml")
with open(cfg_path, "w") as fh:
    fh.write(f'[monitor]\nstate_db = "{db}"\n')
os.environ["LAYNES_MONITOR_CONFIG"] = cfg_path
from fastapi import HTTPException  # noqa: E402
from backend import monitoring_api as MA  # noqa: E402
s.close()
digest = lambda p: hashlib.sha256(open(p, "rb").read()).hexdigest()  # noqa: E731
ck = sqlite3.connect(db)
ck.execute("PRAGMA wal_checkpoint(TRUNCATE)")
ck.close()
before = digest(db)
ov = MA.overview()
inc = MA.incidents()
det = MA.incident(iid)
notes = MA.notifications()
check("admin dashboard reads overview / incidents / detail / notifications",
      ov["open_incidents"] and len(inc) == 1 and det["incident"]["id"] == iid and notes == [])
check("dashboard GETs leave the database file byte-identical", digest(db) == before)
ack = MA.acknowledge(iid, MA.AckIn(note="looking"), {"role": "super_admin", "email": "a@x"})
check("authorized admin acknowledges through the ack-only writer",
      ack == {"acknowledged": iid} and Store.open_readonly(db).get(iid)["status"] == "ACKNOWLEDGED")
try:
    MA.acknowledge(iid, MA.AckIn(note="again"), {"role": "super_admin", "email": "a@x"})
    code = 200
except HTTPException as e:
    code = e.status_code
check("second ack -> 409", code == 409)
try:
    MA.monitoring_admin({"role": "user", "email": "u@x"})
    code = 200
except HTTPException as e:
    code = e.status_code
check("non-admin -> 403", code == 403)
bad_db = os.path.join(TMP, "corrupt.db")
with open(bad_db, "wb") as fh:
    fh.write(b"this is not sqlite" * 100)
with open(cfg_path, "w") as fh:
    fh.write(f'[monitor]\nstate_db = "{bad_db}"\n')
try:
    MA.overview()
    code = 200
except HTTPException as e:
    code = e.status_code
check("corrupt store -> 503 (never a blank 'all clear')", code == 503)
with open(cfg_path, "w") as fh:
    fh.write(f'[monitor]\nstate_db = "{os.path.join(TMP, "nope.db")}"\n')
try:
    MA.overview()
    code = 200
except HTTPException as e:
    code = e.status_code
check("missing store -> 503", code == 503)
with open(cfg_path, "w") as fh:
    fh.write(f'[monitor]\nstate_db = "{db}"\n')

if os.geteuid() == 0:
    probe = ("import sqlite3,sys\n"
             "try:\n"
             "    c=sqlite3.connect('file:'+sys.argv[1]+'?mode=rw',uri=True)\n"
             "    c.execute(\"UPDATE incidents SET title='x'\"); c.commit(); print('WROTE')\n"
             "except Exception as e: print('DENIED', type(e).__name__)\n")
    cp = subprocess.run(["/usr/bin/python3", "-I", "-c", probe, db], capture_output=True, text=True,
                        user=65534, group=65534, timeout=30)
    check("an unprivileged process (nobody) cannot modify the store",
          cp.stdout.startswith("DENIED"), cp.stdout + cp.stderr)
else:
    check("unprivileged-process test needs root (skipped as PASS only when root)", False,
          "run as root")

# Concurrency: monitor writing while the dashboard reads and acknowledges
cdb = os.path.join(TMP, "concurrent.db")
Store(cdb).close()
errors = []
ccfg = make_cfg(monitor={"state_db": cdb})
n_runs = 40


def flip(ctx):
    k = ctx.now.minute % 2
    return [CheckResult(source="revel", check="t", state=FAIL if k else OK, severity=HIGH,
                        title="flip", store_id=i) for i in range(1, 6)]


def writer():
    w = Store(cdb)               # SQLite connections are per thread, like per process
    try:
        for i in range(n_runs):
            run(ccfg, w, T0 + timedelta(minutes=i), [spec(flip)])
    except Exception as e:  # noqa: BLE001
        errors.append(f"writer {type(e).__name__}: {e}")
    finally:
        w.close()


def reader():
    try:
        for _ in range(n_runs):
            r_ = Store.open_readonly(cdb)
            summary.overview(r_, T0, ccfg)
            r_.close()
            a_ = Store.open_for_ack(cdb)
            for row in a_.active_incidents():
                a_.begin()
                a_.acknowledge(row["id"], T0, "admin", None)
                a_.commit()
            a_.close()
    except Exception as e:  # noqa: BLE001
        errors.append(f"reader {type(e).__name__}: {e}")


ts_ = [threading.Thread(target=writer), threading.Thread(target=reader),
       threading.Thread(target=reader)]
[t.start() for t in ts_]
[t.join() for t in ts_]
w = Store(cdb)
check("the writer completed every run", w.db.execute(
    "SELECT COUNT(*) FROM monitor_runs WHERE status = 'SUCCESS'").fetchone()[0] == n_runs)
dups = w.db.execute("SELECT dedup_key, COUNT(*) FROM incidents WHERE status <> 'RESOLVED' "
                    "GROUP BY dedup_key HAVING COUNT(*) > 1").fetchall()
check("concurrent monitor writes + dashboard reads/acks: no errors, no duplicate active incidents",
      not errors and not dups, errors[:3])
check("integrity_check ok after concurrent use", w.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok")
w.close()

# ═══════════════════════════════════════════════════════════════════════════
print("FIX 4. notification retries: backoff, leases, durability")
ndb = os.path.join(TMP, "outbox.db")
cfg = make_cfg(monitor={"state_db": ndb})
s = Store(ndb)
s.begin()
nid = s.enqueue(T0, "OPENED", "HIGH", [], "fake", "Issue: x")
s.commit()
check("new message is QUEUED", s.db.execute("SELECT status FROM notifications").fetchone()[0] == "QUEUED")
flaky = FakeChannel(cfg, fail_times=2)
st1 = notify.deliver_pending(s, cfg, T0, {"fake": flaky})
row = dict(s.db.execute("SELECT * FROM notifications WHERE id=?", (nid,)).fetchone())
check("failure -> RETRYING, next attempt after 60s backoff",
      row["status"] == "RETRYING" and row["attempts"] == 1
      and row["next_attempt_at"] == (T0 + timedelta(seconds=60)).isoformat(timespec="seconds")
      and st1["retrying"] == 1, row)
notify.deliver_pending(s, cfg, T0 + timedelta(seconds=30), {"fake": flaky})
check("not retried before next_attempt_at", flaky.calls == 1)
s.close()
s = Store(ndb)                 # process restart: retry state is durable
notify.deliver_pending(s, cfg, T0 + timedelta(seconds=61), {"fake": flaky})
row = dict(s.db.execute("SELECT * FROM notifications WHERE id=?", (nid,)).fetchone())
check("after a restart the retry continues; 2nd backoff = 120s",
      row["attempts"] == 2 and row["next_attempt_at"]
      == (T0 + timedelta(seconds=181)).isoformat(timespec="seconds"), row)
notify.deliver_pending(s, cfg, T0 + timedelta(seconds=200), {"fake": flaky})
row = dict(s.db.execute("SELECT * FROM notifications WHERE id=?", (nid,)).fetchone())
check("successful retry -> SENT, attempts=3", row["status"] == "SENT" and row["attempts"] == 3
      and flaky.sent, row)
check("backoff is exponential and capped at retry_max_seconds",
      [notify.backoff_seconds(cfg, n) for n in (1, 2, 3, 4, 5, 6, 7, 8)]
      == [60, 120, 240, 480, 960, 1920, 3600, 3600])

s.begin()
nid2 = s.enqueue(T0, "OPENED", "HIGH", [], "fake", "Issue: y")
s.commit()
dead = FakeChannel(cfg, fail_times=99)
t = T0
for _ in range(5):
    notify.deliver_pending(s, cfg, t, {"fake": dead})
    t += timedelta(hours=2)
row = dict(s.db.execute("SELECT * FROM notifications WHERE id=?", (nid2,)).fetchone())
check("exhausted retries -> FAILED (permanent), never SENT",
      row["status"] == "FAILED" and row["attempts"] == 5 and row["sent_at"] is None, row)
notify.deliver_pending(s, cfg, t + timedelta(days=1), {"fake": dead})
check("a FAILED row is never attempted again", dead.calls == 5)
ctx = engine.Context(cfg, s, t, "health")
nd = {r.qualifier: r for r in selfmon.notifications(ctx)}["fake"]
check("permanent failure -> HIGH delivery_failed incident", nd.state == FAIL and nd.severity == HIGH
      and nd.category == "delivery_failed")
ov = summary.overview(s, t, cfg)
check("dashboard shows the failure (monitor.notifications FAILED, failed_24h)",
      ov["monitor"]["notifications"]["state"] == "FAILED" and ov["monitor"]["notifications"]["FAILED"] == 1,
      ov["monitor"]["notifications"])
s.begin()
s.enqueue(t, "OPENED", "HIGH", [], "fake", "Issue: z")
s.commit()
notify.deliver_pending(s, cfg, t, {"fake": FakeChannel(cfg)})
nd = {r.qualifier: r for r in selfmon.notifications(engine.Context(cfg, s, t, "health"))}["fake"]
check("delivery recovers -> incident clears only after a real SENT", nd.state == OK)

# leases: two processes can never send the same row
s.begin()
s.enqueue(t, "OPENED", "HIGH", [], "fake", "Issue: lease")
s.commit()
s2 = Store(ndb)
a_rows = s.claim_due(t, "A", 300)
b_rows = s2.claim_due(t, "B", 300)
check("a leased row cannot be claimed by a second process", len(a_rows) == 1 and b_rows == [])
check("a process without the lease cannot record an outcome",
      not s2.complete_delivery(a_rows[0]["id"], "B", "SENT", t))
b_rows = s2.claim_due(t + timedelta(seconds=301), "B", 300)
check("an expired lease (crashed sender) makes the row due again", len(b_rows) == 1)
check("the original holder's late result is rejected (lease lost)",
      not s.complete_delivery(a_rows[0]["id"], "A", "SENT", t + timedelta(seconds=302)))
s2.complete_delivery(b_rows[0]["id"], "B", "SENT", t + timedelta(seconds=302))
s2.close()

s.begin()
for i in range(20):
    s.enqueue(t, "OPENED", "HIGH", [], "fake", f"Issue: concurrent {i}")
s.commit()
slow = FakeChannel(cfg, delay=0.01)
derr = []


def deliverer():
    x = Store(ndb)
    try:
        notify.deliver_pending(x, cfg, t + timedelta(hours=1), {"fake": slow})
    except Exception as e:  # noqa: BLE001
        derr.append(e)
    finally:
        x.close()


ths = [threading.Thread(target=deliverer) for _ in range(4)]
[th.start() for th in ths]
[th.join() for th in ths]
bodies = [b for b in slow.sent if "concurrent" in b]
check("4 concurrent deliverers: each of 20 messages sent exactly once",
      not derr and len(bodies) == 20 and len(set(bodies)) == 20, (len(bodies), derr[:2]))

# Real adapters, transport replaced (nothing leaves the host)
tcfg = make_cfg(notify={"channels": ["telegram"]})
os.environ["MONITOR_TELEGRAM_BOT_TOKEN"] = "123:SECRETTOKEN"
os.environ["MONITOR_TELEGRAM_CHAT_ID"] = "42"
orig_urlopen = notify.urllib.request.urlopen


def tg_http_error(req, timeout):
    raise urllib.error.HTTPError(req.full_url, 502, "Bad Gateway", {}, None)


def tg_timeout(req, timeout):
    raise socket.timeout("timed out")


class TgResp:
    status = 200

    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def tg_rejected(req, timeout):
    return TgResp(b'{"ok": false, "description": "chat not found"}')


def tg_errmsg(fn):
    notify.urllib.request.urlopen = fn
    try:
        notify.TelegramChannel(tcfg).send("x", "y")
        return None
    except notify.DeliveryError as e:
        return str(e)
    finally:
        notify.urllib.request.urlopen = orig_urlopen


e1 = tg_errmsg(tg_http_error)
check("Telegram HTTP 502 -> DeliveryError with the bot token scrubbed",
      e1 and "SECRETTOKEN" not in e1 and "502" in e1, e1)
e2 = tg_errmsg(tg_timeout)
check("Telegram timeout -> DeliveryError (retried, not lost)", e2 and "timed out" in e2, e2)
e3 = tg_errmsg(tg_rejected)
check("Telegram ok:false -> DeliveryError (never recorded as sent)", e3 and "rejected" in e3, e3)

ecfg = make_cfg(notify={"channels": ["email"], "email": {"smtp_host": "smtp.invalid",
                                                         "from_addr": "m@x", "to_addrs": ["a@x", "b@x"]}})
os.environ["MONITOR_SMTP_USER"] = "user"
os.environ["MONITOR_SMTP_PASS"] = "SMTPSECRET"
orig_smtp = notify.smtplib.SMTP


class FakeSMTP:
    mode = "auth"

    def __init__(self, host, port, timeout):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        pass

    def login(self, u, p):
        if FakeSMTP.mode == "auth":
            raise notify.smtplib.SMTPAuthenticationError(535, f"bad password {p}".encode())

    def send_message(self, msg):
        return {"b@x": (550, b"no such user")} if FakeSMTP.mode == "refused" else {}


def mail_err(mode):
    FakeSMTP.mode = mode
    notify.smtplib.SMTP = FakeSMTP
    try:
        notify.EmailChannel(ecfg).send("x", "y")
        return None
    except notify.DeliveryError as e:
        return str(e)
    finally:
        notify.smtplib.SMTP = orig_smtp


m1 = mail_err("auth")
check("SMTP auth failure -> DeliveryError with the password scrubbed",
      m1 and "SMTPSECRET" not in m1 and "SMTPAuthenticationError" in m1, m1)
m2 = mail_err("refused")
check("SMTP refused recipient -> DeliveryError (partial delivery is not success)",
      m2 and "refused 1 of 2" in m2, m2)
check("SMTP accepted -> no error", mail_err("ok") is None)
mdb = Store(":memory:")
mdb.begin()
mdb.enqueue(T0, "OPENED", "HIGH", [], "email", "Issue: mail")
mdb.commit()
FakeSMTP.mode = "auth"
notify.smtplib.SMTP = FakeSMTP
try:
    stx = notify.deliver_pending(mdb, ecfg, T0)
finally:
    notify.smtplib.SMTP = orig_smtp
row = dict(mdb.db.execute("SELECT * FROM notifications").fetchone())
check("SMTP failure through deliver_pending -> RETRYING with scrubbed error",
      row["status"] == "RETRYING" and "SMTPSECRET" not in (row["last_error"] or "") and stx["retrying"] == 1,
      row)
for k in ("MONITOR_TELEGRAM_BOT_TOKEN", "MONITOR_TELEGRAM_CHAT_ID", "MONITOR_SMTP_USER",
          "MONITOR_SMTP_PASS"):
    os.environ.pop(k, None)
ucfg = make_cfg()
um = Store(":memory:")
um.begin()
um.enqueue(T0, "OPENED", "HIGH", [], "pager", "Issue: u")
um.commit()
notify.deliver_pending(um, ucfg, T0, {"fake": FakeChannel(ucfg)})
check("unknown channel -> FAILED at once (config error is not transient)",
      um.db.execute("SELECT status FROM notifications").fetchone()[0] == "FAILED")
dcfg = make_cfg(notify={"dry_run": True})
dm = Store(":memory:")
dm.begin()
dm.enqueue(T0, "OPENED", "HIGH", [], "fake", "Issue: d")
dm.commit()
spy = FakeChannel(dcfg)
notify.deliver_pending(dm, dcfg, T0, {"fake": spy})
check("dry-run: DRY_RUN status, channel never called", spy.calls == 0
      and dm.db.execute("SELECT status FROM notifications").fetchone()[0] == "DRY_RUN")

# migration of a Phase-1 (v1) store
v1 = os.path.join(TMP, "v1.db")
c = sqlite3.connect(v1)
c.executescript("""
CREATE TABLE monitor_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, tier TEXT NOT NULL,
  started_at TEXT NOT NULL, finished_at TEXT, ok INTEGER, checks_run INTEGER,
  checks_failed INTEGER, results INTEGER, error TEXT, version TEXT);
INSERT INTO monitor_runs (tier, started_at, finished_at, ok, error) VALUES
  ('data','2026-11-25T00:00:00+00:00','2026-11-25T00:01:00+00:00',1,NULL),
  ('data','2026-11-25T00:15:00+00:00','2026-11-25T00:16:00+00:00',1,'crashed=[''x'']');
CREATE TABLE notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
  kind TEXT NOT NULL, severity TEXT, incident_ids TEXT NOT NULL DEFAULT '[]', channel TEXT NOT NULL,
  body TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('PENDING','SENT','DRY_RUN','FAILED')),
  attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, sent_at TEXT);
INSERT INTO notifications (created_at, kind, channel, body, status, attempts) VALUES
  ('2026-11-25T00:00:00+00:00','OPENED','log','b','PENDING',0),
  ('2026-11-25T00:00:00+00:00','OPENED','log','b','PENDING',2),
  ('2026-11-25T00:00:00+00:00','OPENED','log','b','DRY_RUN',1);
""")
c.close()
m = Store(v1)
runs = [r["status"] for r in m.db.execute("SELECT status FROM monitor_runs ORDER BY id")]
ns = [r["status"] for r in m.db.execute("SELECT status FROM notifications ORDER BY id")]
check("v1 store migrates: a v1 'ok' run with an error becomes DEGRADED, not SUCCESS",
      runs == ["SUCCESS", "DEGRADED"], runs)
check("v1 outbox migrates: PENDING -> QUEUED / RETRYING", ns == ["QUEUED", "RETRYING", "DRY_RUN"], ns)
m.close()

# ═══════════════════════════════════════════════════════════════════════════
print("FIX 5. Revel reconciliation runs on a monitor-managed, bounded connection")


class Conn:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeChatSql:
    RECON_TOLERANCE_PCT = 0.5

    def __init__(self, raise_=False):
        self.used, self.raise_ = [], raise_

    @staticmethod
    def _ro_conn():
        raise AssertionError("chat_sql's own analytics-role connection was used")

    def revel_store_breakdown(self, s, e):
        conn = self._ro_conn()         # what the real function does first
        self.used.append(conn)
        if self.raise_:
            raise RuntimeError("query timeout")
        return {"stores": []}          # chat_sql closes in finally; here we leave it open


opened = []


def mk_conn():
    c_ = Conn()
    opened.append(c_)
    return c_


fake = FakeChatSql()
orig_ro = fake._ro_conn
ctx = engine.Context(make_cfg(), Store(":memory:"), T0, "data")
ctx.monitor_pg_connect = mk_conn
with revel.monitor_connection(ctx, fake):
    fake.revel_store_breakdown("2026-11-22", "2026-11-23")
check("revel_store_breakdown got the MONITOR's connection", fake.used and fake.used[0] is opened[0])
check("chat_sql's connection factory restored afterwards", fake._ro_conn is orig_ro)
check("a connection the callee left open is closed by the wrapper", all(c_.closed for c_ in opened))
fake2 = FakeChatSql(raise_=True)
orig2 = fake2._ro_conn
try:
    with revel.monitor_connection(ctx, fake2):
        fake2.revel_store_breakdown("2026-11-22", "2026-11-23")
except RuntimeError:
    pass
check("restored and closed even when the query fails",
      fake2._ro_conn is orig2 and all(c_.closed for c_ in opened))
pg_src = open(os.path.join(ROOT, "monitoring", "pg.py")).read()
check("monitor sessions: read-only verified, statement/lock/idle/connect timeouts",
      all(x in pg_src for x in ("default_transaction_read_only=on", "statement_timeout",
                                "lock_timeout", "idle_in_transaction_session_timeout",
                                "connect_timeout", "SHOW transaction_read_only")))
backend_src = "".join(open(os.path.join(ROOT, "backend", f)).read()
                      for f in os.listdir(os.path.join(ROOT, "backend")) if f.endswith(".py"))
check("laynes-api never RUNS checks (no run_tier / engine / check functions / monitor_connection)",
      not any(x in backend_src for x in ("run_tier", "monitoring.engine", "monitoring import engine",
                                         "monitor_connection", ".reconciliation(", "spec.fn")))
import ast  # noqa: E402
callers = []
for dp, _, fs in os.walk(os.path.join(ROOT, "monitoring")):
    for f in fs:
        if f.endswith(".py"):
            tree = ast.parse(open(os.path.join(dp, f)).read())
            for fn in ast.walk(tree):
                if isinstance(fn, ast.FunctionDef):
                    for node in ast.walk(fn):
                        if isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(
                                node.func, "id", None)) == "monitor_connection":
                            callers.append(f"{f}:{fn.name}")
check("monitor_connection is used only by revel.reconciliation", callers == ["revel.py:reconciliation"],
      callers)
chat_src = open(os.path.join(ROOT, "chat_sql.py")).read()
check("chat_sql unchanged: tolerance ±0.5%, _ro_conn still the analytics role",
      "RECON_TOLERANCE_PCT = 0.5 " in chat_src and 'user=os.environ["DB_RO_USER"]' in chat_src)
env_ex = open(os.path.join(ROOT, "deploy", "monitoring", "monitor.env.example")).read()
check("monitor.env no longer carries the analytics-role credentials",
      "DB_RO_USER" not in env_ex and "DB_RO_PASS" not in env_ex)

# ═══════════════════════════════════════════════════════════════════════════
print("FIX 6. no false HEALTHY")
cfg = make_cfg()
st = Store(":memory:")
ov = summary.overview(st, T0, cfg)
check("empty store: every source listed, none HEALTHY (UNKNOWN)",
      {s_["source"] for s_ in ov["sources"]} >= {"revel", "hme", "r365", "r365_financial",
                                                  "ai_chat", "mcp", "infrastructure", "monitor"}
      and all(s_["state"] == "UNKNOWN" for s_ in ov["sources"]), [(s_["source"], s_["state"]) for s_ in ov["sources"]])
check("monitor itself UNKNOWN before any run", ov["monitor"]["state"] == "UNKNOWN")
names = [s_.name for s_ in registry() if s_.source == "revel"]


def revel_ok(ctx):
    return [CheckResult(source="revel", check="sales_reconciliation", state=OK, title="ok", store_id=6,
                        business_date="2026-11-22")]


specs = [spec(revel_ok, name=n) for n in names]
run(cfg, st, T0, specs)
rv = {s_["source"]: s_ for s_ in summary.overview(st, T0, cfg)["sources"]}["revel"]
check("source with fresh successful checks -> HEALTHY with last success/attempt times",
      rv["state"] == "HEALTHY" and rv["last_successful_check_at"] and rv["last_attempted_check_at"], rv)
rv = {s_["source"]: s_ for s_ in summary.overview(st, T0 + timedelta(hours=4), cfg)["sources"]}["revel"]
check("same results 4h later with no new check runs -> STALE (not HEALTHY)", rv["state"] == "STALE", rv)


def revel_fail(ctx):
    return [CheckResult(source="revel", check="sales_reconciliation", state=FAIL, severity=HIGH,
                        title="bad", store_id=6, business_date="2026-11-22")]


run(cfg, st, T0 + timedelta(hours=1), [spec(revel_fail, name=n) for n in names])
run(cfg, st, T0 + timedelta(hours=1, minutes=15), [spec(boom, name=n) for n in names])
ov = summary.overview(st, T0 + timedelta(hours=1, minutes=15), cfg)
rv = {s_["source"]: s_ for s_ in ov["sources"]}["revel"]
check("crashed latest attempt: monitoring DEGRADED, data FAILED kept, incident preserved",
      rv["monitoring_state"] == "DEGRADED" and rv["data_state"] == "FAILED" and rv["state"] == "FAILED"
      and any(i["check_name"] == "sales_reconciliation" for i in ov["open_incidents"]), rv)
cs = {c_["check"]: c_ for c_ in ov["checks"]}
check("per-check last success and last attempt both reported",
      cs[names[0]]["last_success_at"] < cs[names[0]]["last_attempt_at"]
      and cs[names[0]]["state"] == "DEGRADED", cs[names[0]])
st = Store(":memory:")


def revel_pending(ctx):
    return [CheckResult(source="revel", check="sales_reconciliation", state=PENDING, title="wait",
                        store_id=6, business_date="2026-11-24")]


run(cfg, st, T0, [spec(revel_pending, name=n) for n in names])
rv = {s_["source"]: s_ for s_ in summary.overview(st, T0, cfg)["sources"]}["revel"]
check("only PENDING results -> PENDING", rv["state"] == "PENDING", rv)
st = Store(":memory:")
run(cfg, st, T0, [spec(needs_db, name=n, needs_pg=True) for n in names], pg_connect=broken)
rv = {s_["source"]: s_ for s_ in summary.overview(st, T0, cfg)["sources"]}["revel"]
check("PostgreSQL unavailable from the start -> UNKNOWN, never HEALTHY", rv["state"] == "UNKNOWN", rv)
check("API exposes the same machine-readable state set",
      summary.overview(st, T0, cfg)["states"] == ["HEALTHY", "PENDING", "DEGRADED", "UNKNOWN",
                                                  "STALE", "FAILED"])

# ═══════════════════════════════════════════════════════════════════════════
print("FIX 7. backup monitoring is labelled for what it proves")
now = T0


def bctx(**kw):
    c_ = make_cfg(infra=kw.pop("infra", {}))
    return engine.Context(c_, Store(":memory:"), now, "data", **kw)


ctx = bctx(newest_backup=lambda g: ("/b/x.dump", now - timedelta(hours=2)),
           backup_format=lambda p: "pg_dump custom archive")
res = infra.backups(ctx)
check("fresh dump -> OK, titled freshness and explicitly NOT restore-verified",
      len(res) == 1 and res[0].state == OK and "not restore-verified" in res[0].title
      and res[0].evidence["local"]["restore_verified"] is False, res[0])
det = ctx.source_updates["backup"][1]
check("offsite and restore test reported as 'not monitored' when no evidence is configured",
      det["offsite"].startswith("not monitored") and det["restore_test"].startswith("not monitored"))
ctx = bctx(newest_backup=lambda g: ("/b/x.dump", now - timedelta(hours=2)),
           backup_format=lambda p: "unrecognised")
check("a recent file that is not a pg_dump -> FAIL backup_unrecognised",
      infra.backups(ctx)[0].category == "backup_unrecognised")
ctx = bctx(newest_backup=lambda g: ("/b/x.dump", now - timedelta(days=8)),
           backup_format=lambda p: "pg_dump custom archive")
check("stale dump -> FAIL stale_local_dump", infra.backups(ctx)[0].category == "stale_local_dump")
check("no dump -> FAIL no_local_dump",
      infra.backups(bctx(newest_backup=lambda g: (None, None)))[0].category == "no_local_dump")
rt = {"ok": True, "completed_at": (now - timedelta(days=1)).isoformat(), "dump": "/b/x.dump"}
ctx = bctx(infra={"restore_test_status_glob": "/r/*.json"},
           newest_backup=lambda g: ("/b/x.dump", now - timedelta(hours=2)),
           backup_format=lambda p: "pg_dump custom archive",
           read_status_json=lambda g: ("/r/1.json", rt))
res = {r.check: r for r in infra.backups(ctx)}
check("restore test recorded ok -> OK, and the newest VERIFIED backup is reported separately",
      res["backup_restore_test"].state == OK
      and ctx.source_updates["backup"][1]["newest_verified_backup"]["dump"] == "/b/x.dump")
ctx = bctx(infra={"restore_test_status_glob": "/r/*.json"},
           newest_backup=lambda g: ("/b/x.dump", now - timedelta(hours=2)),
           backup_format=lambda p: "pg_dump custom archive",
           read_status_json=lambda g: ("/r/1.json", {**rt, "ok": False, "detail": "pg_restore exit 1"}))
res = {r.check: r for r in infra.backups(ctx)}
check("restore test failed -> HIGH", res["backup_restore_test"].severity == HIGH
      and "newest_verified_backup" not in ctx.source_updates["backup"][1])
ctx = bctx(infra={"offsite_status_glob": "/o/*.json"},
           newest_backup=lambda g: ("/b/x.dump", now - timedelta(hours=2)),
           backup_format=lambda p: "pg_dump custom archive",
           read_status_json=lambda g: ("/o/1.json", {"ok": True, "completed_at": (now - timedelta(days=3)).isoformat()}))
res = {r.check: r for r in infra.backups(ctx)}
check("offsite copy older than its limit -> WARNING stale", res["backup_offsite"].category == "backup_offsite_stale")
ctx = bctx(infra={"offsite_status_glob": "/o/*.json"},
           newest_backup=lambda g: ("/b/x.dump", now - timedelta(hours=2)),
           backup_format=lambda p: "pg_dump custom archive",
           read_status_json=lambda g: (None, None))
res = {r.check: r for r in infra.backups(ctx)}
check("offsite configured but no status file -> WARNING missing", res["backup_offsite"].category == "backup_offsite_missing")
infra_src = open(os.path.join(ROOT, "monitoring", "checks", "infra.py")).read()
check("backup check never writes, moves or deletes files",
      not any(x in infra_src for x in ("os.remove", "unlink", "shutil.move", "shutil.copy",
                                       "open(path, \"w", "'w')", "\"w\")", "subprocess")))
check("real dump_format reads the custom-archive header", infra.dump_format(__file__) == "unrecognised")

# ═══════════════════════════════════════════════════════════════════════════
print("FIX 9. deploy artefacts")
role_sql = open(os.path.join(ROOT, "deploy", "monitoring", "proposed_monitor_role.sql")).read()
for rel in ("orders_v2", "v_payments_daily_v2", "v_labor_daily_context", "v_orders_classified",
            "v_order_payment_summary", "establishments"):
    check(f"role grants SELECT on {rel} (read by chat_sql.revel_store_breakdown)", rel in role_sql)
check("role grants no write privilege",
      not any(x in role_sql.upper() for x in ("GRANT INSERT", "GRANT UPDATE", "GRANT DELETE",
                                              "GRANT ALL", "GRANT TRUNCATE")))
units = {f: open(os.path.join(ROOT, "deploy", "monitoring", f)).read()
         for f in os.listdir(os.path.join(ROOT, "deploy", "monitoring")) if f.endswith(".service")}
check("every monitor unit sets UMask=0027 and StateDirectoryMode=0750 or ReadWritePaths only",
      all("UMask=0027" in u for u in units.values()), [f for f, u in units.items() if "UMask=0027" not in u])
check("every monitor unit is sandboxed (ProtectSystem=strict, NoNewPrivileges, PrivateTmp)",
      all(all(x in u for x in ("ProtectSystem=strict", "NoNewPrivileges=yes", "PrivateTmp=yes"))
          for u in units.values()))

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed" + (f"; FAILED: {failed}" if failed else ""))
sys.exit(1 if failed else 0)
