#!/usr/bin/env python3
"""Deterministic tests for R365 FINANCIAL ingestion
(r365_financial_load / r365_financial_ingest).

Run:  venv/bin/python tests/test_r365_financial_ingestion.py     (exit 0 = pass)

Payloads are SYNTHETIC, shaped exactly like the proven financial API contract
(2026-10-16), so nothing here depends on the network. Database checks run in a
transaction that is ROLLED BACK and use FY2027 P13, a period with no data.
"""
import copy
import datetime
import json
import os
import stat
import sys
import tempfile
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from dotenv import load_dotenv

load_dotenv(".env", override=True)
import r365_financial_load as L  # noqa: E402
import r365_financial_ingest as I  # noqa: E402

D = datetime.date
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


def raises(fn, exc=L.SourceSchemaError):
    try:
        fn()
    except exc as e:
        return str(e) or True
    return False


# ---------------------------------------------------------------------------
# Synthetic source
# ---------------------------------------------------------------------------
A_ID, B_ID = "83a5757d-6ffc-41c1-875a-5adb6e693e99", "fe62609a-7f59-40e5-a023-fce3636191f3"
X_ID = "ddeb0626-39e9-4e07-a228-796c294ef0fb"           # a non-store entity
MAPPING = {
    A_ID: {"r365_store_name": "LCF Airtex", "establishment_id": 32, "status": "VERIFIED",
           "active": True, "opening_date": None},
    B_ID: {"r365_store_name": "LCF Beaumont", "establishment_id": 14, "status": "VERIFIED",
           "active": True, "opening_date": None},
}
FY, PN = 2027, 13
CAL = (D(2027, 12, 2), D(2027, 12, 29))
SYNC = {"lastSuccessfulSyncAt": "2026-10-16T16:59:44.903Z", "lastSuccessfulSyncMode": "DAILY",
        "lastRunStatus": "SUCCESS", "lastRunMode": "DAILY",
        "lastRunAt": "2026-10-16T16:59:37.961Z", "stale": False}


def srow(loc, name, cat, amount, status="COMPLETE", tx=1):
    amount = Decimal(amount)
    return {"locationId": loc, "locationName": name, "fiscalYear": FY, "periodNumber": PN,
            "periodStart": CAL[0].isoformat(), "periodEnd": CAL[1].isoformat(),
            "analyticsCategory": cat, "expenseAmount": amount, "approvedAmount": amount,
            "sourceQuality": "TRANSACTION_DETAIL", "transactionDetailAmount": amount,
            "migratedSummaryAmount": 0, "transactionCount": tx, "accountCount": 1,
            "dataStatus": status, "coverageStart": "2024-04-07", "coverageEnd": "2026-10-16",
            "transactionDetailStart": "2024-04-07"}


def summary(rows, category=None, no_activity=(), candidates=(), status="COMPLETE"):
    f = {"fiscal_year": FY, "period_number": PN}
    if category:
        f["analytics_category"] = category
    p = {"filters": f, "includeUnapproved": False, "dataStatus": status,
         "statusReasons": [], "sync": dict(SYNC), "dataQuality": {},
         "locationsWithNoActivity": [
             {"locationId": loc, "locationName": n, "fiscalYear": FY, "periodNumber": PN}
             for loc, n in no_activity],
         "notes": [], "totals": {"expenseAmount": sum(Decimal(r["expenseAmount"]) for r in rows)},
         "data": rows, "pagination": {"limit": 5000, "offset": 0, "returned": len(rows)}}
    if candidates:
        p["unverifiedCandidates"] = list(candidates)
    return p


def line(i, loc, name, acct_id, acct_no, cat, status, amount, d="2027-12-10", tx=None,
         fy=FY, pn=PN, approved="Approved", cls="TRANSACTION_DETAIL"):
    amount = Decimal(amount)
    return {"detailId": f"d-{i}", "transactionId": tx or f"t-{i}", "transactionNumber": str(i),
            "transactionType": "APInvoice", "status": approved, "locationId": loc,
            "locationName": name, "accountingDate": d, "fiscalYear": fy, "periodNumber": pn,
            "glAccountId": acct_id, "accountNumber": acct_no, "accountName": acct_no,
            "accountType": "Operating Expense", "analyticsCategory": cat,
            "mappingStatus": status, "debitAmount": max(amount, Decimal(0)),
            "creditAmount": max(-amount, Decimal(0)), "expenseAmount": amount,
            "sourceClass": cls}


