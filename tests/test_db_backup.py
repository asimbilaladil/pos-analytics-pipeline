#!/usr/bin/env python3
"""Tests for backup/laynes_db_backup.py and its systemd units.

Three layers:
  1. PURE (always): retention plan, disk guard, TOC parsing, identifier validation,
     path safety (retention can only ever delete this tool's own files), config.
  2. STATIC (always): units run as postgres with no credential anywhere, sandboxed,
     /root invisible, schedule, tmpfiles prerequisite.
  3. LIVE (root + PostgreSQL 18 binaries + LAYNES_BACKUP_LIVE_TEST=1): a THROWAWAY
     source cluster in /var/lib (UNIX socket only, its own port) with synthetic data.
     Backup under concurrent writes -> full and critical restore tests match the
     snapshot exactly; corruption, a dead server, a held lock and a full disk all
     fail loudly with a status file and leave no partial files; foreign files in the
     backup directory survive retention; the real unit's sandbox runs the job via
     systemd-run. Never touches the production cluster, /var/backups/laynes or any
     installed unit.

Run:  python3 tests/test_db_backup.py
      LAYNES_BACKUP_LIVE_TEST=1 python3 tests/test_db_backup.py      (as root)
"""
import fcntl
import glob
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "backup"))
import laynes_db_backup as B  # noqa: E402

SYSTEMD = os.path.join(ROOT, "systemd")
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {str(detail)[:500]}" if not ok and detail else ""))


def skip(name, why):
    print(f"  [SKIP] {name} -- {why}")


def raises(fn, exc=B.BackupError):
    try:
        fn()
    except exc:
        return True
    except Exception:  # noqa: BLE001
        return False
    return False


UTC = timezone.utc
# ------------------------------------------------------------------------------ 1
print("== pure: retention")
base = datetime(2026, 11, 26, 8, 30, tzinfo=UTC)
daily = [base - timedelta(days=i) for i in range(60)]
keep = B.plan_retention(daily, 7, 4)
exp_weeks = []
for x in daily:                                   # newest first
    w = tuple(x.isocalendar())[:2]
    if w not in [tuple(y.isocalendar())[:2] for y in exp_weeks] and len(exp_weeks) < 4:
        exp_weeks.append(x)
check("7 daily + 4 weekly: the 7 newest nights + newest of each of the 4 newest ISO weeks",
      keep == set(daily[:7]) | set(exp_weeks) and 7 < len(keep) <= 11, sorted(keep))
check("weekly copies span 4 ISO weeks incl. the current one (restic --keep-weekly semantics)",
      daily[0] - timedelta(days=28) < min(keep) <= daily[0] - timedelta(days=14), min(keep))
check("newest is always kept, even with keep_daily=1 keep_weekly=0",
      B.plan_retention(daily, 1, 0) == {daily[0]})
same_day = [base, base - timedelta(hours=1), base - timedelta(hours=2)]
check("several backups on one day count as ONE daily (newest kept)",
      B.plan_retention(same_day, 2, 0) == {base})
check("empty input -> nothing", B.plan_retention([], 7, 4) == set())

print("== pure: disk guard (same definition as the monitor: used/total)")
G = 1024 ** 3
ok, det = B.space_check(40 * G, 31 * G, 7 * G, 2 * G, 84.0, 2 * G)
check("31+2 of 40 GB = 82.5% <= 84 and 5 GB free -> ok", ok and det["used_pct_after"] == 82.5, det)
check("would cross 84% -> refused", not B.space_check(40 * G, 33 * G, 7 * G, 1 * G, 84.0, 0)[0])
check("would leave < min_free -> refused", not B.space_check(40 * G, 10 * G, 3 * G, 2 * G, 99.0, 2 * G)[0])

print("== pure: TOC parsing, identifiers")
toc = B.parse_toc(";\n; Archive created\n215; 1259 16390 TABLE public orders laynes_user\n"
                  "3456; 0 16390 TABLE DATA public orders laynes_user\n"
                  "3457; 0 16400 TABLE DATA public hme_goal_history postgres\n"
                  "3458; 2606 1 CONSTRAINT public orders orders_pkey laynes_user\n")
check("TOC: entries and TABLE DATA set", toc["entries"] == 4 and
      toc["table_data"] == {"public.orders", "public.hme_goal_history"}, toc)
