#!/usr/bin/env python3
"""Revel scheduling correctness and a single Product fetch per run.

Two long-standing operational defects, both silent:

A. The cron entry carried `TZ=America/Chicago` with `0 9 * * *`, but Debian/
   Vixie cron schedules against the SYSTEM timezone and its own crontab(5)
   states it "does not support per-user timezones ... even if a user specifies
   the TZ themselves". The host is Etc/UTC, so the job fired at 09:00 UTC
   (04:00 Chicago). The TZ line did take effect inside the job, so run.sh's own
   `date` printed 04:00 -- which is how the four-hour error hid in plain sight.

B. /resources/Product/ was fetched twice per run under one run_id --
   fetch_and_upsert_products() before the establishment loop, then
   sync_reference_data() after it -- so the second fetch collided with its own
   archive. Swallowed on every run since at least 2026-09-21.
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


timer = open("systemd/revel-daily-sync.timer").read()
service = open("systemd/revel-daily-sync.service").read()
pipe = open("pipeline.py").read()

# ---------------------------------------------------------------------------
# A -- scheduling
# ---------------------------------------------------------------------------
check("the timer carries the timezone in the OnCalendar expression",
      "OnCalendar=*-*-* 09:00:00 America/Chicago" in timer)
check("the schedule is 09:00 local, not 09:00 UTC",
      re.search(r"OnCalendar=\*-\*-\* 09:00:00 America/Chicago", timer) is not None)
check("no bogus Timezone= key (systemd has no such [Timer] option)",
      not re.search(r"(?m)^\s*Timezone\s*=", timer))
check("Persistent=true so a missed elapse is replayed", "Persistent=true" in timer)
# The word appears in the explanatory comment; what matters is that no
# DIRECTIVE sets it, so match at line start.
check("no RandomizedDelaySec directive (deterministic start)",
      not re.search(r"(?m)^\s*RandomizedDelaySec\s*=", timer))

check("the service calls the EXISTING run.sh, not duplicated logic",
      "ExecStart=/root/pos-analytics-pipeline/run.sh" in service)
check("run.sh exists and is executable",
      os.path.isfile("run.sh") and os.access("run.sh", os.X_OK))
check("the service declares no SuccessExitStatus, so any failure stays red",
      "SuccessExitStatus" not in service)
check("run.sh propagates a failed sync as a non-zero exit",
      "ERROR: Revel sync failed" in open("run.sh").read()
      and "exit 1" in open("run.sh").read())


def elapse(expr, base):
    out = subprocess.run(["systemd-analyze", "calendar", "--iterations=1",
                          f"--base-time={base}", expr],
                         capture_output=True, text=True).stdout
    m = re.search(r"Next elapse:\s*(.+)", out)
    return m.group(1).strip() if m else out.strip()


cdt = elapse("*-*-* 09:00:00 America/Chicago", "2026-07-01 00:00:00 UTC")
cst = elapse("*-*-* 09:00:00 America/Chicago", "2026-12-01 00:00:00 UTC")
check("CDT: 09:00 Chicago resolves to 14:00 UTC", "14:00:00 UTC" in cdt, cdt)
check("CST: 09:00 Chicago resolves to 15:00 UTC", "15:00:00 UTC" in cst, cst)
check("the schedule is NOT pinned to one UTC hour across DST", cdt != cst)

# the old cron entry must be gone, and exactly one schedule must remain
cron = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
check("the Revel cron entry is removed",
      "pos-analytics-pipeline/run.sh" not in cron, cron.strip()[:80])
check("no stray TZ= directive is left behind in crontab",
      "TZ=America/Chicago" not in cron)
sys_cron = subprocess.run(
    ["bash", "-lc", "grep -rl 'pos-analytics-pipeline' /etc/cron.d /etc/crontab "
                    "/etc/cron.daily /etc/cron.hourly 2>/dev/null | wc -l"],
    capture_output=True, text=True).stdout.strip()
check("no system-wide cron entry runs the pipeline either", sys_cron == "0", sys_cron)

units = subprocess.run(
    ["bash", "-lc", "grep -l 'pos-analytics-pipeline/run.sh' /etc/systemd/system/*.service "
                    "2>/dev/null | wc -l"], capture_output=True, text=True).stdout.strip()
check("exactly ONE systemd service runs run.sh", units == "1", units)

for prop, want in (("UnitFileState", "enabled"), ("ActiveState", "active")):
    got = subprocess.run(["systemctl", "show", "revel-daily-sync.timer",
                          "-p", prop, "--value"], capture_output=True, text=True).stdout.strip()
    check(f"revel-daily-sync.timer {prop}={want}", got == want, got)

# HME schedules must be untouched by this change
for t in ("hme-daily-ingest.timer", "hme-reconciliation-retry.timer"):
    got = subprocess.run(["systemctl", "is-active", t],
                         capture_output=True, text=True).stdout.strip()
    check(f"{t} still active (untouched)", got == "active", got)

# ---------------------------------------------------------------------------
# B -- exactly one Product fetch per run
# ---------------------------------------------------------------------------
check("sync_reference_data is called exactly once in the pipeline",
      pipe.count("sync_updated.sync_reference_data(") == 1)
def call_sites(src, fn):
    """Occurrences of fn(...) that are calls, not the definition."""
    return len([l for l in src.splitlines()
                if f"{fn}(context" in l and not l.lstrip().startswith("def ")])

check("fetch_and_upsert_products is called exactly once (created mode only)",
      call_sites(pipe, "fetch_and_upsert_products") == 1,
      str(call_sites(pipe, "fetch_and_upsert_products")))
# Compare against the CALL site, not the function definition far above it.
_call = pipe.index("with_retries(lambda: fetch_and_upsert_products(context")
check("the two product loaders are on mutually exclusive branches",
      'elif sync_mode == "updated":' in pipe
      and pipe.index('elif sync_mode == "updated":') < _call)
check("updated mode loads reference data BEFORE the establishment loop "
      "(order_items_v2.product_id has an FK to products)",
      pipe.index("sync_updated.sync_reference_data(")
      < pipe.index("run_establishment_updated("))
check("product_categories still load (category_id FK) via sync_reference_data",
      "sync_reference_data" in open("sync_updated.py").read())
check("modifiers are still fetched in both modes",
      call_sites(pipe, "fetch_and_upsert_modifiers") == 2,
      str(call_sites(pipe, "fetch_and_upsert_modifiers")))
check("reference rows still count toward the zero-row guard",
      "rows_fetched_total[0] += int(st.get(\"rows_fetched\") or 0)" in pipe)

# the narrow duplicate-archive exception is gone, generic handling is not
check("the duplicate-archive exception is removed (no longer needed)",
      "refusing to overwrite existing archive" not in pipe)
check("a genuine ArchiveError still aborts rather than being retried",
      "Archive storage failure" in pipe and "NOT retrying Revel" in pipe)
check("ArchiveError is still non-retryable in with_retries",
      "non_retryable = tuple(non_retryable) + (raw_archive.ArchiveError,)" in pipe)

# auth hardening and guards must survive untouched
check("RevelAuthError detection retained", "class RevelAuthError" in pipe)
check("auth failure classification retained", "def _is_auth_failure" in pipe)
check("single re-authentication retained",
      "def reauthenticate" in pipe and "reauth_box = [False]" in pipe)
check("auth failures are non-retryable", "non_retryable=(RevelAuthError,)" in pipe)
check("zero-row guard retained", "suspicious zero-row sync" in pipe)
check("run-level failure verdict retained", "PIPELINE FAILED for %s" in pipe)
check("watermarks still written only on the success path",
      "record_sync_success" in open("sync_updated.py").read())

passed = sum(1 for _, ok, _ in results if ok)
failed = len(results) - passed
print(f"\n{'='*66}\nRevel schedule + reference: {passed} passed, {failed} failed, {len(results)} total")
if failed:
    for n, ok, d in results:
        if not ok:
            print(f"  FAILED: {n} -- {d}")
sys.exit(1 if failed else 0)