def candidate(loc, acct, name, cat, amount):
    return {"locationId": loc, "fiscalYear": FY, "periodNumber": PN, "accountNumber": acct,
            "accountName": name, "candidateCategory": cat, "expenseAmount": Decimal(amount)}


def build():
    elec = summary([srow(A_ID, "LCF Airtex", "ELECTRICITY", "100.50")], "ELECTRICITY",
                   no_activity=[(B_ID, "LCF Beaumont")])
    allp = summary([srow(A_ID, "LCF Airtex", "ELECTRICITY", "100.50"),
                    srow(A_ID, "LCF Airtex", "UNMAPPED", "500.00", "UNMAPPED_ACCOUNTS", 2),
                    srow(X_ID, "AYG Operation Inc", "UNMAPPED", "10000", "PARTIAL")],
                   status="PARTIAL",
                   candidates=[candidate(A_ID, "6200-01", "Gas", "GAS", "20.00")])
    lines = [line(1, A_ID, "LCF Airtex", "acc-elec", "6200-03", "ELECTRICITY", "VERIFIED", "100.50"),
             line(2, A_ID, "LCF Airtex", "acc-rent", "7500-01", "UNMAPPED", "UNMAPPED", "480.00"),
             line(3, A_ID, "LCF Airtex", "acc-gas", "6200-01", "GAS", "CANDIDATE", "20.00"),
             line(4, X_ID, "AYG Operation Inc", "acc-rent", "7500-01", "UNMAPPED", "UNMAPPED",
                  "10000")]
    return {"ELECTRICITY": elec}, allp, lines


def plan(cats=None, allp=None, lines=None, mapping=MAPPING, cal=CAL):
    c, a, li = build()
    return L.plan_period(FY, PN, cal, cats or c, allp or a, li if lines is None else lines,
                         mapping, ["ELECTRICITY"])


# ---------------------------------------------------------------------------
print("1. source API authentication / configuration")
with tempfile.TemporaryDirectory() as td:
    envf = os.path.join(td, "r365.env")
    with open(envf, "w") as fh:
        fh.write("# c\nR365_API_BASE=https://weekly-report.example.com/weekly-data\n"
                 "R365_API_TOKEN='abc123SECRET'\n")
    base, tok = I.read_env_file(envf)
    check("base is reduced to scheme+host (financial paths are absolute)",
          base == "https://weekly-report.example.com", base)
    check("token is read from the 0600 env file", tok == "abc123SECRET")
    with open(envf, "w") as fh:
        fh.write("R365_API_BASE=https://x\nR365_API_TOKEN=REPLACE_ME\n")
    check("placeholder token is a CONFIG error", raises(lambda: I.read_env_file(envf), I.FetchError))


class Resp:
    def __init__(self, code, body=b"{}"):
        self.status_code, self.content, self.text = code, body, body.decode()


class Sess:
    def __init__(self, codes):
        self.codes, self.calls = list(codes), []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        c = self.codes.pop(0)
        return Resp(*c) if isinstance(c, tuple) else Resp(c)


s = Sess([401])
src = I.Source("https://h", "SECRETTOKEN", session=s, sleep=lambda _: None)
e = raises(lambda: src.get("/financial/gl/accounts"), I.FetchError)
check("HTTP 401 -> AUTH failure, not retried", e and len(s.calls) == 1, str(e))
check("AUTH error message never contains the token", "SECRETTOKEN" not in str(e))
check("token travels only in the Authorization header",
      s.calls[0]["headers"]["Authorization"] == "Bearer SECRETTOKEN"
      and "SECRETTOKEN" not in s.calls[0]["url"]
      and "SECRETTOKEN" not in json.dumps(s.calls[0]["params"]))