check("qualify(): plain name -> public.name", B.qualify("hme_goal_history") == "public.hme_goal_history")
for bad in ("x; drop table y", "a b", "X", "t--", "\"t\"", "a.b.c", ""):
    check(f"qualify() rejects {bad!r}", raises(lambda b=bad: B.qualify(b)))
check("fk_closure follows FK parents transitively",
      B.fk_closure([["public.a", "public.b"], ["public.b", "public.c"], ["public.x", "public.a"]],
                   {"public.a"}) == {"public.a", "public.b", "public.c"})

print("== pure: config")
with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
    f.write('keep_daly = 3\n')
check("unknown config key rejected (typo cannot silently change retention)",
      raises(lambda: B.load_config(f.name), ValueError))
os.unlink(f.name)
with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
    f.write('keep_daily = 0\n')
check("keep_daily = 0 rejected", raises(lambda: B.load_config(f.name), ValueError))
os.unlink(f.name)
check("defaults: local 2 daily, critical restore, zstd, 84% guard",
      B.DEFAULTS["keep_daily"] == 2 and B.DEFAULTS["restore_mode"] == "critical" and
      B.DEFAULTS["compress"].startswith("zstd") and B.DEFAULTS["max_used_pct"] < 85)

print("== pure: path safety -- retention can only delete this tool's own files")
for bad in ("/root/deploy-backups", "/root/x", "/", "relative/dir", "/var/backups/laynes/",
            "/var/backups/../root"):
    check(f"backup_dir {bad!r} refused", raises(lambda b=bad: B.check_backup_dir(b)))
with tempfile.TemporaryDirectory() as t:
    os.chmod(t, 0o755)
    check("backup_dir with group/other access refused", raises(lambda: B.check_backup_dir(t)))
    os.chmod(t, 0o700)
    B.check_backup_dir(os.path.realpath(t))
    link = t + "-link"
    os.symlink(t, link)
    check("symlinked backup_dir refused", raises(lambda: B.check_backup_dir(link)))
    os.unlink(link)
for bad in ("laynes-full.dump", "../laynes-20261101T000000Z.dump", "r365-financial-before.dump",
            "laynes-20261101T000000Z.dump/../../x", "status/../x.json", "hme-fp-before.txt"):
    check(f"safe_unlink refuses {bad!r}", raises(lambda b=bad: B.safe_unlink("/var/backups/laynes", b)))

with tempfile.TemporaryDirectory() as t:
    d = os.path.realpath(t)
    os.chmod(d, 0o700)
    os.makedirs(os.path.join(d, "status"))
    outside = tempfile.NamedTemporaryFile(delete=False)
    outside.write(b"precious")
    outside.close()

    def mkset(stamp, manifest=True, tool=B.TOOL):
        n = f"laynes-{stamp}"
        for suf in (".dump", ".globals.sql"):
            open(os.path.join(d, n + suf), "w").write("x")
        if manifest:
            json.dump({"tool": tool, "name": n, "dump": {"bytes": 1}},
                      open(os.path.join(d, n + ".manifest.json"), "w"))
    stamps = [(base - timedelta(days=i)).strftime(B.STAMP) for i in range(5)]
    for s in stamps:
        mkset(s)
    mkset("20260101T000000Z", tool="something-else")          # not ours -> never deleted
    foreign = ["laynes-full.dump", "r365-financial-before.dump", "notes.txt", "laynes.dump"]
    for fn in foreign:
        open(os.path.join(d, fn), "w").write("foreign")
    os.symlink(outside.name, os.path.join(d, "laynes-20250101T000000Z.dump"))
    old = (base - timedelta(days=3)).timestamp()
    os.utime(os.path.join(d, "laynes-20250101T000000Z.dump"), (old, old), follow_symlinks=False)
    for s in ("20200101T000000Z", "20200102T000000Z"):
        open(os.path.join(d, "status", f"backup-{s}-1.json"), "w").write("{}")
    open(os.path.join(d, "status", "backup-20261126T083000Z-7.json"), "w").write("{}")
    open(os.path.join(d, "status", "restore-20200101T000000Z-9.json"), "w").write("{}")
    cfg = {**B.DEFAULTS, "backup_dir": d, "keep_daily": 2, "keep_weekly": 0}
    plan = B.apply_retention(cfg, base, dry_run=True)
    check("dry-run deletes nothing", len(os.listdir(d)) == 5 * 3 + 3 + len(foreign) + 2)
    r = B.apply_retention(cfg, base)
    left = set(os.listdir(d))
    check("keeps the 2 newest days", {f"laynes-{s}.dump" for s in stamps[:2]} <= left and
          not any(f"laynes-{s}.dump" in left for s in stamps[2:]), sorted(left))
    check("foreign files untouched", all(fn in left for fn in foreign), sorted(left))
    check("set with another tool's manifest untouched", "laynes-20260101T000000Z.dump" in left)
    check("symlink named like a backup: not followed, target intact",
          open(outside.name).read() == "precious")
    st = set(os.listdir(os.path.join(d, "status")))
    check("old status files pruned; newest of each kind kept even if old",
          st == {"backup-20261126T083000Z-7.json", "restore-20200101T000000Z-9.json"}, st)
    check("plan and real run agree", plan["deleted_sets"] == r["deleted_sets"])
    os.unlink(outside.name)

