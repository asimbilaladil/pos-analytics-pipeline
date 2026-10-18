#!/usr/bin/env python3
"""FINANCIAL_WASTE_REVIEW and the empty-answer guard.

Run:  venv/bin/python tests/test_financial_waste_review.py     (exit 0 = pass)

Production jobs 252-254 (2026-10-18) saved EMPTY answers as completed: the
final model call spent its whole max_tokens on thinking and returned no text.
These checks pin the fix: a turn that ends without text gets one tool-less,
thinking-less synthesis call; a broad waste / cost-cutting question gets a
server-chosen window, three set-based queries and a hard run_sql budget.

Loop checks use a fake Anthropic client (no API call). Database checks are
read-only and run as the LLM role.
"""
import json
import os
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from dotenv import load_dotenv

load_dotenv(".env", override=True)
import chat_sql as C  # noqa: E402

results = []
EXACT = ("10 numbers were lackluster at Best help me analyze the financial data to find "
         "financial waste. I think the team is working on food and labor so focus your "
         "attention on everything else we’re looking for utilities. We’re looking at "
         "any other supplies. Maintenance do a deep dive at every category across every "
         "store ask me any questions as necessary to clarify Store FootPrinter any other "
         "information that you would need we want you to be a McKenzie cost cutting "
         "specialist so we can create 2 to 3 points of margin.")


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# A fake client: replays scripted responses and records every create() call.
def text(t):
    return SimpleNamespace(type="text", text=t)


def thinking():
    return SimpleNamespace(type="thinking", thinking="", signature="x")


def tool(i, sql, start=None, end=None):
    return SimpleNamespace(type="tool_use", id=f"t{i}", name="run_sql",
                           input={"sql": sql, "establishment_id": None,
                                  "period_start": start, "period_end_exclusive": end})


def resp(stop, *blocks):
    return SimpleNamespace(stop_reason=stop, content=list(blocks),
                           usage=SimpleNamespace(input_tokens=1, output_tokens=1))


class FakeClient:
    def __init__(self, script):
        self.script, self.calls = list(script), []
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kw):
        self.calls.append(kw)
        return self.script.pop(0) if self.script else resp("end_turn", text(""))


def run(question, script):
    fake = FakeClient(script)
    orig = C.anthropic.Anthropic
    C.anthropic.Anthropic = lambda **kw: fake
    try:
        return C.answer_question([], question), fake
    finally:
        C.anthropic.Anthropic = orig


PERIOD_SQL = ("SELECT period_label FROM v_ayg_business_periods_llm "
              "WHERE fiscal_year = 2026 AND period_number = 10")

# ---------------------------------------------------------------------------
print("1. Empty-answer guard (any question)")
r, f = run("How much electricity did we spend in P10?",
           [resp("max_tokens", thinking()), resp("end_turn", text("Electricity answer."))])
last = f.calls[-1]
check("max_tokens with only thinking -> one forced synthesis call",
      len(f.calls) == 2 and r.answer == "Electricity answer.", r.answer)
check("forced synthesis has no tools and thinking disabled",
      "tools" not in last and last.get("thinking") == {"type": "disabled"})
check("forced synthesis keeps the evidence and forbids further checking",
      "You cannot run SQL any more" in last["messages"][-1]["content"]
      and "do not say you will check" in last["messages"][-1]["content"])
check("step records why synthesis ran",
      any(s.get("forced_synthesis") == "max_tokens" for s in r.steps))

r, f = run("How much electricity did we spend in P10?",
           [resp("end_turn", thinking()), resp("end_turn", text(""))])
check("an answer is never saved empty", r.answer == C.EMPTY_ANSWER_FALLBACK, r.answer)

r, f = run("How much electricity did we spend in P10?",
           [resp("end_turn", text("Plain answer."))])
check("a normal text answer makes no extra call", len(f.calls) == 1 and r.answer == "Plain answer.")

r, f = run("Which store sold the most in P10?",
           [resp("tool_use", tool(i, PERIOD_SQL)) for i in range(C.MAX_LOOPS)]
           + [resp("end_turn", text("Answer from gathered rows."))])
