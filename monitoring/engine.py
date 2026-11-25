"""Turn check results into incidents and notifications.

Per result, against the ONE active incident for its dedup key:

  FAIL, no active incident        -> OPEN a new incident            (alert if severe)
  FAIL, same fingerprint          -> touch last_checked_at only     (no alert)
  FAIL, severity went up          -> ESCALATED                      (alert if severe)
  FAIL, other material change     -> UPDATED / DEESCALATED          (no alert)
  OK, active incident             -> RESOLVED                       (alert if it was alerted)
  ACCEPTED, active incident       -> RESOLVED as accepted exclusion (no alert)
  PENDING                          -> left as is (no verdict yet)

A key that a check did not evaluate this run is never touched, and a check
that raises opens a monitor incident for itself instead of resolving anything.
ACKNOWLEDGED incidents stay acknowledged on re-checks; they still escalate
(and alert) if the severity rises, and still resolve when the issue clears.
"""
from __future__ import annotations

import logging
import time
import traceback
from datetime import datetime, timedelta

from . import VERSION, pg
from .checks import CheckSpec, registry
from .checks import infra as infra_checks
from .checks import mcp as mcp_checks
from .checks import services as svc_checks
from .model import (ACCEPTED, FAIL, HIGH, OK, SEVERITY_RANK, SRC_MONITOR, CheckResult)
from .notify import deliver_pending, format_group
from .store import RUN_DEGRADED, RUN_FAILED, RUN_SUCCESS, Store, row_dict

log = logging.getLogger("laynes.monitor")


class PgUnavailable(RuntimeError):
    pass


class Context:
    """Everything a check may touch. Every external effect is injectable for tests."""

    def __init__(self, cfg: dict, store: Store, now: datetime, tier: str, *,
                 pg_connect=None, http=None, systemctl=None, disk_usage=None,
                 mem_available_mb=None, newest_backup=None, cert_expiry=None,
                 backup_format=None, read_status_json=None):
        self.cfg, self.store, self.now, self.tier = cfg, store, now, tier
        self._pg_connect = pg_connect or (lambda: pg.connect(cfg))
        # A SEPARATE, fresh monitor session for code that manages its own
        # connection (chat_sql's reconciliation; see checks/revel.py).
        self.monitor_pg_connect = pg_connect or (lambda: pg.connect(cfg))
        self._pg = None
        self._pg_error: str | None = None
        self.http = http or mcp_checks.http_status
        self.systemctl = systemctl or svc_checks.systemctl_show
        self.disk_usage = disk_usage or infra_checks.disk_usage
        self.mem_available_mb = mem_available_mb or infra_checks.mem_available_mb
        self.newest_backup = newest_backup or infra_checks.newest_backup
        self.cert_expiry = cert_expiry or svc_checks.cert_expiry
        self.backup_format = backup_format or infra_checks.dump_format
        self.read_status_json = read_status_json or infra_checks.read_status_json
        self.source_updates: dict[str, tuple] = {}

    def pg(self):
        if self._pg is None:
            if self._pg_error:
                raise PgUnavailable(self._pg_error)
            try:
                self._pg = self._pg_connect()
            except Exception as e:  # noqa: BLE001
                self._pg_error = f"{type(e).__name__}: {str(e)[:200]}"
                raise PgUnavailable(self._pg_error) from None
        return self._pg

    def source_status(self, source: str, last_success_at, detail: dict):
        self.source_updates[source] = (
            last_success_at.isoformat() if hasattr(last_success_at, "isoformat") else last_success_at,
            detail)

    def close(self):
        if self._pg is not None:
            try:
                self._pg.close()
            except Exception:  # noqa: BLE001
                pass


def _immediate(cfg: dict, r_or_row) -> bool:
    notify = r_or_row.get("notify") if isinstance(r_or_row, dict) else r_or_row.notify
    sev = r_or_row["severity"] if isinstance(r_or_row, dict) else r_or_row.severity
    if notify is not None:
        return bool(notify)
    return SEVERITY_RANK[sev] >= SEVERITY_RANK[cfg["notify"]["immediate_min_severity"]]


