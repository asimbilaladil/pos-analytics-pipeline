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
# These originally asserted that Revel was FAILING for this date, which was
# true while the sync was broken and became false the moment it was repaired.
# Coupling a unit test to a transient production outage makes it fail for the
# wrong reason, so the separation is now asserted structurally: whatever Revel's
# health, an HME-only profile must carry no Revel gate at all, and an
# HME+Revel profile must carry exactly Revel's verdict.
S, E = "2026-09-24", "2026-09-25"
revel = D.meta_profile(None, S, E, domains=["revel"])
hme = D.meta_profile(None, S, E, domains=["hme"])
both = D.meta_profile(None, S, E, domains=["hme", "revel"])
lab = D.meta_profile(None, S, E, domains=["hme", "labor"])

check("an HME-only profile runs no Revel reconciliation at all",
      "reconciliation" not in hme)
check("the HME profile carries no Revel blocking reason",
      not any("reconcil" in r and "sales" in r for r in hme["blocking_reasons"]))
check("an HME-only question is not affected by Revel's verdict",
      hme["analysis_permitted"] is True, str(hme["blocking_reasons"]))
check("HME + Revel inherits Revel's verdict exactly (no weakening)",
      both["analysis_permitted"] == revel["analysis_permitted"],
      f"both={both['analysis_permitted']} revel={revel['analysis_permitted']}")
check("HME + labour does not require Revel sales",
      "reconciliation" not in lab and lab["analysis_permitted"] is True,
      str(lab["blocking_reasons"]))

# enforce_scope must gate on the statement, not on anything declared
try:
    D.enforce_scope("SELECT establishment_id, drive_thru_cars FROM v_hme_store_daily_llm "
                    "WHERE business_date >= '2026-09-24' AND business_date < '2026-09-25'",
                    None, S, E, cache={})
    ok = True; why = ""
except Exception as e:
    ok = False; why = str(e)[:120]
check("enforce_scope allows HME SQL while Revel is failing", ok, why)

# Revel SQL must follow Revel's verdict, whatever that verdict currently is --
# blocked when the gate fails, permitted when it passes. Asserting only the
# "blocked" half tied this to an outage.
try:
    D.enforce_scope("SELECT sum(total_revenue) FROM features_daily_summary_v2 "
                    "WHERE date >= '2026-09-24' AND date < '2026-09-25'",
                    None, S, E, cache={})
    blocked = False
except D.ScopeError:
    blocked = True
check("enforce_scope applies Revel's own verdict to Revel SQL",
      blocked == (not revel["analysis_permitted"]),
      f"blocked={blocked} revel_permitted={revel['analysis_permitted']}")

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
# 8 -- early short-circuit on a failed required domain
#
# The 2026-09-25 request "Compare drive-thru performance with sales yesterday"
# took 146.6 s: 12 check_data calls, zero run_sql, every one a cache MISS
# because the model kept probing a different scope hunting for a usable Revel
# window. No scope could succeed -- the source itself was untrusted.
# ---------------------------------------------------------------------------
_S, _E = "2026-09-24", "2026-09-25"

_hme_ok = {"available": True, "latest_business_date": "2026-09-24",
           "freshness_lag_days": 1, "per_day": [{"business_date": "2026-09-24"}],
           "incomplete_days": [], "unreconciled_days": [],
           "missing_verified_stores": []}


def _meta(doms, fails, **extra):
    m = {"domains": sorted(D.normalise_domains(doms)),
         "blocking_reasons": fails, "warnings": [],
         "analysis_permitted": not fails,
         "scope": {"establishment_id": None, "period_start": _S}}
    m.update(extra)
    return m


# HME PASS + Revel FAIL -> blocked, but the HME side stays usable
ctx = D.blocked_domain_context(
    _meta(["hme", "revel"], ["sales reconciliation is off by -3.74%"],
          hme=_hme_ok, reconciliation={"delta_pct": -3.74, "status": "FAIL"},
          freshness={"source_lag_hours": 31.3}, volumes={"order_rows": 86}),
    ["hme", "revel"])
