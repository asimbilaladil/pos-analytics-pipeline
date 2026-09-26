#!/usr/bin/env python3
"""HME freshness must be judged against the ingestion schedule, not the calendar.

On 2026-09-26 at ~02:00 Chicago the assistant reported "HME's latest business
date on file is 2026-09-24 (a 2-day freshness lag)". Nothing was late. HME's
business_date is the previous COMPLETED Chicago day and is ingested the NEXT
morning at 05:30 America/Chicago, so Sep 25 was simply not due yet.

The old figure was a bare subtraction:

    lag = Chicago_today - max(business_date)

which cannot tell "not due yet" from "overdue" -- one calendar day is the
structural floor and two is normal before the morning run.

Every test pins an explicit Chicago clock, so none of this depends on when the
suite happens to run.
"""
import os
import re
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from dotenv import load_dotenv

load_dotenv(".env", override=True)
import chat_sql as C  # noqa: E402

CHI = ZoneInfo("America/Chicago")
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


def at(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=CHI)


SEP24, SEP25 = date(2026, 9, 24), date(2026, 9, 25)
ok_row = lambda d: {"business_date": d, "reconciliation_ok": True, "status": "succeeded"}
bad_row = lambda d: {"business_date": d, "reconciliation_ok": False, "status": "partial"}

# ---------------------------------------------------------------------------
# 1 -- the reported bug: pre-ingest is CURRENT, not a 2-day lag
# ---------------------------------------------------------------------------
for hh, mm, label in ((4, 0, "04:00"), (0, 5, "00:05"), (5, 29, "05:29")):
    f = C.hme_freshness(SEP24, [ok_row("2026-09-24")], now=at(2026, 9, 26, hh, mm))
    check(f"{label} Chicago Sep 26: expected business date is still Sep 24",
          f["expected_business_date"] == "2026-09-24", f["expected_business_date"])
    check(f"{label} Chicago Sep 26: status is CURRENT, not stale",
          f["freshness_status"] == "CURRENT", f["freshness_status"])

f = C.hme_freshness(SEP24, [ok_row("2026-09-24")], now=at(2026, 9, 26, 4, 0))
check("the next expected business date is named", f["next_expected_business_date"] == "2026-09-25")
check("the next ingest window is named",
      f["next_ingest_window"] == "2026-09-26 05:30 America/Chicago", f["next_ingest_window"])
check("the raw calendar distance is still reported but relabelled",
      f["calendar_days_behind_today"] == 2 and "structural minimum" in f["calendar_lag_note"])
check("the word 'lag' is not used as a status", "lag" not in f["freshness_status"].lower())

# 05:34 sits inside the randomized-delay + run grace: still not late.
f = C.hme_freshness(SEP24, [ok_row("2026-09-24")], now=at(2026, 9, 26, 5, 34))
check("05:34 Chicago is inside the grace window, still CURRENT",
      f["freshness_status"] == "CURRENT" and f["expected_business_date"] == "2026-09-24",
      f'{f["freshness_status"]} / {f["expected_business_date"]}')

# ---------------------------------------------------------------------------
# 2 -- after the window, the expectation rolls forward
# ---------------------------------------------------------------------------
f = C.hme_freshness(SEP25, [ok_row("2026-09-25")], now=at(2026, 9, 26, 7, 0))
check("after the primary window, Sep 25 is expected and present -> CURRENT",
      f["freshness_status"] == "CURRENT" and f["expected_business_date"] == "2026-09-25",
      f'{f["freshness_status"]} / {f["expected_business_date"]}')

f = C.hme_freshness(SEP24, [ok_row("2026-09-24")], now=at(2026, 9, 26, 7, 0))
check("after the primary window with Sep 25 absent -> LATE",
      f["freshness_status"] == "LATE", f["freshness_status"])
check("LATE explains the retry may still recover it",
      "10:15" in f["freshness_reason"], f["freshness_reason"][:90])

f = C.hme_freshness(SEP24, [ok_row("2026-09-24")], now=at(2026, 9, 26, 12, 0))
check("after the retry window with Sep 25 still absent -> STALE",
      f["freshness_status"] == "STALE", f["freshness_status"])

# ---------------------------------------------------------------------------
# 3 -- retry-pending semantics
# ---------------------------------------------------------------------------
f = C.hme_freshness(SEP25, [bad_row("2026-09-25")], now=at(2026, 9, 26, 7, 0))
check("loaded but unreconciled before the retry window -> RETRY_PENDING",
      f["freshness_status"] == "RETRY_PENDING", f["freshness_status"])
check("RETRY_PENDING names the retry time", "10:15" in f["freshness_reason"])

f = C.hme_freshness(SEP25, [bad_row("2026-09-25")], now=at(2026, 9, 26, 12, 0))
check("still unreconciled after the retry window -> STALE",
      f["freshness_status"] == "STALE", f["freshness_status"])

f = C.hme_freshness(SEP25, [ok_row("2026-09-25")], now=at(2026, 9, 26, 12, 0))
check("reconciled after the retry window -> CURRENT",
      f["freshness_status"] == "CURRENT", f["freshness_status"])

check("no HME data at all is STALE, not CURRENT",
      C.hme_freshness(None, [], now=at(2026, 9, 26, 4, 0))["freshness_status"] == "STALE")