# ------------------------------------------------------------------------------ 2
print("== pure: run trigger (scheduled ONLY when systemd says a timer started it)")
T = B.trigger_info
check("timer-started service -> scheduled, unit + elapse recorded",
      T({"INVOCATION_ID": "x", "TRIGGER_UNIT": "laynes-db-backup.timer",
         "TRIGGER_TIMER_REALTIME_USEC": "1795790931588590"})
      == {"kind": "scheduled", "trigger": {"by": "timer", "unit": "laynes-db-backup.timer",
                                           "timer_realtime_usec": "1795790931588590"}})
check("`systemctl start` (manual validation run) -> manual, by=systemd",
      T({"INVOCATION_ID": "x"}) == {"kind": "manual", "trigger": {"by": "systemd", "unit": None,
                                                                   "timer_realtime_usec": None}})
check("shell run -> manual, by=shell", T({})["kind"] == "manual" and T({})["trigger"]["by"] == "shell")
check("a TRIGGER_UNIT typed in a shell (no INVOCATION_ID) is NOT scheduled",
      T({"TRIGGER_UNIT": "laynes-db-backup.timer"})["kind"] == "manual")
check("started by a non-timer unit (path/socket/dependency) -> manual, unit kept",
      T({"INVOCATION_ID": "x", "TRIGGER_UNIT": "foo.path"}) == {
          "kind": "manual", "trigger": {"by": "systemd", "unit": "foo.path", "timer_realtime_usec": None}})
src = open(os.path.join(ROOT, "backup", "laynes_db_backup.py")).read()
check("no status/manifest hard-codes kind=scheduled any more",
      '"kind": "scheduled",' not in src and src.count("**trigger_info()") == 2)
with tempfile.TemporaryDirectory() as t:
    os.chmod(t, 0o700)
    old_env = {k: os.environ.pop(k, None) for k in ("TRIGGER_UNIT", "INVOCATION_ID")}
    try:
        sp = B.write_status({"backup_dir": t}, "backup", "20261127T083113Z", {"ok": True, "kind": "scheduled"})
        st = json.load(open(sp))
    finally:
        for k, v in old_env.items():
            if v is not None:
                os.environ[k] = v
    check("write_status: the recorded trigger overrides any kind the caller passed",
          st["kind"] == "manual" and st["trigger"]["by"] == "shell", st)

print("== static: units")


def parse_unit(path):
    out, sec = {}, None
    for raw in open(path):
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("["):
            sec = line[1:-1]
            out.setdefault(sec, [])
            continue
        k, _, v = line.partition("=")
        out[sec].append((k, v))
    return out


def vals(sec, k):
    return [v for kk, v in sec if kk == k]