check("MAX_LOOPS unchanged", C.MAX_LOOPS == 6)
check("out of round-trips -> synthesis from the evidence, not the generic fallback",
      r.answer == "Answer from gathered rows."
      and any(s.get("forced_synthesis") == "max_loops" for s in r.steps), r.answer)

r, f = run("How much electricity did we spend in P10?",
           [resp("max_tokens", thinking()), resp("max_tokens", text("Partial report"))])
check("a synthesis cut at the limit says so instead of ending mid-sentence",
      r.answer.startswith("Partial report") and "shortened at the length limit" in r.answer)

# ---------------------------------------------------------------------------
print("2. Intent detection")
for q in (EXACT, EXACT.replace("10 numbers", "Period 10 numbers"),
          "Where can we cut costs outside food and labor?",
          "Help us improve margins by 2 points", "Do an operating expense review for every store",
          "Show me expense outliers across stores"):
    check(f"review: {q[:50]}", C.is_financial_waste_review(q))
for q in ("How much electricity did we spend in P10?", "What did we spend on waste removal in P10?",
          "How much gas did each store spend in P10?", "How much did we spend on water in P10?",
          "What was rent by store in P10?", "Show repairs and maintenance for P10",
          "Show me the R&M transactions for Katy in P10", "What was labor % at Katy last week?",
          "Which store had the most drive-thru cars yesterday?", "What were net sales in P10?"):
    check(f"not a review: {q[:50]}", not C.is_financial_waste_review(q))

# ---------------------------------------------------------------------------
print("3. Window (server-chosen, no arbitrary period)")
w = C.fin_review_window(EXACT)
labels = [p["label"] for p in w["periods"]]
check("default window = latest complete periods with R365 sales + financial data",
      labels == ["P9", "P10"] and w["start"] == "2026-08-13" and w["end_exclusive"] == "2026-10-08",
      str(labels))
check("P8 is excluded: no R365 net sales for it", "P8" not in labels)
w10 = C.fin_review_window("Period 10 numbers were lackluster, find financial waste")
check("a named eligible period ends the window", w10["named_period"] == "P10"
      and [p["label"] for p in w10["periods"]][-1] == "P10" and w10["note"] is None)
w11 = C.fin_review_window("cost cutting review for P11")
check("a CURRENT period is never used; the reason is stated",
      "P11" not in [p["label"] for p in w11["periods"]] and "not an eligible" in (w11["note"] or ""))
plan = C.fin_review_plan(w)
check("plan states the window with its last day, not the exclusive bound",
      '"P9, P10 (2026-08-13 to 2026-10-07)"' in plan and "never write it as a date" in plan)
check("no eligible period -> no SQL, ask which period",
      "Do not run SQL" in C.fin_review_plan({"periods": []}))

# ---------------------------------------------------------------------------
print("4. The three queries: gated, set-based, fast, bounded")
qs = C.fin_review_queries(w)
qs["Q3"] = qs["Q3"].replace("<PAIRS>", "(establishment_id = 36 AND gl_account_number = '6000-14') "
                                       "OR (establishment_id = 32 AND gl_account_number = '70250')")
out = {}
for k, sql in qs.items():
    t = time.perf_counter()
    g = C.enforce_scope(sql, None, w["start"], w["end_exclusive"], cache={})
    o = C.run_sql(sql)
    secs = time.perf_counter() - t
    out[k] = o
    check(f"{k} passes the scope gate and runs in {secs:.2f}s", g["analysis_permitted"] and secs < 5)
    check(f"{k} payload fits the tool-result cap", len(json.dumps(o)) < 60000,
          str(len(json.dumps(o))))
q1 = out["Q1"]["rows"]
accts = {r["gl_account_number"]: r for r in q1}
check("food excluded: no Cost of Goods Sold account in Q1",
      not any(r["account_type"] == "Cost of Goods Sold" for r in q1))
check("labor excluded: no Labor account and no 5500 payroll account",
      not any(r["account_type"] == "Labor" or r["gl_account_number"].startswith("5500") for r in q1))
check("sales deductions excluded (4500 discounts/comps, 2260 sales tax)",
      not any(r["gl_account_number"].startswith("4500") or r["gl_account_number"] == "2260-000"
              for r in q1))
check("supplies, waste removal, pest control, grease trap, R&M present",
      {"6500-02", "6500-05", "6200-06", "6100-03", "6100-06", "6000-14"} <= set(accts))
