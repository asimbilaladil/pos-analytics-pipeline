"""Revel POS: sync freshness and per-store daily sales reconciliation.

Reconciliation is NOT reimplemented here. It calls chat_sql.revel_store_breakdown
-- the function AI Chat uses for its per-store verdict -- so the monitor reports
exactly the store-days AI Chat refuses, with the same ±RECON_TOLERANCE_PCT and the
same failure categories. The monitor only adds:
  * readiness: business day D is judged only after revel-daily-sync on D+1;
  * severity: payment timing (payments dated another day) and open/unpaid
    tickets are real but are NOT data corruption, so they are WARNING, while an
    unexplained mismatch, missing days or stale data are HIGH;
  * a network roll-up when most stores fail at once (a sync failure, CRITICAL).
"""
from __future__ import annotations

import sys
from datetime import timedelta

from .. import clock
from ..model import (ACCEPTED, CRITICAL, FAIL, HIGH, OK, PENDING, SRC_REVEL, WARNING,
                     CheckResult)
from ..pg import rows

CHECK_SYNC = "sync_freshness"
CHECK_RECON = "sales_reconciliation"
CHECK_NETWORK = "network_reconciliation"

_SYNC_SQL = """
SELECT e.id AS establishment_id, e.name AS store_name, s.resource,
       s.last_success_at, s.window_end, s.status
FROM establishments e
LEFT JOIN sync_state s ON s.establishment_id = e.id
                      AND s.resource IN ('orders_v2', 'payments_v2')
WHERE e.active
ORDER BY e.id, s.resource
"""

# Open (not closed) REAL tickets with money still due, for one store-day. Read
# through the analytics view; no customer fields are selected.
_OPEN_TICKETS_SQL = """
SELECT COUNT(*) AS open_tickets,
       COALESCE(ROUND(SUM(c.final_total - COALESCE(ps.payment_amount_total, 0)), 2), 0) AS open_due
FROM v_orders_classified c
LEFT JOIN v_order_payment_summary ps ON ps.order_id = c.id
WHERE c.establishment_id = %(est)s AND c.business_date = %(d)s AND c.txn_class = 'REAL'
  AND c.closed IS NOT TRUE
  AND c.final_total - COALESCE(ps.payment_amount_total, 0) > 0.005
"""


def _chat_sql(ctx):
    rel = ctx.cfg["monitor"]["release_dir"]
    if rel not in sys.path:
        sys.path.insert(0, rel)
    import chat_sql  # noqa: E402  (the production reconciliation, imported not copied)
    return chat_sql


def freshness(ctx) -> list[CheckResult]:
    cfg = ctx.cfg["revel"]
    max_age = timedelta(hours=cfg["sync_max_age_hours"])
    accepted = {int(k): v for k, v in cfg["accepted_stores"].items()}
    by_store: dict[int, dict] = {}
    for r in rows(ctx.pg(), _SYNC_SQL):
        s = by_store.setdefault(r["establishment_id"], {"name": r["store_name"], "res": {}})
        if r["resource"]:
            s["res"][r["resource"]] = r
    out, newest, oldest = [], None, None
    for est, s in sorted(by_store.items()):
        problems, ev = [], {}
        for res in ("orders_v2", "payments_v2"):
            r = s["res"].get(res)
            if r is None or r["last_success_at"] is None:
                problems.append(f"{res}: never synced")
                continue
            age = ctx.now - r["last_success_at"]
            ev[res] = {"last_success_at": r["last_success_at"].isoformat(),
                       "age_hours": round(age.total_seconds() / 3600, 1), "status": r["status"]}
            newest = max(newest or r["last_success_at"], r["last_success_at"])
            oldest = min(oldest or r["last_success_at"], r["last_success_at"])
            if age > max_age:
                problems.append(f"{res} last synced {age.total_seconds() / 3600:.0f}h ago")
            if (r["status"] or "").lower() not in ("success", "ok"):
                problems.append(f"{res} sync status {r['status']!r}")
        base = dict(source=SRC_REVEL, check=CHECK_SYNC, store_id=est, store_name=s["name"],
                    evidence=ev, expected=f"sync within {cfg['sync_max_age_hours']}h")
        if est in accepted:
            out.append(CheckResult(state=ACCEPTED, title=f"Excluded: {accepted[est]}", **base))
        elif problems:
            out.append(CheckResult(
                state=FAIL, severity=HIGH, category="stale_data",
                title="Revel sync is stale or failing", actual="; ".join(problems), **base,
                actions=["Check `systemctl status revel-daily-sync` and "
                         "`journalctl -u revel-daily-sync --since -1d`.",
                         "Check sync_state for this store (resource orders_v2 / payments_v2).",
                         "Do not re-run the sync from the monitor; re-run manually only after "
                         "confirming why it failed."]))
        else:
            out.append(CheckResult(state=OK, title="Revel sync fresh", **base))
    ctx.source_status(SRC_REVEL, newest, {"oldest_store_sync": oldest.isoformat() if oldest else None,
                                          "stores": len(by_store)})
    return out


