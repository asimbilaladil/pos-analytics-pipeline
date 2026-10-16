#!/usr/bin/env python3
"""R365 FINANCIAL ingest orchestrator (source financial API -> PostgreSQL).

    venv/bin/python r365_financial_ingest.py                    # scheduled (daily 07:00 Chicago)
    venv/bin/python r365_financial_ingest.py --period 2026 10   # one fiscal period
    venv/bin/python r365_financial_ingest.py --backfill         # accounts + history + every period
    venv/bin/python r365_financial_ingest.py --dry-run          # fetch + validate, write nothing

The R365 SOURCE VPS owns Restaurant365 GL ingestion and publishes the result
on its authenticated financial API; this VPS only consumes that API. It never
calls Restaurant365 itself.

Targets are computed from the America/Chicago date and the official AYG
calendar (ayg_business_periods), never from the host's UTC date:
  * scheduled:  the chart of accounts and every fiscal period that overlaps
                the last OVERLAP_DAYS (42) days -- six weeks, longer than the
                source's five-week repair window, so any line the source
                re-syncs is re-pulled here; in practice 2-3 periods. On Mondays
                (after the source's Monday 05:30 weekly repair) it re-pulls
                every elapsed period of the current and previous fiscal year.
                All history is never re-pulled daily.
  * --backfill: the chart of accounts, every elapsed calendar period, and the
                pre-calendar GL history (rolled up per account x month, with
                no fiscal period assigned).

Safety posture:
  * the bearer credential is read from /etc/laynes/r365.env (root, 0600) and
    only ever sent as a request header -- never on a command line, in a log
    line, in the archive or in the database;
  * flock prevents overlapping runs;
  * summary/accounts responses are archived verbatim BEFORE parsing, with their
    sha256 (GL detail pages are not archived: they are the source VPS's data);
  * a schema surprise, a calendar mismatch or a summary that does not reconcile
    to the GL detail FAILS that target and writes nothing for it.

Exit contract:
   0  every target COMPLETE
   1  at least one target FAILED (or a configuration error)
   2  no failure, but at least one CLOSED period is PARTIAL (not fully loaded
      at the source, or a store unaccounted for). The current, still-open
      period is PARTIAL by nature; that alone does not make the run exit 2.
  75  EX_TEMPFAIL: another run holds the lock
"""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
import sys
import time
import uuid
import zoneinfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import r365_financial_load as L  # noqa: E402

CHICAGO = zoneinfo.ZoneInfo("America/Chicago")
ENV_FILE = os.environ.get("R365_ENV_FILE", "/etc/laynes/r365.env")
RAW_DIR = os.environ.get("R365_FIN_RAW_DIR", "/var/lib/laynes/r365-financial/raw")
LOCK_PATH = os.environ.get("R365_FIN_LOCK_PATH",
                           "/var/lib/laynes/r365-financial/state/ingest.lock")
ORCHESTRATOR_VERSION = "r365_financial_ingest/1.0.0"
ACCOUNTS_PATH = "/financial/gl/accounts"
SUMMARY_PATH = "/financial/expenses/summary"
DETAIL_PATH = "/financial/expenses"
DETAIL_PAGE = 1000                      # the source caps limit at 1000
MAX_ATTEMPTS = int(os.environ.get("R365_FIN_MAX_ATTEMPTS", "3"))
BACKOFF_SECONDS = [0, 15, 60]
CONNECT_TIMEOUT, READ_TIMEOUT = 15, 120
REPAIR_WEEKDAY = 1                      # Monday, after the source's weekly repair
OVERLAP_DAYS = 42                       # > the source's 5-week (35-day) repair window

EXIT_OK, EXIT_FAILED, EXIT_PARTIAL, EXIT_LOCKED = 0, 1, 2, 75
STATUS_OPEN = "OPEN"


class FetchError(Exception):
    def __init__(self, code, message, http_status=None, retryable=False):
        super().__init__(message)
        self.code, self.http_status, self.retryable = code, http_status, retryable


