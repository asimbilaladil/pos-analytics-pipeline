#!/usr/bin/env python3
"""Data-quality monitor (Phase 1): detection, incidents, dedup, notifications.

Run:  venv/bin/python tests/test_monitoring.py          (exit 0 = pass)

Pure: no database, no network, no systemd. Every external effect goes through
the injectable Context (fake PostgreSQL rows, fake systemctl / HTTP / disk), the
incident store is an in-memory SQLite database, and notification channels are
fakes -- nothing is ever sent. The SQL itself is exercised against a real but
isolated PostgreSQL by tests/test_monitoring_isolated_db.py.

Covers: reconciled store; store beyond ±0.5% (unexplained / payment timing /
open tickets); stale and missing data; HME morning mismatch resolved by the
retry; persistent mismatch after the retry; all 11 HME locations; R365 week and
store gaps and financial period coverage; failed ingestion jobs; AI Chat and
MCP failures; duplicate suppression; escalation; resolution; acknowledgement;
monitor crash; PostgreSQL down; heartbeat; delivery failure and retry; secret
scrubbing; dry-run never touches the network; admin-only API; thresholds
untouched.
"""
from __future__ import annotations

import ast
import json
import os
import sys
from collections import namedtuple
from datetime import date, datetime, timedelta, timezone

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

from monitoring import config as mcfg  # noqa: E402
from monitoring import engine, notify  # noqa: E402
from monitoring.checks import CheckSpec  # noqa: E402
from monitoring.checks import chat, hme, infra, mcp, r365, revel, selfmon, services  # noqa: E402
from monitoring.config import CHICAGO  # noqa: E402
from monitoring.model import (CRITICAL, FAIL, HIGH, OK, PENDING, WARNING,  # noqa: E402
                              CheckResult, dedup_key)
from monitoring.store import Store  # noqa: E402

results: list[tuple[str, bool]] = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {str(detail)[:300]}" if not ok and detail else ""))


def ct(y, m, d, hh, mm=0):
    """A Chicago wall-clock time as an aware UTC datetime."""
    return datetime(y, m, d, hh, mm, tzinfo=CHICAGO).astimezone(timezone.utc)


# ── fakes ───────────────────────────────────────────────────────────────────
class FakeCursor:
    def __init__(self, pg):
        self.pg, self._rows = pg, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.pg.executed.append(sql)
        for needle, fn in self.pg.routes:
            if needle in sql:
                out = fn(params or {}) if callable(fn) else fn
                self._rows = [dict(r) for r in out]
                return
        raise AssertionError(f"FakePG: no route for SQL: {sql.strip()[:120]}")

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakePG:
    def __init__(self, routes):
        self.routes, self.executed, self.closed = routes, [], False

    def cursor(self, cursor_factory=None):
        return FakeCursor(self)

    def rollback(self):
        pass

    def close(self):
        self.closed = True


class FakeChannel(notify.Channel):
    name = "fake"

    def __init__(self, cfg, fail_with=None):
        super().__init__(cfg)
        self.sent, self.fail_with = [], fail_with

    def send(self, body, subject):
        if self.fail_with:
            raise notify.DeliveryError(self.fail_with)
        self.sent.append((subject, body))


def make_cfg(**over):
    base = {"notify": {"channels": ["fake"], "dry_run": False},
            "monitor": {"state_db": ":memory:"}}
    for k, v in over.items():
        base.setdefault(k, {}).update(v)
    # "fake" is not a production channel: validate() would reject it, so build directly.
    cfg = mcfg._merge(mcfg.DEFAULTS, base)
    return cfg


def run(cfg, store, now, specs, tier="data", channel=None, **ctx):
    ch = channel or FakeChannel(cfg)
    out = engine.run_tier(cfg, tier, store=store, now=now, specs=specs, ctx_kwargs=ctx,
                          channels={"fake": ch, "log": notify.LogChannel(cfg)})
    return out, ch


def spec(fn, name="t.check", tiers=("data",), **kw):
    return CheckSpec(name, tiers, fn, **kw)


def active(store):
    return {r["dedup_key"]: dict(r) for r in store.active_incidents()}


# ═══════════════════════════════════════════════════════════════════════════
print("1. incident engine: open, dedup, update, escalate, resolve, ack")
cfg = make_cfg()
st = Store(":memory:")
T0 = ct(2026, 11, 25, 10, 0)
state = {"r": None}


def scripted(ctx):
    return [state["r"]]


S = [spec(scripted)]
R = lambda **kw: CheckResult(**{"source": "revel", "check": "sales_reconciliation",  # noqa: E731
                                "store_id": 36, "store_name": "LCF Missouri City",
                                "business_date": "2026-11-22", **kw})
state["r"] = R(state=FAIL, severity=HIGH, title="Sales reconciliation mismatch",
               difference="+5.42%", actions=["Review source payments and order reconciliation."])
out, ch = run(cfg, st, T0, S)
inc = active(st)
key = dedup_key("revel", "sales_reconciliation", 36, "2026-11-22")
check("FAIL opens one incident keyed by source/check/store/date", list(inc) == [key], inc)
i = inc[key]
check("incident has id, severity, status, first/last timestamps, evidence, actions",
      i["id"].startswith("INC-") and i["severity"] == HIGH and i["status"] == "OPEN"
      and i["first_detected_at"] and i["last_checked_at"] and json.loads(i["actions"]))
check("HIGH incident notified once", len(ch.sent) == 1, ch.sent)
body = ch.sent[0][1]
check("message matches the requested format",
      all(s in body for s in ("Laynes Intelligence — HIGH", "Store: LCF Missouri City",
                              "Source: Revel POS", "Date: 2026-11-22",
                              "Issue: Sales reconciliation mismatch", "Difference: +5.42%",
                              "Status: OPEN", "Recommended action: Review source payments")), body)

out, ch2 = run(cfg, st, T0 + timedelta(minutes=15), S)
i2 = active(st)[key]
check("unchanged FAIL: no new incident, no new alert (dedup)",
      len(active(st)) == 1 and not ch2.sent and i2["id"] == i["id"] and i2["check_count"] == 2)
check("unchanged FAIL only advances last_checked_at", i2["last_checked_at"] > i["last_checked_at"]
      and i2["first_detected_at"] == i["first_detected_at"])

state["r"] = R(state=FAIL, severity=HIGH, title="Sales reconciliation mismatch", difference="+6.10%")
out, ch3 = run(cfg, st, T0 + timedelta(minutes=30), S)
ev = [e["event"] for e in st.events(i["id"])]
check("changed difference: UPDATED in place, no alert", not ch3.sent and ev[-1] == "UPDATED"
      and active(st)[key]["difference"] == "+6.10%", ev)

