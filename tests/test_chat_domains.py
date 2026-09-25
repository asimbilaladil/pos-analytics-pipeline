#!/usr/bin/env python3
"""Source-aware data-trust gating, request-scoped caching, and request bounds.

The bug these defend against: on 2026-09-25 the first real UI question,
"Which stores had the slowest drive-thru yesterday?", was refused because
Revel's SALES reconciliation failed for that date. HME had nothing to do with
it. The request also spent ~38 s per check_data in unrelated Revel metadata and
outlived nginx's 300 s ceiling, so the browser got an HTML 504 and the spinner
never cleared.

Each domain keeps its own checks in full. What changed is that an unrelated
domain can no longer veto an unrelated question.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from dotenv import load_dotenv

load_dotenv(".env", override=True)
import chat_sql  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


D = chat_sql
# ---------------------------------------------------------------------------
# 1 -- domains are derived from the SQL's relations, not from a model's claim
# ---------------------------------------------------------------------------
check("HME-only relations -> {hme}",
      D.domains_for_relations({"v_hme_store_daily_llm"}) == {"hme"})
check("joining establishments does not drag in the Revel gate",
      D.domains_for_relations({"v_hme_store_daily_llm", "establishments"}) == {"hme"})
check("HME + Revel relations -> {hme, revel}",
      D.domains_for_relations({"v_hme_store_daily_llm", "features_daily_summary_v2"})
      == {"hme", "revel"})
check("HME + labour relations -> {hme, labor}",
      D.domains_for_relations({"v_hme_store_daily_llm", "v_labor_daily_context"})
      == {"hme", "labor"})
check("identity relations -> {identity, revel} (identity implies revel)",
      D.domains_for_relations({"v_order_identity_context"}) == {"identity", "revel"})
check("an UNKNOWN relation falls back to the strict Revel gate (fail closed)",
      D.domains_for_relations({"some_future_table"}) == {"revel"})
check("normalise(None) is every domain, never an empty pass",
      D.normalise_domains(None) == set(D.ALL_DOMAINS))
check("normalise([]) is every domain",
      D.normalise_domains([]) == set(D.ALL_DOMAINS))
check("an unrecognised domain name widens to all checks, never ignored",
      D.normalise_domains(["hme", "bogus"]) == set(D.ALL_DOMAINS))
check("a valid subset is honoured", D.normalise_domains(["hme"]) == {"hme"})

# Identity is POS-derived: it is only as trustworthy as the Revel figures under
# it, and its metadata is computed inside the full Revel profile. Letting it
# stand alone would both skip that metadata and escape the sales gate.
check("identity implies revel (declared)",
      D.normalise_domains(["identity"]) == {"identity", "revel"})
check("identity implies revel (derived from relations)",
      D.domains_for_relations({"v_order_identity_context"}) == {"identity", "revel"})
_ident = D.meta_profile(None, "2026-09-17", "2026-09-24", domains=["identity"])
check("an identity question still gets its identity metadata",
      _ident.get("identity") is not None)
check("an identity question still gets the Revel reconciliation block",
      "reconciliation" in _ident)
_hme_only = D.meta_profile(None, "2026-09-17", "2026-09-24", domains=["hme"])
check("an HME-only question carries no Revel/identity/loyalty metadata",
      not {"reconciliation", "identity", "loyalty"} & set(_hme_only))
check("an HME-only question does carry its HME block",
      _hme_only.get("hme") is not None)

# ---------------------------------------------------------------------------
# 2 -- the HME gate itself, in isolation (no weakening)
# ---------------------------------------------------------------------------
GOOD = {"available": True, "per_day": [{"business_date": "2026-09-24"}],
        "missing_verified_stores": [], "unreconciled_days": [],
        "incomplete_days": []}
f, w = D._hme_gate(GOOD, "2026-09-24", "2026-09-25")
check("a complete, reconciled HME day passes its own gate", not f, str(f))

f, w = D._hme_gate(dict(GOOD, unreconciled_days=["2026-09-24"]), "2026-09-24", "2026-09-25")
check("HME reconciliation failure BLOCKS an HME answer", bool(f), str(f))

f, w = D._hme_gate(dict(GOOD, missing_verified_stores=["2026-09-24"]),
                   "2026-09-24", "2026-09-25")
check("a missing VERIFIED store BLOCKS an HME answer", bool(f), str(f))

f, w = D._hme_gate(dict(GOOD, incomplete_days=["2026-09-24"]), "2026-09-24", "2026-09-25")
check("an absent OUT_OF_SCOPE store WARNS but does not block", not f and bool(w), str(w))

f, w = D._hme_gate({"available": False}, "2026-09-24", "2026-09-25")
check("no HME data for the scope blocks with a clear reason", bool(f), str(f))

f, w = D._hme_gate(None, "2026-09-24", "2026-09-25")
check("unknown HME availability fails closed", bool(f), str(f))

# ---------------------------------------------------------------------------
# 3 -- the live cross-domain case: Revel is failing right now
# ---------------------------------------------------------------------------
S, E = "2026-09-24", "2026-09-25"
revel = D.meta_profile(None, S, E, domains=["revel"])
hme = D.meta_profile(None, S, E, domains=["hme"])
check("the Revel gate is genuinely failing for this date (precondition)",
      not revel["analysis_permitted"],
      str(revel["blocking_reasons"])[:100])
check("an HME-only question is PERMITTED despite the Revel failure",
      hme["analysis_permitted"], str(hme["blocking_reasons"]))
check("the HME profile carries no Revel blocking reason",
      not any("reconcil" in r and "sales" in r for r in hme["blocking_reasons"]))

both = D.meta_profile(None, S, E, domains=["hme", "revel"])
check("HME + Revel IS blocked when Revel fails (no weakening)",
      not both["analysis_permitted"], str(both["blocking_reasons"])[:100])

lab = D.meta_profile(None, S, E, domains=["hme", "labor"])
check("HME + labour does not require Revel sales",
      lab["analysis_permitted"], str(lab["blocking_reasons"]))

# enforce_scope must gate on the statement, not on anything declared
try:
    D.enforce_scope("SELECT establishment_id, drive_thru_cars FROM v_hme_store_daily_llm "
                    "WHERE business_date >= '2026-09-24' AND business_date < '2026-09-25'",
                    None, S, E, cache={})
    ok = True; why = ""
except Exception as e:
    ok = False; why = str(e)[:120]
check("enforce_scope allows HME SQL while Revel is failing", ok, why)

try:
    D.enforce_scope("SELECT sum(total_revenue) FROM features_daily_summary_v2 "
                    "WHERE date >= '2026-09-24' AND date < '2026-09-25'",
                    None, S, E, cache={})
    blocked = False
except D.ScopeError:
    blocked = True
check("enforce_scope still blocks Revel SQL while Revel is failing", blocked)

# ---------------------------------------------------------------------------
# 4 -- request-scoped cache
# ---------------------------------------------------------------------------
cache = {}
t = time.perf_counter(); a = D.meta_profile(None, S, E, domains=["hme"], cache=cache)
cold = time.perf_counter() - t
t = time.perf_counter(); b = D.meta_profile(None, S, E, domains=["hme"], cache=cache)
warm = time.perf_counter() - t
check("first call computes, second reuses",
      a["_cache"] == "computed" and b["_cache"] == "reused")
check("the reused call does no measurable work", warm < 0.01, f"{warm:.4f}s")
check("cached and computed payloads agree",
      a["hme"]["latest_business_date"] == b["hme"]["latest_business_date"])

n = len(cache)
D.meta_profile(None, "2026-09-23", "2026-09-24", domains=["hme"], cache=cache)
check("a different DATE does not reuse the entry", len(cache) == n + 1)
D.meta_profile(26, S, E, domains=["hme"], cache=cache)
check("a different STORE does not reuse the entry", len(cache) == n + 2)
D.meta_profile(None, S, E, domains=["hme", "labor"], cache=cache)
check("a different DOMAIN SET does not reuse the entry", len(cache) == n + 3)

fresh = {}
c = D.meta_profile(None, S, E, domains=["hme"], cache=fresh)
check("a new request-scoped cache starts empty (no cross-request reuse)",
      c["_cache"] == "computed" and len(fresh) == 1)

# ---------------------------------------------------------------------------
# 5 -- performance targets
# ---------------------------------------------------------------------------
t = time.perf_counter(); D.meta_profile(None, S, E, domains=["hme"])
hme_s = time.perf_counter() - t
check(f"HME-only meta/check under 2s ({hme_s:.2f}s)", hme_s < 2.0)

t = time.perf_counter(); D.meta_profile(None, S, E, domains=["hme", "labor"])
hl_s = time.perf_counter() - t
check(f"HME+labour meta/check under 5s ({hl_s:.2f}s)", hl_s < 5.0)

chat_sql._nonind_cache.update({"ids": None, "at": 0})
t = time.perf_counter(); ids = chat_sql._suspected_non_individual_ids()
ident_s = time.perf_counter() - t
check(f"identity scan under 2s ({ident_s:.2f}s), previously a 15s timeout",
      ident_s < 2.0)
check("identity scan returns real ids, not a silently-empty timeout result",
      len(ids) > 0, f"{len(ids)} ids")

# ---------------------------------------------------------------------------
# 6 -- request bounds
# ---------------------------------------------------------------------------
check("the app deadline is inside nginx's 300s proxy_read_timeout",
      0 < D.REQUEST_DEADLINE_SECONDS < 300, str(D.REQUEST_DEADLINE_SECONDS))
check("the Anthropic per-call timeout is explicit, not the SDK's 600s",
      0 < D.ANTHROPIC_TIMEOUT_SECONDS < 600, str(D.ANTHROPIC_TIMEOUT_SECONDS))
check("retries are bounded so worst case stays inside the deadline",
      (D.ANTHROPIC_MAX_RETRIES + 1) * D.ANTHROPIC_TIMEOUT_SECONDS
      <= D.REQUEST_DEADLINE_SECONDS,
      f"{D.ANTHROPIC_MAX_RETRIES + 1} x {D.ANTHROPIC_TIMEOUT_SECONDS}s "
      f"vs {D.REQUEST_DEADLINE_SECONDS}s")
check("ChatTimeout exists for the structured error path",
      issubclass(D.ChatTimeout, Exception))

import backend.chat as chat_svc  # noqa: E402
check("the service layer maps ChatTimeout to a user-safe message",
      "took too long" in open("backend/chat.py").read())
check("the service layer never re-raises to the HTTP layer",
      "Never raises to the caller" in open("backend/chat.py").read())

# ---------------------------------------------------------------------------
# 7 -- timing instrumentation logs durations only
# ---------------------------------------------------------------------------
tl = []
D._timing(tl, "stage", 1.2345)
check("timings record a label and a rounded duration", tl == [("stage", 1.234)], str(tl))
src = open("chat_sql.py").read()
i = src.index("def _log_timings")
body = src[i:i + 600]
for forbidden in ("sql", "rows", "answer", "question", "api_key", "token"):
    check(f"the timing log never emits {forbidden!r}",
          f'"{forbidden}"' not in body and f"%({forbidden})" not in body)

# ---------------------------------------------------------------------------
passed = sum(1 for _, ok, _ in results if ok)
failed = len(results) - passed
print(f"\n{'='*66}\nchat domain/perf tests: {passed} passed, {failed} failed, {len(results)} total")
if failed:
    for n, ok, d in results:
        if not ok:
            print(f"  FAILED: {n} -- {d}")
sys.exit(1 if failed else 0)