# ---------------------------------------------------------------------------
# Targeting (pure)
# ---------------------------------------------------------------------------
def chicago_today(now=None):
    now = now or datetime.datetime.now(CHICAGO)
    return now.astimezone(CHICAGO).date()


def current_period(calendar, today):
    for k, (s, e) in calendar.items():
        if s <= today <= e:
            return k
    return None


def elapsed_periods(calendar, today):
    """Every calendar period that has started by today, oldest first."""
    return [k for k, (s, _) in sorted(calendar.items(), key=lambda kv: kv[1][0]) if s <= today]


def scheduled_periods(calendar, today):
    started = elapsed_periods(calendar, today)
    if not started:
        return []
    if today.isoweekday() == REPAIR_WEEKDAY:
        cur_fy = started[-1][0]
        return [k for k in started if k[0] >= cur_fy - 1]
    since = today - datetime.timedelta(days=OVERLAP_DAYS)
    return [k for k in started if calendar[k][1] >= since]


def history_range(calendar):
    """Pre-calendar GL range: everything before the first official period."""
    first = min(s for s, _ in calendar.values())
    return datetime.date(2000, 1, 1), first - datetime.timedelta(days=1)


# ---------------------------------------------------------------------------
# Source access
# ---------------------------------------------------------------------------
def read_env_file(path=ENV_FILE):
    vals = {}
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip().strip('"').strip("'")
    base, token = vals.get("R365_API_BASE"), vals.get("R365_API_TOKEN")
    if not base or not token or token == "REPLACE_ME":
        raise FetchError("CONFIG", f"R365_API_BASE / R365_API_TOKEN not set in {path}")
    # The base names the source host; the financial paths are absolute on it.
    from urllib.parse import urlsplit
    u = urlsplit(base)
    return f"{u.scheme}://{u.netloc}", token