s = Sess([503, 503, (200, b'{"ok": 1}')])
src = I.Source("https://h", "T", session=s, sleep=lambda _: None)
check("5xx is retried, then succeeds", src.get("/x") == b'{"ok": 1}' and len(s.calls) == 3)
s = Sess([400])
check("HTTP 400 is not retried", raises(lambda: I.Source("https://h", "T", session=s,
                                                          sleep=lambda _: None).get("/x"),
                                        I.FetchError) and len(s.calls) == 1)
check("a payload carrying a token-shaped key is rejected",
      raises(lambda: L.parse_payload(b'{"data": [], "authorization": "x"}')))


def pages(lines, page=2, total=None, shift=False, unapproved=True):
    out = []
    for i in range(0, max(len(lines), 1), page):
        t = total if total is not None else len(lines)
        if shift and i > 0:
            t += 1
        out.append((200, json.dumps({"data": lines[i:i + page], "includeUnapproved": unapproved,
                                     "pagination": {"limit": page, "offset": i, "total": t}},
                                    default=str).encode()))
    return out


_, _, LINES = build()
s = Sess(pages(LINES))
got = I.Source("https://h", "T", session=s, sleep=lambda _: None).detail({"fiscal_year": FY})
check("detail paging collects every line across pages", len(got) == 4 and len(s.calls) == 2)
check("detail paging sends limit/offset", s.calls[1]["params"]["offset"] == 2)
check("detail fetch requests include_unapproved=true (Approved AND Unapproved stored)",
      all(c["params"]["include_unapproved"] == "true" for c in s.calls))
s2 = Sess(pages(LINES, unapproved=False))
check("a detail feed that ignores include_unapproved fails closed",
      raises(lambda: I.Source("https://h", "T", session=s2, sleep=lambda _: None).detail({})))
s = Sess(pages(LINES, shift=True))
check("a total that changes mid-scan fails closed",
      raises(lambda: I.Source("https://h", "T", session=s, sleep=lambda _: None).detail({})))

# ---------------------------------------------------------------------------
print("2. financial API parsing")
acc = {"data": [
    {"accountId": "a1", "accountNumber": "6200-03", "accountName": "Electric",
     "accountType": "Operating Expense", "parentAccountId": "p", "parentAccountNumber": "6200",
     "analyticsCategory": "ELECTRICITY", "mappingStatus": "VERIFIED"},
    {"accountId": "a2", "accountNumber": "6200-01", "accountName": "Gas",
     "accountType": "Operating Expense", "parentAccountId": "p", "parentAccountNumber": "6200",
     "analyticsCategory": "GAS", "mappingStatus": "CANDIDATE"},
    {"accountId": "a3", "accountNumber": "7500-01", "accountName": "Rent",
     "accountType": "Operating Expense", "parentAccountId": None, "parentAccountNumber": None,
     "analyticsCategory": None, "mappingStatus": "UNMAPPED"}],
    "pagination": {"limit": 1000, "offset": 0, "total": 3}}
pa = L.plan_accounts(acc)
check("accounts parse; only VERIFIED categories are trusted",
      pa["verified_categories"] == ["ELECTRICITY"] and len(pa["rows"]) == 3)
bad = copy.deepcopy(acc)
bad["pagination"]["total"] = 400
check("accounts: truncated listing fails closed", raises(lambda: L.plan_accounts(bad)))
bad = copy.deepcopy(acc)
bad["data"][1]["mappingStatus"] = "MAYBE"
check("accounts: unknown mapping status fails closed", raises(lambda: L.plan_accounts(bad)))
p = L.parse_payload(b'{"x": 11443.755799999999}')
check("money parses exactly (Decimal, no float rounding)",
      p["x"] == Decimal("11443.755799999999"))

P = plan()
erow = {(r["r365_location_id"], r["analytics_category"]): r for r in P["expense_rows"]}
check("ELECTRICITY row parsed with numeric amount",
      erow[(A_ID, "ELECTRICITY")]["expense_amount"] == Decimal("100.50")
      and isinstance(erow[(A_ID, "ELECTRICITY")]["transaction_count"], int))
