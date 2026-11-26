#!/usr/bin/env python3
"""Scheduled PostgreSQL backup for the Laynes `laynes` database, with evidence.

Runs as the OS user `postgres` and connects over the local UNIX socket with peer
authentication, so there is no password anywhere: not in this file, not in the
config, not in the unit, not in the journal. Standard library only (no venv).

    backup         consistent pg_dump + manifest + archive verification + retention
    restore-test   restore the newest backup into a THROWAWAY cluster and compare it
                   with the manifest (mode "critical" or "full"; never production)
    verify NAME    re-check one backup's checksums and archive readability
    retention      print what retention would keep/delete (no deletion)
    list           list complete backups

What a backup proves, in increasing strength (the status files say which one):
  1. written    -- the file exists (that is all the old monitor could see);
  2. archive    -- sha256 recorded; pg_restore can read the TOC and DECODE EVERY
                   data block (`pg_restore -f /dev/null`); every table counted in the
                   snapshot has a TABLE DATA entry;
  3. restored   -- restore-test loaded it into a fresh cluster and the exact row
                   counts and content fingerprints match what the SAME snapshot
                   contained when the dump was taken.

Consistency: one REPEATABLE READ READ ONLY transaction exports a snapshot; pg_dump
dumps that snapshot (--snapshot) and the manifest's row counts / fingerprints are
taken in the same snapshot, so a restore can be compared exactly. pg_dump takes
only ACCESS SHARE locks (they block DDL, not reads or writes) and gives up after
--lock-wait-timeout instead of queueing behind a migration.

Safety:
  * writes only inside backup_dir (a dedicated directory owned by this user, mode
    0700, not a symlink, never under /root); files are created as *.partial and
    promoted by rename only after verification; the manifest is written LAST and
    marks a backup as complete;
  * retention deletes only regular files directly inside backup_dir whose names
    match this tool's pattern, owned by this user, and only sets with a manifest
    written by this tool -- and only after a NEW backup succeeded. It cannot reach
    /root/deploy-backups (the unit also has ProtectHome=yes);
  * refuses to start if the dump would push the filesystem past max_used_pct
    (kept below the monitor's 85 % disk warning) or below min_free_bytes;
  * one lock file serialises backup and restore-test; neither overlaps the other.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
import tomllib
from datetime import datetime, timedelta, timezone

TOOL = "laynes-db-backup"
FORMAT_VERSION = 1
EXIT_OK, EXIT_FAIL, EXIT_CONFIG = 0, 1, 2

DEFAULTS = {
    "backup_dir": "/var/backups/laynes",
    "database": "laynes",
    "pg_bin": "/usr/lib/postgresql/18/bin",
    "pg_host": "",                    # "" = default UNIX socket (peer auth)
    "pg_port": 5432,
    "compress": "zstd:3",
    "lock_wait_timeout": "120s",
    "lock_wait_minutes": 30,          # wait this long for the other job's lock
    # local retention (the disk is small; long retention belongs off-server)
    "keep_daily": 2,
    "keep_weekly": 0,
    "status_keep_days": 90,
    # disk guard: never let a backup or restore test push "/" past these
    "max_used_pct": 84.0,             # monitor warns at 85 % (used/total)
    "min_free_bytes": 2 * 1024 ** 3,
    "size_estimate_factor": 1.25,     # x the previous dump size
    "first_dump_ratio": 0.20,         # x database size when no previous dump
    # restore test
    "restore_mode": "critical",       # "critical" | "full"
    "restore_port": 54399,
    "restore_extra_bytes": 512 * 1024 ** 2,
    "critical_tables": [
        "establishments", "ayg_business_periods", "app_users", "app_sessions",
        "chat_conversations", "chat_messages", "chat_jobs", "chat_query_log",
        "hme_store_mapping", "hme_ingest_run", "hme_store_ingest_result",
        "hme_store_daily", "hme_outliers_daily", "hme_goal_history", "hme_goal_conflict",
        "hme_goal_history_correction_20261117",
        "r365_store_mapping", "r365_ingest_run", "r365_store_daily", "r365_store_weekly",
        "r365_financial_ingest_run", "r365_financial_account",
        "r365_financial_account_period", "r365_financial_period_expense",
        "r365_financial_candidate_expense", "r365_financial_local_category_mapping",
        "r365_financial_transaction",
        "revel_reconciliation_daily", "sync_state", "ingestion_log",
    ],
    # content fingerprints (md5 over every row, collation-independent order);
    # small tables whose exact content matters (goal history, mappings)
    "fingerprint_tables": [
        "establishments", "ayg_business_periods", "hme_store_mapping", "hme_goal_history",
        "hme_goal_conflict", "hme_goal_history_correction_20261117", "r365_store_mapping",
        "r365_financial_local_category_mapping",
    ],
}

NAME_RE = re.compile(r"^laynes-(\d{8}T\d{6}Z)$")
FILE_RE = re.compile(r"^laynes-(\d{8}T\d{6}Z)\.(dump|globals\.sql|manifest\.json)(\.partial)?$")
STATUS_RE = re.compile(r"^(backup|restore)-(\d{8}T\d{6}Z)-(\d+)\.json$")
STAMP = "%Y%m%dT%H%M%SZ"
SESSION_SETTINGS = ("SET TimeZone = 'UTC'; SET DateStyle = 'ISO, YMD'; "
                    "SET IntervalStyle = 'postgres'; SET extra_float_digits = 3; "
                    "SET bytea_output = 'hex'; SET statement_timeout = 0; "
                    "SET idle_in_transaction_session_timeout = 0;")
FS = "\x1f"


class BackupError(Exception):
    """A failure with a stage label; the message never contains credentials."""

    def __init__(self, stage: str, msg: str):
        super().__init__(f"{stage}: {msg}")
        self.stage, self.msg = stage, msg


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} {msg}", flush=True)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- config
def load_config(path: str | None) -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    if path:
        with open(path, "rb") as fh:
            user = tomllib.load(fh)
        unknown = set(user) - set(DEFAULTS)
        if unknown:
            raise ValueError(f"unknown config keys: {sorted(unknown)}")
        cfg.update(user)
    if cfg["restore_mode"] not in ("critical", "full"):
        raise ValueError("restore_mode must be 'critical' or 'full'")
    if cfg["keep_daily"] < 1:
        raise ValueError("keep_daily must be >= 1 (the newest backup is always kept)")
    if not (0 < float(cfg["max_used_pct"]) < 100):
        raise ValueError("max_used_pct out of range")
    return cfg


def check_backup_dir(path: str) -> None:
    """The ONLY directory this tool writes to or deletes from."""
    if not os.path.isabs(path) or os.path.normpath(path) != path:
        raise BackupError("config", f"backup_dir must be a normalised absolute path: {path!r}")
    if path == "/" or path.startswith("/root") or "deploy-backups" in path:
        raise BackupError("config", f"backup_dir {path!r} is not allowed")
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or os.path.realpath(path) != path:
        raise BackupError("config", f"backup_dir {path!r} must be a real directory, not a symlink")
    if st.st_uid != os.geteuid():
        raise BackupError("config", f"backup_dir {path!r} is not owned by uid {os.geteuid()}")
    if st.st_mode & 0o077:
        raise BackupError("config", f"backup_dir {path!r} must be mode 0700 (is {oct(st.st_mode & 0o777)})")


# ---------------------------------------------------------------------- pure helpers
def parse_stamp(s: str) -> datetime:
    return datetime.strptime(s, STAMP).replace(tzinfo=timezone.utc)


def plan_retention(stamps: list[datetime], keep_daily: int, keep_weekly: int) -> set[datetime]:
    """Which backups to KEEP. Newest per calendar day (UTC) for `keep_daily` days,
    plus newest per ISO week for `keep_weekly` weeks; the newest is always kept."""
    keep: set[datetime] = set()
    ordered = sorted(set(stamps), reverse=True)
    if not ordered:
        return keep
    keep.add(ordered[0])
    days, weeks = set(), set()
    for s in ordered:
        d = s.date()
        if d not in days and len(days) < keep_daily:
            days.add(d)
            keep.add(s)
    for s in ordered:
        w = tuple(s.isocalendar())[:2]
        if w not in weeks and len(weeks) < keep_weekly:
            weeks.add(w)
            keep.add(s)
    return keep


def space_check(total: int, used: int, free: int, need: int, max_used_pct: float,
                min_free_bytes: int) -> tuple[bool, dict]:
    """`used`/`total` exactly as the monitor computes disk use (shutil.disk_usage:
    reserved blocks are neither used nor free); `free` = space available to us."""
    after_pct = round((used + need) / total * 100, 1)
    after_free = free - need
    ok = after_pct <= max_used_pct and after_free >= min_free_bytes
    return ok, {"total_bytes": total, "used_bytes": used, "free_bytes": free, "need_bytes": need,
                "used_pct_after": after_pct, "max_used_pct": max_used_pct,
                "free_bytes_after": after_free, "min_free_bytes": min_free_bytes}


def parse_toc(listing: str) -> dict:
    """`pg_restore --list` -> counts and the set of tables with a TABLE DATA entry."""
    entries, data = 0, set()
    for line in listing.splitlines():
        if not line or line.startswith(";"):
            continue
        entries += 1
        m = re.match(r"^\d+; \d+ \d+ TABLE DATA (\S+) (\S+) ", line + " ")
        if m:
            data.add(f"{m.group(1)}.{m.group(2)}")
    return {"entries": entries, "table_data": data}


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fsync_path(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json_atomic(path: str, obj: dict) -> None:
    tmp = path + ".partial"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    fsync_path(os.path.dirname(path))


# ------------------------------------------------------------------------ postgres
class Pg:
    def __init__(self, cfg: dict, host: str | None = None, port: int | None = None,
                 database: str | None = None):
        self.bin = cfg["pg_bin"]
        self.host = cfg["pg_host"] if host is None else host
        self.port = int(cfg["pg_port"] if port is None else port)
        self.database = database or cfg["database"]

    def tool(self, name: str) -> str:
        return os.path.join(self.bin, name)

    def conn_args(self, database: str | None = None) -> list[str]:
        a = ["-p", str(self.port)]
        if self.host:
            a += ["-h", self.host]
        return a + ["-d", database or self.database]

    def run(self, argv: list[str], stage: str, timeout: int | None = None,
            check: bool = True) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
        env["PGAPPNAME"] = TOOL
        cp = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
        if check and cp.returncode != 0:
            raise BackupError(stage, f"{os.path.basename(argv[0])} exit {cp.returncode}: "
                                     f"{cp.stderr.strip()[-600:]}")
        return cp

    def query(self, sql: str, stage: str, database: str | None = None) -> list[list[str]]:
        cp = self.run([self.tool("psql"), "-X", "-q", "-A", "-t", "-F", FS,
                       "-v", "ON_ERROR_STOP=1", *self.conn_args(database),
                       "-c", SESSION_SETTINGS, "-c", sql], stage)
        return [line.split(FS) for line in cp.stdout.splitlines() if line != ""]


class Session:
    """One long-lived psql process: holds the exported snapshot open while pg_dump runs."""

    def __init__(self, pg: Pg, stage: str):
        env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
        env["PGAPPNAME"] = TOOL + "-snapshot"
        self.stage, self.n = stage, 0
        self.p = subprocess.Popen(
            [pg.tool("psql"), "-X", "-q", "-A", "-t", "-F", FS, "-v", "ON_ERROR_STOP=1",
             *pg.conn_args()], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1, env=env)

    def query(self, sql: str) -> list[list[str]]:
        self.n += 1
        mark = f"__laynes_backup_end_{self.n}__"
        try:
            self.p.stdin.write(sql.strip().rstrip(";") + ";\n\\echo " + mark + "\n")
            self.p.stdin.flush()
        except BrokenPipeError:
            self._died()
        rows = []
        while True:
            line = self.p.stdout.readline()
            if line == "":
                self._died()
            line = line.rstrip("\n")
            if line == mark:
                return rows
            if line != "":
                rows.append(line.split(FS))

    def _died(self):
        self.p.wait(timeout=30)
        err = self.p.stderr.read().strip()[-600:]
        raise BackupError(self.stage, f"psql session ended (exit {self.p.returncode}): {err}")

    def close(self):
        if self.p.poll() is None:
            try:
                self.p.stdin.write("\\q\n")
                self.p.stdin.flush()
            except BrokenPipeError:
                pass
            try:
                self.p.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.p.kill()


LEAF_TABLES_SQL = """
select format('%I.%I', n.nspname, c.relname), pg_total_relation_size(c.oid)
from pg_class c join pg_namespace n on n.oid = c.relnamespace
where c.relkind = 'r' and n.nspname not in ('pg_catalog', 'information_schema')
  and n.nspname !~ '^pg_' order by 1"""


FK_EDGES_SQL = """
select format('%I.%I', n.nspname, c.relname), format('%I.%I', fn.nspname, f.relname)
from pg_constraint k join pg_class c on c.oid = k.conrelid
join pg_namespace n on n.oid = c.relnamespace join pg_class f on f.oid = k.confrelid
join pg_namespace fn on fn.oid = f.relnamespace where k.contype = 'f'"""


def fingerprint_sql(qualified: str) -> str:
    return (f"select count(*), md5(coalesce(string_agg(t::text, E'\\n' order by "
            f"t::text collate \"C\"), '')) from {qualified} t")


def qualify(name: str) -> str:
    """'table' -> 'public.table' (identifiers are validated, never interpolated raw)."""
    if not re.fullmatch(r"[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)?", name):
        raise BackupError("config", f"invalid table name {name!r}")
    return name if "." in name else f"public.{name}"


def snapshot_facts(q, cfg: dict) -> dict:
    """Facts measured INSIDE the dump's snapshot (q = a query function)."""
    tables = {r[0]: {"total_bytes": int(r[1])} for r in q(LEAF_TABLES_SQL)}
    for t in tables:
        tables[t]["rows"] = int(q(f"select count(*) from {t}")[0][0])
    fps = {}
    for name in cfg["fingerprint_tables"]:
        t = qualify(name)
        if t in tables:
            n, md5 = q(fingerprint_sql(t))[0]
            fps[t] = {"rows": int(n), "md5": md5}
    ext = {r[0]: r[1] for r in q("select extname, extversion from pg_extension order by 1")}
    views = int(q("select count(*) from pg_views where schemaname not in "
                  "('pg_catalog','information_schema')")[0][0])
    matviews = int(q("select count(*) from pg_matviews")[0][0])
    fks = sorted({(a, b) for a, b in q(FK_EDGES_SQL)})
    return {"tables": tables, "fingerprints": fps, "extensions": ext,
            "views": views, "matviews": matviews, "fk_edges": [list(e) for e in fks]}