def apply_results(store: Store, cfg: dict, results: list[CheckResult], now: datetime) -> list[tuple]:
    """Apply one check's results. Returns notification events [(kind, incident_id)]."""
    events = []
    for r in results:
        store.record_result(r, now)
        active = store.active_incident(r.key)
        if r.state == FAIL:
            if active is None:
                iid = store.open_incident(r, now)
                if _immediate(cfg, r):
                    events.append(("OPENED", iid))
            elif active["fingerprint"] == r.fingerprint():
                store.touch(active["id"], now)
            else:
                old, new = SEVERITY_RANK[active["severity"]], SEVERITY_RANK[r.severity]
                kind = "ESCALATED" if new > old else ("DEESCALATED" if new < old else "UPDATED")
                store.update_incident(active["id"], r, now, kind,
                                      {"severity": active["severity"],
                                       "difference": active["difference"],
                                       "category": active["category"]})
                if kind == "ESCALATED" and _immediate(cfg, r):
                    events.append(("ESCALATED", active["id"]))
        elif r.state == OK and active is not None:
            store.resolve(active["id"], now, "cleared: check passed")
            if active["notified"] and cfg["notify"]["notify_resolutions"]:
                events.append(("RESOLVED", active["id"]))
        elif r.state == ACCEPTED and active is not None:
            store.resolve(active["id"], now, "accepted exclusion configured")
    return events


def _check_error_result(spec: CheckSpec, err: str | None) -> CheckResult:
    base = dict(source=SRC_MONITOR, check="check_error", qualifier=spec.name,
                expected="check runs without error")
    if err is None:
        return CheckResult(state=OK, title=f"Check {spec.name} ran", **base)
    return CheckResult(state=FAIL, severity=HIGH, category="check_crashed",
                       title=f"Monitor check {spec.name} is failing", actual=err[:300],
                       actions=["Read `journalctl -u laynes-monitor-*` for the traceback.",
                                "Incidents owned by this check are frozen (neither opened nor "
                                "resolved) until it runs again."], **base)


def enqueue_events(store: Store, cfg: dict, events: list[tuple], now: datetime) -> int:
    """Group events into messages and put one per channel in the outbox."""
    groups: dict[tuple, list[dict]] = {}
    for kind, iid in events:
        inc = row_dict(store.get(iid))
        if inc is None:
            continue
        g = (kind, inc["source"], inc["check_name"], inc["business_date"], inc["title"],
             inc["severity"])
        groups.setdefault(g, []).append(inc)
    n = 0
    for (kind, *_rest), incs in groups.items():
        body = format_group(kind, incs, cfg["notify"].get("dashboard_url"))
        for ch in cfg["notify"]["channels"]:
            store.enqueue(now, kind, incs[0]["severity"], [i["id"] for i in incs], ch, body)
            n += 1
        if kind in ("OPENED", "ESCALATED"):
            for i in incs:
                store.mark_notified(i["id"])
    return n


def run_status(summary: dict) -> tuple[str, str | None]:
    """SUCCESS only when every due check ran cleanly AND every delivery attempted
    this run succeeded. Anything less is DEGRADED, with the reason. Data-quality
    FAILs found by the checks do not count: they are incidents, not monitor faults."""
    d = summary.get("delivery") or {}
    reasons = []
    if summary["crashed"]:
        reasons.append(f"crashed={summary['crashed']}")
    if summary["skipped"]:
        reasons.append(f"skipped(postgres unavailable)={summary['skipped']}")
    if d.get("failed") or d.get("retrying") or d.get("lost_lease"):
        reasons.append(f"delivery failed={d.get('failed', 0)} retrying={d.get('retrying', 0)} "
                       f"lost_lease={d.get('lost_lease', 0)}")
    return (RUN_SUCCESS, None) if not reasons else (RUN_DEGRADED, "; ".join(reasons))


def close_unit_failure(store: Store, cfg: dict, unit: str, now: datetime) -> None:
    """A SUCCESSFUL pass of a tier resolves its unit-failed incident (crash-notify)."""
    from .checks.selfmon import unit_failed_result
    store.begin()
    try:
        ev = apply_results(store, cfg, [unit_failed_result(unit, None)], now)
        enqueue_events(store, cfg, ev, now)
        store.commit()
    except Exception:
        store.rollback()
        raise


