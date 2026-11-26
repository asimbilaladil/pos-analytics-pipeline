"""Host health: PostgreSQL reachability, disk, memory, backup freshness (and, where a
status file gives evidence, offsite copy and restore-test status)."""
from __future__ import annotations

import glob
import json
import os
import shutil
from datetime import datetime, timezone

from ..model import CRITICAL, FAIL, HIGH, OK, SRC_INFRA, WARNING, CheckResult
from ..pg import rows


def postgres(ctx) -> list[CheckResult]:
    base = dict(source=SRC_INFRA, check="postgres", expected="reachable, read-only session")
    try:
        r = rows(ctx.pg(), "SELECT now() AS now, current_setting('transaction_read_only') AS ro, "
                           "(SELECT count(*) FROM pg_stat_activity) AS connections")[0]
    except Exception as e:  # noqa: BLE001
        return [CheckResult(state=FAIL, severity=CRITICAL, category="db_unreachable",
                            title="PostgreSQL unreachable from the monitor",
                            actual=f"{type(e).__name__}: {str(e)[:160]}",
                            actions=["`systemctl status postgresql`; check disk space and "
                                     "`journalctl -u postgresql`.",
                                     "AI Chat, MCP and every ingestion job depend on it."], **base)]
    return [CheckResult(state=OK, title="PostgreSQL reachable",
                        actual=f"{r['connections']} connections", **base)]


def resources(ctx) -> list[CheckResult]:
    c = ctx.cfg["infra"]
    out = []
    for path in c["disk_paths"]:
        u = ctx.disk_usage(path)
        pct = round(u.used / u.total * 100, 1)
        base = dict(source=SRC_INFRA, check="disk", qualifier=path,
                    expected=f"< {c['disk_warn_pct']}% used")
        if pct >= c["disk_critical_pct"]:
            sev = CRITICAL
        elif pct >= c["disk_warn_pct"]:
            sev = WARNING
        else:
            out.append(CheckResult(state=OK, title=f"Disk {path} OK", actual=f"{pct}%", **base))
            continue
        out.append(CheckResult(state=FAIL, severity=sev, category="disk",
                               title=f"Disk {path} filling up", actual=f"{pct}% used",
                               evidence={"free_gb": round(u.free / 1e9, 1)},
                               actions=["`du -xh --max-depth=2 / | sort -h | tail`; raw HME "
                                        "extracts and logs under /var/lib/laynes grow daily.",
                                        "PostgreSQL stops accepting writes when the disk is full."],
                               **base))
    avail = ctx.mem_available_mb()
    base = dict(source=SRC_INFRA, check="memory", expected=f">= {c['mem_available_min_mb']} MB available")
    if avail is not None and avail < c["mem_available_min_mb"]:
        out.append(CheckResult(state=FAIL, severity=WARNING, category="memory",
                               title="Low available memory", actual=f"{avail} MB",
                               actions=["`ps aux --sort=-rss | head`; HME (~1 GB) and the Revel "
                                        "sync (~1.3 GB) peak in the morning on a 3.8 GB host."],
                               **base))
    else:
        out.append(CheckResult(state=OK, title="Memory OK", actual=f"{avail} MB", **base))
    return out


def mem_available_mb() -> int | None:
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        return None
    return None


def disk_usage(path):
    return shutil.disk_usage(path)


def newest_backup(globs: list[str]) -> tuple[str | None, datetime | None]:
    best, ts = None, None
    for g in globs:
        for p in glob.glob(g):
            try:
                st = os.stat(p)
            except OSError:
                continue
            if st.st_size < 1024 * 1024:
                continue          # an empty / truncated dump is not a backup
            t = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
            if ts is None or t > ts:
                best, ts = p, t
    return best, ts