# ---------------------------------------------------------------------- inventory
def complete_backups(backup_dir: str) -> dict[str, dict]:
    """name -> manifest, for backups whose manifest was written by this tool."""
    out = {}
    for e in os.scandir(backup_dir):
        m = FILE_RE.match(e.name)
        if not m or m.group(2) != "manifest.json" or m.group(3):
            continue
        if not e.is_file(follow_symlinks=False):
            continue
        try:
            with open(e.path) as fh:
                man = json.load(fh)
        except (OSError, ValueError):
            continue
        if man.get("tool") == TOOL and man.get("name") == f"laynes-{m.group(1)}":
            out[man["name"]] = man
    return out


def owned_regular(path: str) -> bool:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_uid == os.geteuid()


def safe_unlink(backup_dir: str, name: str) -> bool:
    """Delete ONE file directly inside backup_dir that matches this tool's patterns."""
    if "/" in name or not (FILE_RE.match(name) or STATUS_RE.match(name)):
        raise BackupError("retention", f"refusing to delete {name!r}: not a {TOOL} file")
    path = os.path.join(backup_dir, "status" if STATUS_RE.match(name) else "", name)
    path = os.path.normpath(path)
    if os.path.dirname(path) not in (backup_dir, os.path.join(backup_dir, "status")):
        raise BackupError("retention", f"refusing to delete outside {backup_dir}: {path}")
    if not owned_regular(path):
        return False
    os.unlink(path)
    return True