state["r"] = R(state=FAIL, severity=CRITICAL, title="Sales reconciliation mismatch", difference="+9%")
out, ch4 = run(cfg, st, T0 + timedelta(minutes=45), S)
check("severity rise: ESCALATED and alerted once", len(ch4.sent) == 1
      and st.events(i["id"])[-1]["event"] == "ESCALATED" and "CRITICAL" in ch4.sent[0][1])

st.begin(); ok = st.acknowledge(i["id"], T0 + timedelta(hours=1), "admin@example.com", "looking"); st.commit()
check("acknowledge OPEN -> ACKNOWLEDGED with who/when/note",
      ok and st.get(i["id"])["status"] == "ACKNOWLEDGED" and st.get(i["id"])["ack_note"] == "looking")
out, ch5 = run(cfg, st, T0 + timedelta(minutes=75), S)
check("acknowledged incident stays acknowledged on re-check, no alert",
      st.get(i["id"])["status"] == "ACKNOWLEDGED" and not ch5.sent)

state["r"] = R(state=OK, title="Reconciled")
out, ch6 = run(cfg, st, T0 + timedelta(minutes=90), S)
check("OK resolves the incident", st.get(i["id"])["status"] == "RESOLVED" and not active(st))
check("resolution notification sent (it had been alerted)",
      len(ch6.sent) == 1 and "RESOLVED" in ch6.sent[0][1] and i["id"] in ch6.sent[0][1])

state["r"] = R(state=FAIL, severity=HIGH, title="Sales reconciliation mismatch", difference="+1%")
out, ch7 = run(cfg, st, T0 + timedelta(hours=2), S)
new = active(st)[key]
check("recurrence after resolution opens a NEW incident (history kept)",
      new["id"] != i["id"] and st.get(i["id"])["status"] == "RESOLVED" and len(ch7.sent) == 1)
state["r"] = R(state=PENDING, title="Awaiting sync")
out, ch8 = run(cfg, st, T0 + timedelta(hours=3), S)
check("PENDING leaves an open incident untouched", active(st)[key]["status"] == "OPEN" and not ch8.sent)

# WARNING: recorded, not pushed; its resolution is not pushed either.
st2 = Store(":memory:")
state["r"] = R(state=FAIL, severity=WARNING, title="payment timing")
out, chw = run(cfg, st2, T0, S)
check("WARNING opens an incident but sends no immediate alert", len(active(st2)) == 1 and not chw.sent)
state["r"] = R(state=OK, title="Reconciled")
out, chw2 = run(cfg, st2, T0 + timedelta(hours=1), S)
check("WARNING resolution: resolved silently (never alerted)", not active(st2) and not chw2.sent)

# Grouping: nine store incidents of one kind -> one message.
st3 = Store(":memory:")


def nine(ctx):
    return [CheckResult(source="hme", check="store_day_trust", state=FAIL, severity=HIGH,
                        store_id=n, store_name=f"Store {n}", business_date="2026-11-23",
                        title="HME store-day did not reconcile") for n in range(9)]


out, chg = run(cfg, st3, T0, [spec(nine)])
check("9 store incidents of the same issue -> 9 incidents, ONE grouped message",
      len(active(st3)) == 9 and len(chg.sent) == 1 and "(9 items)" in chg.sent[0][1])

# ═══════════════════════════════════════════════════════════════════════════
print("2. monitor failure modes: crash, PostgreSQL down, heartbeat, delivery")
st = Store(":memory:")
state["r"] = R(state=FAIL, severity=HIGH, title="Sales reconciliation mismatch")
run(cfg, st, T0, S)
before = active(st)


def boom(ctx):
    raise RuntimeError("synthetic crash")


out, chc = run(cfg, st, T0 + timedelta(minutes=15), [spec(boom, name="t.check")])
after = active(st)
crash_key = dedup_key("monitor", "check_error", None, None, "t.check")
check("crashed check opens a monitor incident", crash_key in after and after[crash_key]["severity"] == HIGH)
check("crashed check does NOT resolve the issues it owns", key in after and
      after[key]["id"] == before[key]["id"])
check("crash makes the run exit non-OK (unit goes red)", out["ok"] is False and out["crashed"] == ["t.check"])
check("crash is alerted", any("Monitor check t.check is failing" in b for _, b in chc.sent))
out, _ = run(cfg, st, T0 + timedelta(minutes=30), S)
check("check recovers: crash incident resolved", crash_key not in active(st) and out["ok"])


def needs_db(ctx):
    ctx.pg()
    return [R(state=OK, title="Reconciled")]


def broken_connect():
    raise OSError("could not connect to server")


out, chp = run(cfg, st, T0 + timedelta(minutes=45),
               [spec(infra.postgres, name="infra.postgres", needs_pg=True),
                spec(needs_db, name="t.check", needs_pg=True)], pg_connect=broken_connect)
a = active(st)
check("PostgreSQL down -> CRITICAL db_unreachable incident",
      any(v["check_name"] == "postgres" and v["severity"] == CRITICAL for v in a.values()), a)
check("PostgreSQL down: SQL checks skipped, their incidents frozen (not resolved)",
      key in a and out["skipped"] == ["t.check"] and crash_key not in a)

# heartbeat (only SUCCESS runs count; detailed cases in test_monitoring_remediation.py)
ACTIVE_TIMER = lambda unit: {"LoadState": "loaded", "ActiveState": "active",  # noqa: E731
                             "UnitFileState": "enabled"}
st = Store(":memory:")
rid = st.start_run("health", T0 - timedelta(hours=5), "x")
st.finish_run(rid, T0 - timedelta(hours=5), "SUCCESS")
rid = st.start_run("data", T0 - timedelta(hours=3), "x")
st.finish_run(rid, T0 - timedelta(hours=3), "SUCCESS", 1, 0, 0)
ctx = engine.Context(make_cfg(), st, T0, "health", systemctl=ACTIVE_TIMER)
hb = {r.qualifier: r for r in selfmon.heartbeat(ctx)}
check("data tier silent for 3h -> heartbeat FAIL HIGH", hb["data"].state == FAIL and hb["data"].severity == HIGH)
check("summary tier within its install grace -> PENDING (not OK, not an alert)",
      hb["summary"].state == PENDING)
rid = st.start_run("data", T0 - timedelta(minutes=10), "x")
st.finish_run(rid, T0 - timedelta(minutes=10), "SUCCESS", 1, 0, 0)
hb = {r.qualifier: r for r in selfmon.heartbeat(ctx)}
check("recent data run -> heartbeat OK", hb["data"].state == OK)

