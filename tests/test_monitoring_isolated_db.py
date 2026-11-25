#!/usr/bin/env python3
"""Data monitor against a REAL but ISOLATED PostgreSQL (fixture schema + data).

Run (never against production; refuses unless DB_NAME ends in _test on a
non-production port -- tests/isolated_db.require_isolated):

  DB_HOST=<socket dir or host> DB_PORT=55433 DB_NAME=monitor_test \\
  DB_USER=<owner> DB_RO_USER=<ro role> MONITOR_DB_USER=<monitor role> \\
  venv/bin/python tests/test_monitoring_isolated_db.py

What it proves that the pure suite cannot:
  * every SQL statement the checks issue parses and runs on PostgreSQL;
  * the Revel verdicts come from the REAL chat_sql.revel_store_breakdown on this
    data, including the exact ±0.5% boundary (0.50% passes, 0.51% fails);
  * the monitor's session is read-only on the server (a write is rejected);
  * an end-to-end data-tier run opens the right incidents, dedups on re-run, and
    resolves when the fixture data is repaired.

The fixture tables carry the names of the production views the checks read,
with only the columns those queries use. It creates its own schema in the
throw-away database and drops nothing outside it.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import isolated_db  # noqa: E402

isolated_db.require_isolated()
for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
    os.environ.pop(k, None)

import psycopg2  # noqa: E402

from monitoring import config as mcfg  # noqa: E402
from monitoring import engine, notify  # noqa: E402
from monitoring.checks import registry  # noqa: E402
from monitoring.config import CHICAGO  # noqa: E402
from monitoring.model import CRITICAL, FAIL, HIGH, OK, WARNING, dedup_key  # noqa: E402
from monitoring.pg import connect as mon_connect  # noqa: E402
from monitoring.store import Store  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {str(detail)[:300]}" if not ok and detail else ""))


NOW = datetime.now(timezone.utc)
TODAY = NOW.astimezone(CHICAGO).date()
D = TODAY - timedelta(days=2)            # always past every readiness window
D1 = TODAY - timedelta(days=1)


def ts(day: date, hh: int, mm: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=CHICAGO)


owner = psycopg2.connect(host=os.environ["DB_HOST"], port=os.environ["DB_PORT"],
                         dbname=os.environ["DB_NAME"], user=os.environ["DB_USER"],
                         password=os.environ.get("DB_PASS"))
owner.autocommit = True
cur = owner.cursor()

DDL = """
DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;
CREATE TABLE establishments (id int PRIMARY KEY, name text, active bool DEFAULT true);
CREATE TABLE sync_state (establishment_id int, resource text, last_success_at timestamptz,
                         window_end timestamptz, status text);
CREATE TABLE orders_v2 (id bigint PRIMARY KEY, establishment_id int, created_date timestamptz);
CREATE TABLE v_orders_classified (id bigint, establishment_id int, txn_class text,
    final_total numeric, item_count int, business_date date, closed bool);
CREATE TABLE v_payments_daily_v2 (establishment_id int, business_date date, payment_amount numeric);
CREATE TABLE v_order_payment_summary (order_id bigint, establishment_id int,
                                      payment_amount_total numeric);
CREATE TABLE v_labor_daily_context (establishment_id int, business_date date, labor_hours numeric,
                                    open_shift_count int, missing_wage_shift_count int);
CREATE TABLE hme_ingest_run (run_id bigint, business_date date, attempt int, status text,
    reconciliation_ok bool, reconciliation_detail jsonb, store_count int, verified_store_count int,
    started_at timestamptz, finished_at timestamptz, loader_version text, store_scope text[]);
CREATE TABLE hme_store_mapping (hme_store_number text, revel_establishment_id bigint,
                                mapping_status text, active bool);
CREATE TABLE v_hme_store_day_trust_llm (establishment_id int, business_date date, trust_status text,
                                        store_result_status text, store_result_reason text);
CREATE TABLE hme_store_ingest_result (run_id bigint, business_date date, revel_establishment_id bigint,
    hme_store_number text, status text, reason_code text, detail jsonb);