for unit, verb in (("laynes-db-backup.service", "backup"),
                   ("laynes-db-restore-test.service", "restore-test")):
    s = parse_unit(os.path.join(SYSTEMD, unit))["Service"]
    text = open(os.path.join(SYSTEMD, unit)).read()
    check(f"{unit}: runs as postgres (peer auth, no password)",
          vals(s, "User") == ["postgres"] and vals(s, "Group") == ["postgres"])
    check(f"{unit}: no EnvironmentFile, no password/secret in Environment",
          not vals(s, "EnvironmentFile") and
          all(not any(x in v.upper() for x in ("PASS", "SECRET", "TOKEN", "KEY")) for v in vals(s, "Environment")))
    check(f"{unit}: ExecStart = system python3 -I, installed copy, `{verb}`",
          vals(s, "ExecStart") == [f"/usr/bin/python3 -I /usr/local/lib/laynes-backup/laynes_db_backup.py {verb}"])
    check(f"{unit}: writes ONLY /var/backups/laynes; /root invisible",
          vals(s, "ReadWritePaths") == ["/var/backups/laynes"] and vals(s, "ProtectSystem") == ["strict"]
          and vals(s, "ProtectHome") == ["yes"])
    check(f"{unit}: UNIX sockets only, NoNewPrivileges, PrivateTmp",
          vals(s, "RestrictAddressFamilies") == ["AF_UNIX"] and vals(s, "NoNewPrivileges") == ["yes"]
          and vals(s, "PrivateTmp") == ["yes"])
    check(f"{unit}: resource limits (idle I/O, nice, one core, memory cap)",
          vals(s, "IOSchedulingClass") == ["idle"] and vals(s, "Nice") == ["10"]
          and vals(s, "CPUQuota") == ["100%"] and vals(s, "MemoryMax"))
    check(f"{unit}: logs to the journal (no append: file to pre-create)",
          vals(s, "StandardOutput") == ["journal"])
    check(f"{unit}: no ExecStartPre", not vals(s, "ExecStartPre"))
    check(f"{unit}: names the tmpfiles prerequisite", "laynes-db-backup.tmpfiles.conf" in text)
t = parse_unit(os.path.join(SYSTEMD, "laynes-db-backup.timer"))["Timer"]
check("backup timer: 02:30 America/Chicago, no catch-up",
      vals(t, "OnCalendar") == ["*-*-* 02:30:00 America/Chicago"] and vals(t, "Persistent") == ["false"])
t = parse_unit(os.path.join(SYSTEMD, "laynes-db-restore-test.timer"))["Timer"]
check("restore-test timer: weekly Sunday 00:00 America/Chicago",
      vals(t, "OnCalendar") == ["Sun *-*-* 00:00:00 America/Chicago"])
rows = [ln.split() for ln in open(os.path.join(SYSTEMD, "laynes-db-backup.tmpfiles.conf"))
        if ln.strip() and not ln.startswith("#")]
check("tmpfiles: /var/backups/laynes{,/status} 0700 postgres, never aged",
      rows == [["d", "/var/backups/laynes", "0700", "postgres", "postgres", "-"],
               ["d", "/var/backups/laynes/status", "0700", "postgres", "postgres", "-"]], rows)
ex = B.load_config(os.path.join(ROOT, "backup", "backup.example.toml"))
check("example config parses and matches the defaults it documents",
      ex["backup_dir"] == "/var/backups/laynes" and ex["keep_daily"] == B.DEFAULTS["keep_daily"])
sa = shutil.which("systemd-analyze")
if sa:
    for cal in ("*-*-* 02:30:00 America/Chicago", "Sun *-*-* 00:00:00 America/Chicago"):
        cp = subprocess.run([sa, "calendar", cal], capture_output=True, text=True)
        check(f"systemd-analyze calendar accepts {cal!r}", cp.returncode == 0, cp.stderr)

# ------------------------------------------------------------------------------ 3
print("== live: throwaway cluster (never production)")
PGBIN = B.DEFAULTS["pg_bin"]
live = (os.environ.get("LAYNES_BACKUP_LIVE_TEST") == "1" and os.geteuid() == 0
        and os.path.exists(os.path.join(PGBIN, "initdb")))
if not live:
    skip("live", "set LAYNES_BACKUP_LIVE_TEST=1 and run as root with PostgreSQL 18 binaries")
