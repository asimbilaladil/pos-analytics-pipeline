#!/usr/bin/env python3
"""The authoritative AYG fiscal period calendar, and how chat resolves it.

Why this exists: fiscal-period resolution was deliberately BLOCKED until the
official AYG calendar by period arrived, because a guessed boundary silently
misstates every comparison built on it -- 4-4-5, 13x28 and "four weeks from
the 1st" all produce different, plausible-looking, wrong answers. These tests
pin the 26 official rows exactly as supplied and assert that nothing in the
code path invents, extrapolates or re-derives them.

The second half guards the comparison trap: a CURRENT period is partial, and
comparing its running total against a completed period's full total shows a
decline that is purely an artefact of counting fewer days.
"""
import os
import re
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from dotenv import load_dotenv

load_dotenv(".env", override=True)
import chat_sql  # noqa: E402
import psycopg2
import psycopg2.extras

# Own connection: chat_sql exposes no query helper, and the read-only chat role
# deliberately cannot see the raw calendar table that several of these checks
# must inspect.
_CONN = psycopg2.connect(
    host=os.environ["DB_HOST"], port=os.environ["DB_PORT"],
    dbname=os.environ["DB_NAME"], user=os.environ["DB_USER"],
    password=os.environ["DB_PASS"])
_CONN.autocommit = True


def db(sql, params=None, fetch="all"):
    with _CONN.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params or ())
        if fetch == "all":
            return cur.fetchall()
        if fetch == "one":
            return cur.fetchone()
        return None


results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


# The official calendar, transcribed from the source document. This literal is
# the test's own copy on purpose: if the migration or the DB ever drifts from
# what AYG published, these tests must fail rather than agree with the drift.
OFFICIAL = {
    (2026, 1): ("2026-01-01", "2026-01-28"),
    (2026, 2): ("2026-01-29", "2026-02-25"),
    (2026, 3): ("2026-02-26", "2026-03-25"),
    (2026, 4): ("2026-03-26", "2026-04-22"),
    (2026, 5): ("2026-04-23", "2026-05-20"),
    (2026, 6): ("2026-05-21", "2026-06-17"),
    (2026, 7): ("2026-06-18", "2026-07-15"),
    (2026, 8): ("2026-07-16", "2026-08-12"),
    (2026, 9): ("2026-08-13", "2026-09-09"),
    (2026, 10): ("2026-09-10", "2026-10-07"),
    (2026, 11): ("2026-10-08", "2026-11-04"),
    (2026, 12): ("2026-11-05", "2026-12-02"),
    (2026, 13): ("2026-12-03", "2026-12-30"),
    (2027, 1): ("2026-12-31", "2027-01-27"),
    (2027, 2): ("2027-01-28", "2027-02-24"),
    (2027, 3): ("2027-02-25", "2027-03-24"),
    (2027, 4): ("2027-03-25", "2027-04-21"),
    (2027, 5): ("2027-04-22", "2027-05-19"),
    (2027, 6): ("2027-05-20", "2027-06-16"),
    (2027, 7): ("2027-06-17", "2027-07-14"),
    (2027, 8): ("2027-07-15", "2027-08-11"),
    (2027, 9): ("2027-08-12", "2027-09-08"),
    (2027, 10): ("2027-09-09", "2027-10-06"),
    (2027, 11): ("2027-10-07", "2027-11-03"),
    (2027, 12): ("2027-11-04", "2027-12-01"),
    (2027, 13): ("2027-12-02", "2027-12-29"),
}

SRC = open("chat_sql.py").read()
GUIDE = chat_sql._system_static()
VIEW = "v_ayg_business_periods_llm"
TABLE = "ayg_business_periods"
MIG = open("migrations/49_ayg_business_periods.sql").read()
GRANT_MIG_PATH = "migrations/50_ayg_business_periods_grant.sql"
GRANT_MIG = open(GRANT_MIG_PATH).read() if os.path.exists(GRANT_MIG_PATH) else ""


def rows():
    """Every calendar row as loaded in the database."""
    out = db(
        f"SELECT fiscal_year, period_number, period_label, start_date, end_date, "
        f"source FROM {TABLE} ORDER BY fiscal_year, period_number")
    return {(r["fiscal_year"], r["period_number"]): r for r in out}


DB = rows()

# ---------------------------------------------------------------------------
print("\n-- all 26 official mappings, exactly as published --")

check("the calendar holds exactly 26 rows", len(DB) == 26, f"{len(DB)} rows")
for fy in (2026, 2027):
    n = sum(1 for k in DB if k[0] == fy)
    check(f"FY{fy} has all 13 periods", n == 13, f"{n} periods")