# delivery failure + retry + scrubbing (backoff details in test_monitoring_remediation.py)
st = Store(":memory:")
bad = FakeChannel(cfg, fail_with="HTTPError: 502 for https://api.telegram.org/botSECRET123/sendMessage")
state["r"] = R(state=FAIL, severity=HIGH, title="Sales reconciliation mismatch")
out, _ = run(cfg, st, T0, S, channel=bad)
n = st.notifications()[0]
check("failed delivery is RETRYING with a next attempt time, and the run is DEGRADED",
      n["status"] == "RETRYING" and n["attempts"] == 1 and n["next_attempt_at"] > n["created_at"]
      and out["delivery"]["retrying"] == 1 and out["status"] == "DEGRADED" and out["ok"] is False)
for k in range(1, 8):
    out, _ = run(cfg, st, T0 + timedelta(hours=k), S, channel=bad)
check("after max_attempts the notification is FAILED and the run is not OK",
      st.db.execute("SELECT status FROM notifications WHERE id = ?", (n["id"],)).fetchone()[0]
      == "FAILED", [dict(r) for r in st.notifications()])
ctx = engine.Context(cfg, st, T0 + timedelta(hours=9), "health")
nd = [r for r in selfmon.notifications(ctx) if r.qualifier == "fake"][0]
check("delivery failure raises a monitor incident (shown on the dashboard, not re-sent)",
      nd.state == FAIL and nd.severity == HIGH and nd.notify is False)
check("token scrubbed from a Telegram transport error",
      "SECRET" not in notify.scrub("https://api.telegram.org/botSECRET/x", "SECRET"))

st = Store(":memory:")
cfg_dry = make_cfg(notify={"dry_run": True})
spy = FakeChannel(cfg_dry)
out, _ = run(cfg_dry, st, T0, S, channel=spy)
check("dry_run: channel.send is never called; outbox row marked DRY_RUN",
      not spy.sent and st.notifications()[0]["status"] == "DRY_RUN")
check("default config is dry-run with the log channel only",
      mcfg.DEFAULTS["notify"]["dry_run"] is True and mcfg.DEFAULTS["notify"]["channels"] == ["log"])
os.environ.pop("MONITOR_TELEGRAM_BOT_TOKEN", None)
try:
    notify.TelegramChannel(mcfg.DEFAULTS).send("x", "y")
    tg_err = None
except notify.DeliveryError as e:
    tg_err = str(e)
check("telegram without credentials refuses (no request made)", tg_err and "not configured" in tg_err)

# ═══════════════════════════════════════════════════════════════════════════
print("3. Revel: reconciliation classes, readiness, freshness")


class FakeChatSql:
    RECON_TOLERANCE_PCT = 0.5

    @staticmethod
    def _ro_conn():
        raise AssertionError("chat_sql's own connection must not be used by the monitor")

    def __init__(self, stores):
        self.stores, self.calls = stores, []

    def revel_store_breakdown(self, s, e):
        self.calls.append((s, e))
        return {"stores": self.stores.get(s, [])}


def bd(est, name, status, cat=None, delta=None, reasons=()):
    return {"establishment_id": est, "store_name": name, "revel_status": status,
            "failure_category": cat, "reconciliation_delta_pct_display": delta,
            "failure_reasons": list(reasons)}


STORES = [{"id": i, "name": n} for i, n in ((6, "LCF Katy"), (15, "LCF Shepherd"),
                                            (36, "LCF Missouri City"), (40, "LCF Rosenberg"))]
