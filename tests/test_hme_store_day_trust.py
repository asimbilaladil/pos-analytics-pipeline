#!/usr/bin/env python3
"""Store-level HME trust in AI Chat and MCP (migration 65).

Run (isolated test database ONLY -- see tests/isolated_db.py):
    DB_HOST=127.0.0.1 DB_PORT=55432 DB_NAME=laynes_test DB_USER=... DB_PASS=... \\
    DB_RO_USER=laynes_ro DB_RO_PASS=... \\
        venv/bin/python tests/test_hme_store_day_trust.py       (exit 0 = pass)

The test database holds a read-only copy of production's HME tables. The test
applies migration 65 to it (idempotent) and then reads only, as laynes_ro,
exactly as chat and MCP do.

2026-11 finding: LCF Mission Bend (establishment 25) is graded PARTIAL /
RECONCILIATION_MISMATCH for 2026-11-03 and 2026-11-06 (its own goal-D check
failed) while the NETWORK day reconciled. The chat gate and MCP
get_drive_thru_speed judged only the network day, so both served those
store-days as trusted (MCP status "ok").

Pinned:
  A. v_hme_store_day_trust_llm grades the row actually loaded; legacy rows by
     their network run; no HME identifiers; laynes_ro can read it
  B. chat: single-store scope FAILs on an unverified store-day and names the
     dates and reason; network scope names it as a mandatory warning without
     blocking verified stores; every network-day rule is unchanged (10-14,
     10-15, 11-13 still FAIL for every store); unaffected stores/dates PASS
  C. chat fails CLOSED when the view is missing; an older block shape keeps
     the old behaviour; the blocked verdict says the failure is store-specific
  D. MCP: unverified store-days are excluded from figures and listed with their
     grade; status partial/unavailable, never ok; unaffected stores stay ok;
     multi-store answers cannot hide one store's failure
  D2. MCP field contract: days_excluded_unreconciled keeps its 73ef62a meaning
     (network days only); store-level exclusions are listed only in
     days_excluded_store_unverified (overlap flagged); network-only, store-only,
     overlapping and fully verified ranges keep counts/trust_status/figures
     consistent
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import isolated_db  # noqa: E402

isolated_db.require_isolated()
import chat_sql as C  # noqa: E402
from mcp_server import analytics as A  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


MB, OTHER, S15 = 25, 6, 15

# Apply migration 65 to the isolated database (CREATE OR REPLACE: idempotent).
conn = isolated_db.rw()
with conn.cursor() as cur:
    cur.execute(open("migrations/65_hme_store_day_trust.sql").read())
conn.commit()
conn.close()


def ro_query(sql, params=()):
    c = C._ro_conn()
    try:
        c.set_session(readonly=True)
        with c.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        c.close()


def grade(est, d):
    r = ro_query("SELECT trust_status, grade_source, store_result_status, store_result_reason "
                 "FROM v_hme_store_day_trust_llm WHERE establishment_id=%s AND business_date=%s",
                 (est, d))
    return r[0] if r else None


# ═══ A. the view ══════════════════════════════════════════════════════════════
print("A. v_hme_store_day_trust_llm")
for d in ("2026-11-03", "2026-11-06"):
    check(f"Mission Bend {d}: UNVERIFIED from its PARTIAL store result",
          grade(MB, d) == ("UNVERIFIED", "STORE_RESULT", "PARTIAL", "RECONCILIATION_MISMATCH"),
          str(grade(MB, d)))
check("Mission Bend 2026-10-14: UNVERIFIED", grade(MB, "2026-10-14")[0] == "UNVERIFIED")
check("Mission Bend 2026-11-09 (recovered): VERIFIED",
      grade(MB, "2026-11-09") == ("VERIFIED", "STORE_RESULT", "SUCCESS", "OK"))
check("unaffected store 2026-11-03: VERIFIED", grade(OTHER, "2026-11-03")[0] == "VERIFIED")
check("legacy row (2026-09-15, before per-store results): VERIFIED by its network run",
      grade(MB, "2026-09-15")[:2] == ("VERIFIED", "NETWORK_RUN"), str(grade(MB, "2026-09-15")))
check("legacy row on an unreconciled network day (2026-10-11): UNVERIFIED",
      grade(OTHER, "2026-10-11")[:2] == ("UNVERIFIED", "NETWORK_RUN"))
cols = [r[0] for r in ro_query("SELECT column_name FROM information_schema.columns "
                               "WHERE table_name='v_hme_store_day_trust_llm'")]
check("no HME identifiers or run ids exposed",
      not any(c in cols for c in ("hme_store_number", "run_id", "detail")), str(cols))
dup = ro_query("SELECT count(*) FROM (SELECT establishment_id, business_date FROM "
               "v_hme_store_day_trust_llm GROUP BY 1,2 HAVING count(*) > 1) x")[0][0]
check("one row per establishment x business_date", dup == 0)
n_daily = ro_query("SELECT count(*) FROM v_hme_store_daily_llm")[0][0]
n_trust = ro_query("SELECT count(*) FROM v_hme_store_day_trust_llm")[0][0]
check("covers exactly the store-days served by v_hme_store_daily_llm", n_daily == n_trust,
      f"{n_daily} vs {n_trust}")
unv_net_ok = ro_query("""SELECT t.establishment_id, t.business_date::text
                           FROM v_hme_store_day_trust_llm t
                           JOIN v_hme_day_completeness_llm c USING (business_date)
                          WHERE t.trust_status <> 'VERIFIED' AND c.reconciliation_ok
                          ORDER BY 2, 1""")
check("only Mission Bend 11-03 / 11-06 are unverified on network-reconciled days",
      unv_net_ok == [(MB, "2026-11-03"), (MB, "2026-11-06")], str(unv_net_ok))


# ═══ B. chat gate ═════════════════════════════════════════════════════════════
print("B. chat gate (meta_profile, HME domain)")


def prof(est, start, end_excl):
    return C.meta_profile(est, start, end_excl, domains=["hme"])


def store_reasons(m):
    return [r for r in m["blocking_reasons"] if r.startswith(C.HME_STORE_UNVERIFIED_PREFIX)]


for d, nxt in (("2026-11-03", "2026-11-04"), ("2026-11-06", "2026-11-07")):
    m = prof(MB, d, nxt)
    sr = store_reasons(m)
    check(f"Mission Bend {d}: FAIL, store-level, names store, date and reason",
          not m["analysis_permitted"] and len(sr) == 1 and "LCF Mission Bend" in sr[0]
          and d in sr[0] and "RECONCILIATION_MISMATCH" in sr[0]
          and "must not be presented as verified" in sr[0], str(m["blocking_reasons"]))
    check(f"Mission Bend {d}: network day itself is NOT reported as unreconciled",
          not any(r.startswith("HME cross-source") for r in m["blocking_reasons"]))

m = prof(MB, "2026-11-01", "2026-11-09")
sr = store_reasons(m)
check("Mission Bend 11-01..11-08: one FAIL naming exactly 11-03 and 11-06",
      not m["analysis_permitted"] and len(sr) == 1 and "2026-11-03" in sr[0]
      and "2026-11-06" in sr[0] and "2026-11-04" not in sr[0], str(sr))

m = prof(MB, "2026-10-14", "2026-10-15")
check("Mission Bend 10-14: still FAILs on the network rule; not repeated store-level",
      not m["analysis_permitted"]
      and any(r.startswith("HME cross-source reconciliation failed on 2026-10-14")
              for r in m["blocking_reasons"]) and not store_reasons(m),
      str(m["blocking_reasons"]))

for est, d, nxt in ((MB, "2026-11-09", "2026-11-13"), (OTHER, "2026-11-03", "2026-11-04"),
                    (OTHER, "2026-11-01", "2026-11-09"), (MB, "2026-10-16", "2026-11-03")):
    m = prof(est, d, nxt)
    check(f"store {est} {d}..{nxt} (verified): PASS", m["analysis_permitted"],
          str(m["blocking_reasons"]))

m = prof(None, "2026-11-03", "2026-11-04")
sw = [w for w in m["warnings"] if w.startswith(C.HME_STORE_UNVERIFIED_PREFIX)]
check("network 11-03: verified stores not blocked; Mission Bend named in a mandatory warning",
      m["analysis_permitted"] and len(sw) == 1 and "LCF Mission Bend" in sw[0]
      and "never call a sum" in sw[0], str(m["warnings"]))
check("network 11-03: the per-store list is structural, not only text",
      [(u["establishment_id"], u["business_date"])
       for u in m["hme"]["store_day_trust"]["unverified_store_days"]] == [(MB, "2026-11-03")])
m = prof(None, "2026-11-09", "2026-11-13")
check("network 11-09..11-12: no store-level warning",
      m["analysis_permitted"] and not any(w.startswith(C.HME_STORE_UNVERIFIED_PREFIX)
                                          for w in m["warnings"]), str(m["warnings"]))

for d, nxt in (("2026-10-14", "2026-10-15"), ("2026-10-15", "2026-10-16"),
               ("2026-11-13", "2026-11-14")):
    for est in (None, OTHER, MB, S15):
        m = prof(est, d, nxt)
        check(f"network rule unchanged: {d} store {est} still FAIL",
              not m["analysis_permitted"] and any(
                  r.startswith("HME cross-source reconciliation failed")
                  for r in m["blocking_reasons"]))

# ═══ C. fail closed / backward compatibility / blocked verdict ═══════════════
print("C. fail closed, old block shape, blocked verdict")
orig = C._HME_STORE_TRUST_SQL
C._HME_STORE_TRUST_SQL = orig.replace("v_hme_store_day_trust_llm", "v_hme_store_day_trust_missing")
try:
    m = prof(OTHER, "2026-11-09", "2026-11-10")
    check("view missing (code before migration): FAIL closed with a clear reason",
          not m["analysis_permitted"]
          and any(r.startswith(C.HME_STORE_TRUST_UNKNOWN) for r in m["blocking_reasons"]),
          str(m["blocking_reasons"]))
finally:
    C._HME_STORE_TRUST_SQL = orig

old_shape = {"available": True, "per_day": [{"business_date": "2026-11-03"}],
             "unreconciled_days": [], "incomplete_days": [], "missing_verified_stores": []}
check("older block shape (no store_day_trust): gate unchanged",
      C._hme_gate(old_shape, "2026-11-03", "2026-11-04") == ([], []))
m = prof(MB, "2026-11-03", "2026-11-04")
bv = C.blocked_domain_context(m, ["hme"])
check("blocked verdict: store_level_only, unverified store-days and a specific note",
      bv["hme"]["store_level_only"] is True and not bv["hme"]["trusted"]
      and bv["hme"]["unverified_store_days"][0]["business_date"] == "2026-11-03"
      and "not affected" in bv["hme"]["store_level_note"]
      and "specific to the store-days" in bv["instruction"])
m = prof(MB, "2026-10-14", "2026-10-15")
bv = C.blocked_domain_context(m, ["hme"])
check("blocked verdict on a network-unreconciled day is NOT store_level_only",
      bv["hme"]["store_level_only"] is False and "specific to the store-days"
      not in bv["instruction"])

# ═══ D. MCP get_drive_thru_speed ══════════════════════════════════════════════
print("D. MCP drive_thru_speed")


def dts(ids, s, e):
    return A.drive_thru_speed(ids, date.fromisoformat(s), date.fromisoformat(e), None)


def store(r, i):
    return next(x for x in r["stores"] if x["store_id"] == i)


for d in ("2026-11-03", "2026-11-06"):
    r = dts([MB], d, d)
    s = store(r, MB)
    check(f"Mission Bend {d}: status unavailable (was ok), no figures, store-level reason",
          r["status"] == "unavailable" and s["status"] == "unavailable"
          and s["trust_status"] == "UNAVAILABLE" and s["drive_thru_cars"] is None
          and s["avg_speed_of_service_seconds"] is None
          and s["reason"].startswith(C.HME_STORE_UNVERIFIED_PREFIX)
          and s["days_excluded_store_unverified"] == [
              {"date": d, "store_result_status": "PARTIAL",
               "store_result_reason": "RECONCILIATION_MISMATCH",
               "grade_source": "STORE_RESULT", "also_network_unreconciled": False}]
          and s["days_excluded_unreconciled"] == [], str(s))

r = dts([MB], "2026-11-01", "2026-11-08")
s = store(r, MB)
cars = ro_query("""SELECT sum(d.drive_thru_cars) FROM v_hme_store_daily_llm d
                    JOIN v_hme_store_day_trust_llm t USING (establishment_id, business_date)
                   WHERE d.establishment_id=%s AND d.business_date BETWEEN '2026-11-01'
                     AND '2026-11-08' AND t.trust_status='VERIFIED'""", (MB,))[0][0]
check("Mission Bend 11-01..11-08: partial, VERIFIED_SUBSET, 6 days, 11-03/11-06 excluded",
      r["status"] == "partial" and s["status"] == "partial"
      and s["trust_status"] == "VERIFIED_SUBSET" and s["days_included"] == 6
      and s["days_excluded_unreconciled"] == []
      and [x["date"] for x in s["days_excluded_store_unverified"]]
      == ["2026-11-03", "2026-11-06"], str({k: s[k] for k in s if k.startswith("days")}))
check("figures are computed from verified store-days only",
      s["drive_thru_cars"] == int(cars), f"{s['drive_thru_cars']} vs {cars}")
check("coverage summary carries the store-day trust counts",
      s["coverage"]["store_day_trust"]["unverified_store_days"] == 2
      and s["coverage"]["figures_limited_to_reconciled_days"] is True)

r = dts([MB], "2026-11-09", "2026-11-12")
check("Mission Bend 11-09..11-12 (recovered): ok, VERIFIED",
      r["status"] == "ok" and store(r, MB)["trust_status"] == "VERIFIED"
      and store(r, MB)["days_included"] == 4)
r = dts([MB], "2026-11-09", "2026-11-15")
check("Mission Bend 11-09..11-15: 11-13 excluded by the NETWORK rule only",
      r["status"] == "partial" and store(r, MB)["days_excluded_unreconciled"] == ["2026-11-13"]
      and store(r, MB)["days_excluded_store_unverified"] == [])
r = dts([OTHER], "2026-11-01", "2026-11-08")
check("unaffected store 11-01..11-08: ok, VERIFIED, nothing excluded",
      r["status"] == "ok" and store(r, OTHER)["trust_status"] == "VERIFIED"
      and store(r, OTHER)["days_excluded_store_unverified"] == [])
r = dts([OTHER, MB], "2026-11-03", "2026-11-03")
check("multi-store 11-03: overall partial, unaffected store ok, Mission Bend unavailable",
      r["status"] == "partial" and store(r, OTHER)["status"] == "ok"
      and store(r, MB)["status"] == "unavailable")
r = dts([MB], "2026-10-14", "2026-10-14")
check("Mission Bend 10-14: unavailable on the network rule (unchanged)",
      r["status"] == "unavailable"
      and store(r, MB)["reason"].startswith("HME cross-source reconciliation failed"))
r = dts([OTHER], "2026-11-13", "2026-11-13")
check("network-unreconciled 11-13 still excluded for an unaffected store (unchanged)",
      r["status"] == "unavailable")

# MCP fails CLOSED too when migration 65 is not applied (code deployed first).
orig = C._HME_STORE_TRUST_SQL
C._HME_STORE_TRUST_SQL = orig.replace("v_hme_store_day_trust_llm", "v_hme_store_day_trust_missing")
try:
    r = dts([OTHER, MB], "2026-11-09", "2026-11-10")
    check("MCP with the view missing: every store unavailable, no figures, clear reason",
          r["status"] == "unavailable" and all(
              x["status"] == "unavailable" and x["drive_thru_cars"] is None
              and x["trust_status"] == "UNAVAILABLE"
              and x["reason"].startswith(C.HME_STORE_TRUST_UNKNOWN) for x in r["stores"]),
          str([x.get("reason") for x in r["stores"]]))
finally:
    C._HME_STORE_TRUST_SQL = orig

# ── D2. MCP field contract (PR #37 review) ─────────────────────────────────────
# days_excluded_unreconciled keeps its 73ef62a meaning: NETWORK-unreconciled days
# only. Store-level exclusions are reported ONLY in days_excluded_store_unverified.
# Both are excluded from every figure.
print("D2. MCP exclusion-field contract")


def data_days(i, s, e):
    return {str(r[0]) for r in ro_query(
        "SELECT business_date FROM v_hme_store_daily_llm WHERE establishment_id=%s "
        "AND business_date BETWEEN %s AND %s", (i, s, e))}


def verified_cars(i, s, e, network_bad):
    return int(ro_query("""SELECT coalesce(sum(d.drive_thru_cars), 0) FROM v_hme_store_daily_llm d
                    JOIN v_hme_store_day_trust_llm t USING (establishment_id, business_date)
                   WHERE d.establishment_id=%s AND d.business_date BETWEEN %s AND %s
                     AND t.trust_status='VERIFIED' AND NOT (d.business_date::text = ANY(%s))""",
                        (i, s, e, list(network_bad)))[0][0])


def consistent(r, i, s, e):
    """Coverage arithmetic and trust_status agree with the two lists."""
    x = store(r, i)
    net = set(x["days_excluded_unreconciled"])
    sto = {u["date"] for u in x["days_excluded_store_unverified"]}
    have = data_days(i, s, e)
    excluded = (net | sto) & have
    flags_ok = all(u["also_network_unreconciled"] == (u["date"] in net)
                   for u in x["days_excluded_store_unverified"])
    want_trust = ("VERIFIED" if x["status"] == "ok" else
                  "VERIFIED_SUBSET" if x["days_included"] else "UNAVAILABLE")
    return (x["days_included"] == len(have - excluded)
            and x["coverage"]["figures_limited_to_reconciled_days"] == bool(net | sto)
            and x["trust_status"] == want_trust and flags_ok
            and (x["drive_thru_cars"] or 0) == verified_cars(i, s, e, net))


# network-only exclusion
for i, s_, e_ in ((MB, "2026-11-09", "2026-11-15"), (OTHER, "2026-11-10", "2026-11-15"),
                  (MB, "2026-10-15", "2026-10-16")):
    r = dts([i], s_, e_)
    x = store(r, i)
    net_want = ["2026-10-15"] if s_ == "2026-10-15" else ["2026-11-13"]
    check(f"network-only {i} {s_}..{e_}: listed in days_excluded_unreconciled only",
          r["status"] == "partial" and x["days_excluded_unreconciled"] == net_want
          and x["days_excluded_store_unverified"] == []
          and x["trust_status"] == "VERIFIED_SUBSET", str(x["days_excluded_unreconciled"]))
    check(f"network-only {i} {s_}..{e_}: counts, trust_status, figures consistent",
          consistent(r, i, s_, e_))

# store-only exclusion
for s_, e_ in (("2026-11-03", "2026-11-03"), ("2026-11-01", "2026-11-08")):
    r = dts([MB], s_, e_)
    x = store(r, MB)
    check(f"store-only MB {s_}..{e_}: network list empty, store list names the days",
          x["days_excluded_unreconciled"] == []
          and [u["date"] for u in x["days_excluded_store_unverified"]]
          == [d for d in ("2026-11-03", "2026-11-06") if s_ <= d <= e_]
          and not any(u["also_network_unreconciled"]
                      for u in x["days_excluded_store_unverified"]))
    check(f"store-only MB {s_}..{e_}: counts, trust_status, figures consistent",
          consistent(r, MB, s_, e_))

# overlapping: MB 10-14 is store PARTIAL AND network-unreconciled
r = dts([MB], "2026-10-14", "2026-10-14")
x = store(r, MB)
check("overlap MB 10-14: network list unchanged, store list flags the overlap",
      x["status"] == "unavailable" and x["days_excluded_unreconciled"] == ["2026-10-14"]
      and x["days_excluded_store_unverified"] == [
          {"date": "2026-10-14", "store_result_status": "PARTIAL",
           "store_result_reason": "RECONCILIATION_MISMATCH", "grade_source": "STORE_RESULT",
           "also_network_unreconciled": True}]
      and x["trust_status"] == "UNAVAILABLE" and x["drive_thru_cars"] is None, str(x))

# mixed range: network-only (10-15), overlap (10-14), store-only (11-03, 11-06)
r = dts([MB], "2026-10-13", "2026-11-08")
x = store(r, MB)
check("mixed MB 10-13..11-08: network list = 10-14, 10-15 (73ef62a meaning)",
      x["days_excluded_unreconciled"] == ["2026-10-14", "2026-10-15"],
      str(x["days_excluded_unreconciled"]))
check("mixed MB 10-13..11-08: store list = 10-14 (overlap), 11-03, 11-06",
      [(u["date"], u["also_network_unreconciled"]) for u in x["days_excluded_store_unverified"]]
      == [("2026-10-14", True), ("2026-11-03", False), ("2026-11-06", False)])
check("mixed MB 10-13..11-08: all four days excluded; counts/figures consistent",
      r["status"] == "partial" and x["trust_status"] == "VERIFIED_SUBSET"
      and consistent(r, MB, "2026-10-13", "2026-11-08"))

# fully verified
for i in (MB, OTHER, S15):
    r = dts([i], "2026-11-09", "2026-11-12")
    x = store(r, i)
    check(f"fully verified {i} 11-09..11-12: ok, VERIFIED, both lists empty, not limited",
          r["status"] == "ok" and x["trust_status"] == "VERIFIED"
          and x["days_excluded_unreconciled"] == [] and x["days_excluded_store_unverified"] == []
          and x["coverage"]["figures_limited_to_reconciled_days"] is False
          and consistent(r, i, "2026-11-09", "2026-11-12"))

# multi-store: one store's store-level failure cannot leak into another store's lists
r = dts([OTHER, MB], "2026-11-01", "2026-11-08")
check("multi-store 11-01..11-08: overall partial; exclusions stay on Mission Bend",
      r["status"] == "partial" and store(r, OTHER)["status"] == "ok"
      and store(r, OTHER)["days_excluded_store_unverified"] == []
      and len(store(r, MB)["days_excluded_store_unverified"]) == 2
      and consistent(r, OTHER, "2026-11-01", "2026-11-08")
      and consistent(r, MB, "2026-11-01", "2026-11-08"))

failed = [x for x in results if not x[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