check("franchise / management fee / interest are context, rent is lease-driven",
      accts["8500-07"]["review_class"] == "CONTRACTUAL_CONTEXT"
      and accts["94050"]["review_class"] == "CONTRACTUAL_CONTEXT"
      and accts["7500-11"]["review_class"] == "LEASE_DRIVEN")
check("unmapped accounts keep their mapping status (never a verified category)",
      accts["6200-06"]["mapping_status"] == "CANDIDATE" and accts["6500-02"]["mapping_status"] == "UNMAPPED"
      and accts["6500-02"]["analytics_category"] is None)
r0 = q1[0]
check("one margin point = 1% of the same window's R365 net sales",
      abs(r0["one_margin_point"] - r0["network_net_sales"] / 100) < 0.01, str(r0["one_margin_point"]))
q2 = out["Q2"]["rows"]
check("Q2 returns only HIGH/MODERATE outliers with comparable coverage",
      q2 and all(r["outlier_class"] in ("HIGH_OUTLIER", "MODERATE_OUTLIER") for r in q2))
check("Q2 never contains a no-posting store (missing is not $0)",
      all(r["amount"] is not None for r in q2))
check("Q2 labels rent rows LEASE_DRIVEN",
      all((r["review_class"] == "LEASE_DRIVEN") == (r["gl_account_number"] == "7500-11") for r in q2))
no_rent = C.fin_review_queries(w)["Q2"].replace("NOT IN ('8500-07', '94050', '94000')",
                                                "NOT IN ('8500-07', '94050', '94000', '7500-11')")
alt = C.run_sql(no_rent)["rows"]
check("outliers-to-median scenario excludes rent (identical with rent removed from the data)",
      alt[0]["scenario_outliers_to_median_excl_rent"] == q2[0]["scenario_outliers_to_median_excl_rent"],
      f"{alt[0]['scenario_outliers_to_median_excl_rent']} vs {q2[0]['scenario_outliers_to_median_excl_rent']}")
check("Q3 lists documents (not every line), at most 12 per pair",
      out["Q3"]["row_count"] <= 24 and "documents_in_window" in out["Q3"]["columns"])

# ---------------------------------------------------------------------------
print("5. Plan content")
for needle, why in (
        ("FOOD (all Cost of Goods Sold accounts", "food excluded"),
        ("LABOR (Labor accounts and 5500 payroll accounts)", "labor excluded"),
        (f"at most {C.FIN_REVIEW_SQL_BUDGET} run_sql calls", "tool budget stated"),
        ("Do not call check_data", "no extra gate probing"),
        ("no industry benchmarks", "no invented benchmarks"),
        ("do not invent footprints", "no invented store footprint"),
        ("ask for store square footage", "asks for footprint"),
        ("A store with no\n    posting is NOT $0", "missing is not $0"),
        ("Downtown Houston electricity is quarterly", "owner-confirmed cadence"),
        ("Never present a posting gap as a cost saving", "gaps are not savings"),
        ("Never write \"savings identified\" for a scenario", "scenario labelling"),
        ("ALREADY EXCLUDES Rent - Restaurant", "rent scenario stated"),
        ("never with \"let me check\"", "no mid-investigation ending")):
    check(f"plan: {why}", needle in plan, needle)
check("synthesis rules: 700 words, no hand totals",
      "AT MOST 700 WORDS" in C.FIN_REVIEW_SYNTHESIS_RULES
      and "no totals added up by hand" in C.FIN_REVIEW_SYNTHESIS_RULES)

# ---------------------------------------------------------------------------
print("6. Review loop: budget enforced by the server")
TPL = C.fin_review_queries(w)
s, e = w["start"], w["end_exclusive"]
script = [resp("tool_use", thinking(), tool(1, TPL["Q1"], s, e), tool(2, TPL["Q2"], s, e)),
          resp("tool_use", tool(3, PERIOD_SQL), tool(4, PERIOD_SQL)),
          resp("end_turn", text("Review answer."))]
r, f = run(EXACT, script)
executed = [st for st in r.steps if st.get("sql") and not st.get("error")]
skipped = [st for st in r.steps if st.get("error") == "not executed: review tool budget reached"]
check("review: at most 3 run_sql executed", len(executed) == 3, str(len(executed)))
check("review: a 4th query is refused, not run", len(skipped) == 1)
check("review: then one synthesis call without tools",
      len(f.calls) == 3 and "tools" not in f.calls[-1] and r.answer.startswith("Review answer."))