check("UNMAPPED bucket is kept (preserved, not discarded)", (A_ID, "UNMAPPED") in erow)
check("candidate (gas) preserved as unverified, not folded into ELECTRICITY",
      len(P["candidate_rows"]) == 1 and P["candidate_rows"][0]["candidate_category"] == "GAS"
      and erow[(A_ID, "ELECTRICITY")]["expense_amount"] == Decimal("100.50"))
check("period status COMPLETE when the VERIFIED category is COMPLETE", P["status"] == "COMPLETE")
check("summary reconciles to GL detail", P["reconciliation"]["mismatches"] == 0
      and P["reconciliation"]["buckets_checked"] == 3)

c, a, li = build()
a["data"][0]["expenseAmount"] = Decimal("100.49")
check("unfiltered vs category summary disagreement fails closed",
      raises(lambda: plan(allp=a), L.ReconciliationError))
c, a, li = build()
li[0]["expenseAmount"] = li[0]["debitAmount"] = Decimal("90.50")
check("summary vs GL-detail disagreement fails closed",
      raises(lambda: plan(lines=li), L.ReconciliationError))
c, a, li = build()
c["ELECTRICITY"]["data"][0]["periodStart"] = "2027-12-03"
check("period dates differing from the official calendar fail closed",
      "official calendar" in str(raises(lambda: plan(cats=c))))
c, a, li = build()
c["ELECTRICITY"]["filters"]["period_number"] = 12
check("wrong echoed period fails closed", raises(lambda: plan(cats=c)))
c, a, li = build()
li.append(copy.deepcopy(li[0]))
check("duplicate detail line across pages fails closed", raises(lambda: plan(lines=li)))
c, a, li = build()
li.append(line(5, A_ID, "LCF Airtex", "acc-elec", "6200-03", "ELECTRICITY", "VERIFIED", "999",
               approved="Unapproved"))
PU = plan(lines=li)
check("unapproved line is KEPT as a transaction but excluded from summary reconciliation",
      PU["transaction_stats"]["unapproved"] == 1 and PU["transaction_stats"]["lines"] == 5
      and {r["account_number"]: r["expense_amount"] for r in PU["account_period_rows"]
           if r["r365_location_id"] == A_ID}["6200-03"] == Decimal("100.50"))
c, a, li = build()
li[1]["status"] = "Pending"
check("unknown line status fails closed", raises(lambda: plan(lines=li)))
c, a, li = build()
li[1]["fiscalYear"], li[1]["periodNumber"] = 2027, 12
check("detail line from another period fails closed", raises(lambda: plan(lines=li)))
c, a, li = build()
c["ELECTRICITY"]["pagination"]["returned"] = 5000
check("truncated summary fails closed", raises(lambda: plan(cats=c)))

# ---------------------------------------------------------------------------
print("3. store mapping")
check("VERIFIED location -> establishment_id",
      erow[(A_ID, "ELECTRICITY")]["establishment_id"] == 32)
check("non-store entity kept with NULL establishment_id (never exposed)",
      erow[(X_ID, "UNMAPPED")]["establishment_id"] is None
      and P["unmapped_locations"][0]["r365_location_id"] == X_ID)
m2 = copy.deepcopy(MAPPING)
m2[A_ID]["status"] = "CANDIDATE"
P2 = plan(mapping=m2)
check("a non-VERIFIED mapping is not used",
      all(r["establishment_id"] is None for r in P2["expense_rows"] if r["r365_location_id"] == A_ID))
check("mapping is by locationId, never by name: renamed source store still maps",
      plan(cats={"ELECTRICITY": (lambda c: (c["data"][0].update(locationName="Renamed"), c)[1])(
          build()[0]["ELECTRICITY"])})["expense_rows"][0]["establishment_id"] == 32)

# ---------------------------------------------------------------------------
print("4. missing != zero / no-activity locations")
na = erow[(B_ID, "ELECTRICITY")]
check("no-activity store becomes a NO_ACTIVITY row", na["row_kind"] == "NO_ACTIVITY"
      and na["data_status"] == "NO_ACTIVITY")
check("no-activity amount is NULL, never 0",
      na["expense_amount"] is None and na["transaction_count"] is None)
