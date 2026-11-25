#!/usr/bin/env python3
"""Regression: r365-financial-ingest.service must be able to START when its log directory
does not exist yet.

Root cause (2026-11-25, systemd 259): StandardOutput=append:/var/lib/laynes/r365-financial/
logs/... is opened by systemd BEFORE StateDirectory= directories are created and before
ExecStartPre= runs. With logs/ absent, the unit fails with 209/STDOUT before any code runs.
The fix is an installation prerequisite: systemd/r365-financial-ingest.tmpfiles.conf
(installed as /etc/tmpfiles.d/laynes-r365-financial.conf) creates the directories at
install time and on every boot.

Three layers:
  1. STATIC (always): every directory the unit's append: outputs need is created by the
     tmpfiles file (0700 root, never aged out); the unit does not try to create it
     itself; resource limits, sandboxing, exit contract, lock path and schedule unchanged.
  2. TMPFILES (root + systemd-tmpfiles): applying the shipped file to an EMPTY --root
     creates state/ and logs/ 0700 root.
  3. LIVE (root + systemd PID 1 + R365_UNIT_LIVE_TEST=1): transient units built from the
     real unit's [Service] settings, with paths moved to a scratch /var/lib directory
     that starts ABSENT. The prerequisite applied -> exit 0 and the log line written.
     Without it -> fails (209 on systemd 259). ExecStartPre=mkdir is not a fix. Runs only
     /bin/sh echo; never the ingest, never the installed unit, no daemon-reload.

Run:  venv/bin/python tests/test_r365_financial_unit_startup.py      (exit 0 = pass)
      R365_UNIT_LIVE_TEST=1 python3 tests/test_r365_financial_unit_startup.py   (as root)
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SERVICE = os.path.join(ROOT, "systemd", "r365-financial-ingest.service")
TIMER = os.path.join(ROOT, "systemd", "r365-financial-ingest.timer")
TMPFILES = os.path.join(ROOT, "systemd", "r365-financial-ingest.tmpfiles.conf")
BASE = "/var/lib/laynes/r365-financial"
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {str(detail)[:400]}" if not ok and detail else ""))


def skip(name, why):
    print(f"  [SKIP] {name} -- {why}")


def parse_unit(path):
    """{section: [(key, value), ...]} keeping repeats and order (systemd semantics)."""
    out, sec = {}, None
    for raw in open(path):
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("[") and line.endswith("]"):
            sec = line[1:-1]
            out.setdefault(sec, [])
            continue
        k, _, v = line.partition("=")
        out[sec].append((k.strip(), v.strip()))
    return out


def values(sec, key):
    return [v for k, v in sec if k == key]


def parse_tmpfiles(path):
    rows = []
    for raw in open(path):
        line = raw.strip()
        if line and not line.startswith("#"):
            rows.append(line.split())
    return rows


def append_targets(svc):
    return [v.split(":", 1)[1] for k in ("StandardOutput", "StandardError")
            for v in values(svc, k) if v.startswith(("append:", "file:", "truncate:"))]


# ----------------------------------------------------------------------------- 1. static
print("== static: prerequisite covers every directory systemd opens before exec")
unit = parse_unit(SERVICE)
svc = unit["Service"]
tmp_rows = parse_tmpfiles(TMPFILES)
tmp_dirs = {r[1]: r for r in tmp_rows if r[0] == "d"}

targets = append_targets(svc)
check("unit appends stdout AND stderr to a file (logging preserved)", len(targets) == 2, targets)
for d in sorted({os.path.dirname(t) for t in targets}):
    row = tmp_dirs.get(d)
    check(f"tmpfiles creates {d}", row is not None, sorted(tmp_dirs))
    if row:
        check(f"{d}: mode 0700 root:root, never aged out",
              row[2:6] == ["0700", "root", "root", "-"], row)

lock = [v.split("=", 1)[1] for v in values(svc, "Environment") if v.startswith("R365_FIN_LOCK_PATH=")]
check("lock directory is also a prerequisite (0700 root)",
      lock and tmp_dirs.get(os.path.dirname(lock[0]), [None] * 6)[2:6] == ["0700", "root", "root", "-"], lock)
check("tmpfiles file only creates directories under the financial state root",
      all(r[0] == "d" and r[1].startswith(BASE + "/") for r in tmp_rows), tmp_rows)
check("no ExecStartPre (it runs after the output is opened, so it cannot create it)",
      not values(svc, "ExecStartPre"), values(svc, "ExecStartPre"))
check("unit comment points at the prerequisite",
      "r365-financial-ingest.tmpfiles.conf" in open(SERVICE).read())

EXPECTED = {
    "Type": ["oneshot"],
    "WorkingDirectory": ["/root/pos-analytics-pipeline-release"],
    "ExecStart": ["/root/pos-analytics-pipeline/venv/bin/python /root/pos-analytics-pipeline-release/r365_financial_ingest.py"],
    "StateDirectory": ["laynes/r365-financial/state laynes/r365-financial/logs"],
    "StateDirectoryMode": ["0700"],
    "TimeoutStartSec": ["1800"],
    "Nice": ["10"],
    "IOSchedulingClass": ["idle"],
    "MemoryMax": ["600M"],
    "NoNewPrivileges": ["yes"],
    "PrivateTmp": ["yes"],
    "SuccessExitStatus": ["0 75"],
    "StandardOutput": [f"append:{BASE}/logs/financial-ingest.service.log"],
    "StandardError": [f"append:{BASE}/logs/financial-ingest.service.log"],
    "Environment": ["R365_ENV_FILE=/etc/laynes/r365.env",
                    f"R365_FIN_RAW_DIR={BASE}/raw",
                    f"R365_FIN_LOCK_PATH={BASE}/state/ingest.lock"],
}
for k, want in EXPECTED.items():
    check(f"preserved: {k}", values(svc, k) == want, values(svc, k))
check("no EnvironmentFile= (credential never in systemctl show)", not values(svc, "EnvironmentFile"))
timer = parse_unit(TIMER)["Timer"]
check("schedule preserved: 07:00 America/Chicago, Persistent, 120 s spread",
      timer == [("OnCalendar", "*-*-* 07:00:00 America/Chicago"), ("Persistent", "true"),
                ("RandomizedDelaySec", "120")], timer)

# --------------------------------------------------------------------------- 2. tmpfiles
print("== tmpfiles: the shipped file builds the layout from nothing (isolated --root)")
TMPF = shutil.which("systemd-tmpfiles")
if not TMPF or os.geteuid() != 0:
    skip("tmpfiles --root", "needs root and systemd-tmpfiles")
else:
    with tempfile.TemporaryDirectory(prefix="r365-tmpfiles-") as r:
        p = subprocess.run([TMPF, "--create", f"--root={r}", TMPFILES], capture_output=True, text=True)
        check("systemd-tmpfiles --create exit 0", p.returncode == 0, p.stderr)
        for sub in ("state", "logs"):
            path = r + BASE + "/" + sub
            st = os.stat(path) if os.path.isdir(path) else None
            check(f"{sub}/ created 0700 root from an empty root",
                  st and (st.st_mode & 0o777) == 0o700 and st.st_uid == 0 and st.st_gid == 0,
                  st and oct(st.st_mode))
        # idempotent: a second apply (every boot) changes nothing and does not fail
        p2 = subprocess.run([TMPF, "--create", f"--root={r}", TMPFILES], capture_output=True, text=True)
        check("re-apply (every boot) is idempotent", p2.returncode == 0, p2.stderr)
        p3 = subprocess.run([TMPF, "--clean", f"--root={r}", TMPFILES], capture_output=True, text=True)
        check("--clean leaves logs/ in place", p3.returncode == 0 and os.path.isdir(r + BASE + "/logs"), p3.stderr)

# ------------------------------------------------------------------------------- 3. live
print("== live: transient units from the real [Service] settings, log dir initially ABSENT")
live_ok = (os.environ.get("R365_UNIT_LIVE_TEST") == "1" and os.geteuid() == 0
           and os.path.isdir("/run/systemd/system") and shutil.which("systemd-run") and TMPF)
if not live_ok:
    skip("live startup", "set R365_UNIT_LIVE_TEST=1, run as root on a systemd host")
else:
    tag = "r365-unit-test-" + uuid.uuid4().hex[:8]
    scratch = "/var/lib/" + tag                       # StateDirectory= is relative to /var/lib
    assert re.fullmatch(r"/var/lib/r365-unit-test-[0-9a-f]{8}", scratch)

    def moved(v):
        return v.replace(BASE, scratch).replace("laynes/r365-financial", tag)

    KEEP = ("Type", "StateDirectory", "StateDirectoryMode", "StandardOutput", "StandardError",
            "Nice", "IOSchedulingClass", "MemoryMax", "NoNewPrivileges", "PrivateTmp",
            "SuccessExitStatus", "TimeoutStartSec")
    props = [f"{k}={moved(v)}" for k, v in svc if k in KEEP]
    log = moved(targets[0])

    def start(case, extra=()):
        argv = ["systemd-run", "--quiet", "--wait", "--collect", f"--unit={tag}-{case}"]
        for p in list(props) + list(extra):
            argv += ["-p", p]
        argv += ["/bin/sh", "-c", f"echo startup-probe-{case}; echo startup-probe-{case}-err >&2"]
        return subprocess.run(argv, capture_output=True, text=True, timeout=60)

    def wipe():
        if os.path.lexists(scratch):
            shutil.rmtree(scratch)

    try:
        wipe()
        # A. defect reproduction: nothing exists, no prerequisite
        a = start("noprereq")
        check("without the prerequisite the start fails (209/STDOUT on systemd 259)",
              a.returncode != 0, f"rc={a.returncode}")
        print(f"     (observed exit {a.returncode}; systemd {subprocess.run(['systemctl', '--version'], capture_output=True, text=True).stdout.split()[1]})")
        wipe()
        # B. ExecStartPre=mkdir is NOT a fix: its own stdout is opened first, too
        b = start("execstartpre", [f"ExecStartPre=/bin/mkdir -p {os.path.dirname(log)}"])
        check("ExecStartPre=mkdir cannot create the log dir in time",
              b.returncode != 0 and not os.path.exists(log), f"rc={b.returncode}")
        wipe()
        # C. THE FIX: log dir (and everything above it) absent -> prerequisite -> start
        check("scratch layout starts fully absent", not os.path.lexists(scratch))
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as f:
            f.write("".join(moved(l) for l in open(TMPFILES)))
            conf = f.name
        try:
            t = subprocess.run([TMPF, "--create", conf], capture_output=True, text=True)
        finally:
            os.unlink(conf)
        check("prerequisite applied", t.returncode == 0, t.stderr)
        c = start("fixed")
        check("with the prerequisite the unit starts and exits 0", c.returncode == 0,
              f"rc={c.returncode} {c.stderr}")
        body = open(log).read() if os.path.exists(log) else ""
        check("stdout AND stderr appended to the log file",
              "startup-probe-fixed\n" in body and "startup-probe-fixed-err\n" in body, body)
        for sub in ("state", "logs"):
            st = os.stat(f"{scratch}/{sub}") if os.path.isdir(f"{scratch}/{sub}") else None
            check(f"{sub}/ 0700 root after start", st and (st.st_mode & 0o777) == 0o700 and st.st_uid == 0)
        c2 = start("fixed2")
        body2 = open(log).read() if os.path.exists(log) else ""
        check("second start APPENDS (log history kept)",
              c2.returncode == 0 and body2.startswith(body) and "startup-probe-fixed2\n" in body2)
    finally:
        wipe()
        check("scratch removed", not os.path.lexists(scratch))

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