_mismatch = []
for key, (want_s, want_e) in OFFICIAL.items():
    r = DB.get(key)
    if not r or str(r["start_date"]) != want_s or str(r["end_date"]) != want_e:
        _mismatch.append(f"FY{key[0]} P{key[1]}: "
                         f"got {r and (str(r['start_date']), str(r['end_date']))}, "
                         f"want {(want_s, want_e)}")
check("every one of the 26 periods matches the official dates exactly",
      not _mismatch, "; ".join(_mismatch[:3]))

check("every period is labelled P<number>",
      all(DB[k]["period_label"] == f"P{k[1]}" for k in DB))
check("every row records the official source",
      all(DB[k]["source"] == "AYG_OFFICIAL_PERIOD_CALENDAR" for k in DB))

# ---------------------------------------------------------------------------
print("\n-- structural invariants of the published calendar --")

for fy in (2026, 2027):
    ks = sorted(k for k in DB if k[0] == fy)
    lens = {(DB[k]["end_date"] - DB[k]["start_date"]).days + 1 for k in ks}
    check(f"FY{fy}: every period is exactly 28 days", lens == {28}, str(lens))
    gaps = [f"P{ks[i+1][1]}" for i in range(len(ks) - 1)
            if DB[ks[i + 1]]["start_date"] != DB[ks[i]]["end_date"] + timedelta(days=1)]
    check(f"FY{fy}: periods are contiguous with no gap or overlap",
          not gaps, ",".join(gaps))
    starts = {DB[k]["start_date"].strftime("%A") for k in ks}
    check(f"FY{fy}: every period starts on the same weekday", len(starts) == 1,
          str(starts))

# The boundary most likely to be got wrong by guessing.
check("FY2027 P1 begins 2026-12-31, inside the 2026 calendar year",
      str(DB[(2027, 1)]["start_date"]) == "2026-12-31")
check("FY2026 P13 ends the day before FY2027 P1 begins",
      DB[(2027, 1)]["start_date"] == DB[(2026, 13)]["end_date"] + timedelta(days=1))
check("a fiscal year is 364 days, NOT a calendar year",
      sum((DB[k]["end_date"] - DB[k]["start_date"]).days + 1
          for k in DB if k[0] == 2026) == 364)

# ---------------------------------------------------------------------------
print("\n-- unpublished years must fail, never be extrapolated --")

for absent in (2025, 2028):
    check(f"FY{absent} is absent (never invented)",
          not any(k[0] == absent for k in DB))
check("the migration loads only the two published years",
      sorted({k[0] for k in DB}) == [2026, 2027])
check("the migration does not compute dates from a rule",
      not re.search(r"generate_series|interval\s*'(28|4)\s*(day|week)",
                    MIG, re.I))
check("the migration records provenance, not derivation",
      "AYG_OFFICIAL_PERIOD_CALENDAR" in MIG
      and re.search(r"NOT derived", MIG) is not None)
check("guidance forbids assuming 4-week or 4-4-5 arithmetic",
      re.search(r"never by assuming 4-week or 4-4-5", GUIDE) is not None)
check("guidance says an unloaded year must not be estimated",
      re.search(r"do NOT estimate it", GUIDE) is not None)

# ---------------------------------------------------------------------------
print("\n-- table constraints reject bad data --")

check("primary key is (fiscal_year, period_number)",
      re.search(r"PRIMARY KEY \(fiscal_year, period_number\)", MIG) is not None)
check("period_number is constrained to 1..13",
      re.search(r"period_number BETWEEN 1 AND 13", MIG) is not None)
check("end_date must not precede start_date",
      re.search(r"end_date >= start_date", MIG) is not None)

_rejected = []
for sql, why in [
    (f"INSERT INTO {TABLE} (fiscal_year,period_number,period_label,start_date,"
     f"end_date,source) VALUES (2026,14,'P14','2026-12-31','2027-01-27','x')",
     "period 14"),
    (f"INSERT INTO {TABLE} (fiscal_year,period_number,period_label,start_date,"
     f"end_date,source) VALUES (2029,1,'P1','2029-02-01','2029-01-01','x')",
     "end before start"),
    (f"INSERT INTO {TABLE} (fiscal_year,period_number,period_label,start_date,"
     f"end_date,source) VALUES (2026,8,'P8','2026-07-16','2026-08-12','dupe')",
     "duplicate key"),
]:
    try:
        db(sql, fetch=None)
        _rejected.append(why)
    except Exception:
        pass