check("no-activity count reported", P["no_activity_rows"] == 1)
c, a, li = build()
c["ELECTRICITY"]["locationsWithNoActivity"] = []
P3 = plan(cats=c)
check("a VERIFIED store neither posted nor listed -> unaccounted, run PARTIAL, no row invented",
      P3["status"] == "PARTIAL" and P3["unaccounted_stores"] == {"ELECTRICITY": ["LCF Beaumont"]}
      and (B_ID, "ELECTRICITY") not in {(r["r365_location_id"], r["analytics_category"])
                                         for r in P3["expense_rows"]})
c, a, li = build()
c["ELECTRICITY"]["locationsWithNoActivity"].append(
    {"locationId": A_ID, "locationName": "LCF Airtex", "fiscalYear": FY, "periodNumber": PN})
check("a store both posted and no-activity fails closed", raises(lambda: plan(cats=c)))
c, a, li = build()
c["ELECTRICITY"]["dataStatus"] = "PARTIAL"
check("source PARTIAL (open period) -> run PARTIAL", plan(cats=c)["status"] == "PARTIAL")

# ---------------------------------------------------------------------------
print("4b. transaction lines (pure)")
T = {r["source_transaction_detail_id"]: r for r in P["transaction_rows"]}
t1 = T["d-1"]
check("transaction parsed: ids, type, status, account, amounts",
      t1["source_transaction_id"] == "t-1" and t1["transaction_type"] == "APInvoice"
      and t1["transaction_status"] == "Approved" and t1["gl_account_number"] == "6200-03"
      and t1["gl_account_name"] == "6200-03" and t1["expense_amount"] == Decimal("100.50")
      and t1["fiscal_year"] == FY and t1["accounting_date"] == D(2027, 12, 10))
check("every line becomes one transaction row", len(T) == 4)
check("debit - credit = expense (debit line)",
      t1["debit_amount"] - t1["credit_amount"] == t1["expense_amount"])
rev = line(6, A_ID, "LCF Airtex", "acc-elec", "6200-03", "ELECTRICITY", "VERIFIED", "-25.00")
r6 = L.parse_transaction(rev, "x", (FY, PN))
check("a credit/reversal line is negative and reduces expense",
      r6["credit_amount"] == Decimal("25.00") and r6["expense_amount"] == Decimal("-25.00"))
bad = copy.deepcopy(rev)
bad["expenseAmount"] = Decimal("25.00")
check("expenseAmount that is not debit - credit fails closed",
      raises(lambda: L.parse_transaction(bad, "x", (FY, PN))))
twin_a = line(7, A_ID, "LCF Airtex", "acc-elec", "6200-03", "ELECTRICITY", "VERIFIED", "50", tx="T")
twin_b = line(8, A_ID, "LCF Airtex", "acc-elec", "6200-03", "ELECTRICITY", "VERIFIED", "50", tx="T")
tw = L.plan_transactions([twin_a, twin_b], MAPPING, (FY, PN))
check("identical date/amount/account/store lines with different detailIds are BOTH kept",
      len(tw) == 2 and {r["source_transaction_detail_id"] for r in tw} == {"d-7", "d-8"})
split_a = line(9, A_ID, "LCF Airtex", "acc-x", "7000", "UNMAPPED", "UNMAPPED", "10", tx="HDR")
split_b = line(10, B_ID, "LCF Beaumont", "acc-x", "7000", "UNMAPPED", "UNMAPPED", "15", tx="HDR")
sp = {r["source_transaction_detail_id"]: r["establishment_id"]
      for r in L.plan_transactions([split_a, split_b], MAPPING, (FY, PN))}
check("one transaction spanning two stores maps each LINE to its own store",
      sp == {"d-9": 32, "d-10": 14})
check("non-store entity line keeps a NULL establishment_id", T["d-4"]["establishment_id"] is None)
check("category/mapping retained per line: VERIFIED vs CANDIDATE vs UNMAPPED",
      (T["d-1"]["mapping_status"], T["d-3"]["mapping_status"], T["d-2"]["mapping_status"])
      == ("VERIFIED", "CANDIDATE", "UNMAPPED") and T["d-3"]["analytics_category"] == "GAS")