# ---------------------------------------------------------------------------
# 4 -- expected-date rule, stated directly
# ---------------------------------------------------------------------------
check("before the window: expected = Chicago today - 2",
      C.hme_expected_business_date(at(2026, 9, 26, 4, 0)) == date(2026, 9, 24))
check("after the window: expected = Chicago today - 1",
      C.hme_expected_business_date(at(2026, 9, 26, 7, 0)) == date(2026, 9, 25))
check("the boundary sits at 05:30 + grace, not at 05:30:00 sharp",
      C.hme_expected_business_date(at(2026, 9, 26, 5, 31)) == date(2026, 9, 24)
      and C.hme_expected_business_date(at(2026, 9, 26, 6, 30)) == date(2026, 9, 25))

# ---------------------------------------------------------------------------
# 5 -- Chicago time, not server UTC; DST-safe
# ---------------------------------------------------------------------------
# 03:00 UTC on Sep 26 is 22:00 Chicago on Sep 25 -- a DIFFERENT calendar day,
# and 22:00 is past that Chicago day's ingest window. Using the UTC date would
# give Sep 25 (Sep 26 - 1); using Chicago time gives Sep 24 (Sep 25 - 1).
utc_late = datetime(2026, 9, 26, 3, 0, tzinfo=ZoneInfo("UTC"))
got = C.hme_expected_business_date(utc_late)
check("the UTC date is not used as the business day",
      got == date(2026, 9, 24) and got != date(2026, 9, 25), str(got))

# CST side of the year: 07:00 UTC = 01:00 CST on the same date.
cst = datetime(2026, 12, 15, 7, 0, tzinfo=ZoneInfo("UTC"))
check("CST: before the window, expected = Chicago today - 2",
      C.hme_expected_business_date(cst) == date(2026, 12, 13),
      str(C.hme_expected_business_date(cst)))
cst_after = datetime(2026, 12, 15, 15, 0, tzinfo=ZoneInfo("UTC"))  # 09:00 CST
check("CST: after the window, expected = Chicago today - 1",
      C.hme_expected_business_date(cst_after) == date(2026, 12, 14),
      str(C.hme_expected_business_date(cst_after)))
# The spring-forward day has no 02:30 local; 05:30 is unaffected but assert it resolves.
dst_day = datetime(2026, 3, 8, 12, 0, tzinfo=ZoneInfo("UTC"))
check("the DST transition day still resolves an expected date",
      isinstance(C.hme_expected_business_date(dst_day), date))

# ---------------------------------------------------------------------------
# 6 -- the constants must track the real systemd units
# ---------------------------------------------------------------------------
pri = open("systemd/hme-daily-ingest.timer").read()
ret = open("systemd/hme-reconciliation-retry.timer").read()
m = re.search(r"OnCalendar=\*-\*-\* (\d{2}):(\d{2}):00 America/Chicago", pri)
check("primary constants match hme-daily-ingest.timer",
      m and (int(m.group(1)), int(m.group(2))) == (C.HME_PRIMARY_HOUR, C.HME_PRIMARY_MINUTE),
      m.group(0) if m else "not found")
m = re.search(r"OnCalendar=\*-\*-\* (\d{2}):(\d{2}):00 America/Chicago", ret)
check("retry constants match hme-reconciliation-retry.timer",
      m and (int(m.group(1)), int(m.group(2))) == (C.HME_RETRY_HOUR, C.HME_RETRY_MINUTE),
      m.group(0) if m else "not found")
check("the grace covers the timers' RandomizedDelaySec and the orchestrator retries",
      C.HME_WINDOW_GRACE_MINUTES >= (180 / 60) + (300 + 900) / 60,
      str(C.HME_WINDOW_GRACE_MINUTES))

# ---------------------------------------------------------------------------
# 7 -- availability is NOT relaxed by a friendlier explanation
# ---------------------------------------------------------------------------
src = open("chat_sql.py").read()
check("the old bare calendar subtraction is gone",
      "(datetime.now(ZoneInfo(\"America/Chicago\")).date() - latest).days" not in src)
check("freshness_lag_days is no longer emitted as a status figure",
      '"freshness_lag_days": lag' not in src)
check("the prompt tells the model to read freshness_status, not a day count",
      "FRESHNESS IS SCHEDULE-BASED" in src and "Do NOT call it a" in src)
check("the prompt keeps the availability block explicit",
      "the analysis is still blocked" in src)
check("blocked-domain context carries the schedule fields",
      '"next_ingest_window": h.get("next_ingest_window")' in src)

# live wiring
blk = C.meta_extract(None, "2026-09-24", "2026-09-26")["hme"]
for k in ("freshness_status", "expected_business_date",
          "next_expected_business_date", "next_ingest_window"):
    check(f"meta_extract exposes {k}", blk.get(k) is not None, str(blk.get(k)))
check("meta_extract no longer exposes freshness_lag_days",
      "freshness_lag_days" not in blk)
check("freshness_status is one of the defined states",
      blk["freshness_status"] in C.HME_FRESHNESS_STATES, blk["freshness_status"])

passed = sum(1 for _, ok, _ in results if ok)
failed = len(results) - passed
print(f"\n{'='*66}\nHME freshness schedule: {passed} passed, {failed} failed, {len(results)} total")
if failed:
    for n, ok, d in results:
        if not ok:
            print(f"  FAILED: {n} -- {d}")
sys.exit(1 if failed else 0)