def dump_format(path: str) -> str:
    """What the file's first bytes say it is. A format check, NOT a restore test."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(64)
    except OSError as e:
        return f"unreadable ({type(e).__name__})"
    if head.startswith(b"PGDMP"):
        return "pg_dump custom archive"
    if b"PostgreSQL database dump" in head:
        return "pg_dump plain SQL"
    if head[:2] == b"\x1f\x8b":
        return "gzip (contents not inspected)"
    return "unrecognised"


def read_status_json(path_glob: str) -> tuple[str | None, dict | None]:
    """Newest JSON status file matching a glob, written by a backup/restore job."""
    paths = sorted(glob.glob(path_glob), key=lambda p: os.path.getmtime(p), reverse=True)
    for p in paths:
        try:
            with open(p) as fh:
                return p, json.load(fh)
        except (OSError, ValueError):
            return p, None
    return None, None


def read_status_jsons(path_glob: str) -> list[tuple[str, dict | None]]:
    """Every JSON status file matching a glob, newest first by file time (the job
    writes each status once, atomically, when the attempt ends). Unreadable files
    are kept as None so a corrupt status is visible, not skipped."""
    out = []
    for p in glob.glob(path_glob):
        try:
            with open(p) as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            d = None
        try:
            mt = os.path.getmtime(p)
        except OSError:
            continue
        out.append((mt, p, d if isinstance(d, dict) else None))
    out.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [(p, d) for _m, p, d in out]


def file_size(path: str) -> int | None:
    try:
        return os.stat(path).st_size
    except OSError:
        return None


def _parse_ts(v) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(v))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _scheduled_backup(ctx, c, out: list, detail: dict):
    """Freshness from the scheduled job's OWN status files (laynes-db-backup):
    only a backup the job recorded as complete and verified counts, the file it
    names must still exist with the recorded size, and a failed attempt newer than
    the last success is reported at once. Manual / pre-deploy dumps are listed for
    information and never satisfy this check."""
    base = dict(source=SRC_INFRA, check="backup_freshness",
                expected=f"a successful SCHEDULED backup newer than {c['backup_max_age_hours']}h "
                         f"(archive-verified; restore status reported separately)")
    statuses = ctx.read_status_jsons(c["backup_status_glob"])
    ok_i = next((i for i, (_p, d) in enumerate(statuses) if d and d.get("ok") is True
                 and _parse_ts(d.get("completed_at"))), None)
    fail_i = next((i for i, (_p, d) in enumerate(statuses) if not d or d.get("ok") is not True), None)
    last_ok = statuses[ok_i] if ok_i is not None else None
    last_fail = statuses[fail_i] if fail_i is not None else None
    ok_at = _parse_ts(last_ok[1]["completed_at"]) if last_ok else None
    t = ctx.systemctl(c["backup_timer"]) if c.get("backup_timer") else {}
    detail["mode"] = "scheduled job status files"
    detail["timer"] = {"unit": c.get("backup_timer"), "state": t.get("UnitFileState"),
                       "next_scheduled": t.get("NextElapseUSecRealtime") or None}
    if last_ok:
        d = last_ok[1]
        size_now = ctx.file_size(d.get("dump") or "")
        detail["last_success"] = {
            "completed_at": d.get("completed_at"), "dump": d.get("dump"),
            "age_hours": round((ctx.now - ok_at).total_seconds() / 3600, 1),
            "size_bytes": d.get("bytes"), "size_on_disk": size_now,
            "sha256": (d.get("sha256") or "")[:16], "verified": d.get("verify"),
            "status_file": last_ok[0]}
        detail["local"] = {"path": d.get("dump"), "modified_at": d.get("completed_at"),
                           "age_hours": detail["last_success"]["age_hours"], "format": "pg_dump custom archive",
                           "restore_verified": False}
    else:
        detail["last_success"] = None
    if last_fail:
        f = last_fail[1] or {}
        detail["last_failure"] = {"started_at": f.get("started_at"), "stage": f.get("stage"),
                                  "error": str(f.get("error") or "unreadable status file")[:300],
                                  "status_file": last_fail[0]}
    else:
        detail["last_failure"] = None
    path, ts = ctx.newest_backup(c["backup_globs"]) if c.get("backup_globs") else (None, None)
    detail["manual_backups"] = ({"newest": path, "modified_at": ts.isoformat(timespec="seconds"),
                                 "note": "manual/pre-deploy dump: does NOT satisfy this check"}
                                if ts else None)
    ev = {**detail}
    if last_fail and (ok_i is None or fail_i < ok_i):        # newest attempt failed
        out.append(CheckResult(state=FAIL, severity=c["backup_severity"], category="backup_failed",
                               title="Last scheduled database backup attempt failed",
                               actual=f"{(last_fail[1] or {}).get('stage')}: "
                                      f"{detail['last_failure']['error'][:160]}",
                               evidence=ev, actions=_backup_actions(scheduled=True), **base))
    elif not last_ok:
        out.append(CheckResult(state=FAIL, severity=c["backup_severity"], category="no_backup",
                               title="No successful scheduled database backup recorded",
                               actual="none", evidence=ev, actions=_backup_actions(scheduled=True), **base))
    elif detail["last_success"]["size_on_disk"] != last_ok[1].get("bytes"):
        out.append(CheckResult(state=FAIL, severity=c["backup_severity"], category="backup_missing",
                               title="Newest scheduled backup file is missing or changed",
                               actual=f"{last_ok[1].get('dump')}: recorded {last_ok[1].get('bytes')} bytes, "
                                      f"on disk {detail['last_success']['size_on_disk']}",
                               evidence=ev, actions=_backup_actions(scheduled=True), **base))
    elif detail["last_success"]["age_hours"] > c["backup_max_age_hours"]:
        out.append(CheckResult(state=FAIL, severity=c["backup_severity"], category="stale_backup",
                               title="Database backup is stale (no successful scheduled backup)",
                               actual=f"last successful scheduled backup {last_ok[1]['completed_at']}",
                               evidence=ev, actions=_backup_actions(scheduled=True), **base))
    else:
        mib = (last_ok[1].get("bytes") or 0) / 1024 / 1024
        out.append(CheckResult(state=OK, title="Scheduled backup OK (archive verified)",
                               actual=f"{detail['last_success']['age_hours']}h old, {mib:.0f} MiB",
                               evidence=ev, **base))
    return ok_at


def backups(ctx) -> list[CheckResult]:
    """Backup FRESHNESS -- plus offsite and restore-test status only where a
    status file gives real evidence. A file's modification time proves a dump
    was written, not that it can be restored: nothing here calls a backup
    "recoverable" unless a restore test recorded success."""
    c = ctx.cfg["infra"]
    out = []
    if c.get("backup_status_glob"):
        detail = {"local": None, "offsite": "not monitored (no offsite status configured)",
                  "restore_test": "not monitored (no restore-test status configured)"}
        ts = _scheduled_backup(ctx, c, out, detail)
        return _evidence_checks(ctx, c, out, detail, ts)
    path, ts = ctx.newest_backup(c["backup_globs"])
    base = dict(source=SRC_INFRA, check="backup_freshness",
                expected=f"a local database dump newer than {c['backup_max_age_hours']}h "
                         f"(freshness only, not restore-verified)")
    detail = {"local": None, "offsite": "not monitored (no offsite status configured)",
              "restore_test": "not monitored (no restore-test status configured)"}
    if ts is None:
        out.append(CheckResult(state=FAIL, severity=c["backup_severity"], category="no_backup",
                               title="No local database backup found", actual="none",
                               evidence={"searched": c["backup_globs"], **detail},
                               actions=_backup_actions(), **base))
    else:
        fmt = ctx.backup_format(path)
        age_h = round((ctx.now - ts).total_seconds() / 3600, 1)
        detail["local"] = {"path": path, "modified_at": ts.isoformat(timespec="seconds"),
                           "age_hours": age_h, "format": fmt, "restore_verified": False}
        ev = {**detail, "note": "freshness = file modification time; not a restore test"}
        if fmt == "unrecognised" or fmt.startswith("unreadable"):
            out.append(CheckResult(state=FAIL, severity=c["backup_severity"],
                                   category="backup_unrecognised",
                                   title="Newest backup file is not a recognisable pg_dump",
                                   actual=f"{path}: {fmt}", evidence=ev,
                                   actions=_backup_actions(), **base))
        elif age_h > c["backup_max_age_hours"]:
            out.append(CheckResult(state=FAIL, severity=c["backup_severity"],
                                   category="stale_backup",
                                   title="Database backup is stale (freshness check)",
                                   actual=f"newest local dump written {ts:%Y-%m-%d %H:%M} UTC",
                                   evidence=ev, actions=_backup_actions(), **base))
        else:
            out.append(CheckResult(state=OK, title="Local backup fresh (not restore-verified)",
                                   actual=f"{age_h}h old", evidence=ev, **base))
    return _evidence_checks(ctx, c, out, detail, ts)


def _evidence_checks(ctx, c, out, detail, ts) -> list[CheckResult]:
    out += _status_file_check(ctx, detail, "offsite", c.get("offsite_status_glob"),
                              c["offsite_max_age_hours"], "backup_offsite",
                              "an offsite copy newer than {h}h")
    out += _status_file_check(ctx, detail, "restore_test", c.get("restore_test_status_glob"),
                              c["restore_test_max_age_hours"], "backup_restore_test",
                              "a successful restore test newer than {h}h")
    ctx.source_status("backup", ts, detail)
    return out


def _status_file_check(ctx, detail, kind, path_glob, max_h, check, expected):
    """Evidence written by a (future) offsite-sync or restore-test job:
    {"ok": true|false, "completed_at": ISO-8601, "dump": "<path>", "detail": "..."}.
    Not configured -> not evaluated (and the dashboard says "not monitored")."""
    if not path_glob:
        return []
    base = dict(source=SRC_INFRA, check=check, expected=expected.format(h=max_h))
    path, st = ctx.read_status_json(path_glob)
    label = "Offsite backup" if kind == "offsite" else "Backup restore test"
    if st is None:
        detail[kind] = {"status_file": path, "state": "missing or unreadable"}
        return [CheckResult(state=FAIL, severity=WARNING, category=f"{check}_missing",
                            title=f"{label}: no status recorded",
                            actual="no readable status file" if path is None else f"{path} unreadable",
                            evidence={"glob": path_glob}, actions=_backup_actions(), **base)]
    try:
        at = datetime.fromisoformat(str(st.get("completed_at")))
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
    except ValueError:
        at = None
    detail[kind] = {"status_file": path, "ok": st.get("ok"), "completed_at": st.get("completed_at"),
                    "dump": st.get("dump")}
    if st.get("mode"):
        detail[kind]["mode"] = st.get("mode")
    if kind == "restore_test" and st.get("ok") is True and at is not None:
        detail["newest_verified_backup"] = {"dump": st.get("dump"), "verified_at": st.get("completed_at")}
        if st.get("mode"):
            detail["newest_verified_backup"]["mode"] = st.get("mode")
    if kind == "restore_test" and st.get("mode"):
        label += f" ({st.get('mode')})"
    if st.get("ok") is not True:
        return [CheckResult(state=FAIL, severity=HIGH, category=f"{check}_failed",
                            title=f"{label} failed", actual=str(st.get("detail") or "ok=false")[:200],
                            evidence=detail[kind], actions=_backup_actions(), **base)]
    if at is None or (ctx.now - at).total_seconds() / 3600 > max_h:
        return [CheckResult(state=FAIL, severity=WARNING, category=f"{check}_stale",
                            title=f"{label} is overdue",
                            actual=f"last success {st.get('completed_at')}",
                            evidence=detail[kind], actions=_backup_actions(), **base)]
    return [CheckResult(state=OK, title=f"{label} OK", actual=f"at {st.get('completed_at')}",
                        evidence=detail[kind], **base)]


def _backup_actions(scheduled: bool = False) -> list[str]:
    if scheduled:
        return ["`journalctl -u laynes-db-backup -n 200`; status files in /var/backups/laynes/status/.",
                "Run one backup now (approved operators): `systemctl start laynes-db-backup.service`.",
                "docs/DATABASE_BACKUP.md \"Failure handling\". This check never creates, deletes "
                "or modifies backups."]
    return ["No scheduled pg_dump exists on this host (only manual pre-deploy dumps). "
            "Confirm whether the hosting provider takes snapshots.",
            "See docs/BACKUP_PROPOSAL.md; point infra.backup_globs (and the offsite / "
            "restore-test status globs) at the scheduled job once one is approved. Until "
            "then this incident stays open by design.",
            "This check never creates, deletes or modifies backups."]
