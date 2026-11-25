"""Host health: PostgreSQL reachability, disk, memory, backup freshness."""
from __future__ import annotations

import glob
import os
import shutil
from datetime import datetime, timezone

from ..model import CRITICAL, FAIL, OK, SRC_INFRA, WARNING, CheckResult
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


def backups(ctx) -> list[CheckResult]:
    c = ctx.cfg["infra"]
    path, ts = ctx.newest_backup(c["backup_globs"])
    base = dict(source=SRC_INFRA, check="backup_freshness",
                expected=f"a database backup newer than {c['backup_max_age_hours']}h")
    if ts is None:
        return [CheckResult(state=FAIL, severity=c["backup_severity"], category="no_backup",
                            title="No database backup found", actual="none",
                            evidence={"searched": c["backup_globs"]},
                            actions=_backup_actions(), **base)]
    age_h = round((ctx.now - ts).total_seconds() / 3600, 1)
    ctx.source_status("backup", ts, {"path": path})
    if age_h > c["backup_max_age_hours"]:
        return [CheckResult(state=FAIL, severity=c["backup_severity"], category="stale_backup",
                            title="Database backup is stale",
                            actual=f"newest backup {age_h:.0f}h old ({ts:%Y-%m-%d %H:%M} UTC)",
                            evidence={"path": path}, actions=_backup_actions(), **base)]
    return [CheckResult(state=OK, title="Backup fresh", actual=f"{age_h}h old", **base)]


def _backup_actions() -> list[str]:
    return ["No scheduled pg_dump exists on this host (only manual pre-deploy dumps). "
            "Confirm whether the hosting provider takes snapshots.",
            "Decide on a scheduled backup and point infra.backup_globs at it; until then "
            "this incident stays open by design."]

