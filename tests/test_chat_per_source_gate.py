#!/usr/bin/env python3
"""Per-source trust verdicts, HME explanations and dated conversation history.

Run:  venv/bin/python tests/test_chat_per_source_gate.py     (exit 0 = pass)

2026-11 incident (conversation 627): "what are my priorities today at
shepherd" was asked on Nov 11 in a thread last used on Nov 4. The model checked
Revel, HME, labour and R365 together; HME was stale (ingestion failing since
Oct 13), and the single combined verdict blocked the WHOLE turn -- Revel,
labour and R365 had all passed their own gates. No query ran, the tool-less
final call only had R365 figures for Nov 4, and the answer led with week-old
data. The HME reason also said the store had no verified HME mapping, which is
false for Shepherd.

These checks pin:
  1. Shepherd priorities with stale HME and current Revel: the turn continues
     on the trusted sources, an HME query is still rejected, the HME reason
     says "mapped, not loaded" with the latest HME date (conversation 627
     replayed with a mocked model)
  2. Nov 3 cash deposits from loaded R365 data (live, read-only), and the four
     cash metrics are defined separately in the prompt
  3. several sources, only one unavailable (R365 not loaded; HME queried
     before check_data)
  4. every requested source unavailable -> one blocked, tool-less answer
  5. one store failing Revel reconciliation while others pass -> the existing
     per-store verdict path, unchanged
  6. a conversation resumed after seven days: dated turns, resume note, one
     re-check nudge; a new conversation and a same-day thread are unchanged
Loop checks use a fake Anthropic client (no API call). Database checks are
read-only, through the LLM role.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(".env", override=True)
import chat_sql as C  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
def revel_meta(est, s, e, fail=False):
    return {
        "scope": {"establishment_id": est, "store_name": "LCF Shepherd" if est == 15 else "ALL STORES",
                  "period_start": s, "period_end_exclusive": e},
        "scope_valid": True,
        "reconciliation": {"status": "FAIL" if fail else "PASS",
                           "delta_pct": 1.9 if fail else 0.01},
        "freshness": {"source_lag_hours": 3}, "volumes": {"order_rows": 900},
        "labor": {"status": "available", "labor_hours": 120.0, "orders_per_labor_hour": 3.5,
                  "sales_per_labor_hour": 80.0, "labor_pct_sales": 15.0},
        "blocking_reasons": ["sales reconciliation is off by +1.90%"] if fail else [],
        "warnings": [],
    }


def hme_stale(est=15):
    return {"available": False, "latest_business_date": "2026-10-13",
            "freshness_status": "STALE",
            "freshness_reason": "both the primary and retry windows have passed",
            "next_ingest_window": "2026-11-12 05:30 America/Chicago",
            "mapped_store_coverage": {"verified_mapped": 11, "revel_establishments_in_scope": 1,
                                      "hme_stores_with_data_in_scope": 0,
                                      "unmapped_establishments": []},
            "per_day": [], "incomplete_days": [], "unreconciled_days": [],
            "missing_verified_stores": []}


def r365_loaded():
    return {"available": True, "coverage_start": "2026-08-13", "latest_daily_date": "2026-11-04",
            "weeks_in_scope": [], "partial_weeks_in_scope": [], "uncovered_dates_in_scope": {},
            "store_missing_dates_in_scope": [], "special_status_days_in_scope": []}


def r365_unloaded():
    return {"available": False, "coverage_start": "2026-08-13", "latest_daily_date": "2026-11-04",
            "ingest_schedule": "weekly, Thursday 20:00 America/Chicago",
            "next_ingest_window": "2026-11-12 20:00 America/Chicago",
            "uncovered_dates_in_scope": {"after_latest_loaded": ["2026-11-11", "2026-11-11"],
                                         "before_coverage_start": None}}


class DummyCur:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        pass


class DummyConn:
    def set_session(self, **k):
        pass

    def cursor(self, **k):
        return DummyCur()

    def rollback(self):
        pass

    def close(self):
        pass


SQL_RAN = []


class World:
    """Swap the data layer for fakes; restore on exit."""

    def __init__(self, revel_fail=False, hme=hme_stale, r365=r365_loaded, breakdown=None):
        self.cfg = (revel_fail, hme, r365, breakdown)

    def __enter__(self):
        revel_fail, hme, r365, breakdown = self.cfg
        self.saved = {k: getattr(C, k) for k in
                      ("meta_extract", "_ro_conn", "_hme_block", "_r365_block", "_light_scope",
                       "run_sql", "revel_store_breakdown", "_labor_gate")}
        C.meta_extract = lambda e, s, en: revel_meta(e, s, en, fail=revel_fail)
        C._ro_conn = lambda: DummyConn()
        C._hme_block = lambda cur, e, s, en: hme()
        C._r365_block = lambda cur, e, s, en: r365()
        C._light_scope = lambda cur, e, s, en: {"scope_valid": True,
                                                "scope": {"establishment_id": e, "period_start": s,
                                                          "period_end_exclusive": en}}
        C._labor_gate = lambda e, s, en: ({"available": True, "labor_hours": 100.0}, [], [])
        C.run_sql = lambda sql: (SQL_RAN.append(sql),
                                 {"columns": ["x"], "rows": [[1]], "row_count": 1})[1]
        C.revel_store_breakdown = lambda s, e, cache=None: breakdown or {}
        return self

    def __exit__(self, *a):
        for k, v in self.saved.items():
            setattr(C, k, v)
        return False


def text(t):
    return SimpleNamespace(type="text", text=t)


def resp(stop, *blocks):
    return SimpleNamespace(stop_reason=stop, content=list(blocks),
                           usage=SimpleNamespace(input_tokens=1, output_tokens=1))


_n = [0]


def tool(name, **inp):
    _n[0] += 1
    return SimpleNamespace(type="tool_use", id=f"t{_n[0]}", name=name, input=inp)


class FakeClient:
    def __init__(self, script):
        self.script, self.calls = list(script), []
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kw):
        self.calls.append(json.loads(json.dumps(kw, default=lambda o: getattr(o, "__dict__", str(o)))))
        return self.script.pop(0) if self.script else resp("end_turn", text("done"))


def run(history, question, script, **world):
    fake = FakeClient(script)
    orig = C.anthropic.Anthropic
    C.anthropic.Anthropic = lambda **kw: fake
    SQL_RAN.clear()
    try:
        with World(**world):
            return C.answer_question(history, question), fake
    finally:
        C.anthropic.Anthropic = orig


def tool_results(call):
    out = {}
    for m in call["messages"]:
        if m["role"] == "user" and isinstance(m["content"], list):
            for c in m["content"]:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    out[c["tool_use_id"]] = c["content"]
    return out


def system_text(call):
    return "\n".join(b["text"] for b in call["system"])


# ---------------------------------------------------------------------------
print("0. HME explanation: unmapped vs mapped-but-not-loaded")
r = C.hme_unavailable_reason(hme_stale(), "2026-11-04", "2026-11-11")
check("mapped store with stale HME: 'HME DATA NOT LOADED', latest date, not 'unmapped'",
      r.startswith("HME DATA NOT LOADED") and "2026-10-13" in r and "IS mapped" in r
      and "NOT MAPPED" not in r and "no VERIFIED HME mapping" not in r, r)
check("...with the inclusive requested range and freshness status",
      "2026-11-04..2026-11-10" in r and "STALE" in r, r)
um = dict(hme_stale(), mapped_store_coverage={"revel_establishments_in_scope": 1,
                                              "unmapped_establishments": [48]})
ru = C.hme_unavailable_reason(um, "2026-11-04", "2026-11-11")
check("unmapped store: 'HME NOT MAPPED', never zero cars", ru.startswith("HME NOT MAPPED")
      and "not zero cars" in ru, ru)
net = dict(hme_stale(), mapped_store_coverage={"revel_establishments_in_scope": 12,
                                               "unmapped_establishments": [48]})
rn = C.hme_unavailable_reason(net, "2026-11-04", "2026-11-11")
check("network scope with one unmapped store: not-loaded reason naming the unmapped one",
      rn.startswith("HME DATA NOT LOADED") and "[48]" in rn, rn)
fails, _ = C._hme_gate(hme_stale(), "2026-11-04", "2026-11-11")
check("HME gate uses the new reason", fails and fails[0].startswith("HME DATA NOT LOADED"))

# live, read-only: Shepherd is mapped, so whatever the HME state, it is never "unmapped"
live = C.meta_profile(15, "2026-11-04", "2026-11-11", domains=["hme"], cache={})
lh = live["hme"]
if lh.get("available"):
    check("live: Shepherd HME loaded for the period (no reason needed)", not any(
        "NOT MAPPED" in x for x in live["blocking_reasons"]))
else:
    lr = live["blocking_reasons"][0]
    check("live: Shepherd HME reason says mapped/not loaded with the latest HME date",
          lr.startswith("HME DATA NOT LOADED") and str(lh["latest_business_date"]) in lr, lr)
check("live: Shepherd is not in the unmapped list",
      15 not in (lh.get("mapped_store_coverage") or {}).get("unmapped_establishments", []))

# ---------------------------------------------------------------------------
print("1. domain verdicts are per source")
with World():
    m = C.meta_profile(15, "2026-11-04", "2026-11-11",
                       domains=["revel", "hme", "labor", "r365"], cache={})
v = m["domain_verdicts"]
check("combined verdict still blocks a query that reads HME (unchanged)",
      m["analysis_permitted"] is False)
check("HME untrusted; Revel, labour, R365 trusted",
      not v["hme"]["trusted"] and v["revel"]["trusted"] and v["labor"]["trusted"]
      and v["r365"]["trusted"], json.dumps(v)[:300])
p = C.partial_trust_context(m, ["revel", "hme", "labor", "r365"])
check("partial trust: continue on revel/labor/r365, hme untrusted with its reason",
      p and p["untrusted_domains"] == ["hme"] and set(p["trusted_domains"]) == {"labor", "r365", "revel"}
      and p["hme_status"]["latest_business_date"] == "2026-10-13")
with World(revel_fail=True):
    mf = C.meta_profile(15, "2026-11-04", "2026-11-11", domains=["revel", "hme", "r365"], cache={})
check("Revel failing -> no partial trust (per-store verdict path is kept)",
      C.partial_trust_context(mf, ["revel", "hme", "r365"]) is None)
with World(r365=r365_unloaded):
    ma = C.meta_profile(15, "2026-11-11", "2026-11-12", domains=["hme", "r365"], cache={})
check("every requested source failed -> no partial trust",
      C.partial_trust_context(ma, ["hme", "r365"]) is None and not ma["analysis_permitted"])

# ---------------------------------------------------------------------------
print("1b. conversation 627 replayed: Shepherd priorities, stale HME, current Revel")
T = lambda s: datetime.fromisoformat(s).replace(tzinfo=timezone.utc)  # noqa: E731
H627 = [
    {"role": "user", "content": "can you tell me what is the cash deposit for each store yesterday",
     "created_at": T("2026-11-04T23:07:12")},
    {"role": "assistant", "content": "The latest loaded R365 week ends 2026-10-28 -- the data for "
     "yesterday (2026-11-03) isn't loaded yet.", "created_at": T("2026-11-04T23:07:24")},
    {"role": "user", "content": "Revel", "created_at": T("2026-11-04T23:08:25")},
    {"role": "assistant", "content": "Revel can't identify which payments were cash.",
     "created_at": T("2026-11-04T23:08:34")},
]
SCOPE = dict(establishment_id=15, period_start="2026-11-11", period_end="2026-11-12",
             domains=["revel", "hme", "labor", "r365"])
REVEL_SQL = ("SELECT total_revenue FROM features_daily_summary_v2 WHERE establishment_id = 15 "
             "AND business_date >= '2026-11-11' AND business_date < '2026-11-12'")
HME_SQL = ("SELECT * FROM v_hme_store_daily_llm WHERE establishment_id = 15 "
           "AND business_date >= '2026-11-11' AND business_date < '2026-11-12'")
RUN = dict(establishment_id=15, period_start="2026-11-11", period_end_exclusive="2026-11-12")
c1 = tool("check_data", **SCOPE)
q1 = tool("run_sql", sql=REVEL_SQL, **RUN)
q2 = tool("run_sql", sql=HME_SQL, **RUN)
res, fk = run(H627, "what are my priorities today at shepherd",
              [resp("tool_use", c1), resp("tool_use", q1), resp("tool_use", q2),
               resp("end_turn", text("Revel today so far ...; HME stale since Oct 13"))])
steps = res.steps
check("no short circuit: the turn was NOT blocked by HME",
      not any(s.get("short_circuit") for s in steps), json.dumps(steps, default=str)[:300])
check("no tool-less forced synthesis", not any(s.get("forced_synthesis") for s in steps))
check("all four model calls kept their tools", len(fk.calls) == 4
      and all("tools" in c for c in fk.calls), str(len(fk.calls)))
tr = tool_results(fk.calls[-1])
cd = json.loads(tr[c1.id])
check("check_data result carries partial_trust (hme untrusted)",
      cd.get("partial_trust", {}).get("untrusted_domains") == ["hme"])
reason = cd["partial_trust"]["untrusted_reasons"]["hme"][0]
check("HME reason: mapped, not loaded, latest 2026-10-13 -- never 'unmapped'",
      "HME DATA NOT LOADED" in reason and "2026-10-13" in reason and "NOT MAPPED" not in reason)
check("Revel query ran", SQL_RAN == [REVEL_SQL], str(SQL_RAN))
check("HME query was rejected and never executed",
      tr[q2.id].startswith("REJECTED BEFORE EXECUTION (this statement only)")
      and HME_SQL not in SQL_RAN)
check("HME rejection does not tell the model to stop the analysis",
      "Report this to the user and stop" not in tr[q2.id])
check("final answer is the model's own", res.answer.startswith("Revel today so far"))
sys0 = system_text(fk.calls[0])
check("resumed thread: system says the conversation resumed and earlier turns are stale",
      "CONVERSATION RESUMED" in sys0 and "NOT evidence about today" in sys0)
um0 = fk.calls[0]["messages"][0]["content"]
check("earlier user turn is dated in America/Chicago",
      um0.startswith("[Asked ") and "2026-11-04 17:07 America/Chicago]" in um0, um0[:80])
check("earlier assistant turn is not stamped",
      not fk.calls[0]["messages"][1]["content"].startswith("[Asked"))
check("no created_at key reaches the provider",
      all(set(m) == {"role", "content"} for m in fk.calls[0]["messages"]))

# ---------------------------------------------------------------------------
print("2. Nov 3 cash deposits from loaded R365 (live, read-only)")
md = C.meta_profile(None, "2026-11-03", "2026-11-04", domains=["r365"], cache={})
check("R365 gate passes for all stores on Nov 3", md["analysis_permitted"], str(md["blocking_reasons"]))
DEP_SQL = ("SELECT store_name, total_deposit, expected_cash, over_short, paid_in, paid_out "
           "FROM v_r365_store_daily_llm WHERE business_date = '2026-11-03'")
gm = C.enforce_scope(DEP_SQL, None, "2026-11-03", "2026-11-04", cache={})
out = C.run_sql(DEP_SQL)
cols = out["columns"]
_get = (lambda r, k: r[k]) if out["rows"] and isinstance(out["rows"][0], dict) \
    else (lambda r, k: r[cols.index(k)])
dep = [_get(r, "total_deposit") for r in out["rows"]]
os_ = [_get(r, "over_short") for r in out["rows"]]
check("12 stores, actual deposit present for each", out["row_count"] == 12
      and all(x is not None for x in dep), str(out["row_count"]))
check("deposit and over/short are different figures",
      sum(1 for a, b in zip(dep, os_) if a != b) == 12)
fk2 = FakeClient([resp("tool_use", tool("check_data", establishment_id=None, period_start="2026-11-03",
                                         period_end="2026-11-04", domains=["r365"])),
                  resp("tool_use", tool("run_sql", sql=DEP_SQL, establishment_id=None,
                                        period_start="2026-11-03", period_end_exclusive="2026-11-04")),
                  resp("end_turn", text("deposits by store"))])
orig = C.anthropic.Anthropic
C.anthropic.Anthropic = lambda **kw: fk2
try:
    r2 = C.answer_question([], "What was the cash deposit for each store on November 3?")
finally:
    C.anthropic.Anthropic = orig
rows2 = [json.loads(x) for x in tool_results(fk2.calls[-1]).values() if x.startswith("{\"columns\"")]
check("loop (mocked model): deposit query ran, 12 rows, no block",
      rows2 and rows2[0]["row_count"] == 12 and not any(s.get("short_circuit") for s in r2.steps))
sp = C._system_static()
check("prompt defines actual deposit, expected cash, over/short, paid in/out separately",
      all(k in sp for k in ("ACTUAL CASH DEPOSIT = total_deposit", "EXPECTED CASH = expected_cash",
                            "CASH OVER/SHORT = over_short", "PAID IN / PAID OUT = paid_in / paid_out")))
check("prompt: never substitute one cash metric for another; Revel cannot supply cash",
      "Never substitute one for another" in sp and "Revel CANNOT supply any of these" in sp)
check("prompt: today/priorities rules -- source and date on every figure, no week-old 'today'",
      "EVERY FIGURE CARRIES ITS SOURCE AND BUSINESS DATE" in sp
      and "NEVER describe an older date as today" in sp
      and "A STALE OR FAILED SOURCE IS REPORTED, NOT A REASON TO STOP" in sp)
check("prompt: 'yesterday' is the literal Chicago date, not SQL current_date - 1",
      '"yesterday"  -> date = current_date - 1' not in sp
      and '"yesterday"  -> the literal yesterday date under CURRENT TIME' in sp)
check("prompt: only 'HME NOT MAPPED' justifies saying a store is unmapped",
      'ONLY say a\n    store is unmapped when its reason starts "HME NOT MAPPED"' in sp)

# ---------------------------------------------------------------------------
print("3. several sources, only one unavailable")
c3 = tool("check_data", establishment_id=15, period_start="2026-11-11", period_end="2026-11-12",
          domains=["revel", "labor", "r365"])
q3 = tool("run_sql", sql=REVEL_SQL, **RUN)
res3, fk3 = run([], "sales and labor today at shepherd vs R365",
                [resp("tool_use", c3), resp("tool_use", q3), resp("end_turn", text("ok"))],
                r365=r365_unloaded)
cd3 = json.loads(tool_results(fk3.calls[-1])[c3.id])
check("R365 not loaded, Revel+labour pass -> partial, R365 reason names latest date",
      cd3["partial_trust"]["untrusted_domains"] == ["r365"]
      and "2026-11-04" in cd3["partial_trust"]["untrusted_reasons"]["r365"][0])
check("...and the Revel query ran, no block", SQL_RAN == [REVEL_SQL]
      and not any(s.get("short_circuit") for s in res3.steps))
# HME queried BEFORE any check_data, after a trusted Revel query
q4 = tool("run_sql", sql=REVEL_SQL, **RUN)
q5 = tool("run_sql", sql=HME_SQL, **RUN)
res4, fk4 = run([], "priorities today at shepherd",
                [resp("tool_use", q4), resp("tool_use", q5), resp("end_turn", text("ok"))])
tr4 = tool_results(fk4.calls[-1])
check("HME query after a passing Revel query: rejected, turn continues",
      tr4[q5.id].startswith("REJECTED BEFORE EXECUTION (this statement only)")
      and not any(s.get("short_circuit") for s in res4.steps) and len(fk4.calls) == 3)
# HME-only question, nothing trusted seen: unchanged blocked path
q6 = tool("run_sql", sql=HME_SQL, **RUN)
res5, fk5 = run([], "drive thru times today at shepherd", [resp("tool_use", q6)])
check("HME-only question with stale HME: unchanged blocked verdict, one tool-less call",
      any(s.get("short_circuit") for s in res5.steps) and "tools" not in fk5.calls[-1])
JOIN_SQL = ("SELECT * FROM features_daily_summary_v2 f JOIN v_hme_store_daily_llm h "
            "ON h.establishment_id = f.establishment_id AND h.business_date = f.business_date "
            "WHERE f.establishment_id = 15 AND f.business_date >= '2026-11-11' "
            "AND f.business_date < '2026-11-12'")
try:
    with World():
        C.enforce_scope(JOIN_SQL, 15, "2026-11-11", "2026-11-12", cache={})
    joined = "ran"
except C.ScopeError as e:
    joined = str(e)
check("a query that JOINS a trusted and an untrusted source is still rejected",
      joined != "ran" and "hme" in joined, joined[:120])

# ---------------------------------------------------------------------------
print("4. every requested source unavailable")
c7 = tool("check_data", establishment_id=15, period_start="2026-11-11", period_end="2026-11-12",
          domains=["hme", "r365"])
res7, fk7 = run([], "drive thru and R365 today at shepherd",
                [resp("tool_use", c7), resp("end_turn", text("both unavailable"))],
                r365=r365_unloaded)
check("blocked: short circuit, exactly one tool-less final call",
      any(s.get("short_circuit") for s in res7.steps) and len(fk7.calls) == 2
      and "tools" not in fk7.calls[-1])
instr7 = json.dumps(fk7.calls[-1]["messages"][-1]["content"])
check("blocked verdict names both sources' reasons",
      "HME DATA NOT LOADED" in instr7 and "R365 has no loaded data" in instr7)

# ---------------------------------------------------------------------------
print("5. one store fails Revel reconciliation, others pass")
SB = {"stores_verified": 11, "stores_failed": 1, "all_stores_verified": False,
      "failed_store_names": ["LCF Shepherd"],
      "stores": [{"store_name": "LCF Rosenberg", "revel_status": "PASS"},
                 {"store_name": "LCF Shepherd", "revel_status": "FAIL",
                  "failure_category": "reconciliation_mismatch"}],
      "verified_stores_subtotal": {"label": "SUBTOTAL OF THE 11 VERIFIED STORES ONLY -- NOT a network total"}}
c8 = tool("check_data", establishment_id=None, period_start="2026-11-10", period_end="2026-11-11",
          domains=["revel", "labor", "hme"])
res8, fk8 = run([], "sales per labor hour yesterday for every store",
                [resp("tool_use", c8), resp("end_turn", text("partial report"))],
                revel_fail=True, breakdown=SB)
instr8 = json.dumps(fk8.calls[-1]["messages"][-1]["content"])
check("Revel FAIL keeps the per-store verdict path (not partial trust)",
      any(s.get("short_circuit") for s in res8.steps) and "tools" not in fk8.calls[-1]
      and not any(s.get("partial_trust") for s in res8.steps))
check("...final call carries each store's verdict and the subtotal label",
      "LCF Rosenberg" in instr8 and "reconciliation_mismatch" in instr8
      and "NOT a network total" in instr8 and "FOR THIS SCOPE ONLY" in instr8)
check("reconciliation tolerance unchanged at 0.5%", C.RECON_TOLERANCE_PCT == 0.5)

# ---------------------------------------------------------------------------
print("6. conversation resumed after seven days")
now = C.chicago_now()
old = [{"role": "user", "content": "cash deposit yesterday?",
        "created_at": (now - timedelta(days=7)).astimezone(timezone.utc)},
       {"role": "assistant", "content": "R365 is not loaded for yesterday yet.",
        "created_at": (now - timedelta(days=7)).astimezone(timezone.utc)}]
h, note = C.annotate_history(old, now)
check("7-day gap: resume note states 7 days and requires a re-check",
      note and "7 days before today" in note and "re-check availability with check_data" in note)
check("dated user turn, undated assistant turn",
      h[0]["content"].startswith("[Asked ") and not h[1]["content"].startswith("[Asked"))
same = [dict(old[0], created_at=now.astimezone(timezone.utc) - timedelta(minutes=5))]
hs, ns = C.annotate_history(same, now)
check("same-day thread: dated, no resume note", ns is None and hs[0]["content"].startswith("[Asked "))
hn, nn = C.annotate_history([], now)
check("new conversation: nothing added", hn == [] and nn is None)
hm, nm = C.annotate_history([{"role": "user", "content": "x"}], now)
check("history without timestamps (MCP-style) passes through unchanged",
      hm == [{"role": "user", "content": "x"}] and nm is None)
c9 = tool("check_data", establishment_id=None, period_start="2026-11-03", period_end="2026-11-04",
          domains=["r365"])
res9, fk9 = run(old, "and now?",
                [resp("end_turn", text("Still not loaded (from memory).")),
                 resp("tool_use", c9), resp("end_turn", text("Now loaded: deposits ..."))])
check("resumed + answered from memory -> one re-check nudge, then data checked",
      len(fk9.calls) == 3 and fk9.calls[1]["messages"][-1]["content"] == C.RESUME_RECHECK_PROMPT
      and any(s.get("resume_recheck") for s in res9.steps), str(len(fk9.calls)))
check("final answer comes after the re-check", res9.answer.startswith("Now loaded"))
res10, fk10 = run([], "hi", [resp("end_turn", text("Hello"))])
check("new conversation: no nudge, single call", len(fk10.calls) == 1 and res10.answer == "Hello")
res11, fk11 = run(same, "thanks", [resp("end_turn", text("You're welcome"))])
check("same-day thread: no nudge", len(fk11.calls) == 1)

# backend: history carries created_at to the engine
import backend.conversations as conv  # noqa: E402
_q, _o = conv.query, conv.owned
conv.owned = lambda cid, uid: {"id": cid}
conv.query = lambda sql, params=None, **k: [
    {"role": "user", "content": "x", "created_at": now, "model": None}] if "created_at" in sql else []
try:
    msgs = conv.messages(1, 1)
finally:
    conv.query, conv.owned = _q, _o
check("conversations.messages returns created_at for the engine",
      msgs and msgs[0].get("created_at") == now)

passed = sum(ok for _, ok, _ in results)
print(f"\nChat per-source gate: {passed} passed, {len(results) - passed} failed, {len(results)} total")
sys.exit(0 if passed == len(results) else 1)