else:
    tag = uuid.uuid4().hex[:8]
    W = f"/var/lib/laynes-backup-test-{tag}"   # not /var/tmp: PrivateTmp hides it
    assert W.startswith("/var/lib/laynes-backup-test-") and not os.path.exists(W)
    SRC, BK, BIN = f"{W}/src", f"{W}/backups", f"{W}/bin"
    SPORT, RPORT = 54388, 54387
    pgu = pwd.getpwnam("postgres")

    def as_pg(argv, **kw):
        return subprocess.run(["runuser", "-u", "postgres", "--", *argv], capture_output=True,
                              text=True, cwd="/", **kw)

    def sql(q, db="laynes"):
        return as_pg([f"{PGBIN}/psql", "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", "-h", SRC,
                      "-p", str(SPORT), "-d", db, "-c", q])

    def tool(*a, cfg=None):
        return as_pg(["/usr/bin/python3", "-I", f"{BIN}/laynes_db_backup.py", "--config",
                      cfg or f"{W}/backup.toml", *a])

    def statuses(kind):
        return [json.load(open(p)) for p in sorted(glob.glob(f"{BK}/status/{kind}-*.json"), key=lambda p: (json.load(open(p))["started_at"], os.path.getmtime(p)))]

    def write_cfg(path, **over):
        c = {"backup_dir": BK, "pg_host": SRC, "pg_port": SPORT, "restore_port": RPORT,
             "max_used_pct": 99.0, "min_free_bytes": 0, "lock_wait_minutes": 0.05,
             "critical_tables": ["establishments", "hme_store_mapping", "hme_goal_history"],
             "fingerprint_tables": ["establishments", "hme_store_mapping", "hme_goal_history"], **over}
        with open(path, "w") as fh:
            for k, v in c.items():
                fh.write(f"{k} = {json.dumps(v)}\n")
        os.chown(path, pgu.pw_uid, pgu.pw_gid)

    try:
        for p in (W, SRC, BK, BIN):
            os.makedirs(p, mode=0o700)
            os.chown(p, pgu.pw_uid, pgu.pw_gid)
        shutil.copy(os.path.join(ROOT, "backup", "laynes_db_backup.py"), BIN)
        os.chmod(f"{BIN}/laynes_db_backup.py", 0o755)
        write_cfg(f"{W}/backup.toml")
        as_pg([f"{PGBIN}/initdb", "-D", f"{SRC}/data", "-U", "postgres", "-A", "trust", "--no-sync",
               "-E", "UTF8"])
        cp = as_pg([f"{PGBIN}/pg_ctl", "-D", f"{SRC}/data", "-l", f"{SRC}/log", "-w", "-o",
                    f"-p {SPORT} -c listen_addresses='' -c unix_socket_directories={SRC} "
                    "-c shared_buffers=32MB", "start"])
        check("throwaway source cluster started (UNIX socket only)", cp.returncode == 0, cp.stderr)
        sql("create role laynes_user login", "postgres")
        sql("create role laynes_ro login", "postgres")
        sql("create database laynes owner laynes_user", "postgres")
        cp = sql("""
create extension citext;
create table establishments(id int primary key, name citext);
insert into establishments select g, 'Store '||g from generate_series(1,40) g;
create table hme_store_mapping(store text primary key, establishment_id int references establishments);
insert into hme_store_mapping values ('1',40),('115',15);
create table hme_goal_history(store text references hme_store_mapping, valid_from date, goal_a int,
  note text, primary key(store, valid_from));
insert into hme_goal_history values ('1','2026-01-01',180,'tab	and ''quote'''),('115','2026-01-01',200,null);
create table orders(id bigint, establishment_id int references establishments, created_at timestamptz,
  total numeric(10,2), note text) partition by range(created_at);
create table orders_2026_11 partition of orders for values from ('2026-11-01') to ('2026-12-01');
create table orders_2026_12 partition of orders for values from ('2026-12-01') to ('2027-01-01');
insert into orders select g, 1+g%40, '2026-11-01'::timestamptz + g*interval '20 seconds', (g%997)/7.0,
  repeat('x', g%50) from generate_series(1,120000) g;
create table big_unrelated(id bigint primary key, payload text);
insert into big_unrelated select g, md5(g::text) from generate_series(1,50000) g;
create view v_store as select e.id, count(o.*) n from establishments e
  left join orders o on o.establishment_id=e.id group by 1;
create materialized view mv_store as select * from v_store;
alter table orders owner to laynes_user;
grant select on all tables in schema public to laynes_ro;""")
        check("synthetic schema: partitions, FKs, citext, view, matview, 2 roles", cp.returncode == 0, cp.stderr)

        # A. backup while another session writes -> restore equals the snapshot
        writer = subprocess.Popen(
            ["runuser", "-u", "postgres", "--", f"{PGBIN}/psql", "-X", "-q", "-h", SRC, "-p", str(SPORT),
             "-d", "laynes"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, text=True, cwd="/")
        writer.stdin.write("".join(
            f"insert into orders select 1000000+{i}*100+g, 1, '2026-12-02', 1, 'w' from generate_series(1,100) g;"
            f"insert into hme_goal_history values ('1', date '2026-02-01' + {i}, {i}, 'w');\n"
            for i in range(3000)))
        writer.stdin.flush()
        cp = tool("backup")
        writer.stdin.close()
        writer.wait(timeout=300)
        check("backup exit 0 (concurrent writer running)", cp.returncode == 0, cp.stdout + cp.stderr)
        mans = sorted(glob.glob(f"{BK}/laynes-*.manifest.json"))
        man = json.load(open(mans[-1]))
        dump = f"{BK}/{man['dump']['file']}"
        check("dump, globals and manifest present; files 0600 postgres; no .partial left",
              all(os.stat(f"{BK}/{man[k]['file']}").st_mode & 0o777 == 0o600 for k in ("dump", "globals"))
              and not glob.glob(f"{BK}/*.partial") and os.stat(dump).st_uid == pgu.pw_uid)
        check("manifest sha256 matches the dump", B.sha256_file(dump) == man["dump"]["sha256"])
        check("manifest: every leaf table counted, partitions included",
              {"public.orders_2026_11", "public.orders_2026_12", "public.hme_goal_history"} <= set(man["tables"])
              and "public.orders" not in man["tables"])
        check("archive verified: TOC + full decode + every table has TABLE DATA",
              man["verify"]["full_decode"] and man["verify"]["table_data_entries"] >= len(man["tables"]))
        gl = open(f"{BK}/{man['globals']['file']}").read()
        check("globals: roles present, NO password hashes", "laynes_ro" in gl and "SCRAM" not in gl
              and "PASSWORD" not in gl.upper().replace("NOPASSWORD", ""))
        st = statuses("backup")[-1]
        check("status: ok, dump path, size, sha256; a shell run is labelled MANUAL (by=shell)",
              st["ok"] is True and st["kind"] == "manual" and st["trigger"]["by"] == "shell"
              and st["dump"] == dump and st["bytes"] == man["dump"]["bytes"], st.get("trigger"))
        check("manifest records the same trigger as the status",
              man["kind"] == "manual" and man["trigger"]["by"] == "shell", man.get("trigger"))
        live_rows = int(sql("select count(*) from orders").stdout.strip())
        check("the writer kept writing after the snapshot (test is meaningful)",
              live_rows > man["tables"]["public.orders_2026_11"]["rows"] +
              man["tables"]["public.orders_2026_12"]["rows"], live_rows)

        cp = tool("restore-test", "--mode", "full")
        rs = statuses("restore")[-1]
        check("FULL restore test passes: every table's row count + fingerprints == snapshot",
              cp.returncode == 0 and rs["ok"] and rs["mode"] == "full" and not rs["mismatches"]
              and rs["tables_count_checked"] == len(man["tables"]), cp.stdout + cp.stderr)
        cp = tool("restore-test", "--mode", "critical")
        rs = statuses("restore")[-1]
        check("CRITICAL restore test passes (critical tables + FK parents, schema complete)",
              cp.returncode == 0 and rs["ok"] and rs["mode"] == "critical" and rs["tables_count_checked"] == 3
              and rs["fingerprints_checked"] == 3 and rs["tables_not_loaded"] > 0, cp.stdout + cp.stderr)
        check("restore scratch cluster removed afterwards", not os.path.exists(f"{BK}/restore-scratch"))
        check("source cluster untouched by the restore test (still has the writer's rows)",
              int(sql("select count(*) from orders").stdout.strip()) == live_rows)

        # B. a fingerprint mismatch is detected (tamper with the manifest's expectation)
        tam = dict(man)
        tam["fingerprints"] = {**man["fingerprints"],
                               "public.hme_goal_history": {"rows": 1, "md5": "0" * 32}}
        B.write_json_atomic(mans[-1], tam)
        os.chown(mans[-1], pgu.pw_uid, pgu.pw_gid)
        cp = tool("restore-test", "--mode", "critical")
        rs = statuses("restore")[-1]
        check("content mismatch -> restore test FAILS (exit 1, ok=false, names the table)",
              cp.returncode == 1 and rs["ok"] is False and "hme_goal_history" in rs["detail"], rs)
        B.write_json_atomic(mans[-1], man)
        os.chown(mans[-1], pgu.pw_uid, pgu.pw_gid)

        # C. corruption: one flipped byte in the middle of the dump
        with open(dump, "r+b") as fh:
            fh.seek(man["dump"]["bytes"] // 2)
            b = fh.read(1)
            fh.seek(-1, 1)
            fh.write(bytes([b[0] ^ 0xFF]))
        cp = tool("verify", man["name"])
        check("verify: flipped byte -> checksum mismatch", cp.returncode == 1 and "sha256" in cp.stdout, cp.stdout)
        cp = tool("restore-test")
        rs = statuses("restore")[-1]
        check("restore-test refuses a corrupt dump (status ok=false, stage checksum)",
              cp.returncode == 1 and rs["ok"] is False and rs["stage"] == "checksum", rs)
        tr = f"{W}/truncated.dump"
        shutil.copy(dump, tr)
        os.truncate(tr, man["dump"]["bytes"] // 3)
        os.chown(tr, pgu.pw_uid, pgu.pw_gid)
        cp = as_pg([f"{PGBIN}/pg_restore", "-f", "/dev/null", tr])
        check("full decode (pg_restore -f /dev/null) catches a truncated archive", cp.returncode != 0)
        os.unlink(dump)                       # the corrupt one goes; the next backup replaces it
        for suf in (".manifest.json", ".globals.sql"):
            os.unlink(f"{BK}/{man['name']}{suf}")

        # D. foreign files in the backup dir survive retention
        for fn in ("laynes-full.dump", "manual-before-deploy.dump"):
            open(f"{BK}/{fn}", "w").write("operator file")
            os.chown(f"{BK}/{fn}", pgu.pw_uid, pgu.pw_gid)
        cp1, cp2 = tool("backup"), tool("backup")
        check("two more backups succeed", cp1.returncode == 0 and cp2.returncode == 0, cp1.stdout + cp2.stdout)
        check("same-day backups: newest kept, older one rotated",
              len(glob.glob(f"{BK}/laynes-*.manifest.json")) == 1)
        check("operator files in the backup dir untouched by retention",
              os.path.exists(f"{BK}/laynes-full.dump") and os.path.exists(f"{BK}/manual-before-deploy.dump"))
        good = json.load(open(glob.glob(f"{BK}/laynes-*.manifest.json")[0]))

        # E. failures leave a status, no partial files, and the last good backup intact
        def failure_case(label, stage, cfg=None, before=None, after=None):
            if before:
                before()
            n_before = len(statuses("backup"))
            cp = tool("backup", cfg=cfg)
            if after:
                after()
            st = statuses("backup")[-1]
            check(f"{label}: exit != 0, status ok=false stage={stage}",
                  cp.returncode != 0 and len(statuses("backup")) == n_before + 1 and st["ok"] is False
                  and st["stage"] == stage, (cp.returncode, st))
            check(f"{label}: no partial files, last good backup intact",
                  not glob.glob(f"{BK}/*.partial") and
                  B.sha256_file(f"{BK}/{good['dump']['file']}") == good["dump"]["sha256"])

        write_cfg(f"{W}/full.toml", max_used_pct=1.0)
        failure_case("disk guard", "preflight", cfg=f"{W}/full.toml")
        holder = {}

        def hold():
            holder["fh"] = open(f"{BK}/.lock", "a")
            fcntl.flock(holder["fh"], fcntl.LOCK_EX)

        def release():
            fcntl.flock(holder["fh"], fcntl.LOCK_UN)
            holder["fh"].close()
        failure_case("lock held by another job", "lock", before=hold, after=release)
        write_cfg(f"{W}/nodb.toml", database="no_such_db")
        failure_case("database unreachable", "snapshot", cfg=f"{W}/nodb.toml")
        write_cfg(f"{W}/badlock.toml", lock_wait_timeout="1s")
        blocker = subprocess.Popen(
            ["runuser", "-u", "postgres", "--", f"{PGBIN}/psql", "-X", "-q", "-h", SRC, "-p", str(SPORT),
             "-d", "laynes"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            text=True, cwd="/")
        blocker.stdin.write("begin; lock table big_unrelated in access exclusive mode; select pg_sleep(30);\n")
        blocker.stdin.flush()
        import time
        time.sleep(1)
        failure_case("table locked by DDL -> pg_dump gives up (lock-wait-timeout)", "pg_dump",
                     cfg=f"{W}/badlock.toml")
        sql("select pg_terminate_backend(pid) from pg_stat_activity "
            "where query like '%pg_sleep(30)%' and pid <> pg_backend_pid()")
        blocker.kill()
        blocker.wait()

        # F. the real unit's sandbox (paths moved to the test dir) runs a backup
        if shutil.which("systemd-run") and os.path.isdir("/run/systemd/system"):
            s = parse_unit(os.path.join(SYSTEMD, "laynes-db-backup.service"))["Service"]
            keep = ("User", "Group", "UMask", "Nice", "IOSchedulingClass", "CPUQuota", "MemoryMax",
                    "NoNewPrivileges", "PrivateTmp", "ProtectSystem", "ProtectHome", "PrivateDevices",
                    "ProtectKernelTunables", "ProtectKernelModules", "ProtectControlGroups",
                    "RestrictAddressFamilies", "RestrictSUIDSGID", "LockPersonality")
            props = [f"{k}={v}" for k, v in s if k in keep] + [f"ReadWritePaths={BK}", f"ReadOnlyPaths={W}"]
            n_before = len(statuses("backup"))
            argv = ["systemd-run", "--quiet", "--wait", "--collect", f"--unit=laynes-db-backup-test-{tag}"]
            for p in props:
                argv += ["-p", p]
            argv += ["/usr/bin/python3", "-I", f"{BIN}/laynes_db_backup.py", "--config", f"{W}/backup.toml",
                     "backup"]
            cp = subprocess.run(argv, capture_output=True, text=True, timeout=600)
            check("backup inside the real unit's sandbox: exit 0, status ok",
                  cp.returncode == 0 and len(statuses("backup")) == n_before + 1 and statuses("backup")[-1]["ok"],
                  cp.stderr)
            st = statuses("backup")[-1]
            check("a direct `systemctl start`-style run (no timer) is MANUAL (by=systemd), never scheduled",
                  st["kind"] == "manual" and st["trigger"] == {"by": "systemd", "unit": None,
                                                               "timer_realtime_usec": None}, st.get("trigger"))
            # the same sandboxed command, started by a TIMER -> systemd sets TRIGGER_UNIT
            n_before = len(statuses("backup"))
            targv = [a for a in argv if a not in ("--wait",)]
            targv[targv.index(f"--unit=laynes-db-backup-test-{tag}")] = f"--unit=laynes-db-backup-ttest-{tag}"
            targv.insert(1, "--on-active=1")
            cp = subprocess.run(targv, capture_output=True, text=True, timeout=60)
            deadline = time.monotonic() + 600
            while len(statuses("backup")) == n_before and time.monotonic() < deadline:
                time.sleep(3)
            st = statuses("backup")[-1] if len(statuses("backup")) > n_before else {}
            check("a TIMER-started run is SCHEDULED with the timer unit recorded",
                  cp.returncode == 0 and st.get("ok") is True and st.get("kind") == "scheduled"
                  and st["trigger"]["by"] == "timer"
                  and st["trigger"]["unit"] == f"laynes-db-backup-ttest-{tag}.timer"
                  and str(st["trigger"]["timer_realtime_usec"]).isdigit(), (cp.stderr, st.get("trigger")))
            argv[argv.index(f"--unit=laynes-db-backup-test-{tag}")] = f"--unit=laynes-db-restore-test-{tag}"
            argv[-1] = "restore-test"
            argv[argv.index("MemoryMax=768M")] = "MemoryMax=1G"
            cp = subprocess.run(argv, capture_output=True, text=True, timeout=600)
            check("restore test inside the sandbox (scratch postgres, UNIX socket only): exit 0",
                  cp.returncode == 0 and statuses("restore")[-1]["ok"], cp.stderr)
        else:
            skip("sandbox", "needs systemd")
    finally:
        as_pg([f"{PGBIN}/pg_ctl", "-D", f"{SRC}/data", "-m", "immediate", "-w", "stop"])
        if os.path.exists(f"{BK}/restore-scratch/data/postmaster.pid"):
            as_pg([f"{PGBIN}/pg_ctl", "-D", f"{BK}/restore-scratch/data", "-m", "immediate", "-w", "stop"])
        if W.startswith("/var/lib/laynes-backup-test-"):
            shutil.rmtree(W, ignore_errors=True)
        check("throwaway clusters stopped and test dir removed", not os.path.exists(W))

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