def run_tier(cfg: dict, tier: str, *, store: Store | None = None, now: datetime | None = None,
             specs: list[CheckSpec] | None = None, ctx_kwargs: dict | None = None,
             channels: dict | None = None) -> dict:
    """One monitor pass. monitor_runs.status records the outcome honestly:
    SUCCESS / DEGRADED (completed, but crash, skip or delivery failure) / FAILED
    (aborted; re-raised). summary["ok"] is True only for SUCCESS, and the CLI
    exit code is derived from the same status (0 / 1 / 2)."""
    from .clock import utcnow
    now = now or utcnow()
    own_store = store is None
    store = store or Store(cfg["monitor"]["state_db"])
    specs = [s for s in (specs if specs is not None else registry()) if tier in s.tiers]
    ctx = Context(cfg, store, now, tier, **(ctx_kwargs or {}))
    run_id = store.start_run(tier, now, VERSION)
    summary = {"tier": tier, "run_id": run_id, "checks_run": 0, "checks_failed": 0,
               "checks_skipped": 0, "results": 0, "events": 0, "crashed": [], "skipped": []}
    events: list[tuple] = []
    try:
        try:
            for spec in specs:
                last = store.check_run(spec.name)
                if spec.min_interval_minutes and last and last["last_ok_at"] and \
                        now - datetime.fromisoformat(last["last_ok_at"]) < \
                        timedelta(minutes=spec.min_interval_minutes):
                    summary["checks_skipped"] += 1      # not due: not a fault
                    continue
                t0 = time.monotonic()
                err = None
                try:
                    results = spec.fn(ctx)
                except PgUnavailable as e:
                    # PostgreSQL itself is down: infra.postgres reports it (CRITICAL).
                    # Do not pile a check_error incident per SQL check on top of it,
                    # but the check did NOT run: the pass is DEGRADED, never SUCCESS.
                    summary["skipped"].append(spec.name)
                    store.record_check_run(spec.name, now, False, time.monotonic() - t0,
                                           f"postgres unavailable: {e}")
                    continue
                except Exception as e:  # noqa: BLE001
                    err = f"{type(e).__name__}: {e}"
                    log.error("check %s crashed:\n%s", spec.name, traceback.format_exc())
                    results = []
                    summary["checks_failed"] += 1
                    summary["crashed"].append(spec.name)
                summary["checks_run"] += 1
                store.begin()
                try:
                    events += apply_results(store, cfg, results + [_check_error_result(spec, err)],
                                            now)
                    store.record_check_run(spec.name, now, err is None, time.monotonic() - t0, err)
                    store.commit()
                except Exception:
                    store.rollback()
                    raise
                summary["results"] += len(results)
                if ctx._pg is not None:
                    ctx._pg.rollback()
            store.begin()
            for source, (ts, detail) in ctx.source_updates.items():
                store.set_source_status(source, ts, detail, now)
            summary["events"] = enqueue_events(store, cfg, events, now)
            store.prune_results(now)
            store.commit()
        finally:
            ctx.close()
        summary["delivery"] = deliver_pending(store, cfg, now, channels)
        status, reason = run_status(summary)
        store.finish_run(run_id, now, status, summary["checks_run"], summary["checks_failed"],
                         summary["results"], reason, len(summary["skipped"]))
        if status == RUN_SUCCESS:
            close_unit_failure(store, cfg, f"laynes-monitor-{tier}.service", now)
    except Exception as e:
        store.rollback()
        try:
            store.finish_run(run_id, now, RUN_FAILED, summary["checks_run"],
                             summary["checks_failed"], summary["results"],
                             f"{type(e).__name__}: {str(e)[:300]}", len(summary["skipped"]))
        except Exception:  # noqa: BLE001 -- the store itself may be what failed
            log.exception("could not record the failed run")
        if own_store:
            store.close()
        raise
    summary["status"], summary["ok"] = status, status == RUN_SUCCESS
    if own_store:
        store.close()
    return summary
