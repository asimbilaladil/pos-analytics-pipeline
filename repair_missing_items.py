#!/usr/bin/env python3
"""
repair_missing_items.py
=======================
Re-fetch OrderItems (+ nested ModifierItems) and OrderHistory for paid orders
that have NO item rows, then rebuild the v2 features for the dates touched.

Why: until the defer_watermark fix in sync_updated.py, the daily sync advanced
the orders watermark before fetching items. When the process was OOM-killed
during the items fetch (2026-10-25 Shepherd, 2026-10-26 Cypress), the next run
never revisited those orders. Thousands of paid orders were left with
no items, classified OTHER, and excluded from REAL sales, so the AI Chat
reconciliation gate failed for Shepherd and Cypress.

Two modes, same code path (sync_updated.repair_missing_items, which calls
the daily sync's own sync_order_items_and_modifiers / sync_order_history):

  * Explicit repair:  --start 2026-10-18 --end 2026-10-24 --establishments 15,54
    One chunk per (establishment, Chicago business day), checkpointed to
    --checkpoint so an interrupted run resumes where it stopped.
  * Self-heal (systemd timer, no dates): the last REVEL_SELF_HEAL_LOOKBACK_DAYS
    days for every establishment, one chunk per establishment.

Safety:
  * Writes only order_items_v2, modifier_items_v2, order_history_v2 (idempotent
    upserts) and the feature_recompute_queue. Never writes orders or payments.
  * Refuses to run while revel-daily-sync.service is active, and holds an
    exclusive lock so two copies never overlap.
  * Stops cleanly (exit 75, checkpoint kept) if available memory drops below
    --min-available-mb before a chunk.

Exit codes: 0 ok / nothing to do, 1 failure, 75 skipped (sync running, lock
held, low memory) -- retry later.
"""
import argparse
import fcntl
import json
import logging
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(".env")

import pipeline as P  # noqa: E402
import raw_archive  # noqa: E402
import sync_updated as S  # noqa: E402

log = logging.getLogger("repair_missing_items")
CHI = ZoneInfo("America/Chicago")
LOCK_PATH = "/run/lock/laynes-revel-items-repair.lock"
EX_TEMPFAIL = 75


def mem_available_mb() -> int:
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    return 0


def daily_sync_active() -> bool:
    r = subprocess.run(["systemctl", "is-active", "--quiet", "revel-daily-sync.service"])
    return r.returncode == 0