check("the table rejects period 14, inverted dates and duplicate keys",
      not _rejected, "accepted: " + ",".join(_rejected))

# ---------------------------------------------------------------------------
print("\n-- the safe view: status and COMPLETE elapsed days --")

v = {(r["fiscal_year"], r["period_number"]): r
     for r in db(f"SELECT * FROM {VIEW}")}
today = db("SELECT (now() AT TIME ZONE 'America/Chicago')::date AS d",
           fetch="one")["d"]

check("the view exposes exactly the approved fields",
      set(v[(2026, 8)].keys()) == {
          "fiscal_year", "period_number", "period_label", "start_date",
          "end_date", "period_status", "days_total", "days_elapsed"},
      str(sorted(v[(2026, 8)].keys())))
check("the view carries no revenue, store or guest field",
      not any(re.search(r"revenue|order|store|establishment|customer|guest|price",
                        k) for k in v[(2026, 8)].keys()))
check("days_total is 28 for every period",
      {r["days_total"] for r in v.values()} == {28})

_bad = []
for key, r in v.items():
    want = ("FUTURE" if today < r["start_date"]
            else "COMPLETED" if today > r["end_date"] else "CURRENT")
    if r["period_status"] != want:
        _bad.append(f"FY{key[0]} P{key[1]}: {r['period_status']} != {want}")
check("period_status is FUTURE / CURRENT / COMPLETED against the Chicago date",
      not _bad, "; ".join(_bad[:3]))
check("all three statuses are representable", {"FUTURE", "COMPLETED"} <=
      {r["period_status"] for r in v.values()})

check("a COMPLETED period has days_elapsed == days_total",
      all(r["days_elapsed"] == r["days_total"]
          for r in v.values() if r["period_status"] == "COMPLETED"))
check("a FUTURE period has days_elapsed == 0",
      all(r["days_elapsed"] == 0
          for r in v.values() if r["period_status"] == "FUTURE"))

_cur = [r for r in v.values() if r["period_status"] == "CURRENT"]
check("exactly one period is CURRENT", len(_cur) == 1, f"{len(_cur)}")
if _cur:
    c = _cur[0]
    # today is in progress and has no settled sales, so it must NOT be counted
    expect = (min(c["end_date"], today - timedelta(days=1))
              - c["start_date"]).days + 1
    check("days_elapsed counts COMPLETE days only, excluding today",
          c["days_elapsed"] == expect,
          f"{c['days_elapsed']} vs expected {expect}")
    check("days_elapsed is strictly less than days_total mid-period",
          c["days_elapsed"] < c["days_total"])

# ---------------------------------------------------------------------------
print("\n-- P8/P9/P10 resolve to the official ranges --")

for pn, want in ((8, ("2026-07-16", "2026-08-12")),
                 (9, ("2026-08-13", "2026-09-09")),
                 (10, ("2026-09-10", "2026-10-07"))):
    r = v[(2026, pn)]
    check(f"FY2026 P{pn} = {want[0]}..{want[1]}",
          (str(r["start_date"]), str(r["end_date"])) == want,
          f"{r['start_date']}..{r['end_date']}")
check("P8 and P9 are COMPLETED, P10 is CURRENT",
      v[(2026, 8)]["period_status"] == "COMPLETED"
      and v[(2026, 9)]["period_status"] == "COMPLETED"
      and v[(2026, 10)]["period_status"] == "CURRENT")

# ---------------------------------------------------------------------------
print("\n-- alias and word-form coverage in the guidance --")

for alias in ("P8", "p8", "period 8", "period eight", "P. 8", "eighth period"):
    check(f"alias documented: {alias!r}", alias in GUIDE)
check("digit and word forms are both declared to resolve",
      re.search(r"Digits and words both resolve", GUIDE) is not None)
check("'current period' / 'this period' map to period_status = CURRENT",
      re.search(r'"current period" / "this period" -> the row with '
                r'period_status = CURRENT', GUIDE) is not None)
check("'last period' maps to the most recent COMPLETED period",
      re.search(r'"last period" -> the COMPLETED row with the highest '
                r'start_date', GUIDE) is not None)
check("PTD / period-to-date / P10-to-date are all supported",
      all(t in GUIDE for t in ("P10-to-date", "period-to-date", "PTD")))

# ---------------------------------------------------------------------------
print("\n-- never ask the user for dates --")

check("guidance forbids asking the user for raw dates",
      re.search(r"never by asking the user for\s+raw dates", GUIDE) is not None)
check("guidance forbids resolving periods from memory",
      re.search(r"never from memory", GUIDE) is not None)
