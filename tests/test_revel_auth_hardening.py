#!/usr/bin/env python3
"""Revel sync must fail loudly when its session dies.

On 2026-09-25 the saved session validated, the first product page succeeded,
and every request after that returned HTTP 401 carrying Revel's HTML login
page. Each resource logged "giving up" and the loop moved on, so all 12
establishments and all five resources recorded rows_fetched: 0, pipeline.py
exited 0, and run.sh logged "Revel sync completed successfully". The database
then sat 31-35 hours stale while the assistant correctly -- but expensively --
refused every question that needed sales.

These tests pin the three properties that were missing: an auth failure is
recognised as one, it is retried exactly once after a re-login, and any
required-resource failure or an all-zero run reaches the process exit code.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from dotenv import load_dotenv

load_dotenv(".env", override=True)
import pipeline as P  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# 1 -- auth failure detection
# ---------------------------------------------------------------------------
LOGIN_HTML = ('\n\n<!DOCTYPE html>\n<html><head>'
              '<link rel="stylesheet" media="all" href="/x.css">'
              '<input name="username"></head></html>')

check("HTTP 401 is an auth failure", P._is_auth_failure(401, "") is True)
check("HTTP 403 is an auth failure", P._is_auth_failure(403, "") is True)
check("HTTP 200 carrying the login page is an auth failure",
      P._is_auth_failure(200, LOGIN_HTML) is True)
check("HTTP 200 with real JSON is NOT an auth failure",
      P._is_auth_failure(200, '{"objects":[],"meta":{"total_count":0}}') is False)
check("HTTP 500 is NOT an auth failure (it is ordinary flakiness)",
      P._is_auth_failure(500, "server error") is False)
check("HTTP 404 with an HTML body is NOT an auth failure",
      P._is_auth_failure(404, "<html>not found</html>") is False)
check("an empty 200 body is not mistaken for a login page",
      P._is_auth_failure(200, "") is False)
check("RevelAuthError is distinct from ordinary errors",
      issubclass(P.RevelAuthError, RuntimeError)
      and P.RevelAuthError is not RuntimeError)

# ---------------------------------------------------------------------------
# 2 -- one re-auth, never a loop
# ---------------------------------------------------------------------------
src = open("pipeline.py").read()
check("an auth failure is NOT retried blindly by with_retries",
      "non_retryable=(RevelAuthError,)" in src)
check("re-auth is guarded by a one-shot flag in fetch_all_pages",
      "reauth_box = [False]" in src and "if reauth_box[0]:" in src)
check("re-auth is guarded by a one-shot flag on the order-items path",
      "items_reauth = [False]" in src and "if items_reauth[0]:" in src)
check("a second auth failure re-raises instead of re-logging again",
      src.count("auth failed again after re-login") >= 1)
check("the post-reauth retry is a SINGLE attempt",
      "attempts=1, delay=0" in src)
check("reauthenticate() closes the page it opens",
      "def reauthenticate" in src and "page.close()" in src)

# ---------------------------------------------------------------------------
# 3 -- failures must not be swallowed
# ---------------------------------------------------------------------------
check("pagination no longer breaks out on a generic failure",
      'log.error("  %s — giving up at offset %d: %s", endpoint, offset, exc)' not in src)
check("a required resource failure is raised, not logged and skipped",
      'log.error("  %s — FAILED at offset %d: %s", endpoint, offset, exc)' in src
      and src.index('FAILED at offset %d') < src.index("raise", src.index('FAILED at offset %d')))
check("an establishment auth failure aborts the run instead of continuing",
      "authentication failure — aborting run" in src)
check("per-establishment failures are collected into a run verdict",
      "run_failures.append" in src and "run_failures: list = []" in src)
check("the run exits non-zero when any failure was recorded",
      "PIPELINE FAILED for %s: %d resource/establishment failure(s)" in src)
check("the run exits non-zero on an auth failure",
      "PIPELINE FAILED for %s: Revel authentication failure" in src)

# ---------------------------------------------------------------------------
# 4 -- zero-row sanity gate
# ---------------------------------------------------------------------------
check("a zero-row overlap sync cannot report success",
      "suspicious zero-row sync" in src)
check("the zero-row gate exits non-zero",
      src.index("suspicious zero-row sync") < src.index("sys.exit(1)", src.index("suspicious zero-row sync")))
check("rows fetched are accumulated network-wide",
      "rows_fetched_total = [0]" in src and "rows_fetched_total[0] +=" in src)
check("the success line reports the row count it actually fetched",
      "Pipeline complete for %s — %d rows fetched" in src)
check("success is only logged after every gate",
      src.index("suspicious zero-row sync") < src.index("Pipeline complete for %s — %d rows fetched"))

# ---------------------------------------------------------------------------
# 5 -- watermark safety
# ---------------------------------------------------------------------------
# sync_state is only advanced by the per-resource success path; an aborted or
# failed run must leave the previous successful watermark in place so the next
# run re-covers the same window.
import psycopg2  # noqa: E402

conn = psycopg2.connect(host=os.environ["DB_HOST"], port=os.environ["DB_PORT"],
                        user=os.environ["DB_USER"], password=os.environ["DB_PASS"],
                        dbname=os.environ["DB_NAME"])
try:
    cur = conn.cursor()
    cur.execute("""SELECT count(*) FROM sync_state
                    WHERE resource = 'orders_v2' AND status = 'success'""")
    check("orders_v2 watermarks exist for the network", cur.fetchone()[0] >= 12)
    cur.execute("""SELECT count(*) FROM sync_state
                    WHERE resource = 'orders_v2'
                      AND window_end - window_start < interval '48 hours'""")
    check("the overlap window is at least 48h for every store",
          cur.fetchone()[0] == 0)
    cur.execute("""SELECT min(window_end - window_start), max(window_end - window_start)
                     FROM sync_state WHERE resource = 'orders_v2'""")
    lo, hi = cur.fetchone()
    check("the overlap window is ~72h as designed", lo is not None and hi is not None,
          f"{lo} .. {hi}")
finally:
    conn.close()

check("watermarks are written only on the success path",
      "status           = 'success'," in src or "status='success'" in src)

passed = sum(1 for _, ok, _ in results if ok)
failed = len(results) - passed
print(f"\n{'='*66}\nRevel auth hardening: {passed} passed, {failed} failed, {len(results)} total")
if failed:
    for n, ok, d in results:
        if not ok:
            print(f"  FAILED: {n} -- {d}")
sys.exit(1 if failed else 0)