CREATE TABLE v_r365_completeness_llm (week_start date, week_end date, week_status text,
    source_complete bool, reconciliation_ok bool, expected_store_count int, observed_store_count int,
    stores_with_full_week int, missing_store_days int, closed_store_days int,
    pre_opening_store_days int, latest_refresh_failed bool, loaded_at timestamptz);
CREATE TABLE r365_store_mapping (r365_store_id text, establishment_id int, status text, active bool,
                                 opening_date date);
CREATE TABLE r365_store_daily (r365_store_id text, week_start date, business_date date);
CREATE TABLE r365_ingest_run (id bigint, status text, error_code text, completed_at timestamptz,
                              requested_week_start date);
CREATE TABLE ayg_business_periods (fiscal_year int, period_number int, period_label text,
                                   start_date date, end_date date);
CREATE TABLE r365_financial_ingest_run (id bigint, requested_fiscal_year int,
    requested_period_number int, target_kind text, status text, completed_at timestamptz,
    reconciliation_ok bool, source_sync_stale bool, source_last_sync_at timestamptz, error_code text);
CREATE TABLE chat_jobs (id bigint, status text, error_code text, created_at timestamptz,
                        started_at timestamptz, completed_at timestamptz, question text);
CREATE TABLE chat_query_log (id bigint, asked_at timestamptz, error text, steps jsonb, question text);
"""
cur.execute(DDL)
ro, mon = os.environ["DB_RO_USER"], os.environ["MONITOR_DB_USER"]
cur.execute(f"GRANT USAGE ON SCHEMA public TO {ro}")
cur.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {ro}")
# The monitor role gets EXACTLY the proposed grants and role settings, parsed from
# deploy/monitoring/proposed_monitor_role.sql -- so a relation a check (or
# chat_sql.revel_store_breakdown) reads without a grant fails this test.
ROLE_SQL = open(os.path.join(ROOT, "deploy", "monitoring", "proposed_monitor_role.sql")).read()
role_stmts = []
for stmt in "\n".join(l.split("--", 1)[0] for l in ROLE_SQL.splitlines()).split(";"):
    stmt = " ".join(stmt.split())
    if stmt.startswith(("GRANT SELECT", "GRANT USAGE", "ALTER ROLE")):
        role_stmts.append(stmt.replace("laynes_monitor", mon))
for stmt in role_stmts:
    cur.execute(stmt)
cur.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC")
cur.execute("SELECT table_name FROM information_schema.role_table_grants WHERE grantee = %s "
            "UNION SELECT table_name FROM information_schema.column_privileges WHERE grantee = %s",
            (mon, mon))
GRANTED = {r[0] for r in cur.fetchall()}
STORES = [(6, "LCF Katy"), (7, "LCF Ella"), (14, "LCF Beaumont"), (15, "LCF Shepherd"),
          (20, "LCF Pasadena"), (25, "LCF Mission Bend"), (26, "LCF Nederland"),
          (32, "LCF Airtex"), (36, "LCF Missouri City"), (40, "LCF Rosenberg"),
          (48, "LCF Downtown Houston"), (54, "LCF Cypress")]
cur.executemany("INSERT INTO establishments (id, name) VALUES (%s, %s)", STORES)
recent = NOW - timedelta(hours=2)
for est, _ in STORES:
    for res in ("orders_v2", "payments_v2"):
        cur.execute("INSERT INTO sync_state VALUES (%s,%s,%s,%s,'success')", (est, res, recent, recent))
cur.execute("UPDATE sync_state SET last_success_at = %s WHERE establishment_id = 26 "
            "AND resource = 'orders_v2'", (NOW - timedelta(hours=50),))

# Revel fixture for D. order totals / payments by business date / linked payments.
oid = [1000]


def order(est, total, paid_linked, *, closed=True, day=D):
    oid[0] += 1
    cur.execute("INSERT INTO orders_v2 VALUES (%s,%s,%s)", (oid[0], est, recent))
    cur.execute("INSERT INTO v_orders_classified VALUES (%s,%s,'REAL',%s,1,%s,%s)",
                (oid[0], est, total, day, closed))
    if paid_linked is not None:
        cur.execute("INSERT INTO v_order_payment_summary VALUES (%s,%s,%s)", (oid[0], est, paid_linked))


def paid(est, amount, day=D):
    cur.execute("INSERT INTO v_payments_daily_v2 VALUES (%s,%s,%s)", (est, day, amount))


order(6, 1000, 1000); paid(6, 1000)                           # exact
order(7, 1005, 1005); paid(7, 1000)                           # +0.50% exactly: passes
order(14, 1005.10, 1000); paid(14, 1000)                      # +0.51%: fails, unexplained
order(15, 994, 994); order(15, 6, None, closed=False); paid(15, 994)   # open unpaid ticket
order(36, 900, 900); order(36, 100, 100); paid(36, 900)       # 100 prepaid on an earlier day
order(40, 1000, 950); paid(40, 950)                           # short-paid, unexplained
for est in (20, 25, 26, 32, 48):
    order(est, 500, 500); paid(est, 500)
# 54 Cypress: no orders and no payments on D -> missing.

# HME: 11 VERIFIED mapped stores (Downtown Houston 48 unmapped).
HME = [("108", 6), ("109", 7), ("00111", 14), ("115", 15), ("16", 20), ("1158614", 25),
       ("15", 26), ("26", 32), ("1163022", 36), ("1", 40), ("1171670", 54)]
cur.executemany("INSERT INTO hme_store_mapping VALUES (%s,%s,'VERIFIED',true)", HME)
cur.execute("INSERT INTO hme_store_mapping VALUES ('999', NULL, 'UNMAPPED', true)")


def checks(failed=()):
    names = ["pa_total_orders_vs_outliers_car_departures", "store_rows_sum_vs_source_total",
             "trend_total_cars_matches_pa_total_orders_per_store", "goal_d_matches_pa_threshold"]
    return json.dumps({"checks": [{"check": n, "ok": n not in failed} for n in names]})


def hme_day(day, runs, unverified=(), missing=()):
    for rid, ok_, failed, hh in runs:
        cur.execute("INSERT INTO hme_ingest_run VALUES (%s,%s,1,'partial',%s,%s,11,11,%s,%s,"
                    "'hme_load/1.7.0',NULL)", (rid, day, ok_, checks(failed), ts(day + timedelta(days=1), hh),
                                               ts(day + timedelta(days=1), hh, 20)))
    last = runs[-1][0]
    for num, est in HME:
        if est in missing:
            cur.execute("INSERT INTO hme_store_ingest_result VALUES (%s,%s,%s,%s,'FAILED','NO_ACCESS','{}')",
                        (last, day, est, num))
            continue
        bad = est in unverified
        cur.execute("INSERT INTO v_hme_store_day_trust_llm VALUES (%s,%s,%s,%s,%s)",
                    (est, day, "UNVERIFIED" if bad else "VERIFIED", "PARTIAL" if bad else "SUCCESS",
                     "RECONCILIATION_MISMATCH" if bad else "OK"))
        cur.execute("INSERT INTO hme_store_ingest_result VALUES (%s,%s,%s,%s,%s,%s,'{}')",
                    (last, day, est, num, "PARTIAL" if bad else "SUCCESS",
                     "RECONCILIATION_MISMATCH" if bad else "OK"))


# D: morning Trend mismatch, fixed by the retry. D1: goal mismatch persists after the retry
# for two stores, and Rosenberg not visible.
hme_day(D, [(1, False, ("trend_total_cars_matches_pa_total_orders_per_store",), 5),
            (2, True, (), 10)])
hme_day(D1, [(3, False, ("goal_d_matches_pa_threshold",), 5), (4, False, ("goal_d_matches_pa_threshold",), 10)],
        unverified=(15, 36), missing=(40,))
for d in range(3, 8):   # older lookback days reconciled
    day = TODAY - timedelta(days=d)
    hme_day(day, [(100 + d, True, (), 5)])

# R365: expected weeks per monitoring.checks.r365.expected_weeks
from monitoring.checks.r365 import expected_weeks  # noqa: E402
wk = expected_weeks(NOW, "21:30", 3)
cur.executemany("INSERT INTO r365_store_mapping VALUES (%s,%s,'VERIFIED',true,NULL)",
                [(f"r-{est}", est) for est, _ in STORES])
for i, ws in enumerate(wk):
    full = 12 if i else 11
    cur.execute("INSERT INTO v_r365_completeness_llm VALUES (%s,%s,'COMPLETE',true,true,12,12,%s,0,0,0,false,%s)",
                (ws, ws + timedelta(days=6), full, NOW - timedelta(days=2)))
    for est, _ in STORES:
        n = 5 if (i == 0 and est == 54) else 7
        for k in range(n):
            cur.execute("INSERT INTO r365_store_daily VALUES (%s,%s,%s)", (f"r-{est}", ws, ws + timedelta(days=k)))
cur.execute("INSERT INTO r365_ingest_run VALUES (1,'COMPLETE',NULL,%s,%s)", (NOW - timedelta(days=2), wk[0]))
ps = TODAY - timedelta(days=20)
cur.execute("INSERT INTO ayg_business_periods VALUES (2026,12,'P12',%s,%s),(2026,11,'P11',%s,%s)",
            (ps, ps + timedelta(days=27), ps - timedelta(days=28), ps - timedelta(days=1)))
cur.execute("INSERT INTO r365_financial_ingest_run VALUES (1,2026,11,'PERIOD','PARTIAL',%s,true,false,NULL,NULL)",
            (datetime.combine(ps - timedelta(days=10), datetime.min.time(), tzinfo=timezone.utc),))

cur.execute("INSERT INTO chat_jobs VALUES (1,'completed',NULL,now(),now(),now(),'SECRET QUESTION TEXT')")
cur.execute("INSERT INTO chat_query_log VALUES (1, now(), NULL, '[{\"error\": \"did not pass the hme trust gate\"}]',"
            " 'SECRET QUESTION TEXT')")

# ── 1. read-only session ────────────────────────────────────────────────────
print("1. monitor PostgreSQL session")
cfg = mcfg._merge(mcfg.DEFAULTS, {
    "monitor": {"release_dir": ROOT, "state_db": ":memory:"},
    "postgres": {"host_env": "DB_HOST", "port_env": "DB_PORT", "dbname_env": "DB_NAME"},
    "notify": {"channels": ["log"], "dry_run": True},
    "revel": {"lookback_days": 2}, "hme": {"lookback_days": 7}, "r365": {"lookback_weeks": 3},
})
c = mon_connect(cfg)
try:
    with c.cursor() as k:
        k.execute("INSERT INTO establishments VALUES (999, 'x')")
    wrote = True
except psycopg2.Error as e:
    wrote = False
    err = type(e).__name__
c.rollback()
check("monitor session rejects writes on the server", not wrote and "ReadOnly" in err, err if not wrote else "")
with c.cursor() as k:
    k.execute("SHOW statement_timeout")
    stmo = k.fetchone()[0]
c.close()
check("statement_timeout bounded on the monitor session", stmo == "30s", stmo)
c = mon_connect(cfg)
try:
    with c.cursor() as k:
        k.execute("SELECT question FROM chat_query_log")
    leaked = True
except psycopg2.Error:
    leaked = False
c.close()
check("with the proposed grants the monitor cannot read chat question text", not leaked)

check("proposed role statements applied verbatim (GRANT + ALTER ROLE)",
      len(role_stmts) >= 5 and any(x.startswith("ALTER ROLE") for x in role_stmts), role_stmts)
check("monitor role holds only the proposed table grants (no question/answer columns)",
      "chat_jobs" in GRANTED and "orders_v2" in GRANTED and "v_payments_daily_v2" in GRANTED, GRANTED)

# ── 2. Revel through the REAL chat_sql ──────────────────────────────────────
print("2. Revel reconciliation via chat_sql.revel_store_breakdown")
from monitoring.checks import revel  # noqa: E402
# The analytics-role credentials are NOT needed: chat_sql runs on monitor sessions.
saved_ro = {k: os.environ.pop(k) for k in ("DB_RO_USER", "DB_RO_PASS") if k in os.environ}
ctx = engine.Context(cfg, Store(":memory:"), NOW, "data")
sessions = []
_orig_mpc = ctx.monitor_pg_connect


def _recording_connect():
    conn = _orig_mpc()
    with conn.cursor() as k_:
        k_.execute("SELECT current_user, current_setting('transaction_read_only'), "
                   "current_setting('lock_timeout'), current_setting('application_name'), "
                   "current_setting('idle_in_transaction_session_timeout')")
        sessions.append(k_.fetchone())
    conn.rollback()
    return conn


ctx.monitor_pg_connect = _recording_connect
rv = {(r.check, r.store_id, r.business_date): r for r in revel.reconciliation(ctx)}
ctx.close()
check("chat_sql.revel_store_breakdown ran on monitor sessions only (role, read-only, bounded)",
      sessions and all(x == (mon, "on", "2s", "laynes_monitor", "1min") for x in sessions), sessions)
with owner.cursor() as k_:
    k_.execute("SELECT count(*) FROM pg_stat_activity WHERE usename = %s", (mon,))
    left = k_.fetchone()[0]
check("every reconciliation connection was released", left == 0, left)
os.environ.update(saved_ro)
g = lambda est: rv[("sales_reconciliation", est, D.isoformat())]  # noqa: E731
check("reconciled store -> OK", g(6).state == OK)
check("delta exactly +0.50% -> OK (tolerance is inclusive, unchanged)", g(7).state == OK, g(7).difference)
check("delta +0.51% unexplained -> HIGH", g(14).state == FAIL and g(14).severity == HIGH, g(14).difference)
check("open unpaid ticket explains the gap -> WARNING open_unpaid_tickets",
      g(15).severity == WARNING and g(15).category == "open_unpaid_tickets", (g(15).category, g(15).actual))
check("payment dated another day -> WARNING payment_timing",
      g(36).severity == WARNING and g(36).category == "payment_timing", (g(36).category, g(36).actual))
check("short-paid with no timing explanation -> HIGH reconciliation_mismatch",
      g(40).severity == HIGH and g(40).category == "reconciliation_mismatch", g(40).category)
check("store with no orders/payments -> HIGH no_data", g(54).severity == HIGH and g(54).category == "no_data")
check("difference shown as AI Chat shows it", g(14).difference == "+0.51%", g(14).difference)

# ── 3. end-to-end data tier ─────────────────────────────────────────────────
print("3. end-to-end data tier (real SQL, fake systemd/HTTP, dry-run log channel)")
fake_sys = lambda u: {"ActiveState": "active", "Result": "success", "LoadState": "loaded"}  # noqa: E731
ctxkw = dict(systemctl=fake_sys, http=lambda u, t: (200, None),
             newest_backup=lambda g: ("/b.dump", NOW - timedelta(hours=1)),
             cert_expiry=lambda h: NOW + timedelta(days=60))
st = Store(":memory:")
# monitor.heartbeat is excluded: only the data tier runs here, so after the install
# grace it would (correctly) report the never-run health tier -- covered by
# tests/test_monitoring_remediation.py.
specs = [s for s in registry() if s.name not in ("mcp.tool_errors", "monitor.heartbeat")]
out = engine.run_tier(cfg, "data", store=st, now=NOW, specs=specs, ctx_kwargs=ctxkw)
check("data tier completes with no crashed check", out["ok"] and not out["crashed"], out)
act = {r["dedup_key"]: dict(r) for r in st.active_incidents()}
k = lambda *a: dedup_key(*a)  # noqa: E731
check("Revel: HIGH incidents for 14, 40, 54; WARNING for 15, 36; none for 6, 7",
      all(act[k("revel", "sales_reconciliation", e, D.isoformat())]["severity"] == HIGH for e in (14, 40, 54))
      and all(act[k("revel", "sales_reconciliation", e, D.isoformat())]["severity"] == WARNING for e in (15, 36))
      and not any(k("revel", "sales_reconciliation", e, D.isoformat()) in act for e in (6, 7)))
check("Revel stale sync at Nederland -> HIGH", act[k("revel", "sync_freshness", 26, None)]["severity"] == HIGH)
check("HME day fixed by its retry: no incident",
      k("hme", "day_reconciliation", None, D.isoformat()) not in act)
lr = {r["dedup_key"]: dict(r) for r in st.latest_results("hme")}
check("HME: all 11 mapped stores evaluated for each lookback day",
      sum(1 for key in lr if key.startswith("hme:store_day_trust:") and key.endswith(D.isoformat())) == 11)
day1 = k("hme", "day_reconciliation", None, D1.isoformat())
retry_done = NOW >= datetime.combine(TODAY, datetime.min.time(), tzinfo=CHICAGO).replace(hour=10, minute=45)
morning_ready = NOW >= datetime.combine(TODAY, datetime.min.time(), tzinfo=CHICAGO).replace(hour=6, minute=30)
if not morning_ready:
    check("HME D-1 before 06:30 Chicago: no verdict yet (PENDING, no incident)",
          day1 not in act and lr[day1]["state"] == "PENDING", lr.get(day1))
    check("HME D-1 stores PENDING before 06:30, no incident",
          all(k("hme", "store_day_trust", e, D1.isoformat()) not in act for e in (15, 36, 40)))
elif retry_done:
    check("HME persistent goal mismatch after the retry -> HIGH",
          act[day1]["severity"] == HIGH and act[day1]["category"] == "goal_threshold_mismatch")
    check("HME unverified stores (Shepherd, Missouri City) and missing Rosenberg -> HIGH",
          all(act[k("hme", "store_day_trust", e, D1.isoformat())]["severity"] == HIGH for e in (15, 36, 40)))
else:
    check("HME D-1 before today's retry window: non-settling failure is WARNING only",
          act[day1]["severity"] == WARNING)
    check("HME D-1 store failures WARNING before the retry window",
          all(act[k("hme", "store_day_trust", e, D1.isoformat())]["severity"] == WARNING for e in (15, 36, 40)))
check("HME unmapped location (Downtown Houston) never evaluated",
      not any(":48:" in key for key in lr))
check("R365: newest week 11/12 full -> HIGH; Cypress 5/7 days -> HIGH",
      act[k("r365", "weekly_coverage", None, (wk[0] + timedelta(days=6)).isoformat(),
            f"{wk[0]}..{wk[0] + timedelta(days=6)}")]["severity"] == HIGH
      and act[k("r365", "store_week_coverage", 54, (wk[0] + timedelta(days=6)).isoformat(),
                f"{wk[0]}..{wk[0] + timedelta(days=6)}")]["actual"] == '"5 of 7 days"')
check("R365 financial: closed period loaded before close -> WARNING",
      any(v["source"] == "r365_financial" and v["severity"] == WARNING for v in act.values()))
check("AI Chat trust-gate refusal counted as WARNING",
      act[k("ai_chat", "trust_gate_blocks", None, None)]["severity"] == WARNING)
blob = json.dumps([dict(r) for r in st.db.execute("SELECT * FROM incidents")], default=str)
check("no chat question text copied into the incident store", "SECRET QUESTION" not in blob)
notes = [dict(r) for r in st.notifications(200)]
check("dry-run: every notification marked DRY_RUN", notes and all(n["status"] == "DRY_RUN" for n in notes))
n_notes, n_act = len(notes), len(act)

out = engine.run_tier(cfg, "data", store=st, now=NOW + timedelta(minutes=61), specs=specs, ctx_kwargs=ctxkw)
check("re-run on unchanged data: no new incidents, no new notifications",
      len(st.active_incidents()) == n_act and len(st.notifications(500)) == n_notes)

# repair the fixture (as a real fix upstream would) and confirm resolution
cur.execute("UPDATE v_payments_daily_v2 SET payment_amount = 1005.10 WHERE establishment_id = 14")
cur.execute("UPDATE v_order_payment_summary SET payment_amount_total = 1005.10 WHERE establishment_id = 14")
cur.execute("UPDATE sync_state SET last_success_at = %s WHERE establishment_id = 26", (recent,))
out = engine.run_tier(cfg, "data", store=st, now=NOW + timedelta(minutes=122), specs=specs, ctx_kwargs=ctxkw)
act2 = {r["dedup_key"] for r in st.active_incidents()}
check("upstream fix resolves the incident on the next check",
      k("revel", "sales_reconciliation", 14, D.isoformat()) not in act2
      and k("revel", "sync_freshness", 26, None) not in act2)
res_notes = [dict(r) for r in st.notifications(500) if r["kind"] == "RESOLVED"]
check("resolution notification produced for the alerted incident",
      any("LCF Beaumont" in n["body"] or "LCF Nederland" in n["body"] for n in res_notes))

cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
owner.close()
failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed" + (f"; FAILED: {failed}" if failed else ""))
sys.exit(1 if failed else 0)
