#!/usr/bin/env python3
"""
run_task10_remaining.py
========================
Task 10 orchestrator. Runs backfill_order_items_v2.py once per (establishment,
month) chunk, strictly sequentially, each invocation a fresh OS subprocess —
this script itself never fetches, parses, or holds a single OrderItem row in
memory, so it is not subject to the OOM risk Task 09 hit with a long-lived
fetch process. It is purely a supervisor: spawn one chunk, wait for it to
exit completely, check its result, spawn the next.

Safety thresholds (per Task 10 instructions):
  - peak RSS > RSS_LIMIT_KB (~1.5 GB) on any single chunk -> abort immediately
  - a chunk failing MAX_RETRIES+1 times in a row -> abort immediately
Both conditions stop the whole run rather than skipping ahead, so a real
problem gets surfaced instead of silently leaving gaps.

Already-successful chunks (checked by backfill_order_items_v2.py itself,
against backfill_progress) are skipped without a real fetch -- this script
does not duplicate that logic, it just iterates the full chunk list and lets
each subprocess decide.
"""
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime, timezone

os.chdir("/root/pos-analytics-pipeline")

BACKFILL_START = date(2026, 2, 10)
CATCHUP_END_EXCLUSIVE = date(2026, 9, 1)
ESTABLISHMENTS = [6, 7, 14, 15, 20, 25, 26, 32, 36, 40, 48, 54]
RSS_LIMIT_KB = 1_572_864  # ~1.5 GB
MAX_RETRIES = 1  # one retry per chunk before aborting the whole run


def month_chunks(start_date, end_date_exclusive):
    cur = start_date
    while cur < end_date_exclusive:
        nxt = date(cur.year + 1, 1, 1) if cur.month == 12 else date(cur.year, cur.month + 1, 1)
        nxt = min(nxt, end_date_exclusive)
        yield cur, nxt
        cur = nxt


CHUNKS = list(month_chunks(BACKFILL_START, CATCHUP_END_EXCLUSIVE))

RUN_STAMP = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
LOG_PATH = f"/root/pos-analytics-pipeline/logs/task10_orchestrator_{RUN_STAMP}.log"
SUMMARY_PATH = "/root/pos-analytics-pipeline/logs/task10_orchestrator_summary.jsonl"
STATUS_PATH = "/root/pos-analytics-pipeline/logs/task10_orchestrator_status.json"


def log(msg):
    line = f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def write_status(**kw):
    with open(STATUS_PATH, "w") as f:
        json.dump({"updated_at": datetime.now(timezone.utc).isoformat(), **kw}, f, indent=2, default=str)


def run_chunk(est, start, end):
    cmd = [
        "/usr/bin/time", "-v",
        "python3", "backfill_order_items_v2.py",
        "--establishments", str(est),
        "--start", start.isoformat(),
        "--end", end.isoformat(),
    ]
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.time() - t0

    peak_rss_kb = None
    for line in proc.stderr.splitlines():
        if "Maximum resident set size" in line:
            try:
                peak_rss_kb = int(line.strip().split()[-1])
            except ValueError:
                pass

    status = None
    rows_fetched = rows_affected = None
    try:
        data = json.loads(proc.stdout)
        chunks = data["by_establishment"][str(est)]["chunks"]
        if chunks:
            status = chunks[0]["status"]
            rows_fetched = chunks[0].get("rows_fetched")
            rows_affected = chunks[0].get("rows_affected")
    except Exception:
        pass

    ok = (proc.returncode == 0 and status == "success")
    return {
        "ok": ok, "exit_code": proc.returncode, "peak_rss_kb": peak_rss_kb,
        "status": status, "rows_fetched": rows_fetched, "rows_affected": rows_affected,
        "elapsed_s": round(elapsed, 1), "stderr_tail": proc.stderr[-2000:],
    }


def main():
    total_chunks = len(ESTABLISHMENTS) * len(CHUNKS)
    log(f"=== Task 10 orchestrator start — {len(ESTABLISHMENTS)} establishments x {len(CHUNKS)} chunks = {total_chunks} total ===")
    max_rss_seen = 0
    total_fetched = 0
    n_done = 0
    n = 0
    for est in ESTABLISHMENTS:
        for start, end in CHUNKS:
            n += 1
            log(f"[{n}/{total_chunks}] est={est} {start}..{end}")
            attempt = 0
            while True:
                attempt += 1
                result = run_chunk(est, start, end)
                rss = result["peak_rss_kb"] or 0
                max_rss_seen = max(max_rss_seen, rss)
                with open(SUMMARY_PATH, "a") as f:
                    f.write(json.dumps({"est": est, "start": str(start), "end": str(end),
                                         "attempt": attempt, **result}) + "\n")

                if rss > RSS_LIMIT_KB:
                    log(f"ABORT: est={est} {start}..{end} peak RSS {rss}KB exceeds {RSS_LIMIT_KB}KB (~1.5GB) limit")
                    log(f"stderr tail:\n{result['stderr_tail']}")
                    write_status(state="ABORTED_RSS", chunk=f"est={est} {start}..{end}",
                                 peak_rss_kb=rss, chunks_done=n_done, max_rss_seen_kb=max_rss_seen,
                                 total_fetched=total_fetched)
                    return 1

                if result["ok"]:
                    log(f"  OK status={result['status']} fetched={result['rows_fetched']} "
                        f"affected={result['rows_affected']} rss={rss}KB elapsed={result['elapsed_s']}s")
                    total_fetched += (result["rows_fetched"] or 0)
                    n_done += 1
                    write_status(state="RUNNING", chunks_done=n_done, chunks_total=total_chunks,
                                 max_rss_seen_kb=max_rss_seen, total_fetched=total_fetched,
                                 last_chunk=f"est={est} {start}..{end}")
                    break

                log(f"  FAILED attempt={attempt} exit={result['exit_code']} status={result['status']} rss={rss}KB")
                log(f"  stderr tail:\n{result['stderr_tail']}")
                if attempt > MAX_RETRIES:
                    log(f"ABORT: est={est} {start}..{end} failed {attempt} times, stopping")
                    write_status(state="ABORTED_FAILURE", chunk=f"est={est} {start}..{end}",
                                 chunks_done=n_done, max_rss_seen_kb=max_rss_seen,
                                 total_fetched=total_fetched)
                    return 1
                log("  retrying chunk (fresh process)...")

    log(f"=== Task 10 orchestrator DONE — {n} chunks processed, max_rss_seen={max_rss_seen}KB, total_fetched={total_fetched} ===")
    write_status(state="DONE", chunks_done=n_done, chunks_total=total_chunks,
                 max_rss_seen_kb=max_rss_seen, total_fetched=total_fetched)
    return 0


if __name__ == "__main__":
    sys.exit(main())