check("line from another period fails closed",
      raises(lambda: L.parse_transaction(line(11, A_ID, "x", "a", "1", "U", "UNMAPPED", "1",
                                              fy=2027, pn=12), "x", (FY, PN))))

# ---------------------------------------------------------------------------
print("5. fiscal periods / history")
cal = {(2026, 9): (D(2026, 8, 13), D(2026, 9, 9)), (2026, 10): (D(2026, 9, 10), D(2026, 10, 7)),
       (2026, 11): (D(2026, 10, 8), D(2026, 11, 4)), (2026, 12): (D(2026, 11, 5), D(2026, 12, 2))}
check("scheduled (weekday): every period touching the last 42 days (> 5-week source repair)",
      I.scheduled_periods(cal, D(2026, 10, 16)) == [(2026, 9), (2026, 10), (2026, 11)])
check("scheduled overlap drops a period once it ends > 42 days ago",
      I.scheduled_periods(cal, D(2026, 10, 25)) == [(2026, 10), (2026, 11)])
check("overlap is at least the source's 5-week repair window", I.OVERLAP_DAYS >= 35)
check("scheduled (Monday repair): every elapsed period of current/previous FY",
      I.scheduled_periods(cal, D(2026, 10, 19)) == [(2026, 9), (2026, 10), (2026, 11)])
check("future periods are never targeted",
      (2026, 12) not in I.elapsed_periods(cal, D(2026, 10, 16)))
check("history range ends the day before the first official period",
      I.history_range(cal)[1] == D(2026, 8, 12))
hl = [line(10, A_ID, "LCF Airtex", "acc-elec", "6200-03", "ELECTRICITY", "VERIFIED", "50",
           d="2025-03-05", fy=None, pn=None),
      line(11, A_ID, "LCF Airtex", "acc-elec", "6200-03", "ELECTRICITY", "VERIFIED", "60",
           d="2025-03-28", fy=None, pn=None),
      line(12, A_ID, "LCF Airtex", "acc-elec", "6200-03", "ELECTRICITY", "VERIFIED", "70",
           d="2025-04-02", fy=None, pn=None)]
H = L.plan_history(hl, MAPPING, D(2000, 1, 1), D(2025, 12, 31))
hk = {r["bucket_key"]: r for r in H["account_period_rows"]}
check("pre-calendar history grouped by CALENDAR MONTH with no fiscal period",
      set(hk) == {"CM-2025-03", "CM-2025-04"}
      and all(r["fiscal_year"] is None and r["period_number"] is None for r in hk.values())
      and hk["CM-2025-03"]["expense_amount"] == Decimal("110")
      and hk["CM-2025-03"]["bucket_end"] == D(2025, 3, 31))
hl2 = copy.deepcopy(hl)
hl2[0]["fiscalYear"], hl2[0]["periodNumber"] = 2025, 3
check("history line carrying a fiscal period fails closed (no invented FY2025)",
      raises(lambda: L.plan_history(hl2, MAPPING, D(2000, 1, 1), D(2025, 12, 31))))
ap = {r["account_number"]: r for r in P["account_period_rows"] if r["r365_location_id"] == A_ID}
check("every account (rent, gas, electric) preserved at account x period grain",
      set(ap) == {"6200-03", "7500-01", "6200-01"} and ap["7500-01"]["expense_amount"] == 480)

