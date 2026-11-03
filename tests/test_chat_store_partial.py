#!/usr/bin/env python3
"""Per-store Revel verdicts, partial reports and labour trust.

Run:  venv/bin/python tests/test_chat_store_partial.py     (exit 0 = pass)

2026-10 incident: a network-wide reconciliation failure (two stores had paid
orders without line items) made the assistant refuse transactions per labour
hour for EVERY store, although most stores reconciled on their own. The
blocked verdict also told the model "the same gate applies to every scope",
and read labour trust from a key ('available') that the Revel-path labour
block does not have, so labour hours were withheld too.

After the data repair the opposite case appeared: the network PASSES
while two individual stores still fail, because
their errors cancel in the sum.

These checks pin:
  1. labour trust follows the real data contract (status / available)
  2. per-store verdicts: figures only for PASS stores, reasons for FAIL
     stores, classified as missing ingestion / payment timing / genuine
  3. a subtotal of verified stores is never labelled a network total
  4. ratios built on unverified orders are withheld, labour hours are not
  5. the blocked verdict is scope-specific and carries every store's verdict
  6. through the real answer loop (mocked model): a network FAIL yields a
     partial report input, not a blanket refusal
  7. live, read-only: the four validation questions' figures
Loop checks use a fake Anthropic client (no API call). Database checks are
read-only and run as the LLM role.
"""
import json
import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(".env", override=True)
import chat_sql as C  # noqa: E402
import store_scope  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


# ---------------------------------------------------------------------------
print("1. labour trust follows the data contract")
revel_path = {"status": "available", "labor_hours": 1015.1791, "open_shift_count": 0,
              "missing_wage_shift_count": 0, "orders_per_labor_hour": 2.13}
t = C._labor_trust(revel_path)
check("Revel-path block (status='available', no 'available' key) is trusted",
      t["trusted"] and t["labor_hours"] == 1015.2, str(t))
check("labour-only block (available=True) is trusted",
      C._labor_trust({"available": True, "labor_hours": 970.0})["trusted"])
check("available=False is untrusted",
      not C._labor_trust({"available": False, "labor_hours": None})["trusted"])
nt = C._labor_trust({"status": "not_evaluated_scope_too_large", "labor_hours": None})
check("scope too large is untrusted, with a reason", not nt["trusted"] and "too large" in nt["reason"])
check("caveats carried: breaks included, cost not payroll",
      "INCLUDES unpaid breaks" in t["caveats"] and "NOT verified payroll" in t["caveats"])
check("trust result never carries orders_per_labor_hour", "orders_per_labor_hour" not in t)

# ---------------------------------------------------------------------------
print("2/3. per-store verdicts (fake rows)")
NOW = datetime.now(timezone.utc)


def row(est, name, computed, reference, real=500, other_n=0, other_amt=0, days=7,
        labor_days=7, hours=500.0, dup=0, real_no_items=0, open_shifts=0):
    return {"establishment_id": est, "store_name": name, "order_rows": real + 100,
            "duplicate_order_ids": dup, "real_count": real, "real_without_items": real_no_items,
            "computed_total": computed, "days_with_data": days,
            "money_orders_without_items": other_n, "money_without_items_total": other_amt,
            "reference_total": reference, "labor_days": labor_days, "labor_hours": hours,
            "open_shift_count": open_shifts, "missing_wage_shift_count": 0,
            "latest_source_ts": NOW}


ROWS = [
    row(40, "LCF Rosenberg", 70000.00, 70000.00, real=3441, hours=945.93),
    row(54, "LCF Cypress", 36000.00, 67000.00, real=1414, other_n=2721, other_amt=31000.0, hours=1015.18),
    row(6, "LCF Katy", 29500.00, 29900.00, real=1397, hours=422.91),
    row(36, "LCF Missouri City", 40200.00, 39000.00, real=1991, hours=563.34),
    row(7, "LCF Ella", 56900.00, 56920.00, real=2908, hours=616.16, labor_days=5),
]
LINKED = {54: 36000.00, 6: 29500.00, 36: 38000.00}   # MC fixture: payments really missing


class FakeCur:
    def __init__(self):
        self.last, self.executed = None, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self.last = sql

    def fetchall(self):
        if "v_order_payment_summary" in (self.last or ""):
            ests = self.executed[-1][1]["ests"]
            return [{"establishment_id": e, "linked_total": LINKED[e]} for e in ests if e in LINKED]
        return [dict(r) for r in ROWS]


