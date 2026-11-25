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


def backups(ctx) -> list[CheckResult]:
    """Backup FRESHNESS -- plus offsite and restore-test status only where a
    status file gives real evidence. A file's modification time proves a dump
    was written, not that it can be restored: nothing here calls a backup
    "recoverable" unless a restore test recorded success."""
    c = ctx.cfg["infra"]
    out = []
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
    if kind == "restore_test" and st.get("ok") is True and at is not None:
        detail["newest_verified_backup"] = {"dump": st.get("dump"), "verified_at": st.get("completed_at")}
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


def _backup_actions() -> list[str]:
    return ["No scheduled pg_dump exists on this host (only manual pre-deploy dumps). "
            "Confirm whether the hosting provider takes snapshots.",
            "See docs/BACKUP_PROPOSAL.md; point infra.backup_globs (and the offsite / "
            "restore-test status globs) at the scheduled job once one is approved. Until "
            "then this incident stays open by design.",
            "This check never creates, deletes or modifies backups."]