def apply_retention(cfg: dict, now: datetime, dry_run: bool = False) -> dict:
    d = cfg["backup_dir"]
    backups = complete_backups(d)
    stamps = {parse_stamp(NAME_RE.match(n).group(1)): n for n in backups}
    keep = plan_retention(list(stamps), cfg["keep_daily"], cfg["keep_weekly"])
    delete = sorted(n for s, n in stamps.items() if s not in keep)
    removed = []
    for n in delete:
        # manifest first: an interrupted deletion leaves an incomplete set, never a
        # manifest that points at a missing dump
        for suffix in (".manifest.json", ".dump", ".globals.sql"):
            if not dry_run and safe_unlink(d, n + suffix):
                removed.append(n + suffix)
    # leftovers: *.partial from a crashed run, or a dump whose manifest never landed
    complete = set(backups)
    for e in os.scandir(d):
        m = FILE_RE.match(e.name)
        if not m or not e.is_file(follow_symlinks=False):
            continue
        name = f"laynes-{m.group(1)}"
        if name in complete and not m.group(3):
            continue
        age = now.timestamp() - e.stat(follow_symlinks=False).st_mtime
        if age > 24 * 3600:
            if not dry_run and safe_unlink(d, e.name):
                removed.append(e.name)
    # status history
    sd = os.path.join(d, "status")
    if os.path.isdir(sd):
        newest = {}
        files = sorted((e.name for e in os.scandir(sd) if STATUS_RE.match(e.name)),
                       key=lambda f: STATUS_RE.match(f).group(2), reverse=True)
        for f in files:
            kind, stamp, _pid = STATUS_RE.match(f).groups()
            if kind not in newest:
                newest[kind] = f
                continue
            if now - parse_stamp(stamp) > timedelta(days=cfg["status_keep_days"]):
                if not dry_run and safe_unlink(d, f):
                    removed.append(f)
    return {"kept": sorted(stamps[s] for s in keep), "deleted_sets": delete, "removed_files": removed}