check("review: the plan is in the system prompt of every call",
      all(any("FINANCIAL_WASTE_REVIEW" in b["text"] for b in c["system"]) for c in f.calls))
check("review: synthesis carries the review answer rules",
      C.FIN_REVIEW_SYNTHESIS_RULES in f.calls[-1]["messages"][-1]["content"])
check("review: intent and window recorded in steps",
      r.steps[0].get("intent") == "FINANCIAL_WASTE_REVIEW" and r.steps[0].get("window") == ["P9", "P10"])
r, f = run("How much electricity did we spend in P10?", [resp("end_turn", text("x"))])
check("ordinary question: no review plan in the system prompt",
      not any("FINANCIAL_WASTE_REVIEW" in b["text"] for b in f.calls[0]["system"]))

# ---------------------------------------------------------------------------
print("7. Margin targets (computed in SQL, guaranteed in the answer)")
r0 = out["Q1"]["rows"][0]
check("Q1: net sales 4,178,625.62", r0["network_net_sales"] == 4178625.62, str(r0["network_net_sales"]))
check("Q1: one_margin_point = 41,786.26", r0["one_margin_point"] == 41786.26, str(r0["one_margin_point"]))
check("Q1: two_margin_points = 83,572.52", r0["two_margin_points"] == 83572.52, str(r0["two_margin_points"]))
check("Q1: three_margin_points = 125,358.78", r0["three_margin_points"] == 125358.78, str(r0["three_margin_points"]))
ev = {"targets": r0, "scenario": out["Q2"]["rows"][0]}
g = C.fin_review_guarantee("Review body without the figures.", ev)
check("backstop: missing targets are appended from SQL",
      "1 point = $41,786.26" in g and "2 points = $83,572.52" in g and "3 points = $125,358.78" in g, g[-260:])
check("backstop: targets labelled target size only, not savings", "target size only, not savings" in g)
hyp = ("The $93,954.27 / 2.25-point outlier-to-median scenario is the current working hypothesis "
       "for the 2–3 point objective, pending invoice, contract and business validation. "
       "It is not confirmed savings.")
check("backstop: exact working-hypothesis sentence appended from SQL", hyp in g, g[-300:])
full = "1 point = $41,786.26, 2 points = $83,572.52, 3 points = $125,358.78. " + hyp
check("backstop: nothing appended when the answer already has both", C.fin_review_guarantee(full, ev) == full)
check("backstop: never turns an empty answer into a partial one", C.fin_review_guarantee("", ev) == "")
check("synthesis rules: targets + exact hypothesis sentence + banned framings",
      "\"2 points = $<two_margin_points>\"" in C.FIN_REVIEW_SYNTHESIS_RULES
      and "working hypothesis for the 2–3 point objective" in C.FIN_REVIEW_SYNTHESIS_RULES
      and "It is not confirmed savings." in C.FIN_REVIEW_SYNTHESIS_RULES
      and "identified, achievable or confirmed savings" in C.FIN_REVIEW_SYNTHESIS_RULES)
check("plan: the three target lines in the executive summary",
      "\"3 points = $<three_margin_points>\"" in C.fin_review_plan(w))
r, f = run(EXACT, [resp("tool_use", tool(1, TPL["Q1"], s, e), tool(2, TPL["Q2"], s, e)),
                   resp("tool_use", tool(3, PERIOD_SQL)),
                   resp("end_turn", text("Review body that forgot the targets."))])
check("review answer always carries the three targets and the hypothesis sentence",
      all(x in r.answer for x in ("$41,786.26", "$83,572.52", "$125,358.78", "working hypothesis",
                                  "It is not confirmed savings.")), r.answer[-300:])

# ---------------------------------------------------------------------------
print("8. Blocked path: same synthesis helper, evidence kept, never empty")
UNBOUNDED = ("SELECT store_name, SUM(expense_amount) AS total FROM v_r365_financial_transaction_llm "
             "WHERE gl_account_number = '6000-14' GROUP BY store_name")