def reconciliation(ctx) -> list[CheckResult]:
    cfg = ctx.cfg["revel"]
    C = _chat_sql(ctx)
    tol = C.RECON_TOLERANCE_PCT
    accepted = {int(k): v for k, v in cfg["accepted_stores"].items()}
    out: list[CheckResult] = []
    stores = rows(ctx.pg(), "SELECT id, name FROM establishments WHERE active ORDER BY id")
    for day in clock.closed_business_days(ctx.now, cfg["lookback_days"]):
        d = day.isoformat()
        ready = clock.ready_at(day, cfg["ready_after_local"])
        if ctx.now < ready:
            for s in stores:
                out.append(CheckResult(
                    source=SRC_REVEL, check=CHECK_RECON, state=PENDING, store_id=s["id"],
                    store_name=s["name"], business_date=d,
                    title=f"Awaiting Revel daily sync (ready after {ready:%H:%M %Z} {ready:%m-%d})"))
            continue
        b = C.revel_store_breakdown(d, (day + timedelta(days=1)).isoformat())
        seen = set()
        hard = 0
        for s in b["stores"]:
            est = s["establishment_id"]
            seen.add(est)
            r = _classify(ctx, s, d, tol, cfg, accepted)
            if r.state == FAIL and r.severity == HIGH:
                hard += 1
            out.append(r)
        closed = cfg["accepted_closed_dates"].get(d)
        for s in stores:                  # an active store with no row at all
            if s["id"] in seen or s["id"] in accepted:
                continue
            if closed:
                out.append(CheckResult(source=SRC_REVEL, check=CHECK_RECON, state=ACCEPTED,
                                       store_id=s["id"], store_name=s["name"], business_date=d,
                                       title=f"Closed: {closed}"))
            else:
                hard += 1
                out.append(CheckResult(
                    source=SRC_REVEL, check=CHECK_RECON, state=FAIL, severity=HIGH,
                    store_id=s["id"], store_name=s["name"], business_date=d,
                    category="no_data", title="No Revel orders or payments for the day",
                    expected="orders and payments for a trading day", actual="none",
                    actions=_actions("no_data")))
        n = len(stores) - len(accepted)
        net = dict(source=SRC_REVEL, check=CHECK_NETWORK, business_date=d,
                   expected=f"most stores reconciled within ±{tol}%")
        if n and hard >= max(2, (n + 1) // 2):
            out.append(CheckResult(
                state=FAIL, severity=CRITICAL, category="network",
                title=f"{hard} of {n} stores fail Revel reconciliation -- likely a sync failure",
                actual=f"{hard} stores failing", **net,
                actions=["Treat as a pipeline problem, not a store problem: check "
                         "revel-daily-sync logs and sync_state first."]))
        else:
            out.append(CheckResult(state=OK, title="Network Revel reconciliation", **net))
    return out


def _classify(ctx, s, d, tol, cfg, accepted) -> CheckResult:
    est = s["establishment_id"]
    base = dict(source=SRC_REVEL, check=CHECK_RECON, store_id=est, store_name=s["store_name"],
                business_date=d, expected=f"orders vs payments within ±{tol}%",
                difference=s.get("reconciliation_delta_pct_display"))
    ev = {"failure_reasons": s.get("failure_reasons") or [],
          "failure_category": s.get("failure_category"),
          "basis": "chat_sql.revel_store_breakdown (same verdict AI Chat uses)"}
    if est in accepted:
        return CheckResult(state=ACCEPTED, title=f"Excluded: {accepted[est]}", evidence=ev, **base)
    if s["revel_status"] == "PASS":
        return CheckResult(state=OK, title="Reconciled", evidence=ev, **base)
    cat = s.get("failure_category") or "reconciliation_mismatch"
    if cat == "no_data" and d in cfg["accepted_closed_dates"]:
        return CheckResult(state=ACCEPTED, title=f"Closed: {cfg['accepted_closed_dates'][d]}",
                           evidence=ev, **base)
    actual = "; ".join(ev["failure_reasons"]) or cat
    if cat == "payment_timing":
        return CheckResult(
            state=FAIL, severity=cfg["payment_timing_severity"], category=cat,
            title="Sales reconciliation outside tolerance: payment timing, not data loss",
            actual=actual, evidence=ev, actions=_actions(cat), **base)
    if cat == "reconciliation_mismatch":
        ot = rows(ctx.pg(), _OPEN_TICKETS_SQL, {"est": est, "d": d})[0]
        due = float(ot["open_due"] or 0)
        delta = _delta_dollars(ev["failure_reasons"])
        ev["open_unpaid_tickets"] = int(ot["open_tickets"] or 0)
        ev["open_unpaid_amount"] = due
        if delta and delta > 0 and due >= cfg["open_ticket_explained_share"] * delta:
            return CheckResult(
                state=FAIL, severity=WARNING, category="open_unpaid_tickets",
                title="Sales reconciliation outside tolerance: open/unpaid tickets in Revel",
                actual=f"{actual}; {ev['open_unpaid_tickets']} open ticket(s) with "
                       f"${due:,.2f} unpaid explain the gap",
                evidence=ev, actions=_actions("open_unpaid_tickets"), **base)
    return CheckResult(state=FAIL, severity=HIGH, category=cat,
                       title=_TITLES.get(cat, "Sales reconciliation mismatch"),
                       actual=actual, evidence=ev, actions=_actions(cat), **base)


def _delta_dollars(reasons: list[str]) -> float | None:
    import re
    for r in reasons:
        m = re.search(r"\(\$([+-][\d,]+\.\d+)\)", r)
        if m:
            return float(m.group(1).replace(",", ""))
    return None


_TITLES = {
    "reconciliation_mismatch": "Sales reconciliation mismatch (unexplained)",
    "missing_ingestion": "Revel data missing from ingestion",
    "stale_data": "Revel data stale for the day",
    "no_data": "No Revel orders or payments for the day",
    "integrity": "Duplicate Revel order ids",
}


def _actions(cat: str) -> list[str]:
    common = "AI Chat withholds this store-day's sales figures until it reconciles."
    return {
        "payment_timing": [
            "Payments for these orders exist but are dated on another business day "
            "(prepaid/scheduled orders, or paid after midnight). Not data loss.",
            "Confirm in Revel: find the order(s) whose payment date differs from the "
            "order date (v_orders_classified joined to payments_v2 by order_id).",
            "No repair needed if confirmed; the day stays outside tolerance by design. " + common],
        "open_unpaid_tickets": [
            "Open tickets with an unpaid balance remain in Revel for this day.",
            "Ask the store manager to close, settle or void the open tickets in Revel; "
            "the next daily sync picks up the change and this incident resolves itself.",
            common],
        "reconciliation_mismatch": [
            "Compare orders vs payments for the store-day: REAL orders without a payment, "
            "under-paid orders, and payments without a REAL order.",
            "Check whether the daily sync finished for this store (sync_state, "
            "revel-daily-sync journal).",
            "If Revel itself shows the same totals, document it; do not edit orders or payments.",
            common],
        "missing_ingestion": [
            "Orders exist without line items or business days have no orders: an ingestion gap.",
            "Check revel-daily-sync and revel-items-selfheal logs for this store and day.",
            common],
        "no_data": [
            "No orders or no payments: confirm whether the store traded that day "
            "(holiday / closure). If it was closed, add the date to the accepted list.",
            "If it traded, check the daily sync for this store.", common],
        "stale_data": ["The store's newest order is too old: a sync was missed. Check "
                       "revel-daily-sync.", common],
        "integrity": ["Duplicate order ids inflate totals: inspect orders_v2 for the "
                      "store-day before anyone uses its figures.", common],
    }.get(cat, [common])