def make_room(cfg: dict, need: int) -> list[str]:
    """Before a dump that would not fit: delete the OLDEST complete backups, but keep
    at least max(1, keep_daily - 1), so after this run succeeds there are keep_daily.
    If the dump then fails, the newest good backup is still there."""
    d = cfg["backup_dir"]
    names = sorted(complete_backups(d))           # oldest first (timestamped names)
    floor = max(1, cfg["keep_daily"] - 1)
    removed = []
    while len(names) > floor:
        du = shutil.disk_usage(d)
        if space_check(du.total, du.used, du.free, need, float(cfg["max_used_pct"]),
                       int(cfg["min_free_bytes"]))[0]:
            break
        n = names.pop(0)
        for suffix in (".manifest.json", ".dump", ".globals.sql"):
            safe_unlink(d, n + suffix)
        removed.append(n)
    return removed


# --------------------------------------------------------------------------- locking
class Lock:
    def __init__(self, backup_dir: str, wait_minutes: float):
        self.path = os.path.join(backup_dir, ".lock")
        self.wait = wait_minutes * 60
        self.fh = None

    def __enter__(self):
        self.fh = os.fdopen(os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600), "r+")
        deadline = time.monotonic() + self.wait
        while True:
            try:
                fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise BackupError("lock", f"another backup/restore-test held the lock for "
                                              f"{self.wait:.0f} s")
                time.sleep(min(10.0, max(0.5, deadline - time.monotonic())))

    def __exit__(self, *exc):
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()


def write_status(cfg: dict, kind: str, stamp: str, body: dict) -> str:
    sd = os.path.join(cfg["backup_dir"], "status")
    os.makedirs(sd, mode=0o700, exist_ok=True)
    path = os.path.join(sd, f"{kind}-{stamp}-{os.getpid()}.json")
    write_json_atomic(path, {"tool": TOOL, "format_version": FORMAT_VERSION, **body})
    return path


# ---------------------------------------------------------------------------- backup
def verify_archive(pg: Pg, dump: str, expected_tables: set[str] | None) -> dict:
    toc = parse_toc(pg.run([pg.tool("pg_restore"), "--list", dump], "verify-toc").stdout)
    if toc["entries"] == 0:
        raise BackupError("verify-toc", "archive has no entries")
    if expected_tables is not None:
        missing = sorted(expected_tables - toc["table_data"])
        if missing:
            raise BackupError("verify-toc", f"{len(missing)} tables have no TABLE DATA entry: "
                                            f"{missing[:10]}")
    # decodes and decompresses every data block; catches truncation and corruption
    pg.run([pg.tool("pg_restore"), "-f", os.devnull, dump], "verify-decode")
    return {"toc_entries": toc["entries"], "table_data_entries": len(toc["table_data"]),
            "full_decode": True}