D = "2026-11-22"
fake_cs = FakeChatSql({D: [
    bd(6, "LCF Katy", "PASS", delta="+0.01%"),
    bd(15, "LCF Shepherd", "FAIL", "reconciliation_mismatch", "+0.66%",
       ["sales reconciliation off by +0.66% ($+59.15); tolerance ±0.5%"]),
    bd(36, "LCF Missouri City", "FAIL", "payment_timing", "+5.42%",
       ["sales reconciliation off by +5.42% ($+334.51); tolerance ±0.5%"]),
    bd(40, "LCF Rosenberg", "FAIL", "reconciliation_mismatch", "+2.00%",
       ["sales reconciliation off by +2.00% ($+120.00); tolerance ±0.5%"]),
]})
open_due = {15: (5, 54.18), 40: (0, 0)}
pg = FakePG([
    ("FROM establishments WHERE active", STORES),
    ("c.closed IS NOT TRUE", lambda p: [{"open_tickets": open_due[p["est"]][0],
                                         "open_due": open_due[p["est"]][1]}]),
])
orig = revel._chat_sql
revel._chat_sql = lambda ctx: fake_cs
cfgr = make_cfg(revel={"lookback_days": 1})
try:
    ctx = engine.Context(cfgr, Store(":memory:"), ct(2026, 11, 23, 10, 0), "data", pg_connect=lambda: pg)
    res = {(r.check, r.store_id): r for r in revel.reconciliation(ctx)}
    check("reconciled store -> OK", res[("sales_reconciliation", 6)].state == OK)
    m = res[("sales_reconciliation", 36)]
    check("±0.5% exceeded by payment timing -> WARNING payment_timing (not corruption)",
          m.state == FAIL and m.severity == WARNING and m.category == "payment_timing"
          and m.difference == "+5.42%")
    s15 = res[("sales_reconciliation", 15)]
    check("±0.5% exceeded, open unpaid tickets explain it -> WARNING open_unpaid_tickets",
          s15.state == FAIL and s15.severity == WARNING and s15.category == "open_unpaid_tickets"
          and s15.evidence["open_unpaid_amount"] == 54.18)
    s40 = res[("sales_reconciliation", 40)]
    check("±0.5% exceeded, unexplained -> HIGH reconciliation_mismatch",
          s40.state == FAIL and s40.severity == HIGH and s40.category == "reconciliation_mismatch")
    check("verdict comes from chat_sql.revel_store_breakdown for exactly one day",
          fake_cs.calls == [("2026-11-22", "2026-11-23")])
    check("one bad store does not raise the network CRITICAL",
          res[("network_reconciliation", None)].state == OK)

    # before 09:45 on D+1: pending, and the breakdown is not even computed
    fake_cs.calls.clear()
    ctx = engine.Context(cfgr, Store(":memory:"), ct(2026, 11, 23, 9, 30), "data", pg_connect=lambda: pg)
    res = revel.reconciliation(ctx)
    check("before the daily sync window: every store PENDING, nothing computed",
          all(r.state == PENDING for r in res) and not fake_cs.calls and len(res) == 4)

    # network failure: most stores missing
    fake_cs.stores[D] = [bd(6, "LCF Katy", "FAIL", "no_data", None, ["no orders"])]
    ctx = engine.Context(cfgr, Store(":memory:"), ct(2026, 11, 23, 10, 0), "data", pg_connect=lambda: pg)
    res = {(r.check, r.store_id): r for r in revel.reconciliation(ctx)}
    check("store with no row at all -> HIGH no_data", res[("sales_reconciliation", 40)].severity == HIGH
          and res[("sales_reconciliation", 40)].category == "no_data")
    check("most stores failing -> network CRITICAL (likely sync failure)",
          res[("network_reconciliation", None)].severity == CRITICAL)
    cfgc = make_cfg(revel={"lookback_days": 1, "accepted_closed_dates": {D: "holiday"}})
    ctx = engine.Context(cfgc, Store(":memory:"), ct(2026, 11, 23, 10, 0), "data", pg_connect=lambda: pg)
    res = {(r.check, r.store_id): r for r in revel.reconciliation(ctx)}
    check("accepted closed date: no-data store-days ACCEPTED, no network alarm",
          res[("sales_reconciliation", 40)].state == "ACCEPTED"
          and res[("network_reconciliation", None)].state == OK)
    # aged-out OPEN incidents are re-checked until they resolve on evidence
    D2, D_OLD = "2026-11-26", "2026-11-22"
    st = Store(":memory:")
    fake_cs.stores = {D_OLD: [bd(15, "LCF Shepherd", "FAIL", "reconciliation_mismatch", "+0.66%",
                                 ["sales reconciliation off by +0.66% ($+59.15); tolerance ±0.5%"]),
                              bd(36, "LCF Missouri City", "FAIL", "payment_timing", "+5.42%",
                                 ["sales reconciliation off by +5.42% ($+334.51); tolerance ±0.5%"])]}
    ctx = engine.Context(cfgr, st, ct(2026, 11, 23, 10, 0), "data", pg_connect=lambda: pg)
    engine.apply_results(st, cfgr, revel.reconciliation(ctx), ctx.now)
    opened = {i["store_id"] for i in st.active_incidents("revel", "sales_reconciliation")}
    check("setup: incidents open for the 11-22 failures", {15, 36} <= opened, opened)
    # four days later, lookback 1 = only 11-26; 11-22 Shepherd now passes, MC still fails
    fake_cs.calls.clear()
    fake_cs.stores = {D2: [bd(i["id"], i["name"], "PASS", delta="+0.00%") for i in STORES],
                      D_OLD: [bd(15, "LCF Shepherd", "PASS", delta="+0.19%"),
                              bd(6, "LCF Katy", "PASS", delta="+0.00%"),
                              bd(40, "LCF Rosenberg", "PASS", delta="+0.00%"),
                              bd(36, "LCF Missouri City", "FAIL", "payment_timing", "+5.42%",
                                 ["sales reconciliation off by +5.42% ($+334.51); tolerance ±0.5%"])]}
    ctx = engine.Context(cfgr, st, ct(2026, 11, 27, 10, 0), "data", pg_connect=lambda: pg)
    check("days_to_check = window + the aged-out day of an open incident",
          revel.days_to_check(ctx, cfgr["revel"]) == [date(2026, 11, 26), date(2026, 11, 22)])
    engine.apply_results(st, cfgr, revel.reconciliation(ctx), ctx.now)
    check("the aged-out day is evaluated by the SAME verdict code",
          ("2026-11-22", "2026-11-23") in fake_cs.calls and ("2026-11-26", "2026-11-27") in fake_cs.calls,
          fake_cs.calls)
    still = {i["store_id"] for i in st.active_incidents("revel", "sales_reconciliation")}
    check("now-passing aged-out store-day RESOLVES; still-failing one stays OPEN",
          still == {36}, still)
    # nothing open -> nothing extra; too old -> not re-checked
    ctx = engine.Context(make_cfg(revel={"lookback_days": 1, "recheck_open_max_days": 3}), st,
                         ct(2026, 11, 27, 10, 0), "data", pg_connect=lambda: pg)
    check("beyond recheck_open_max_days: not re-checked (bounded work)",
          revel.days_to_check(ctx, ctx.cfg["revel"]) == [date(2026, 11, 26)])
    ctx = engine.Context(cfgr, Store(":memory:"), ct(2026, 11, 27, 10, 0), "data", pg_connect=lambda: pg)
    check("no open incidents -> exactly the lookback window",
          revel.days_to_check(ctx, cfgr["revel"]) == [date(2026, 11, 26)])
    check("default re-check horizon is 35 days", mcfg.DEFAULTS["revel"]["recheck_open_max_days"] == 35)
finally:
    revel._chat_sql = orig

now = ct(2026, 11, 25, 10, 0)
fresh = now - timedelta(hours=1)
stale = now - timedelta(hours=40)
pg = FakePG([("FROM establishments e\nLEFT JOIN sync_state", [
    {"establishment_id": 6, "store_name": "LCF Katy", "resource": "orders_v2", "last_success_at": fresh,
     "window_end": fresh, "status": "success"},
    {"establishment_id": 6, "store_name": "LCF Katy", "resource": "payments_v2", "last_success_at": fresh,
     "window_end": fresh, "status": "success"},
    {"establishment_id": 15, "store_name": "LCF Shepherd", "resource": "orders_v2", "last_success_at": stale,
     "window_end": stale, "status": "success"},
    {"establishment_id": 15, "store_name": "LCF Shepherd", "resource": "payments_v2",
     "last_success_at": fresh, "window_end": fresh, "status": "failed"},
])])
ctx = engine.Context(make_cfg(), Store(":memory:"), now, "data", pg_connect=lambda: pg)
res = {r.store_id: r for r in revel.freshness(ctx)}
check("fresh store -> OK", res[6].state == OK)
check("stale / failed sync -> HIGH stale_data with both reasons", res[15].state == FAIL and
      res[15].severity == HIGH and "40h" in res[15].actual and "failed" in res[15].actual)
check("latest successful Revel sync recorded for the dashboard", ctx.source_updates["revel"][0] == fresh.isoformat())

# ═══════════════════════════════════════════════════════════════════════════
print("4. HME: morning settling, retry resolution, persistent mismatch, 11 stores")
MAPPED = [{"hme_store_number": n, "establishment_id": e, "store_name": s} for n, e, s in (
    ("108", 6, "LCF Katy"), ("109", 7, "LCF Ella"), ("00111", 14, "LCF Beaumont"),
    ("115", 15, "LCF Shepherd"), ("16", 20, "LCF Pasadena"), ("1158614", 25, "LCF Mission Bend"),
    ("26", 32, "LCF Airtex"), ("1163022", 36, "LCF Missouri City"), ("1", 40, "LCF Rosenberg"),
    ("1171670", 54, "LCF Cypress"), ("15", 26, "LCF Nederland"))]
BD = date(2026, 11, 23)


def chk(name, ok_, **kw):
    return {"check": name, "ok": ok_, **kw}