class FakeConn:
    def __init__(self):
        self.cur = FakeCur()

    def set_session(self, **kw):
        pass

    def cursor(self, **kw):
        return self.cur

    def rollback(self):
        pass

    def close(self):
        pass


_orig_conn = C._ro_conn
fc = FakeConn()
C._ro_conn = lambda: fc
try:
    sb = C.revel_store_breakdown("2026-10-22", "2026-10-29")
finally:
    C._ro_conn = _orig_conn
by = {s["establishment_id"]: s for s in sb["stores"]}
check("Rosenberg PASS with figures", by[40]["revel_status"] == "PASS"
      and by[40]["real_orders"] == 3441 and by[40]["orders_per_labor_hour"] == 3.64, str(by[40]))
check("Cypress FAIL classified missing_ingestion", by[54]["revel_status"] == "FAIL"
      and by[54]["failure_category"] == "missing_ingestion", str(by[54]["failure_reasons"]))
check("Katy FAIL classified payment_timing (linked payments match orders)",
      by[6]["failure_category"] == "payment_timing")
check("Missouri City FAIL classified genuine reconciliation_mismatch",
      by[36]["failure_category"] == "reconciliation_mismatch")
check("failed stores carry NO orders, sales or orders-per-labour-hour",
      all(by[e]["real_orders"] is None and by[e]["real_sales"] is None
          and by[e]["orders_per_labor_hour"] is None for e in (54, 6, 36)))
check("failed store's trusted labour hours are still reported",
      by[54]["labor"]["trusted"] and by[54]["labor"]["labor_hours"] == 1015.2)
check("orders-per-labour-hour withheld where labour coverage is incomplete (Ella 5/7 days)",
      by[7]["revel_status"] == "PASS" and by[7]["orders_per_labor_hour"] is None
      and by[7]["orders_per_labor_hour_withheld"] == "labour coverage is incomplete")
check("linked-payment query runs only for stores outside tolerance",
      sorted(fc.cur.executed[-1][1]["ests"]) == [6, 36, 54], str(fc.cur.executed[-1][1]["ests"]))
sub = sb["verified_stores_subtotal"]
check("subtotal of verified stores is labelled NOT a network total",
      "NOT a network total" in sub["label"] and "LCF Katy" in sub["label"], sub["label"])
check("all_stores_verified is false", sb["all_stores_verified"] is False)
check("rules say a network PASS/FAIL does not decide individual stores",
      "network PASS does not verify" in sb["rules"] and "network FAIL does not mean every store failed" in sb["rules"])

ROWS[:] = [row(40, "LCF Rosenberg", 100.0, 100.0, hours=50.0), row(14, "LCF Beaumont", 200.0, 200.0, hours=40.0)]
fc = FakeConn()
C._ro_conn = lambda: fc
tok = store_scope.activate([40])
try:
    sb2 = C.revel_store_breakdown("2026-10-22", "2026-10-29")
    params = fc.cur.executed[1][1]
finally:
    store_scope.deactivate(tok)
    C._ro_conn = _orig_conn
check("all stores verified -> labelled NETWORK TOTAL",
      sb2["all_stores_verified"] and sb2["verified_stores_subtotal"]["label"].startswith("NETWORK TOTAL"))
check("MCP store policy restricts the stores read", params["ests"] == [40], str(params))

# ---------------------------------------------------------------------------
print("4/5. ratios withheld; blocked verdict scope-specific")


def fail_meta(est=None):
    return {
        "scope": {"establishment_id": est, "store_name": "ALL STORES" if est is None else "x",
                  "period_start": "2026-10-15", "period_end_exclusive": "2026-10-22"},
        "reconciliation": {"status": "FAIL", "delta_pct": -17.35},
        "freshness": {"source_lag_hours": 5}, "volumes": {"order_rows": 50000},
        "labor": {"status": "available", "labor_hours": 7990.0, "orders_per_labor_hour": 3.1,
                  "sales_per_labor_hour": 70.0, "labor_pct_sales": 20.0},
        "blocking_reasons": ["sales reconciliation is off by -17.35%"], "warnings": [],
    }