class Source:
    """Authenticated GETs against the source financial API with bounded retries."""

    def __init__(self, base, token, session=None, sleep=time.sleep):
        import requests
        self._requests = requests
        self.base, self._token = base, token
        self.session = session or requests.Session()
        self.sleep = sleep
        self.requests_made, self.seconds = 0, 0.0

    def get(self, path, params=None):
        rq = self._requests
        last = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if attempt > 1:
                self.sleep(BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)])
            t0 = time.monotonic()
            self.requests_made += 1
            try:
                r = self.session.get(self.base + path, params=params or {},
                                     headers={"Authorization": f"Bearer {self._token}",
                                              "Accept": "application/json"},
                                     timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
            except (rq.Timeout, rq.ConnectionError) as e:
                last = FetchError("NETWORK", f"{type(e).__name__} on attempt {attempt}",
                                  retryable=True)
                continue
            finally:
                self.seconds += time.monotonic() - t0
            if r.status_code == 200:
                return r.content
            if r.status_code in (401, 403):
                raise FetchError("AUTH", f"HTTP {r.status_code}: credential rejected",
                                 r.status_code)
            if r.status_code == 400:
                raise FetchError("BAD_REQUEST", f"HTTP 400: {r.text[:300]}", 400)
            last = FetchError("HTTP", f"HTTP {r.status_code} on attempt {attempt}",
                              r.status_code,
                              retryable=r.status_code >= 500 or r.status_code == 429)
            if not last.retryable:
                raise last
        raise last

    def detail(self, params):
        """Every approved GL detail line for params, across pages. The total
        must hold steady through the scan and every line must be unique."""
        lines, offset, total = [], 0, None
        params = {**params, "include_unapproved": "true"}
        while True:
            doc = L.parse_payload(self.get(DETAIL_PATH, {**params, "limit": DETAIL_PAGE,
                                                         "offset": offset}))
            if doc.get("includeUnapproved") is not True:
                raise L.SourceSchemaError("detail feed did not honour include_unapproved")
            pag = doc.get("pagination") or {}
            if total is None:
                total = pag.get("total")
            elif pag.get("total") != total:
                raise L.SourceSchemaError(f"detail total changed mid-scan ({total} -> "
                                          f"{pag.get('total')}); the source was syncing")
            page = doc.get("data") or []
            lines.extend(page)
            offset += len(page)
            if not page or offset >= total:
                break
        if len(lines) != total:
            raise L.SourceSchemaError(f"detail scan returned {len(lines)} lines, total {total}")
        return lines


def archive(target, run_id, fetched_at, payloads):
    """Write summary/accounts responses verbatim inside a provenance envelope."""
    d = os.path.join(RAW_DIR, target)
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    h = hashlib.sha256()
    for _, raw in payloads:
        h.update(raw)
    digest = h.hexdigest()
    stamp = fetched_at.astimezone(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(d, f"run-{run_id}-{stamp}.json")
    meta = json.dumps({"run_id": run_id, "target": target,
                       "fetched_at": fetched_at.isoformat(),
                       "requests": [label for label, _ in payloads],
                       "payload_sha256": digest,
                       "orchestrator_version": ORCHESTRATOR_VERSION})
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(b'{"meta": ' + meta.encode() + b', "payloads": [')
        fh.write(b", ".join(raw for _, raw in payloads))
        fh.write(b"]}\n")
    return path, digest


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------
def _run_target(conn, run_id, work, log, label):
    """Run fetch+plan+write for one target; any failure marks the run FAILED
    and rolls back everything it wrote."""
    fetch = {}
    try:
        status, msg = work(fetch)
        conn.commit()
        log(f"run {run_id} {label} {status} {msg}")
        return status
    except FetchError as e:
        L.fail_run(conn, run_id, e.code, str(e), fetch)
        log(f"run {run_id} {label} FAILED {e.code}: {e}")
    except L.SourceSchemaError as e:
        L.fail_run(conn, run_id, "SOURCE_SCHEMA", str(e), fetch)
        log(f"run {run_id} {label} FAILED SOURCE_SCHEMA: {e}")
    except L.ReconciliationError as e:
        L.fail_run(conn, run_id, "RECONCILIATION", str(e), fetch)
        log(f"run {run_id} {label} FAILED RECONCILIATION: {e}")
    except Exception as e:  # noqa: BLE001 -- the run row must never stay RUNNING
        L.fail_run(conn, run_id, "LOAD_ERROR", f"{type(e).__name__}: {e}", fetch)
        log(f"run {run_id} {label} FAILED LOAD_ERROR: {type(e).__name__}: {e}")
    return L.STATUS_FAILED


def _stamp_fetch(fetch, src, t_req, path, digest):
    fetch.setdefault("fetched_at", datetime.datetime.now(datetime.timezone.utc))
    fetch.update(http_requests=src.requests_made - t_req[0],
                 fetch_seconds=round(src.seconds - t_req[1], 3),
                 raw_archive_path=path, payload_sha256=digest)


def ingest_accounts(conn, src, batch_id, trigger, dry_run, log):
    if dry_run:
        plan = L.plan_accounts(L.parse_payload(src.get(ACCOUNTS_PATH, {"limit": 1000})))
        log(f"[dry-run] accounts {len(plan['rows'])} VERIFIED={plan['verified_categories']}")
        return L.STATUS_COMPLETE, plan
    run_id = L.start_run(conn, batch_id, trigger, "ACCOUNTS")
    holder = {}

    def work(fetch):
        t_req = (src.requests_made, src.seconds)
        raw = src.get(ACCOUNTS_PATH, {"limit": 1000})
        path, digest = archive("accounts", run_id, datetime.datetime.now(datetime.timezone.utc),
                               [("accounts", raw)])
        _stamp_fetch(fetch, src, t_req, path, digest)
        plan = L.plan_accounts(L.parse_payload(raw))
        c = L.write_accounts(conn, run_id, plan, fetch)
        holder["plan"] = plan
        return L.STATUS_COMPLETE, (f"accounts={c['upserted']} changed={c['changed']} "
                                   f"VERIFIED categories={plan['verified_categories']}")
    status = _run_target(conn, run_id, work, log, "accounts")
    return status, holder.get("plan")


def ingest_period(conn, src, fy, pn, calendar, verified_categories, batch_id, trigger,
                  dry_run, log):
    label = f"FY{fy}-P{pn}"
    cal = calendar[(fy, pn)]

    def fetch_and_plan(fetch, run_id):
        t_req = (src.requests_made, src.seconds)
        base = {"fiscal_year": fy, "period_number": pn}
        raws = [(f"summary {cat}", src.get(SUMMARY_PATH, {**base, "analytics_category": cat}))
                for cat in verified_categories]
        raws.append(("summary ALL", src.get(SUMMARY_PATH, base)))
        path = digest = None
        if run_id is not None:
            path, digest = archive(label, run_id,
                                   datetime.datetime.now(datetime.timezone.utc), raws)
        lines = src.detail(base)
        _stamp_fetch(fetch, src, t_req, path, digest)
        cat_payloads = {cat: L.parse_payload(raw)
                        for cat, (_, raw) in zip(verified_categories, raws)}
        with conn.cursor() as cur:
            mapping = L.load_mapping(cur)
        return L.plan_period(fy, pn, cal, cat_payloads, L.parse_payload(raws[-1][1]),
                             lines, mapping, verified_categories)

    if dry_run:
        plan = fetch_and_plan({}, None)
        conn.rollback()
        log(f"[dry-run] {label} {plan['status']} expense_rows={len(plan['expense_rows'])} "
            f"no_activity={plan['no_activity_rows']} candidates={len(plan['candidate_rows'])} "
            f"account_period={len(plan['account_period_rows'])} lines={plan['detail_lines']}")
        return plan["status"]

    run_id = L.start_run(conn, batch_id, trigger, "PERIOD", fy, pn, cal[0], cal[1])

    def work(fetch):
        plan = fetch_and_plan(fetch, run_id)
        c = L.write_period(conn, run_id, plan, fetch)
        return plan["status"], (
            f"{cal[0]}..{cal[1]} expense={c['expense'][0]} (changed {c['expense'][1]}, "
            f"deleted {c['expense'][2]}) no_activity={plan['no_activity_rows']} "
            f"candidates={c['candidates'][0]} account_period={c['account_period'][0]} "
            f"lines={plan['detail_lines']} txn={c['transactions'][0]} (changed "
            f"{c['transactions'][1]}, deleted {c['transactions'][2]}, unapproved "
            f"{plan['transaction_stats']['unapproved']}) source={plan['source_data_status']}"
            + (f" unaccounted={plan['unaccounted_stores']}" if plan["unaccounted_stores"] else "")
            + (f" unmapped_locations={[u['name'] for u in plan['unmapped_locations']]}"
               if plan["unmapped_locations"] else ""))
    return _run_target(conn, run_id, work, log, label)


def ingest_history(conn, src, calendar, batch_id, trigger, dry_run, log):
    start, end = history_range(calendar)

    def fetch_and_plan(fetch):
        t_req = (src.requests_made, src.seconds)
        lines = src.detail({"start_date": start.isoformat(), "end_date": end.isoformat()})
        _stamp_fetch(fetch, src, t_req, None, None)
        with conn.cursor() as cur:
            mapping = L.load_mapping(cur)
        return L.plan_history(lines, mapping, start, end)

    if dry_run:
        plan = fetch_and_plan({})
        conn.rollback()
        log(f"[dry-run] HISTORY lines={plan['detail_lines']} "
            f"account_months={len(plan['account_period_rows'])} "
            f"range={plan['source_min_date']}..{plan['source_max_date']}")
        return plan["status"]
    run_id = L.start_run(conn, batch_id, trigger, "HISTORY", start=start, end=end)

    def work(fetch):
        plan = fetch_and_plan(fetch)
        c = L.write_history(conn, run_id, plan, fetch)
        return plan["status"], (f"lines={plan['detail_lines']} txn={c['transactions'][0]} "
                                f"(changed {c['transactions'][1]}) account_months="
                                f"{c['account_period'][0]} (changed {c['account_period'][1]}) "
                                f"range={plan['source_min_date']}..{plan['source_max_date']}")
    return _run_target(conn, run_id, work, log, "HISTORY")


def check_transaction_coverage(conn, src, log):
    """After a full backfill the local transaction table must hold exactly the
    source's line set: same count, nothing synthesised, nothing missing."""
    doc = L.parse_payload(src.get(DETAIL_PATH, {"include_unapproved": "true", "limit": 1}))
    total = (doc.get("pagination") or {}).get("total")
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM r365_financial_transaction")
        local = cur.fetchone()[0]
    conn.rollback()
    ok = local == total
    log(f"transaction coverage: local {local} vs source {total} -> "
        f"{'MATCH' if ok else 'MISMATCH'}")
    return L.STATUS_COMPLETE if ok else L.STATUS_FAILED


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--period", nargs=2, type=int, metavar=("FISCAL_YEAR", "PERIOD"))
    g.add_argument("--backfill", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    os.makedirs(os.path.dirname(LOCK_PATH), mode=0o700, exist_ok=True)
    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another R365 financial ingest holds the lock; exiting")
        return EXIT_LOCKED

    try:
        base, token = read_env_file()
    except (OSError, FetchError) as e:
        print(f"configuration error: {e}")
        return EXIT_FAILED

    today = chicago_today()
    conn = L.connect()
    try:
        with conn.cursor() as cur:
            calendar = L.load_calendar(cur)
        conn.rollback()
        if args.period:
            key = tuple(args.period)
            if key not in calendar:
                print(f"refusing: FY{key[0]} P{key[1]} is not in the official AYG calendar")
                return EXIT_FAILED
            if calendar[key][0] > today:
                print(f"refusing: FY{key[0]} P{key[1]} has not started (Chicago today {today})")
                return EXIT_FAILED
            periods, trigger = [key], "manual"
        elif args.backfill:
            periods, trigger = elapsed_periods(calendar, today), "backfill"
        else:
            periods, trigger = scheduled_periods(calendar, today), "scheduled"

        batch_id = uuid.uuid4().hex
        print(f"{ORCHESTRATOR_VERSION} batch={batch_id} trigger={trigger} chicago_today={today} "
              f"periods={[f'FY{a}-P{b}' for a, b in periods]}"
              + (" +history" if args.backfill else ""), flush=True)
        src = Source(base, token)
        results = []
        status, acc = ingest_accounts(conn, src, batch_id, trigger, args.dry_run, print)
        results.append(status)
        if acc is None:
            print("chart of accounts unavailable; no period can be categorised -- stopping")
            return EXIT_FAILED
        cats = acc["verified_categories"]
        if args.backfill:
            results.append(ingest_history(conn, src, calendar, batch_id, trigger,
                                          args.dry_run, print))
        for fy, pn in periods:
            st = ingest_period(conn, src, fy, pn, calendar, cats, batch_id, trigger,
                               args.dry_run, print)
            if st == L.STATUS_PARTIAL and calendar[(fy, pn)][1] >= today:
                st = STATUS_OPEN          # an open period is expected to be partial
            results.append(st)
        if args.backfill and not args.dry_run:
            results.append(check_transaction_coverage(conn, src, print))
        print(f"done: {src.requests_made} requests in {src.seconds:.1f}s", flush=True)
    finally:
        conn.close()
    if L.STATUS_FAILED in results:
        return EXIT_FAILED
    if L.STATUS_PARTIAL in results:
        return EXIT_PARTIAL
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