check("the ONLY permitted question is the fiscal-year disambiguation",
      '"Do you mean P10 FY2026 or FY2027?"' in GUIDE
      and re.search(r"never ask the\s+user for start/end dates", GUIDE)
      is not None)
check("guidance says an unambiguous year must not trigger a question",
      re.search(r"do not ask", GUIDE, re.I) is not None)

# ---------------------------------------------------------------------------
print("\n-- partial vs full comparisons are forbidden --")

check("guidance states a CURRENT period is partial",
      "A CURRENT PERIOD IS PARTIAL" in GUIDE)
check("guidance names the artefact: fewer days looks like a decline",
      re.search(r"looks like a sales decline and is not one", GUIDE) is not None)
check("guidance prescribes equal elapsed-day windows",
      re.search(r"take the FIRST days_elapsed days of each comparison period",
                GUIDE) is not None)
check("guidance gives the exact window arithmetic",
      "start_date .. start_date + (days_elapsed - 1)" in GUIDE)
check("guidance explains why weekday mix already matches",
      re.search(r"same weekday mix", GUIDE) is not None)
check("guidance requires stating how far into the period we are",
      re.search(r"P10 is 25 days\s+into its 28-day period", GUIDE) is not None)
for metric in ("revenue", "REAL transactions", "AOV", "discount %",
               "store coverage"):
    check(f"period comparison must report {metric}", metric in GUIDE)
check("store coverage must not treat an absent store as zero",
      re.search(r"an absent store is not a zero", GUIDE) is not None)
check("a period total must not be projected unless asked",
      re.search(r"Do not project a period's final total unless", GUIDE)
      is not None)

# The equal-window arithmetic itself, independent of the model.
if _cur:
    n = _cur[0]["days_elapsed"]
    for pn in (8, 9):
        r = v[(2026, pn)]
        w_end = r["start_date"] + timedelta(days=n - 1)
        check(f"P{pn}'s equivalent first-{n}-day window ends {w_end}",
              (w_end - r["start_date"]).days + 1 == n and w_end <= r["end_date"])
    check("every comparison window is the same length",
          len({n, n, n}) == 1)
    check("the current window never reaches the period end",
          _cur[0]["start_date"] + timedelta(days=n - 1) < _cur[0]["end_date"])

# ---------------------------------------------------------------------------
print("\n-- HME resolves through the calendar with its rules intact --")

check("guidance says fiscal periods apply to HME too",
      "FISCAL PERIODS APPLY TO HME TOO" in GUIDE)
check("HME period questions resolve dates FIRST",
      re.search(r"resolves P9's\s+dates first", GUIDE) is not None)
for rule, pat in (("VERIFIED stores only", r"VERIFIED mapped stores only"),
                  ("no Downtown Houston", r"no Downtown Houston HME data"),
                  ("missing is not zero", r"missing HME is NOT\s+zero"),
                  ("cars are not orders", r"cars are NOT POS orders"),
                  ("no fabricated hourly", r"no fabricated hourly detail"),
                  ("completeness unchanged", r"completeness/freshness contract")):
    check(f"HME rule preserved: {rule}",
          re.search(pat, GUIDE) is not None)

# ---------------------------------------------------------------------------
print("\n-- validator wiring: allowlisted, ungated, raw table not exposed --")

check(f"{VIEW} is allowlisted", VIEW in chat_sql._ALLOWED_RELATIONS)
check(f"{VIEW} implies NO domain (resolving a period starts no gate)",
      chat_sql._RELATION_DOMAIN.get(VIEW, "missing") is None)
check(f"{VIEW} is reference data, so it needs no scope declaration",
      VIEW in chat_sql._REFERENCE_RELATIONS)
check("the raw calendar table is NOT allowlisted for chat",
      TABLE not in chat_sql._ALLOWED_RELATIONS)


def _rejects(sql):
    try:
        chat_sql._validate(sql)
    except chat_sql.SqlError:
        return True
    return False


def _accepts(sql):
    try:
        chat_sql._validate(sql)
    except chat_sql.SqlError as e:
        print(f"      (unexpected rejection: {e})")
        return False
    return True


check("the validator accepts a plain calendar lookup",
      _accepts(f"SELECT period_label, start_date, end_date FROM {VIEW} "
               f"WHERE fiscal_year = 2026"))
check("the validator rejects the raw calendar table",
      _rejects(f"SELECT * FROM {TABLE}"))
check("the validator still rejects writes to the view",
      _rejects(f"DELETE FROM {VIEW}"))