FAKE_SB = {"stores_verified": 10, "stores_failed": 2, "all_stores_verified": False,
           "failed_store_names": ["LCF Shepherd", "LCF Cypress"],
           "stores": [{"store_name": "LCF Rosenberg", "revel_status": "PASS", "orders_per_labor_hour": 3.69},
                      {"store_name": "LCF Cypress", "revel_status": "FAIL", "orders_per_labor_hour": None,
                       "failure_category": "missing_ingestion"}],
           "verified_stores_subtotal": {"label": "SUBTOTAL OF THE 10 VERIFIED STORES ONLY -- NOT a network total"}}

_orig_me, _orig_sb = C.meta_extract, C.revel_store_breakdown
C.meta_extract = lambda e, s, en: fail_meta(e)
calls = []
C.revel_store_breakdown = lambda s, e, cache=None: (calls.append((s, e)), FAKE_SB)[1]
try:
    m = C.meta_profile(None, "2026-10-15", "2026-10-22", domains=["revel", "labor"], cache={})
    ms = C.meta_profile(54, "2026-10-15", "2026-10-22", domains=["revel", "labor"], cache={})
finally:
    C.meta_extract = _orig_me
check("failed scope: orders/sales per labour hour and labour % are scrubbed",
      m["labor"]["orders_per_labor_hour"] is None and m["labor"]["sales_per_labor_hour"] is None
      and m["labor"]["labor_pct_sales"] is None and "ratios_withheld" in m["labor"])
check("failed scope keeps labour hours", m["labor"]["labor_hours"] == 7990.0)
check("network Revel profile carries store_breakdown", m.get("store_breakdown") is FAKE_SB)
check("single-store profile does not compute the breakdown up front",
      "store_breakdown" not in ms and len(calls) == 1)

b = C.blocked_domain_context(m, ["revel", "labor"])
check("blocked labour is trusted with hours (was always False before)",
      b["labor"]["trusted"] and b["labor"]["labor_hours"] == 7990.0, str(b["labor"]))
check("instruction no longer claims the gate applies to every scope",
      "same gate applies to every scope" not in b["instruction"]
      and "FOR THIS SCOPE ONLY" in b["instruction"])
check("instruction: network FAIL != every store failed; subtotal is not the network total",
      "network FAIL does not mean every store failed" in b["instruction"]
      and "never call it the network total" in b["instruction"])
bs = C.blocked_domain_context(ms, ["revel", "labor"])
C._attach_store_breakdown(bs, ms, {})
check("single-store failure gets every store's verdict for the same period",
      bs.get("store_breakdown") is FAKE_SB and calls[-1] == ("2026-10-15", "2026-10-22"))
hme_only = {"blocking_reasons": ["HME data missing"], "warnings": [], "hme": {}}
bh = C.blocked_domain_context(hme_only, ["hme"])
C._attach_store_breakdown(bh, hme_only, {})
check("non-Revel failure: no Revel breakdown attached", "store_breakdown" not in bh)
C.revel_store_breakdown = _orig_sb

# ---------------------------------------------------------------------------
print("6. answer loop (mocked model): network FAIL -> partial-report input")


def text(t):
    return SimpleNamespace(type="text", text=t)


def resp(stop, *blocks):
    return SimpleNamespace(stop_reason=stop, content=list(blocks),
                           usage=SimpleNamespace(input_tokens=1, output_tokens=1))


def check_call(i, est):
    return SimpleNamespace(type="tool_use", id=f"c{i}", name="check_data",
                           input={"establishment_id": est, "period_start": "2026-10-15",
                                  "period_end": "2026-10-22", "domains": ["revel", "labor"]})


class FakeClient:
    def __init__(self, script):
        self.script, self.calls = list(script), []
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kw):
        self.calls.append(kw)
        return self.script.pop(0) if self.script else resp("end_turn", text("partial report"))


def run(question, script):
    fake = FakeClient(script)
    orig = C.anthropic.Anthropic
    C.anthropic.Anthropic = lambda **kw: fake
    C.meta_extract = lambda e, s, en: fail_meta(e)
    C.revel_store_breakdown = lambda s, e, cache=None: FAKE_SB
    try:
        return C.answer_question([], question), fake
    finally:
        C.anthropic.Anthropic = orig
        C.meta_extract, C.revel_store_breakdown = _orig_me, _orig_sb


r, f = run("Show transactions per labor hour for all locations for October 15-21, 2026.",
           [resp("tool_use", check_call(1, None)), resp("end_turn", text("partial report"))])