check("HME PASS + Revel FAIL is reported as blocked", ctx["blocked"] is True)
check("the trusted HME side is preserved for the answer",
      ctx["hme"]["trusted"] is True and ctx["hme"]["latest_business_date"] == "2026-09-24")
check("the Revel side is marked untrusted with its reason",
      ctx["revel"]["trusted"] is False and "-3.74" in ctx["revel"]["reason"])
check("the numeric reconciliation delta is carried through",
      ctx["revel"]["reconciliation_delta_pct"] == -3.74)
check("the freshness lag is carried through",
      ctx["revel"]["freshness_lag_hours"] == 31.3)
check("the model is told not to retry other scopes",
      "do NOT retry other stores or dates" in ctx["instruction"])
check("the model is told not to run further SQL",
      "Do NOT run further SQL" in ctx["instruction"])

# HME FAIL + Revel PASS -> same principle, mirrored
ctx2 = D.blocked_domain_context(
    _meta(["hme", "revel"], ["HME cross-source reconciliation failed on 2026-09-24"],
          hme=dict(_hme_ok, unreconciled_days=["2026-09-24"]),
          reconciliation={"delta_pct": 0.01, "status": "PASS"},
          freshness={"source_lag_hours": 2.0}, volumes={"order_rows": 6000}),
    ["hme", "revel"])
check("HME FAIL + Revel PASS is also blocked", ctx2["blocked"] is True)
check("the HME side is marked untrusted when HME is the failure",
      ctx2["hme"]["trusted"] is False)
check("the Revel side stays trusted when only HME failed",
      ctx2["revel"]["trusted"] is True, str(ctx2["revel"]))

# HME + Labor with labour failing
ctx3 = D.blocked_domain_context(
    _meta(["hme", "labor"], ["no labour records exist for this store and period"],
          hme=_hme_ok, labor={"available": False, "store_day_rows": 0}),
    ["hme", "labor"])
check("HME + labour with labour absent is blocked", ctx3["blocked"] is True)
check("the labour side is marked untrusted", ctx3["labor"]["trusted"] is False)
check("the HME side survives a labour failure", ctx3["hme"]["trusted"] is True)

# HME PASS + Revel PASS -> nothing to short-circuit
_ok = D.meta_profile(None, "2026-09-23", "2026-09-24", domains=["hme"])
check("a passing profile permits analysis (no short circuit)",
      _ok["analysis_permitted"] is True)

# Built from a LIVE profile but with a synthetic Revel failure, so the shape is
# exercised against real data without depending on Revel actually being broken.
_live = D.meta_profile(None, _S, _E, domains=["hme", "revel"])
_forced = dict(_live, blocking_reasons=["sales reconciliation is off by -3.74%"],
               analysis_permitted=False)
_live_ctx = D.blocked_domain_context(_forced, ["hme", "revel"])
check("a blocked live profile still reports HME as trusted",
      _live_ctx["hme"]["trusted"] is True, str(_live_ctx["hme"])[:120])
check("a blocked live profile names Revel as the failure",
      _live_ctx["revel"]["trusted"] is False)
check("a healthy live profile is not marked blocked",
      D.blocked_domain_context(_live, ["hme", "revel"])["revel"]["trusted"]
      is bool(_live["analysis_permitted"]))

# The wiring itself: one final tool-free call, and the loop stops.
_src = open("chat_sql.py").read()
check("the short circuit makes a final model call WITHOUT tools",
      "anthropic.final_blocked" in _src
      and "model=model, max_tokens=4096, system=system, messages=messages)" in _src)
check("the short circuit breaks the tool loop",
      _src.index("anthropic.final_blocked") < _src.index("break", _src.index("anthropic.final_blocked")))
check("the verdict is marked authoritative so the model does not re-check",
      "DATA TRUST VERDICT (authoritative, do not re-check)" in _src)

# ---------------------------------------------------------------------------
passed = sum(1 for _, ok, _ in results if ok)
failed = len(results) - passed
print(f"\n{'='*66}\nchat domain/perf tests: {passed} passed, {failed} failed, {len(results)} total")
if failed:
    for n, ok, d in results:
        if not ok:
            print(f"  FAILED: {n} -- {d}")
sys.exit(1 if failed else 0)