def run_row(rid, ok_, failed=(), finished=None, status="partial"):
    checks = [chk(c, c not in failed) for c in (
        "pa_total_orders_vs_outliers_car_departures", "store_rows_sum_vs_source_total",
        "trend_total_cars_matches_pa_total_orders_per_store", "goal_d_matches_pa_threshold")]
    return {"run_id": rid, "business_date": BD, "attempt": 1, "status": status,
            "reconciliation_ok": ok_, "reconciliation_detail": {"checks": checks},
            "store_count": 11, "verified_store_count": 11, "started_at": finished,
            "finished_at": finished, "loader_version": "hme_load/1.7.0"}


def hme_pg(runs, trust, store_res):
    return FakePG([("FROM hme_ingest_run", runs), ("FROM hme_store_mapping m", MAPPED),
                   ("FROM v_hme_store_day_trust_llm", trust),
                   ("FROM hme_store_ingest_result r", store_res)])


def trust_rows(bad=()):
    return [{"establishment_id": m["establishment_id"], "business_date": BD,
             "trust_status": "UNVERIFIED" if m["establishment_id"] in bad else "VERIFIED",
             "store_result_status": "PARTIAL" if m["establishment_id"] in bad else "SUCCESS",
             "store_result_reason": "RECONCILIATION_MISMATCH" if m["establishment_id"] in bad else "OK"}
            for m in MAPPED]


def sres(bad, reason="RECONCILIATION_MISMATCH"):
    return [{"business_date": BD, "establishment_id": e, "hme_store_number": "x", "status": "PARTIAL",
             "reason_code": reason, "detail": {}, "run_id": 1} for e in bad]


cfgh = make_cfg(hme={"lookback_days": 1})
st = Store(":memory:")
hspec = [spec(hme.days, name="hme.days", needs_pg=True)]
morning = run_row(597, False, ("trend_total_cars_matches_pa_total_orders_per_store",),
                  ct(2026, 11, 24, 5, 37))
pg = hme_pg([morning], trust_rows({15}), sres({15}))
out, chm = run(cfgh, st, ct(2026, 11, 24, 8, 0), hspec, pg_connect=lambda: pg)
lr = {r["dedup_key"]: dict(r) for r in st.latest_results("hme")}
dkey = dedup_key("hme", "day_reconciliation", None, "2026-11-23")
check("morning Trend lag before the retry: day PENDING (settling), no incident, no alert",
      lr[dkey]["state"] == PENDING and not active(st) and not chm.sent)
check("all 11 mapped HME locations evaluated", len([k for k in lr if "store_day_trust" in k]) == 11)
check("lagging store PENDING, the other 10 OK",
      lr[dedup_key("hme", "store_day_trust", 15, "2026-11-23")]["state"] == PENDING
      and sum(1 for k, v in lr.items() if "store_day_trust" in k and v["state"] == OK) == 10)

retry = run_row(598, True, (), ct(2026, 11, 24, 10, 23))
pg = hme_pg([morning, retry], trust_rows(), [])
out, chr_ = run(cfgh, st, ct(2026, 11, 24, 11, 0), hspec, pg_connect=lambda: pg)
lr = {r["dedup_key"]: dict(r) for r in st.latest_results("hme")}
check("retry reconciles: day OK, all 11 stores OK, never an incident or alert",
      lr[dkey]["state"] == OK and all(v["state"] == OK for k, v in lr.items() if "store_day_trust" in k)
      and not active(st) and not chr_.sent)

st = Store(":memory:")
bad = {6, 7, 14, 15, 20, 25, 32, 36, 40}
retry_bad = run_row(598, False, ("goal_d_matches_pa_threshold",), ct(2026, 11, 24, 10, 23))
pg = hme_pg([morning, retry_bad], trust_rows(bad), sres(bad))
out, chb = run(cfgh, st, ct(2026, 11, 24, 11, 0), hspec, pg_connect=lambda: pg)
a = active(st)
check("persistent mismatch after the retry -> HIGH day incident (goal threshold)",
      a[dkey]["severity"] == HIGH and a[dkey]["category"] == "goal_threshold_mismatch")
check("each unverified store gets its own HIGH incident (9), verified stores none",
      sum(1 for k in a if "store_day_trust" in k) == 9)
check("alerts: one day message + one grouped store message", len(chb.sent) == 2
      and any("(9 items)" in b for _, b in chb.sent), [s for s, _ in chb.sent])
out, chb2 = run(cfgh, st, ct(2026, 11, 24, 11, 15), hspec, pg_connect=lambda: pg)
check("persistent mismatch re-checked: no repeat alert", not chb2.sent and len(active(st)) == 10)

st = Store(":memory:")
pg = hme_pg([run_row(597, False, ("goal_d_matches_pa_threshold",), ct(2026, 11, 24, 5, 37))],
            trust_rows({15}), sres({15}))
run(cfgh, st, ct(2026, 11, 24, 8, 0), hspec, pg_connect=lambda: pg)
check("non-settling failure in the morning: WARNING (visible, not pushed) until the retry",
      active(st)[dkey]["severity"] == WARNING)
out, che = run(cfgh, st, ct(2026, 11, 24, 11, 0), hspec, pg_connect=lambda: pg)
check("... then ESCALATED to HIGH after the retry window, alerted",
      active(st)[dkey]["severity"] == HIGH and any("escalated" in b for _, b in che.sent))

st = Store(":memory:")
pg = hme_pg([], [], [])
run(cfgh, st, ct(2026, 11, 24, 8, 0), hspec, pg_connect=lambda: pg)
check("no HME run after the morning window -> HIGH missing_ingestion",
      active(st)[dkey]["severity"] == HIGH and active(st)[dkey]["category"] == "missing_ingestion")
run(cfgh, st, ct(2026, 11, 24, 11, 0), hspec, pg_connect=lambda: pg)
check("still no run after the retry window -> escalated to CRITICAL", active(st)[dkey]["severity"] == CRITICAL)
pg = hme_pg([run_row(597, False, (), ct(2026, 11, 24, 5, 40), status="failed")], [], [])
st = Store(":memory:")
run(cfgh, st, ct(2026, 11, 24, 8, 0), hspec, pg_connect=lambda: pg)
check("failed HME run -> ingestion_failed", active(st)[dkey]["category"] == "ingestion_failed")

st = Store(":memory:")
pg = hme_pg([run_row(591, True, (), ct(2026, 11, 24, 10, 22))],
            [r for r in trust_rows() if r["establishment_id"] != 40], sres({40}, "NO_ACCESS"))