check("joining the calendar does not exempt a gated fact table",
      chat_sql.DOMAIN_REVEL in chat_sql.domains_for_relations(
          {VIEW, "features_daily_summary_v2"}))
check("the calendar alone implies no domain at all",
      chat_sql.domains_for_relations({VIEW}) == set(),
      str(chat_sql.domains_for_relations({VIEW})))

# ---------------------------------------------------------------------------
print("\n-- grants follow the migration separation convention --")

_g = db(
    "SELECT table_name, privilege_type FROM information_schema.role_table_grants "
    "WHERE grantee='laynes_ro' AND table_name IN (%s, %s)", (VIEW, TABLE))
_gset = {(r["table_name"], r["privilege_type"]) for r in _g}
check("laynes_ro has SELECT on the safe view", (VIEW, "SELECT") in _gset)
check("laynes_ro has NO privilege on the raw calendar table",
      not any(t == TABLE for t, _ in _gset), str(sorted(_gset)))
check("migration 49 grants nothing (grants stay separate)",
      "GRANT" not in "\n".join(
          l for l in MIG.splitlines() if not l.strip().startswith("--")).upper())
_g50 = "\n".join(l for l in GRANT_MIG.splitlines()
                 if not l.strip().startswith("--"))
check("migration 50 exists and grants only the view",
      re.search(r"GRANT\s+SELECT\s+ON\s+" + VIEW + r"\s+TO\s+laynes_ro",
                _g50, re.I) is not None)
check("migration 50 contains exactly one GRANT",
      len(re.findall(r"\bGRANT\b", _g50, re.I)) == 1)
check("migration 50 never grants the raw table",
      not re.search(r"GRANT[^;]*\b" + TABLE + r"\b", _g50, re.I))

# ---------------------------------------------------------------------------
print("\n-- this feature stands alone --")

# These assertions deliberately do NOT require the pricing phase to be present.
# An earlier version asserted that the pricing guidance and tool-loop changes
# existed, which coupled this suite to an unfinished, separately-tracked phase:
# the fiscal tests would have failed in a calendar-only tree for reasons that
# have nothing to do with the calendar. What matters here is that the calendar
# feature is self-contained and carries no pricing files with it.
check("fiscal-period guidance is present and non-trivial",
      "FISCAL PERIODS" in GUIDE and len(chat_sql.BUSINESS_PERIOD_GUIDANCE) > 1500,
      f"{len(chat_sql.BUSINESS_PERIOD_GUIDANCE)} chars")
check("period guidance is its own composable block, not inlined",
      "BUSINESS_PERIOD_GUIDANCE" in SRC
      and "{BUSINESS_PERIOD_GUIDANCE}" in SRC)
check("the period block carries no pricing text",
      not re.search(r"PRICING QUESTIONS|modal_menu_price|volume-response",
                    chat_sql.BUSINESS_PERIOD_GUIDANCE))
check("the calendar feature needs no pricing relation",
      "v_item_price_history_llm" not in chat_sql.BUSINESS_PERIOD_GUIDANCE)
check("core request bounds are untouched by this phase",
      chat_sql.MAX_LOOPS == 6
      and chat_sql.REQUEST_DEADLINE_SECONDS == 240
      and chat_sql.STATEMENT_TIMEOUT_MS == 15000)

# The calendar commit must not drag pricing artefacts in with it.
_pricing_files = ["migrations/47_item_price_history.sql",
                  "migrations/48_item_price_history_grant.sql",
                  "tests/test_pricing_questions.py"]
import subprocess
_tracked = subprocess.run(["git", "ls-files"], capture_output=True, text=True,
                          cwd=".").stdout.split()
_leaked = [f for f in _pricing_files if f in _tracked]
check("no pricing migration or test is tracked by this commit",
      not _leaked, "leaked: " + ",".join(_leaked))
_cal_files = ["migrations/49_ayg_business_periods.sql",
              "migrations/50_ayg_business_periods_grant.sql",
              "tests/test_fiscal_periods.py"]
check("every calendar file exists on disk",
      all(os.path.exists(f) for f in _cal_files),
      str([f for f in _cal_files if not os.path.exists(f)]))

# ---------------------------------------------------------------------------
passed = sum(1 for _, ok, _ in results if ok)
failed = len(results) - passed
print(f"\n{'='*66}\nfiscal period tests: {passed} passed, {failed} failed, "
      f"{len(results)} total")
if failed:
    for n, ok, d in results:
        if not ok:
            print(f"  FAILED: {n} -- {d}")
sys.exit(1 if failed else 0)