# ---------------------------------------------------------------------------
print("6. idempotent local upsert + DB constraints (rolled back)")
conn = L.connect()
try:
    cur = conn.cursor()
    cur.execute("""INSERT INTO r365_financial_ingest_run (batch_id, trigger, target_kind,
                     requested_fiscal_year, requested_period_number, loader_version)
                   VALUES ('test', 'manual', 'PERIOD', %s, %s, 'test') RETURNING id""", (FY, PN))
    run_id = cur.fetchone()[0]
    fetch = {"http_requests": 3, "fetch_seconds": 1}
    first = L.write_period(conn, run_id, plan(), fetch)
    second = L.write_period(conn, run_id, plan(), fetch)
    check("first write inserts every row",
          first["expense"][0] == 4 and first["expense"][1] == 4, str(first["expense"]))
    check("re-writing the same payload changes nothing",
          second["expense"][1] == 0 and second["candidates"][1] == 0
          and second["account_period"][1] == 0, str(second))
    c, a, li = build()
    a["data"] = a["data"][:2]
    a["pagination"]["returned"] = 2
    li = li[:3]
    third = L.write_period(conn, run_id, plan(allp=a, lines=li), fetch)
    check("a bucket the source stops reporting is deleted (faithful replace)",
          third["expense"][2] == 1, str(third["expense"]))
    cur.execute("""SELECT expense_amount, row_kind FROM r365_financial_period_expense
                    WHERE fiscal_year = %s AND period_number = %s AND r365_location_id = %s
                      AND analytics_category = 'ELECTRICITY'""", (FY, PN, B_ID))
    check("NO_ACTIVITY stored with NULL amount", cur.fetchone() == (None, "NO_ACTIVITY"))
    cur.execute("SELECT count(*) FROM r365_financial_period_expense WHERE fiscal_year = 2026")
    real_before = cur.fetchone()[0]
    check("writing FY2027 P13 did not touch real FY2026 rows", real_before > 0)

    def violates(sql, args):
        cur.execute("SAVEPOINT sp")
        try:
            cur.execute(sql, args)
            ok = False
        except Exception:  # noqa: BLE001
            ok = True
        cur.execute("ROLLBACK TO SAVEPOINT sp")
        return ok
    ins = ("INSERT INTO r365_financial_period_expense (r365_location_id, fiscal_year, "
           "period_number, analytics_category, store_name, period_start, period_end, row_kind, "
           "expense_amount, data_status, ingest_run_id) VALUES (%s,%s,%s,'ELECTRICITY','x',"
           "'2025-01-01','2025-01-28',%s,%s,%s,%s)")
    check("DB rejects an invented fiscal period (FY2025)",
          violates(ins, ("z", 2025, 1, "ACTIVITY", 1, "COMPLETE", run_id)))
    check("DB rejects NO_ACTIVITY with an amount (missing must be NULL)",
          violates(ins, ("z", 2027, 13, "NO_ACTIVITY", 0, "NO_ACTIVITY", run_id)))
    check("DB rejects ACTIVITY without an amount",
          violates(ins, ("z", 2027, 13, "ACTIVITY", None, "COMPLETE", run_id)))
    check("DB rejects a calendar-month bucket carrying a fiscal period",
          violates("INSERT INTO r365_financial_account_period (r365_location_id, gl_account_id, "
                   "bucket_key, bucket_kind, fiscal_year, period_number, bucket_start, bucket_end,"
                   " location_name, account_number, expense_amount, debit_amount, credit_amount,"
                   " migrated_summary_amount, line_count, transaction_count, min_accounting_date,"
                   " max_accounting_date, ingest_run_id) VALUES ('z','a','CM-2025-01',"
                   "'CALENDAR_MONTH',2026,1,'2025-01-01','2025-01-31','x','1',1,1,0,0,1,1,"
                   "'2025-01-02','2025-01-02',%s)", (run_id,)))

    # --- transaction table: UPSERT by source detail id
    cur.execute("DELETE FROM r365_financial_transaction WHERE fiscal_year = %s "
                "AND period_number = %s", (FY, PN))
    c, a, li = build()
    li.append(line(5, A_ID, "LCF Airtex", "acc-elec", "6200-03", "ELECTRICITY", "VERIFIED",
                   "999", approved="Unapproved"))
    t1 = L.write_period(conn, run_id, plan(lines=li), fetch)["transactions"]
    t2 = L.write_period(conn, run_id, plan(lines=li), fetch)["transactions"]
    cur.execute("SELECT count(*), count(DISTINCT source_transaction_detail_id) FROM "
                "r365_financial_transaction WHERE fiscal_year = %s AND period_number = %s", (FY, PN))
    n, nd = cur.fetchone()
    check("transactions: first sync inserts every line (Approved + Unapproved)",
          t1[0] == 5 and t1[1] == 5 and n == 5, str(t1))
    check("transactions: re-sync creates no duplicates and changes nothing",
          t2[1] == 0 and t2[2] == 0 and n == nd == 5, str(t2))
    li2 = copy.deepcopy(li)
    li2[4]["status"] = "Approved"            # the unapproved line gets approved...
    li2[4]["expenseAmount"] = li2[4]["debitAmount"] = Decimal("100")  # ...at a new amount
    c2 = copy.deepcopy(c)
    c2["ELECTRICITY"]["data"][0]["expenseAmount"] = Decimal("200.50")
    a2 = copy.deepcopy(a)
    a2["data"][0]["expenseAmount"] = Decimal("200.50")
    li2.append(line(6, A_ID, "LCF Airtex", "acc-elec", "6200-03", "ELECTRICITY", "VERIFIED",
                    "-25.00"))                # a reversal
    c2["ELECTRICITY"]["data"][0]["expenseAmount"] = Decimal("175.50")
    a2["data"][0]["expenseAmount"] = Decimal("175.50")
    t3 = L.write_period(conn, run_id, plan(cats=c2, allp=a2, lines=li2), fetch)["transactions"]
    cur.execute("SELECT transaction_status, expense_amount FROM r365_financial_transaction "
                "WHERE source_transaction_detail_id = 'd-5'")
    check("transactions: changed status/amount is UPDATED in place (same detail id)",
          cur.fetchone() == ("Approved", Decimal("100")) and t3[1] == 2, str(t3))
    cur.execute("SELECT expense_amount FROM r365_financial_transaction "
                "WHERE source_transaction_detail_id = 'd-6'")
    check("transactions: reversal line preserved as a negative line", cur.fetchone()[0] == -25)
    cur.execute("SELECT sum(expense_amount) FROM r365_financial_transaction WHERE fiscal_year = %s "
                "AND period_number = %s AND mapping_status = 'VERIFIED' "
                "AND transaction_status = 'Approved' AND r365_location_id = %s", (FY, PN, A_ID))
    check("transactions: Approved VERIFIED lines still add up to the summary after corrections",
          cur.fetchone()[0] == Decimal("175.50"))
    li3 = [x for x in li2 if x["detailId"] != "d-4"]
    a3 = copy.deepcopy(a2)
    a3["data"] = a3["data"][:2]
    a3["pagination"]["returned"] = 2
    t4 = L.write_period(conn, run_id, plan(cats=c2, allp=a3, lines=li3), fetch)["transactions"]
    check("transactions: a line the source no longer publishes is removed", t4[2] == 1, str(t4))
    check("DB rejects a transaction violating expense = debit - credit",
          violates("INSERT INTO r365_financial_transaction (source_transaction_detail_id, "
                   "source_transaction_id, transaction_type, transaction_status, r365_location_id,"
                   " location_name, accounting_date, gl_account_id, gl_account_number, "
                   "gl_account_name, mapping_status, debit_amount, credit_amount, expense_amount,"
                   " source_class, source_fetched_at, ingest_run_id) VALUES ('zz','t','J',"
                   "'Approved','l','x','2025-01-01','g','1','n','UNMAPPED',10,0,5,"
                   "'TRANSACTION_DETAIL',now(),%s)", (run_id,)))
finally:
    conn.rollback()
    conn.close()

conn = L.connect()
cur = conn.cursor()
cur.execute("SELECT (SELECT count(*) FROM r365_financial_period_expense WHERE fiscal_year = 2027)"
            " + (SELECT count(*) FROM r365_financial_transaction WHERE fiscal_year = 2027)")
check("rollback left no test rows behind", cur.fetchone()[0] == 0)
conn.close()

# ---------------------------------------------------------------------------
print("7. archive")
with tempfile.TemporaryDirectory() as td:
    I.RAW_DIR = td
    path, digest = I.archive("FY2027-P13", 1, datetime.datetime.now(datetime.timezone.utc),
                             [("summary ALL", b'{"a": 1}')])
    doc = json.load(open(path))
    check("archive embeds the payload verbatim with its sha256",
          doc["payloads"] == [{"a": 1}] and doc["meta"]["payload_sha256"] == digest)
    check("archive file is 0600", stat.S_IMODE(os.stat(path).st_mode) == 0o600)

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
