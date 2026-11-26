#!/usr/bin/env python3
"""Monitor: backup freshness from the SCHEDULED job's status files.

The legacy check took the newest *.dump >= 1 MiB anywhere under the globs as proof of
a backup -- on 2026-11-26 that was a 3.2 MB single-table pre-deploy dump. With
infra.backup_status_glob set, only a successful scheduled backup recorded by
laynes-db-backup counts; manual dumps are listed and never satisfy the check.

Run:  venv/bin/python tests/test_monitoring_backup_status.py   (or python3; stdlib + repo)
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
from monitoring import config as mcfg, engine  # noqa: E402
from monitoring.checks import infra  # noqa: E402
from monitoring.model import FAIL, HIGH, OK  # noqa: E402
from monitoring.store import Store  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {str(detail)[:400]}" if not ok and detail else ""))


NOW = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)
DUMP = "/var/backups/laynes/laynes-20261127T083000Z.dump"
SIZE = 893_000_000


def cfg(**infra_over):
    base = {"notify": {"channels": ["log"], "dry_run": True}, "monitor": {"state_db": ":memory:"},
            "infra": {"backup_status_glob": "/var/backups/laynes/status/backup-*.json", **infra_over}}
    out = mcfg.DEFAULTS
    return mcfg._merge(out, base)


def ok_status(hours_ago, dump=DUMP, size=SIZE):
    t = NOW - timedelta(hours=hours_ago)
    return {"ok": True, "kind": "scheduled", "started_at": (t - timedelta(minutes=4)).isoformat(),
            "completed_at": t.isoformat(), "dump": dump, "bytes": size, "sha256": "ab" * 32,
            "verify": "archive (toc + full decode + table coverage)"}


def fail_status(hours_ago, stage="preflight", error="insufficient_space: {...}"):
    t = NOW - timedelta(hours=hours_ago)
    return {"ok": False, "kind": "scheduled", "started_at": t.isoformat(),
            "completed_at": (t + timedelta(seconds=3)).isoformat(), "stage": stage, "error": error,
            "dump": None}


def ctx(statuses, *, sizes=None, manual=(None, None), timer=None, **infra_over):
    sizes = {DUMP: SIZE} if sizes is None else sizes
    timer = timer or {"UnitFileState": "enabled", "NextElapseUSecRealtime": "Sat 2026-11-28 08:32:11 UTC"}
    return engine.Context(
        cfg(**infra_over), Store(":memory:"), NOW, "data",
        read_status_jsons=lambda g: [(f"/s/{i}.json", s) for i, s in enumerate(statuses)],
        file_size=lambda p: sizes.get(p), newest_backup=lambda g: manual,
        systemctl=lambda u: timer, backup_format=lambda p: "pg_dump custom archive",
        read_status_json=lambda g: (None, None))


def run(c):
    res = infra.backups(c)
    return res[0], c.source_updates["backup"][1]


print("== scheduled mode")
r, d = run(ctx([ok_status(3)]))
check("fresh scheduled backup -> OK 'archive verified', not 'restore-verified'",
      r.state == OK and "archive verified" in r.title and r.check == "backup_freshness", r)
check("evidence: last success (time, age, size, sha prefix, verification)",
      d["last_success"]["age_hours"] == 3.0 and d["last_success"]["size_bytes"] == SIZE
      and len(d["last_success"]["sha256"]) == 16 and "full decode" in d["last_success"]["verified"], d)
check("evidence: next scheduled run and timer state",
      d["timer"]["next_scheduled"].startswith("Sat 2026-11-28") and d["timer"]["state"] == "enabled", d)
check("evidence: no failure recorded", d["last_failure"] is None)

manual = ("/root/deploy-backups/20261127T100000Z-x/r365-financial-before.dump", NOW - timedelta(hours=1))
r, d = run(ctx([], manual=manual))
check("a fresh MANUAL/partial dump does NOT satisfy the check (the 2026-11-26 false-green path)",
      r.state == FAIL and r.severity == HIGH and r.category == "no_backup", r)
check("...and is listed for information, labelled as not counting",
      d["manual_backups"]["newest"] == manual[0] and "does NOT satisfy" in d["manual_backups"]["note"])

r, _ = run(ctx([ok_status(27)]))
check("last success 27 h ago -> HIGH stale_backup (26 h threshold unchanged)",
      r.state == FAIL and r.severity == HIGH and r.category == "stale_backup", r)
r, _ = run(ctx([ok_status(25.9)]))
check("25.9 h -> still OK", r.state == OK)

r, d = run(ctx([fail_status(1), ok_status(25)]))
check("failed attempt NEWER than the last success -> HIGH backup_failed at once (not after 26 h)",
      r.state == FAIL and r.severity == HIGH and r.category == "backup_failed" and "preflight" in r.actual, r)
check("evidence: last failure stage + error", d["last_failure"]["stage"] == "preflight")
r, d = run(ctx([ok_status(2), fail_status(20)]))
check("failure OLDER than a later success -> OK (but the failure stays in the evidence)",
      r.state == OK and d["last_failure"]["stage"] == "preflight", r)
r, _ = run(ctx([None, ok_status(2)]))
check("an unreadable status file newer than the success -> HIGH (never silently skipped)",
      r.state == FAIL and r.category == "backup_failed", r)

r, _ = run(ctx([ok_status(2)], sizes={}))
check("recorded dump deleted -> HIGH backup_missing", r.state == FAIL and r.category == "backup_missing", r)
r, _ = run(ctx([ok_status(2)], sizes={DUMP: SIZE - 1}))
check("recorded dump changed size -> HIGH backup_missing", r.category == "backup_missing", r)

r, d = run(ctx([ok_status(2)], timer={"UnitFileState": "disabled", "NextElapseUSecRealtime": ""}))
check("disabled timer is visible in evidence (freshness still decides the state)",
      d["timer"]["state"] == "disabled" and d["timer"]["next_scheduled"] is None)

print("== restore-test evidence carries its mode")
rt = {"ok": True, "mode": "critical", "completed_at": (NOW - timedelta(days=2)).isoformat(), "dump": DUMP}
c = ctx([ok_status(2)], restore_test_status_glob="/var/backups/laynes/status/restore-*.json")
c.read_status_json = lambda g: ("/s/restore.json", rt)
res = {x.check: x for x in infra.backups(c)}
det = c.source_updates["backup"][1]
check("restore test OK titled with its mode", res["backup_restore_test"].state == OK
      and "(critical)" in res["backup_restore_test"].title, res["backup_restore_test"])
check("newest verified backup says WHICH kind of restore proved it",
      det["newest_verified_backup"]["mode"] == "critical")

print("== legacy mode unchanged when backup_status_glob is empty")
c = engine.Context(mcfg._merge(mcfg.DEFAULTS, {"monitor": {"state_db": ":memory:"}}), Store(":memory:"),
                   NOW, "data", newest_backup=lambda g: manual,
                   backup_format=lambda p: "pg_dump custom archive")
r = infra.backups(c)[0]
check("default config: legacy glob/mtime behaviour (no surprise change on deploy)",
      r.state == OK and "not restore-verified" in r.title, r)
check("defaults: 26 h, HIGH, scheduled glob off by default",
      mcfg.DEFAULTS["infra"]["backup_max_age_hours"] == 26 and mcfg.DEFAULTS["infra"]["backup_severity"] == "HIGH"
      and mcfg.DEFAULTS["infra"]["backup_status_glob"] == "")

print("== real status reader")
with tempfile.TemporaryDirectory() as t:
    for i, (name, body) in enumerate((("backup-20261126T083000Z-11.json", ok_status(27)),
                                      ("backup-20261127T083000Z-12.json", ok_status(3)),
                                      ("backup-20261127T090000Z-13.json", "{not json"))):
        with open(os.path.join(t, name), "w") as fh:
            fh.write(body if isinstance(body, str) else json.dumps(body))
        os.utime(os.path.join(t, name), (1_800_000_000 + i * 60,) * 2)
    got = infra.read_status_jsons(os.path.join(t, "backup-*.json"))
    check("reader: newest first (file time); unreadable kept as None",
          [os.path.basename(p) for p, _ in got][1:] == ["backup-20261127T083000Z-12.json",
                                                         "backup-20261126T083000Z-11.json"]
          and got[0][1] is None, [os.path.basename(p) for p, _ in got])
check("check code is read-only (no unlink/remove/rename/write)",
      not any(x in open(os.path.join(ROOT, "monitoring", "checks", "infra.py")).read()
              for x in ("os.remove", "os.unlink", "os.rename", "shutil.rmtree", "open(p, \"w\")")))

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