def day_bounds_utc(d: date):
    start = datetime(d.year, d.month, d.day, tzinfo=CHI)
    end = start + timedelta(days=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def plan_chunks(establishments, start: date = None, end_exclusive: date = None):
    """[(chunk_key, est, since_utc, until_utc)]. Dated mode: one per est/day.
    Self-heal mode: one per est over the lookback window."""
    if start is None:
        now = datetime.now(timezone.utc)
        return [(f"{e}:selfheal", e, now - S.SELF_HEAL_LOOKBACK, now - S.SELF_HEAL_SETTLE)
                for e in establishments]
    chunks = []
    d = start
    while d < end_exclusive:
        s, u = day_bounds_utc(d)
        chunks += [(f"{e}:{d.isoformat()}", e, s, u) for e in establishments]
        d += timedelta(days=1)
    return chunks


def load_checkpoint(path):
    if path and os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {"done": {}}


def save_checkpoint(path, ck):
    if not path:
        return
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(ck, f, indent=1, default=str)
    os.replace(tmp, path)


def open_context(browser, fresh_login: bool):
    """Same session handling as pipeline.main(). fresh_login=True logs in
    with the .env credentials in a clean context and does NOT write the
    shared session cache."""
    if fresh_login or not os.path.exists(P.STATE_FILE):
        ctx = browser.new_context()
        page = ctx.new_page()
        if fresh_login:
            _login_no_save(page)
        else:
            P.login_and_save(ctx, page)
    else:
        ctx = browser.new_context(storage_state=P.STATE_FILE)
        page = ctx.new_page()
        page.goto(P.BASE_URL)
        if "login" in page.url or "authentication" in page.url:
            log.info("Session expired -- re-logging in")
            P.login_and_save(ctx, page)
    if "authentication" in page.url or "login" in page.url:
        raise P.RevelAuthError("Revel login did not complete")
    page.close()
    return ctx


def _login_no_save(page):
    page.goto(P.BASE_URL)
    page.wait_for_url("**authentication.revelup.com**")
    page.wait_for_selector('input[name="username"]', timeout=60000)
    page.fill('input[name="username"]', P.REVEL_USER)
    page.click('button[type="submit"]')
    page.wait_for_selector('input[name="password"]', timeout=60000)
    page.fill('input[name="password"]', P.REVEL_PASS)
    page.click('button[type="submit"]')
    page.wait_for_load_state("networkidle", timeout=60000)


def run_chunks(context, conn, chunks, modifier_cache, run_id, ck, ck_path, min_avail_mb,
               repair=S.repair_missing_items, mem=mem_available_mb):
    """Process chunks in order, skipping ones already done. Returns
    (status, results) where status is 'ok' or 'low_memory'. A chunk is marked
    done only after its items and history have committed."""
    results = {}
    for key, est, since, until in chunks:
        if key in ck["done"]:
            continue
        if mem() < min_avail_mb:
            log.warning("MemAvailable %d MB < %d MB -- stopping before %s (checkpoint kept)",
                        mem(), min_avail_mb, key)
            return "low_memory", results
        res = repair(context, conn, est, run_id, modifier_cache, target="shadow_v2",
                     since=since, until=until, window_prefix=key.replace(":", "_").replace("-", ""))
        log.info("chunk %s: %s", key, res)
        results[key] = res
        ck["done"][key] = {"at": datetime.now(timezone.utc).isoformat(),
                           "missing_before": res.get("missing_before"),
                           "missing_after": res.get("missing_after")}
        save_checkpoint(ck_path, ck)
    return "ok", results


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--establishments", help="comma-separated ids (default: all)")
    ap.add_argument("--start", help="first Chicago business date YYYY-MM-DD (dated mode)")
    ap.add_argument("--end", help="last Chicago business date YYYY-MM-DD, inclusive")
    ap.add_argument("--checkpoint", help="JSON checkpoint path (dated mode)")
    ap.add_argument("--dry-run", action="store_true", help="count only, no Revel calls, no writes")
    ap.add_argument("--fresh-login", action="store_true",
                    help="log in with .env credentials in a clean context; do not touch the session cache")
    ap.add_argument("--min-available-mb", type=int, default=500)
    ap.add_argument("--no-features", action="store_true", help="skip reprocess_dirty_features_v2.py")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    ests = [int(x) for x in a.establishments.split(",")] if a.establishments else list(P.ESTABLISHMENTS)
    if bool(a.start) != bool(a.end):
        ap.error("--start and --end go together")
    start = date.fromisoformat(a.start) if a.start else None
    end_excl = date.fromisoformat(a.end) + timedelta(days=1) if a.end else None
    chunks = plan_chunks(ests, start, end_excl)

    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.warning("another repair holds %s -- skipping", LOCK_PATH)
        return EX_TEMPFAIL
    if daily_sync_active():
        log.warning("revel-daily-sync.service is running -- skipping, retry later")
        return EX_TEMPFAIL

    conn = P.db_connect()
    total = 0
    with conn.cursor() as cur:
        for key, est, since, until in chunks:
            n = len(S.find_orders_missing_items(cur, est, since, until))
            total += n
            if n:
                log.info("%s: %d paid orders without items", key, n)
    conn.commit()
    log.info("paid orders without items in scope: %d", total)
    if a.dry_run or total == 0:
        conn.close()
        return 0

    with conn.cursor() as cur:
        cur.execute("SELECT id, name FROM modifiers")
        modifier_cache = {r[0]: r[1] for r in cur.fetchall()}
    ck = load_checkpoint(a.checkpoint)
    run_id = raw_archive.new_run_id()
    log.info("run_id %s, %d chunks, %d already done", run_id, len(chunks), len(ck["done"]))

    from playwright.sync_api import sync_playwright
    status = "ok"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = open_context(browser, a.fresh_login)
            status, _ = run_chunks(context, conn, chunks, modifier_cache, run_id, ck,
                                   a.checkpoint, a.min_available_mb)
        finally:
            browser.close()
    conn.close()

    if not a.no_features:
        r = subprocess.run([sys.executable, "reprocess_dirty_features_v2.py"])
        if r.returncode != 0:
            log.error("reprocess_dirty_features_v2.py exited %s", r.returncode)
            return 1
    return EX_TEMPFAIL if status == "low_memory" else 0


if __name__ == "__main__":
    sys.exit(main())