run(cfgh, st, ct(2026, 11, 24, 11, 0), hspec, pg_connect=lambda: pg)
k40 = dedup_key("hme", "store_day_trust", 40, "2026-11-23")
check("network reconciled but a store missing (NO_ACCESS) -> store HIGH no_access",
      active(st)[k40]["category"] == "no_access" and dkey not in active(st))
cfga = make_cfg(hme={"lookback_days": 1, "accepted_dates": {"2026-11-23": "accepted outage"}})
st = Store(":memory:")
run(cfga, st, ct(2026, 11, 24, 11, 0), hspec, pg_connect=lambda: pg)
check("accepted HME date -> ACCEPTED, no incident", not active(st))
ctx = engine.Context(cfgh, Store(":memory:"), ct(2026, 11, 24, 5, 0), "data", pg_connect=lambda: hme_pg([], [], []))
check("before 06:30 on D+1: PENDING, no verdict", all(r.state == PENDING for r in hme.days(ctx)))

# ═══════════════════════════════════════════════════════════════════════════
print("5. R365: week coverage, store gaps, financial period coverage")
now = ct(2026, 11, 25, 12, 0)        # Wednesday
weeks = r365.expected_weeks(now, "21:30", 2)
check("expected weeks are Thursday starts ending last Wednesday",
      weeks == [date(2026, 11, 12), date(2026, 11, 5)], weeks)
check("Thursday before 21:30 still expects the previous week",
      r365.expected_weeks(ct(2026, 11, 26, 20, 0), "21:30", 1) == [date(2026, 11, 12)])
check("Thursday after 21:30 expects the week just ended",
      r365.expected_weeks(ct(2026, 11, 26, 22, 0), "21:30", 1) == [date(2026, 11, 19)])


def wk(ws, **kw):
    base = {"week_start": ws, "week_end": ws + timedelta(days=6), "week_status": "COMPLETE",
            "source_complete": True, "reconciliation_ok": True, "expected_store_count": 12,
            "observed_store_count": 12, "stores_with_full_week": 12, "missing_store_days": 0,
            "closed_store_days": 0, "pre_opening_store_days": 0, "latest_refresh_failed": False,
            "loaded_at": now - timedelta(days=5)}
    base.update(kw)
    return base


def store_days(ws_params):
    return [{"establishment_id": 6, "store_name": "LCF Katy", "opening_date": None, "days_present": 7},
            {"establishment_id": 54, "store_name": "LCF Cypress", "opening_date": None,
             "days_present": 5 if ws_params["ws"] == date(2026, 11, 12) else 7},
            {"establishment_id": 99, "store_name": "New store", "opening_date": date(2027, 1, 1),
             "days_present": 0}]


pg = FakePG([("FROM v_r365_completeness_llm", [wk(date(2026, 11, 12), stores_with_full_week=11)]),
             ("FROM r365_store_mapping m", store_days), ("FROM r365_ingest_run", [])])
ctx = engine.Context(make_cfg(r365={"lookback_weeks": 2}), Store(":memory:"), now, "data", pg_connect=lambda: pg)
res = r365.weekly(ctx)
wres = {(r.check, r.store_id, r.qualifier): r for r in res}
check("missing week -> HIGH missing_ingestion",
      wres[("weekly_coverage", None, "2026-11-05..2026-11-11")].category == "missing_ingestion")
check("week with 11/12 full stores -> HIGH incomplete",
      wres[("weekly_coverage", None, "2026-11-12..2026-11-18")].severity == HIGH)
check("store with 5 of 7 days -> HIGH coverage_gap", wres[("store_week_coverage", 54, "2026-11-12..2026-11-18")].actual == "5 of 7 days")
check("complete store -> OK", wres[("store_week_coverage", 6, "2026-11-12..2026-11-18")].state == OK)
check("store not yet open -> ACCEPTED", wres[("store_week_coverage", 99, "2026-11-12..2026-11-18")].state == "ACCEPTED")

P = [{"fiscal_year": 2026, "period_number": 12, "period_label": "P12", "start_date": date(2026, 11, 5),
      "end_date": date(2026, 12, 2)},
     {"fiscal_year": 2026, "period_number": 11, "period_label": "P11", "start_date": date(2026, 10, 8),
      "end_date": date(2026, 11, 4)},
     {"fiscal_year": 2026, "period_number": 10, "period_label": "P10", "start_date": date(2026, 9, 10),
      "end_date": date(2026, 10, 7)}]
RUNS = [{"requested_fiscal_year": 2026, "requested_period_number": 11, "id": 31, "status": "PARTIAL",
         "completed_at": datetime(2026, 10, 16, 17, 35, tzinfo=timezone.utc), "reconciliation_ok": True,
         "source_sync_stale": False, "source_last_sync_at": None, "error_code": None},
        {"requested_fiscal_year": 2026, "requested_period_number": 10, "id": 33, "status": "COMPLETE",
         "completed_at": datetime(2026, 10, 16, 17, 35, tzinfo=timezone.utc), "reconciliation_ok": True,
         "source_sync_stale": False, "source_last_sync_at": None, "error_code": None}]
pg = FakePG([("FROM ayg_business_periods", P), ("FROM r365_financial_ingest_run", RUNS)])
ctx = engine.Context(make_cfg(), Store(":memory:"), now, "data", pg_connect=lambda: pg)
fres = {r.qualifier: r for r in r365.financial(ctx) if r.check == "period_coverage"}
check("open fiscal period never loaded -> WARNING missing_ingestion (no longer hidden as 'open')",
      fres["FY2026 P12"].category == "missing_ingestion" and fres["FY2026 P12"].severity == WARNING)
check("closed period last loaded before close -> WARNING stale_after_close",
      fres["FY2026 P11"].severity == WARNING and fres["FY2026 P11"].category == "stale_after_close")
check("P10 loaded after close but before the 42-day repair window ended -> WARNING "
      "not_refreshed_after_repair_window (the old check passed it as OK)",
      fres["FY2026 P10"].severity == WARNING and fres["FY2026 P10"].category == "not_refreshed_after_repair_window")

# ═══════════════════════════════════════════════════════════════════════════
print("6. services, jobs, AI Chat and MCP failures, host")
UNITS = {"laynes-api": {"ActiveState": "inactive", "SubState": "dead", "Result": "exit-code", "LoadState": "loaded"},
         "laynes-mcp": {"ActiveState": "active", "LoadState": "loaded"},
         "nginx": {"ActiveState": "active", "LoadState": "loaded"},
         "postgresql": {"ActiveState": "active", "LoadState": "loaded"},
         "laynes-sales-report": {"ActiveState": "active", "LoadState": "loaded"},
         "revel-daily-sync": {"ActiveState": "failed", "Result": "exit-code", "ExecMainStatus": "1", "LoadState": "loaded"},
         "hme-reconciliation-retry": {"ActiveState": "inactive", "Result": "success", "ExecMainStatus": "0", "LoadState": "loaded"},
         "hme-daily-ingest": {"ActiveState": "failed", "Result": "exit-code", "ExecMainStatus": "2", "LoadState": "loaded"}}