BOUNDED = ("SELECT store_name, SUM(expense_amount) AS total FROM v_r365_financial_transaction_llm "
           f"WHERE gl_account_number = '6000-14' AND accounting_date >= '{s}' "
           f"AND accounting_date < '{e}' GROUP BY store_name")


def check_data(i):
    return SimpleNamespace(type="tool_use", id=f"c{i}", name="check_data",
                           input={"establishment_id": None, "period_start": s, "period_end": e,
                                  "domains": ["r365_financial_detail"]})


Q_HVAC = "Which stores spent most on HVAC repairs in P9 and P10?"
blocked_script = [resp("tool_use", check_data(1), tool(2, BOUNDED, s, e)),
                  resp("tool_use", tool(3, UNBOUNDED, s, e))]
r, f = run(Q_HVAC, blocked_script + [resp("max_tokens", thinking()),
                                     resp("end_turn", text("Answer from the bounded HVAC rows."))])
ok_steps = [st for st in r.steps if st.get("sql") == BOUNDED and not st.get("error")]
rej = [st for st in r.steps if st.get("sql") == UNBOUNDED]
check("E. the unbounded query is still rejected by the gate, never executed",
      rej and "explicit date bounds" in rej[0]["error"] and rej[0]["row_count"] is None)
synth = f.calls[2:]
check("A. blocked path goes through the common helper (tools off, thinking disabled)",
      synth and all("tools" not in c and c.get("thinking") == {"type": "disabled"} for c in synth))
sent = json.dumps(synth[0]["messages"], default=str)
check("A. earlier successful evidence is in the synthesis context",
      ok_steps and ok_steps[0]["row_count"] > 0 and "LCF Missouri City" in sent)
verdict = synth[0]["messages"][-1]["content"]
check("A. the rejected query's error is passed as context; source not called untrusted",
      "rejected_query_error" in verdict and '"source_trusted":true' in verdict
      and "The data source PASSED its trust gate" in verdict
      and "explicit date bounds" in verdict and "failed its trust gate" not in verdict, verdict[:200])
check("B/C. thinking-only/max_tokens on the blocked call -> one retry -> visible answer",
      len(synth) == 2 and r.answer == "Answer from the bounded HVAC rows.", r.answer)
check("blocked step records the common helper",
      any(st.get("forced_synthesis") == "blocked" for st in r.steps))
r, f = run(Q_HVAC, blocked_script + [resp("max_tokens", thinking()), resp("end_turn", text(""))])
check("D. retry also empty -> explicit fallback, never \"\"",
      r.answer == C.EMPTY_ANSWER_FALLBACK and len(f.calls) == 4, r.answer)
r, f = run(Q_HVAC, [resp("tool_use", check_data(1))] * 1 + [resp("end_turn", text("ok"))])
check("a passing check_data alone does not trigger the blocked path",
      not any(st.get("forced_synthesis") == "blocked" for st in r.steps))
Q3_BAD = TPL["Q3"].replace("AND (<PAIRS>)", "").replace(
    f"WHERE accounting_date >= '{s}' AND accounting_date < '{e}'\n", "WHERE TRUE\n")
r, f = run(EXACT, [resp("tool_use", tool(1, TPL["Q1"], s, e), tool(2, TPL["Q2"], s, e)),
                   resp("tool_use", tool(3, Q3_BAD, s, e)),
                   resp("end_turn", text("Review from Q1 and Q2; drill-down not completed."))])
done = [st for st in r.steps if st.get("sql") and not st.get("error")]
bad = [st for st in r.steps if st.get("sql") == Q3_BAD]
sent = json.dumps(f.calls[-1]["messages"], default=str)
check("review: Q3 rejected by the gate (unbounded), Q1 + Q2 kept",
      len(done) == 2 and bad and bad[0]["error"] and "one_margin_point" in sent
      and "scenario_outliers_to_median_excl_rent" in sent)
check("review: still answers (with targets) instead of the fallback",
      r.answer.startswith("Review from Q1 and Q2") and "$83,572.52" in r.answer
      and r.answer != C.EMPTY_ANSWER_FALLBACK)
check("only two messages.create call sites remain (main loop + common helper)",
      open("chat_sql.py").read().count("client.messages.create(") == 2)

failed = [x for x in results if not x[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