def estimate_need(cfg: dict, pg: Pg) -> int:
    backups = complete_backups(cfg["backup_dir"])
    if backups:
        newest = backups[max(backups)]
        return int(newest["dump"]["bytes"] * cfg["size_estimate_factor"])
    size = int(pg.query("select pg_database_size(current_database())", "preflight")[0][0])
    return int(size * cfg["first_dump_ratio"])


def do_backup(cfg: dict) -> int:
    started = utcnow()
    stamp = started.strftime(STAMP)
    name = f"laynes-{stamp}"
    d = cfg["backup_dir"]
    stage = "config"
    partials: list[str] = []
    try:
        check_backup_dir(d)
        pg = Pg(cfg)
        with Lock(d, cfg["lock_wait_minutes"]):
            # the name is chosen under the lock and never reuses an existing one
            while any(e.name.startswith(f"laynes-{stamp}.") for e in os.scandir(d)):
                time.sleep(1.0)
                stamp = utcnow().strftime(STAMP)
            name = f"laynes-{stamp}"
            stage = "preflight"
            need = estimate_need(cfg, pg)
            du = shutil.disk_usage(d)
            ok, space = space_check(du.total, du.used, du.free, need, float(cfg["max_used_pct"]),
                                    int(cfg["min_free_bytes"]))
            log(f"preflight: need~{need >> 20} MiB, used after {space['used_pct_after']}% "
                f"(limit {cfg['max_used_pct']}%), free after {space['free_bytes_after'] >> 20} MiB")
            if not ok:
                freed = make_room(cfg, need)
                if freed:
                    log(f"preflight: removed older backup(s) {freed} to make room "
                        f"(at least {max(1, cfg['keep_daily'] - 1)} complete backup(s) kept)")
                    du = shutil.disk_usage(d)
                    ok, space = space_check(du.total, du.used, du.free, need, float(cfg["max_used_pct"]),
                                            int(cfg["min_free_bytes"]))
            if not ok:
                raise BackupError("preflight", f"insufficient_space: {json.dumps(space)}")

            dump = os.path.join(d, name + ".dump")
            globs = os.path.join(d, name + ".globals.sql")
            partials = [dump + ".partial", globs + ".partial"]

            stage = "snapshot"
            sess = Session(pg, "snapshot")
            try:
                sess.query(SESSION_SETTINGS)
                sess.query("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
                snap = sess.query("select pg_export_snapshot()")[0][0]
                snap_at = utcnow()
                meta = sess.query(
                    "select current_setting('server_version'), current_setting('server_version_num'), "
                    "pg_encoding_to_char(encoding), datcollate, datctype, datlocprovider, "
                    "coalesce(datlocale, ''), pg_database_size(datname) "
                    "from pg_database where datname = current_database()")[0]
                roles = [r[0] for r in sess.query(
                    "select rolname from pg_roles where rolname !~ '^pg_' order by 1")]
                stage = "pg_dump"
                log(f"pg_dump {cfg['database']} snapshot {snap} -> {os.path.basename(dump)}")
                t0 = time.monotonic()
                pg.run([pg.tool("pg_dump"), "-Fc", f"--compress={cfg['compress']}",
                        f"--lock-wait-timeout={cfg['lock_wait_timeout']}",
                        f"--snapshot={snap}", "-f", partials[0], *pg.conn_args()], "pg_dump")
                dump_s = round(time.monotonic() - t0, 1)
                stage = "snapshot-facts"
                facts = snapshot_facts(sess.query, cfg)
                sess.query("COMMIT")
            finally:
                sess.close()
            os.chmod(partials[0], 0o600)

            stage = "globals"
            pg.run([pg.tool("pg_dumpall"), "--globals-only", "--no-role-passwords",
                    "-f", partials[1], *pg.conn_args(cfg["database"])[:-2],
                    "-l", cfg["database"]], "globals")
            os.chmod(partials[1], 0o600)

            stage = "verify"
            verify = verify_archive(pg, partials[0], set(facts["tables"]))
            dump_bytes = os.path.getsize(partials[0])
            sha = sha256_file(partials[0])
            gsha = sha256_file(partials[1])
            for p in partials:
                fsync_path(p)

            stage = "promote"
            os.replace(partials[0], dump)
            os.replace(partials[1], globs)
            partials = []
            fsync_path(d)
            finished = utcnow()
            manifest = {
                "tool": TOOL, "format_version": FORMAT_VERSION, "kind": "scheduled",
                "name": name, "host": socket.gethostname(), "database": cfg["database"],
                "started_at": iso(started), "snapshot_at": iso(snap_at), "completed_at": iso(finished),
                "server_version": meta[0], "server_version_num": int(meta[1]),
                "encoding": meta[2], "datcollate": meta[3], "datctype": meta[4],
                "datlocprovider": meta[5], "datlocale": meta[6], "db_size_bytes": int(meta[7]),
                "roles": roles, "compress": cfg["compress"], "pg_dump_seconds": dump_s,
                "dump": {"file": os.path.basename(dump), "bytes": dump_bytes, "sha256": sha},
                "globals": {"file": os.path.basename(globs), "bytes": os.path.getsize(globs),
                            "sha256": gsha},
                "verify": verify, **facts,
            }
            stage = "manifest"
            write_json_atomic(os.path.join(d, name + ".manifest.json"), manifest)
            log(f"backup complete: {name}.dump {dump_bytes >> 20} MiB sha256 {sha[:16]}... "
                f"{len(facts['tables'])} tables, archive decoded in full")

            status = {"ok": True, "kind": "scheduled", "started_at": iso(started),
                      "completed_at": iso(finished), "dump": dump, "bytes": dump_bytes,
                      "sha256": sha, "verify": "archive (toc + full decode + table coverage)",
                      "tables": len(facts["tables"]),
                      "rows": sum(t["rows"] for t in facts["tables"].values()),
                      "duration_s": round((finished - started).total_seconds(), 1),
                      "disk": space}
            stage = "retention"
            try:
                ret = apply_retention(cfg, utcnow())
                status["retention"] = ret
                log(f"retention: kept {ret['kept']}; removed {ret['removed_files'] or 'nothing'}")
            except Exception as e:  # noqa: BLE001  retention never invalidates a good backup
                status["retention_error"] = f"{type(e).__name__}: {e}"[:300]
                log(f"retention FAILED (backup itself is good): {e}")
            write_status(cfg, "backup", stamp, status)
            return EXIT_OK
    except Exception as e:  # noqa: BLE001
        for p in partials:
            try:
                if owned_regular(p):
                    os.unlink(p)
            except OSError:
                pass
        err = e.msg if isinstance(e, BackupError) else f"{type(e).__name__}: {e}"
        st = e.stage if isinstance(e, BackupError) else stage
        log(f"BACKUP FAILED at {st}: {err}")
        try:
            write_status(cfg, "backup", stamp, {
                "ok": False, "kind": "scheduled", "started_at": iso(started),
                "completed_at": iso(utcnow()), "stage": st, "error": str(err)[:1000],
                "dump": None})
        except Exception as e2:  # noqa: BLE001
            log(f"could not write failure status: {e2}")
        return EXIT_CONFIG if st == "config" else EXIT_FAIL


# --------------------------------------------------------------------- restore test
def verify_checksums(d: str, man: dict) -> None:
    for part in ("dump", "globals"):
        p = os.path.join(d, man[part]["file"])
        if not owned_regular(p):
            raise BackupError("checksum", f"{man[part]['file']} missing")
        if os.path.getsize(p) != man[part]["bytes"]:
            raise BackupError("checksum", f"{man[part]['file']} size changed")
        if sha256_file(p) != man[part]["sha256"]:
            raise BackupError("checksum", f"{man[part]['file']} sha256 mismatch")


def fk_closure(edges, start: set[str]) -> set[str]:
    """Tables that must be loaded with `start` so every FK in post-data validates.
    `edges` come from the manifest: FKs are post-data, so the scratch catalog does
    not have them yet when the data is loaded."""
    refs: dict[str, set[str]] = {}
    for a, b in edges:
        refs.setdefault(a, set()).add(b)
    out, todo = set(), list(start)
    while todo:
        t = todo.pop()
        if t in out:
            continue
        out.add(t)
        todo.extend(refs.get(t, ()))
    return out


class ScratchCluster:
    """A throwaway cluster: UNIX socket only (listen_addresses=''), inside backup_dir."""

    def __init__(self, cfg: dict, man: dict):
        self.cfg, self.man = cfg, man
        self.root = os.path.join(cfg["backup_dir"], "restore-scratch")
        self.data = os.path.join(self.root, "data")
        self.port = int(cfg["restore_port"])
        self.pg = Pg(cfg, host=self.root, port=self.port, database=man["database"])

    def _remove(self):
        if os.path.lexists(self.root):
            if os.path.realpath(self.root) != self.root or \
                    os.path.dirname(self.root) != self.cfg["backup_dir"]:
                raise BackupError("scratch", f"refusing to remove {self.root}")
            if os.path.exists(os.path.join(self.data, "postmaster.pid")):
                self.pg.run([self.pg.tool("pg_ctl"), "-D", self.data, "-m", "immediate", "-w",
                             "stop"], "scratch-stop", timeout=120, check=False)
            shutil.rmtree(self.root)

    def __enter__(self):
        self._remove()                       # leftovers of a crashed test
        os.makedirs(self.root, mode=0o700)
        m = self.man
        init = [self.pg.tool("initdb"), "-D", self.data, "-U", "postgres", "-A", "trust",
                "--no-sync", "-E", m["encoding"]]
        prov = m.get("datlocprovider", "c")
        if prov == "i":
            init += ["--locale-provider=icu", f"--icu-locale={m['datlocale']}",
                     f"--lc-collate={m['datcollate']}", f"--lc-ctype={m['datctype']}"]
        elif prov == "b":
            init += ["--locale-provider=builtin", f"--builtin-locale={m['datlocale']}",
                     f"--lc-collate={m['datcollate']}", f"--lc-ctype={m['datctype']}"]
        else:
            init += [f"--lc-collate={m['datcollate']}", f"--lc-ctype={m['datctype']}"]
        self.pg.run(init, "scratch-initdb", timeout=300)
        opts = (f"-p {self.port} -c listen_addresses='' -c unix_socket_directories={self.root} "
                "-c fsync=off -c full_page_writes=off -c synchronous_commit=off "
                "-c autovacuum=off -c shared_buffers=64MB -c maintenance_work_mem=64MB "
                "-c max_wal_size=2GB -c max_locks_per_transaction=256 -c logging_collector=off")
        self.pg.run([self.pg.tool("pg_ctl"), "-D", self.data, "-l",
                     os.path.join(self.root, "postgres.log"), "-w", "-t", "120", "-o", opts,
                     "start"], "scratch-start", timeout=180)
        return self

    def __exit__(self, *exc):
        try:
            self._remove()
        except Exception as e:  # noqa: BLE001
            log(f"WARNING: scratch cleanup failed: {e}")


def compare(man: dict, q, loaded: set[str]) -> dict:
    """Restored cluster vs the manifest. Returns {"checked": ..., "mismatches": [...]}."""
    bad = []
    ext = {r[0]: r[1] for r in q("select extname, extversion from pg_extension order by 1")}
    if ext != man["extensions"]:
        bad.append(f"extensions differ: {sorted(set(ext.items()) ^ set(man['extensions'].items()))}")
    tables = {r[0] for r in q(LEAF_TABLES_SQL)}
    if tables != set(man["tables"]):
        bad.append(f"table set differs: missing {sorted(set(man['tables']) - tables)[:10]}, "
                   f"extra {sorted(tables - set(man['tables']))[:10]}")
    views = int(q("select count(*) from pg_views where schemaname not in "
                  "('pg_catalog','information_schema')")[0][0])
    if views != man["views"]:
        bad.append(f"views {views} != {man['views']}")
    rows = 0
    for t in sorted(loaded & tables):
        n = int(q(f"select count(*) from {t}")[0][0])
        rows += n
        if n != man["tables"][t]["rows"]:
            bad.append(f"{t}: {n} rows restored, {man['tables'][t]['rows']} in snapshot")
    fp_checked = 0
    for t, fp in man["fingerprints"].items():
        if t in loaded:
            n, md5 = q(fingerprint_sql(t))[0]
            fp_checked += 1
            if md5 != fp["md5"] or int(n) != fp["rows"]:
                bad.append(f"{t}: content fingerprint differs")
    unloaded = [t for t in man["tables"] if t not in loaded and man["tables"][t]["rows"]]
    return {"tables_count_checked": len(loaded & tables), "rows_checked": rows,
            "fingerprints_checked": fp_checked, "tables_not_loaded": len(unloaded),
            "mismatches": bad}


def do_restore_test(cfg: dict, mode: str | None = None, name: str | None = None) -> int:
    started = utcnow()
    stamp = started.strftime(STAMP)
    mode = mode or cfg["restore_mode"]
    d = cfg["backup_dir"]
    stage, man = "config", None
    try:
        check_backup_dir(d)
        with Lock(d, cfg["lock_wait_minutes"]):
            stage = "select"
            backups = complete_backups(d)
            if not backups:
                raise BackupError("select", "no complete backup to test")
            man = backups[name] if name else backups[max(backups)]
            dump = os.path.join(d, man["dump"]["file"])
            log(f"restore-test ({mode}) of {man['name']}")
            stage = "checksum"
            verify_checksums(d, man)
            vout = Pg(cfg).run([Pg(cfg).tool("postgres"), "-V"], "version").stdout
            local_major = int(re.search(r"\(PostgreSQL\) (\d+)", vout).group(1))
            if local_major != man["server_version_num"] // 10000:
                raise BackupError("version", f"restore binaries are PG{local_major}, "
                                             f"backup is PG{man['server_version_num'] // 10000}")
            stage = "preflight"
            crit = {qualify(t) for t in cfg["critical_tables"]}
            missing = sorted(crit - set(man["tables"]))
            if missing:
                raise BackupError("preflight", f"critical tables not in backup: {missing}")
            if mode == "full":
                need = int(man["db_size_bytes"] * 1.15) + int(cfg["restore_extra_bytes"])
            else:
                need = 2 * sum(man["tables"][t]["total_bytes"] for t in crit) \
                    + int(cfg["restore_extra_bytes"])
            du = shutil.disk_usage(d)
            ok, space = space_check(du.total, du.used, du.free, need, float(cfg["max_used_pct"]),
                                    int(cfg["min_free_bytes"]))
            log(f"preflight: need~{need >> 20} MiB, used after {space['used_pct_after']}%")
            if not ok:
                raise BackupError("preflight", f"insufficient_space for a {mode} restore: "
                                               f"{json.dumps(space)}")
            t0 = time.monotonic()
            with ScratchCluster(cfg, man) as sc:
                spg = sc.pg
                stage = "restore-globals"
                cp = spg.run([spg.tool("psql"), "-X", "-q", *spg.conn_args("postgres"),
                              "-f", os.path.join(d, man["globals"]["file"])],
                             "restore-globals", check=False)
                have = {r[0] for r in spg.query("select rolname from pg_roles", "roles", "postgres")}
                lost = sorted(set(man["roles"]) - have)
                if lost:
                    raise BackupError("restore-globals", f"roles not recreated: {lost} "
                                                         f"({cp.stderr.strip()[-300:]})")
                spg.query(f"create database {man['database']} template template0", "createdb",
                          "postgres")
                rargs = [spg.tool("pg_restore"), "--exit-on-error", *spg.conn_args()]
                if mode == "full":
                    stage = "restore"
                    spg.run(rargs + [dump], "restore")
                    loaded = set(man["tables"])
                else:
                    stage = "restore-schema"
                    spg.run(rargs + ["--section=pre-data", dump], "restore-schema")
                    loaded = fk_closure(man["fk_edges"], crit)
                    big = sum(man["tables"][t]["total_bytes"] for t in loaded if t in man["tables"])
                    if 2 * big + int(cfg["restore_extra_bytes"]) > need:
                        raise BackupError("restore-schema",
                                          f"FK closure of critical tables grew to {len(loaded)} "
                                          f"tables ({big >> 20} MiB); raise the budget or the list")
                    stage = "restore-data"
                    targs = []
                    for t in sorted(loaded):
                        targs += ["-t", t.split(".", 1)[1]]
                    spg.run(rargs + ["--section=data", *targs, dump], "restore-data")
                    stage = "restore-post"
                    spg.run(rargs + ["--section=post-data", dump], "restore-post")
                stage = "compare"
                res = compare(man, lambda sql: spg.query(sql, "compare"), loaded)
            dur = round(time.monotonic() - t0, 1)
            ok = not res["mismatches"]
            status = {"ok": ok, "mode": mode, "started_at": iso(started), "completed_at": iso(utcnow()),
                      "dump": dump, "backup": man["name"], "backup_completed_at": man["completed_at"],
                      "sha256": man["dump"]["sha256"], "duration_s": dur, **res,
                      "detail": ("restored and matched the snapshot" if ok
                                 else "; ".join(res["mismatches"])[:500])}
            write_status(cfg, "restore", stamp, status)
            log(f"restore-test {'PASSED' if ok else 'FAILED'} ({mode}) in {dur}s: "
                f"{res['tables_count_checked']} tables / {res['rows_checked']} rows counted, "
                f"{res['fingerprints_checked']} fingerprints; mismatches: {res['mismatches'] or 'none'}")
            return EXIT_OK if ok else EXIT_FAIL
    except Exception as e:  # noqa: BLE001
        err = e.msg if isinstance(e, BackupError) else f"{type(e).__name__}: {e}"
        st = e.stage if isinstance(e, BackupError) else stage
        log(f"RESTORE TEST FAILED at {st}: {err}")
        try:
            write_status(cfg, "restore", stamp, {
                "ok": False, "mode": mode, "started_at": iso(started), "completed_at": iso(utcnow()),
                "stage": st, "detail": str(err)[:1000],
                "dump": os.path.join(d, man["dump"]["file"]) if man else None,
                "backup": man["name"] if man else None})
        except Exception as e2:  # noqa: BLE001
            log(f"could not write failure status: {e2}")
        return EXIT_CONFIG if st == "config" else EXIT_FAIL


# ------------------------------------------------------------------------------ CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=os.environ.get("LAYNES_BACKUP_CONFIG"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("backup")
    rt = sub.add_parser("restore-test")
    rt.add_argument("--mode", choices=("critical", "full"))
    rt.add_argument("--backup", help="backup name (default: newest)")
    v = sub.add_parser("verify")
    v.add_argument("name")
    sub.add_parser("retention")
    sub.add_parser("list")
    a = ap.parse_args(argv)
    try:
        cfg = load_config(a.config)
    except (OSError, ValueError) as e:
        log(f"config error: {e}")
        return EXIT_CONFIG
    if a.cmd == "backup":
        return do_backup(cfg)
    if a.cmd == "restore-test":
        return do_restore_test(cfg, a.mode, a.backup)
    try:
        check_backup_dir(cfg["backup_dir"])
        if a.cmd == "list":
            for n, m in sorted(complete_backups(cfg["backup_dir"]).items()):
                print(f"{n}  {m['dump']['bytes'] >> 20} MiB  {m['completed_at']}  "
                      f"sha256 {m['dump']['sha256'][:16]}")
        elif a.cmd == "retention":
            print(json.dumps(apply_retention(cfg, utcnow(), dry_run=True), indent=2))
        elif a.cmd == "verify":
            man = complete_backups(cfg["backup_dir"]).get(a.name)
            if not man:
                raise BackupError("verify", f"no complete backup named {a.name}")
            verify_checksums(cfg["backup_dir"], man)
            r = verify_archive(Pg(cfg), os.path.join(cfg["backup_dir"], man["dump"]["file"]),
                               set(man["tables"]))
            print(json.dumps({"name": a.name, "checksums": "ok", **r}, indent=2))
    except BackupError as e:
        log(str(e))
        return EXIT_FAIL
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