fake_sys = lambda u: UNITS.get(u, {"ActiveState": "active", "Result": "success", "LoadState": "loaded"})  # noqa: E731
ctx = engine.Context(make_cfg(), Store(":memory:"), now, "health", systemctl=fake_sys)
u = {r.qualifier: r for r in services.units(ctx)}
check("AI Chat API (laynes-api) down -> CRITICAL", u["laynes-api"].severity == CRITICAL and u["laynes-api"].state == FAIL)
j = {r.qualifier: r for r in services.jobs(ctx)}
check("failed Revel ingestion job -> HIGH", j["revel-daily-sync"].severity == HIGH and j["revel-daily-sync"].state == FAIL)
check("HME morning unit red -> WARNING only (data checks own the verdict)",
      j["hme-daily-ingest"].severity == WARNING)
check("successful job -> OK", j["hme-reconciliation-retry"].state == OK)
ctx = engine.Context(make_cfg(), Store(":memory:"), now, "data",
                     systemctl=lambda u_: {"ActiveState": "inactive", "LoadState": "loaded"})
check("inactive timer -> HIGH (its job would silently stop)",
      all(r.state == FAIL and r.severity == HIGH for r in services.timers(ctx)))

ctx = engine.Context(make_cfg(), Store(":memory:"), now, "health", http=lambda url, t: (502, None))
m = mcp.health(ctx)[0]
check("MCP health 502 -> HIGH", m.state == FAIL and m.severity == HIGH)
h = {r.qualifier: r for r in services.http(ctx)}
check("public site / API not answering -> CRITICAL", h["public_site"].severity == CRITICAL)
ctx = engine.Context(make_cfg(), Store(":memory:"), now, "health", http=lambda url, t: (200, None))
h = {r.qualifier: r for r in services.http(ctx)}
check("API answering 200 without a session is a FAIL (auth must give 401)",
      h["public_api_auth"].state == FAIL)

pg = FakePG([("status IN ('queued', 'processing')", [{"n": 2, "oldest": now - timedelta(hours=1)}])])
ctx = engine.Context(make_cfg(), Store(":memory:"), now, "health", pg_connect=lambda: pg)
check("stuck chat jobs -> HIGH", chat.stuck_jobs(ctx)[0].severity == HIGH)
pg = FakePG([
    ("status NOT IN ('queued', 'processing', 'completed')",
     [{"status": "failed", "error_code": "provider_error", "n": 2}]),
    ("FROM chat_query_log\nWHERE asked_at", [{"n": 4, "sample": "ProviderError[provider_rate_limited]"}]),
    ("jsonb_array_elements", lambda p: [
        {"kind": "sql_error", "n": 5, "sample": "column x does not exist"},
        {"kind": "trust_gate", "n": 3, "sample": "did not pass the revel trust gate"}]),
])
ctx = engine.Context(make_cfg(), Store(":memory:"), now, "data", pg_connect=lambda: pg)
c = {r.check: r for r in chat.errors(ctx)}
check("failed chat jobs -> HIGH", c["failed_jobs"].severity == HIGH)
check("provider errors over threshold -> WARNING", c["provider_errors"].state == FAIL)
check("SQL errors over threshold -> WARNING", c["sql_errors"].state == FAIL)
check("trust-gate refusals -> WARNING pointing at the data incident",
      c["trust_gate_blocks"].severity == WARNING and "matching" in c["trust_gate_blocks"].actions[0])
check("chat checks never select question/answer text",
      not any(w in " ".join(pg.executed).lower() for w in ("question", "answer", "content", "chat_messages")))

import sqlite3 as _sq  # noqa: E402
import tempfile as _tf  # noqa: E402
tmp = _tf.mkdtemp()
dbp = os.path.join(tmp, "mcp.sqlite3")
c_ = _sq.connect(dbp)
c_.execute("PRAGMA journal_mode=WAL")
c_.execute("CREATE TABLE usage_log (id INTEGER PRIMARY KEY, key_id TEXT, ts TEXT, tool TEXT, outcome TEXT)")
ts = (now - timedelta(minutes=5)).isoformat(timespec="seconds")
c_.executemany("INSERT INTO usage_log (ts, tool, outcome) VALUES (?,?,?)",
               [(ts, "drive_thru_speed", "error"), (ts, "sales", "ok"), (ts, "x", "partial")]
               + [(ts, None, "auth_failed")] * 25)
c_.commit()
mt = os.path.getmtime(dbp)
ctx = engine.Context(make_cfg(mcp={"usage_db": dbp}), Store(":memory:"), now, "data")
mres = {r.check: r for r in mcp.tool_errors(ctx)}
check("MCP tool error -> HIGH with the tool name", mres["tool_errors"].severity == HIGH
      and mres["tool_errors"].evidence["tools"] == ["drive_thru_speed"])
check("'partial'/'unavailable' are trust outcomes, not errors", mres["tool_errors"].evidence["outcomes"]["partial"] == 1)
check("auth-failure burst -> WARNING", mres["auth_failures"].severity == WARNING)
check("MCP database read from a private copy (live file untouched)", os.path.getmtime(dbp) == mt)
c_.close()

DU = namedtuple("DU", "total used free")
ctx = engine.Context(make_cfg(), Store(":memory:"), now, "health",
                     disk_usage=lambda p: DU(100, 95, 5), mem_available_mb=lambda: 120)
hr = {r.check: r for r in infra.resources(ctx)}
check("disk 95% -> CRITICAL; low memory -> WARNING",
      hr["disk"].severity == CRITICAL and hr["memory"].severity == WARNING)
ctx = engine.Context(make_cfg(), Store(":memory:"), now, "data", newest_backup=lambda g: (None, None))
check("no backup -> HIGH", infra.backups(ctx)[0].severity == HIGH)
ctx = engine.Context(make_cfg(), Store(":memory:"), now, "data",
                     newest_backup=lambda g: ("/x.dump", now - timedelta(hours=2)),
                     backup_format=lambda p: "pg_dump custom archive")
check("fresh backup -> OK", infra.backups(ctx)[0].state == OK)
ctx = engine.Context(make_cfg(), Store(":memory:"), now, "data", cert_expiry=lambda h: now + timedelta(days=5))
check("TLS certificate 5 days from expiry -> WARNING", services.tls(ctx)[0].severity == WARNING)

# ═══════════════════════════════════════════════════════════════════════════
print("7. safety: thresholds, read-only sessions, privacy, admin-only API")
src = {p: open(os.path.join(ROOT, "monitoring", p)).read()
       for p in ("config.py", "checks/revel.py", "engine.py", "pg.py")}