final = f.calls[-1]
instr = json.dumps(final["messages"][-1]["content"])
check("exactly one tool-less final call after the failed network check",
      len(f.calls) == 2 and "tools" not in final, str(len(f.calls)))
check("final call carries every store's verdict (Rosenberg PASS 3.69)",
      "LCF Rosenberg" in instr and "3.69" in instr)
check("final call carries the failed stores and their category",
      "LCF Cypress" in instr and "missing_ingestion" in instr)
check("final call forbids calling the subtotal the network total",
      "NOT a network total" in instr and "never call it the network total" in instr)
check("final call carries trusted labour hours", '\\"labor_hours\\":7990.0' in instr
      or '"labor_hours":7990.0' in instr)
tool_result = next(c for c in final["messages"] if c["role"] == "user" and isinstance(c["content"], list))
payload = json.loads(tool_result["content"][0]["content"])
check("tool result: network orders_per_labor_hour scrubbed to null",
      payload["labor"]["orders_per_labor_hour"] is None)
check("step log records the short circuit", any(s.get("short_circuit") for s in r.steps))

# system prompt
sysprompt = C._system_static()
check("system prompt: reconciliation is per scope, not per source",
      "RECONCILIATION IS PER SCOPE, NOT PER SOURCE" in sysprompt)
check("system prompt: labour hours independent of order reconciliation",
      "LABOUR HOURS ARE INDEPENDENT OF THE ORDER RECONCILIATION" in sysprompt)
check("tolerance unchanged at 0.5%", C.RECON_TOLERANCE_PCT == 0.5)

# ---------------------------------------------------------------------------
print("7. live read-only: the four validation questions")
try:
    w1 = C.revel_store_breakdown("2026-10-15", "2026-10-22")
    w2 = C.revel_store_breakdown("2026-10-22", "2026-10-29")
    s1 = {s["establishment_id"]: s for s in w1["stores"]}
    s2 = {s["establishment_id"]: s for s in w2["stores"]}
    check("Q1 all locations Oct 22-28: partial report, failed stores named",
          w2["stores_verified"] >= 1 and w2["stores_total"] == 12,
          f"{w2['stores_verified']} verified, failed={w2['failed_store_names']}")
    check("Q1 failed stores have no orders-per-labour-hour",
          all(s["orders_per_labor_hour"] is None for s in w2["stores"] if s["revel_status"] == "FAIL"))
    check("Q2 Rosenberg Oct 22-28 = 3,441 / 945.9 h / 3.64",
          s2[40]["revel_status"] == "PASS" and s2[40]["real_orders"] == 3441
          and s2[40]["labor"]["labor_hours"] == 945.9 and s2[40]["orders_per_labor_hour"] == 3.64,
          str((s2[40]["real_orders"], s2[40]["labor"]["labor_hours"], s2[40]["orders_per_labor_hour"])))
    check("Q3 Rosenberg Oct 15-21 = 3,420 / 926.1 h / 3.69",
          s1[40]["revel_status"] == "PASS" and s1[40]["real_orders"] == 3420
          and s1[40]["labor"]["labor_hours"] == 926.1 and s1[40]["orders_per_labor_hour"] == 3.69)
    check("Q2/Q3 Cypress verified after repair, both weeks",
          s1[54]["revel_status"] == "PASS" and s2[54]["revel_status"] == "PASS",
          f"{s1[54]['reconciliation_delta_pct_display']} / {s2[54]['reconciliation_delta_pct_display']}")
    check("Q4 labour hours Cypress 970.0 / 1015.2, Rosenberg 926.1 / 945.9",
          (s1[54]["labor"]["labor_hours"], s2[54]["labor"]["labor_hours"],
           s1[40]["labor"]["labor_hours"], s2[40]["labor"]["labor_hours"]) == (970.0, 1015.2, 926.1, 945.9))
except Exception as e:  # DB not reachable from this environment
    check("live database checks", False, f"{type(e).__name__}: {e}")

passed = sum(1 for _, ok, _ in results if ok)
failed = len(results) - passed
print(f"\n{'='*66}\nChat store partial reporting: {passed} passed, {failed} failed, {len(results)} total")
if failed:
    for n, ok, d in results:
        if not ok:
            print(f"  FAILED: {n} -- {d}")
sys.exit(1 if failed else 0)