check("no reconciliation tolerance defined in the monitor (imported from chat_sql)",
      "RECON_TOLERANCE_PCT =" not in "".join(src.values())
      and "tol = C.RECON_TOLERANCE_PCT" in src["checks/revel.py"])
chat_src = open(os.path.join(ROOT, "chat_sql.py")).read()
check("chat_sql tolerance is still ±0.5% (unchanged)", "RECON_TOLERANCE_PCT = 0.5 " in chat_src)
check("pg sessions forced read-only, bounded, and verified",
      all(s in src["pg.py"] for s in ("default_transaction_read_only=on", "statement_timeout",
                                      "lock_timeout", "SHOW transaction_read_only",
                                      "set_session(readonly=True")))


class RWConn:
    def set_session(self, **kw):
        pass

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, q):
        pass

    def fetchone(self):
        return ("off",)

    def rollback(self):
        pass

    def close(self):
        self.closed = True


import monitoring.pg as mpg  # noqa: E402
orig_connect = mpg.psycopg2.connect
mpg.psycopg2.connect = lambda **kw: RWConn()
os.environ["MONITOR_DB_USER"] = "x"
try:
    mpg.connect(mcfg.DEFAULTS)
    refused = False
except mpg.MonitorDbError:
    refused = True
finally:
    mpg.psycopg2.connect = orig_connect
check("a session that is not read-only is refused before any query", refused)


import re  # noqa: E402

_WRITE_SQL = re.compile(r"^\s*(INSERT|UPDATE|DELETE|TRUNCATE|ALTER|DROP|CREATE|COPY|GRANT|VACUUM)\b",
                        re.I | re.M)


def writes_anywhere(path):
    """Write statements in any SQL string constant of a check module."""
    tree = ast.parse(open(path).read())
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and re.search(r"\b(SELECT|FROM|WHERE)\b", node.value):
            m = _WRITE_SQL.search(node.value)
            if m:
                bad.append(m.group(1))
    return bad


def systemctl_verbs(path):
    """The verb of every systemctl argv list literal in the module."""
    verbs = []
    for node in ast.walk(ast.parse(open(path).read())):
        if isinstance(node, ast.List) and node.elts and isinstance(node.elts[0], ast.Constant) \
                and node.elts[0].value == "systemctl":
            verbs.append(node.elts[1].value if len(node.elts) > 1 else None)
    return verbs


bad_sql = {p: writes_anywhere(os.path.join(ROOT, "monitoring", "checks", p))
           for p in os.listdir(os.path.join(ROOT, "monitoring", "checks")) if p.endswith(".py")}
check("no write SQL in any production check", not any(bad_sql.values()), bad_sql)
verbs = [v for dp, _, fs in os.walk(os.path.join(ROOT, "monitoring")) for f in fs if f.endswith(".py")
         for v in systemctl_verbs(os.path.join(dp, f))]
check("the only systemctl verb the monitor uses is 'show'", verbs == ["show"], verbs)
check("no LLM client used by the monitor",
      not any("anthropic" in open(os.path.join(dp, f)).read() or "openai" in open(os.path.join(dp, f)).read()
              for dp, _, fs in os.walk(os.path.join(ROOT, "monitoring")) for f in fs if f.endswith(".py")))
inc_msg = notify.format_group("OPENED", [{
    "id": "INC-1", "source": "ai_chat", "check_name": "failed_jobs", "store_id": None, "store_name": None,
    "business_date": None, "qualifier": None, "title": "AI Chat jobs failed", "difference": None,
    "severity": "HIGH", "actions": ["x"]}])
check("chat notification carries no conversation content", "question" not in inc_msg.lower())

# Admin-only API (dependency-level: httpx/TestClient are not installed here).
os.environ.setdefault("LAYNES_MONITOR_CONFIG", "/nonexistent")
from fastapi import HTTPException  # noqa: E402
from backend import monitoring_api as MA  # noqa: E402
try:
    MA.monitoring_admin({"role": "user", "email": "u@example.com"})
    code = 200
except HTTPException as e:
    code = e.status_code
check("non-admin -> 403 on monitoring routes", code == 403)
check("super_admin passes", MA.monitoring_admin({"role": "super_admin", "email": "a"})["role"] == "super_admin")
from backend import auth as AUTH  # noqa: E402
orig_gsu = AUTH.get_session_user
AUTH.get_session_user = lambda tok: None
try:
    AUTH.current_user(None)
    code = 200
except HTTPException as e:
    code = e.status_code
finally:
    AUTH.get_session_user = orig_gsu
check("no session -> 401 (current_user runs before the admin check)", code == 401)
deps = {r.path: [d.call for d in r.dependant.dependencies] for r in MA.router.routes}
check("every monitoring route carries the admin dependency",
      deps and all(any(getattr(d, "__name__", "") == "monitoring_admin" for d in ds) for ds in deps.values()),
      list(deps))
check("every monitoring route is under /api/admin/", all(p.startswith("/api/admin/monitoring") for p in deps))
api_src = open(os.path.join(ROOT, "backend", "api.py")).read()
check("backend/api.py mounts the monitoring router", "app.include_router(_monitoring_router)" in api_src)

# ═══════════════════════════════════════════════════════════════════════════
print("8. config + summary")
try:
    mcfg.validate(mcfg._merge(mcfg.DEFAULTS, {"hme": {"morning_ready_local": "11:00"}}))
    bad_ok = True
except ValueError:
    bad_ok = False
check("config: morning window after the retry window is rejected", not bad_ok)
try:
    mcfg.validate(mcfg._merge(mcfg.DEFAULTS, {"notify": {"channels": ["sms"]}}))
    bad_ok = True
except ValueError:
    bad_ok = False
check("config: unknown channel rejected", not bad_ok)
mcfg.validate(mcfg.DEFAULTS)
check("defaults validate", True)
from monitoring import summary  # noqa: E402
st = Store(":memory:")
state["r"] = R(state=FAIL, severity=HIGH, title="Sales reconciliation mismatch", difference="+5.42%")
run(cfg, st, T0, S)
ov = summary.overview(st, T0)
check("overview: per-store matrix, unresolved incidents, reconciliation differences",
      ov["stores"][0]["sources"]["revel"]["state"] == "FAIL" and len(ov["open_incidents"]) == 1
      and ov["reconciliation_differences"][0]["difference"] == "+5.42%")
txt = summary.daily_summary_text(st, T0)
check("daily summary lists the open incident", "LCF Missouri City" in txt and "HIGH 1" in txt)

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed" + (f"; FAILED: {failed}" if failed else ""))
sys.exit(1 if failed else 0)
