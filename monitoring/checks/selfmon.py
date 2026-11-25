"""The monitor watching itself.

* heartbeat: every EXPECTED tier (health, data, summary minus
  monitor.disabled_tiers) must have a SUCCESSFUL run within its limit, and its
  systemd timer must be loaded, enabled and active. Run by BOTH the health and
  the data tier, so each watches the other (and both watch the daily summary).
  Missing history is never "healthy":
    - within the install grace (from the store's first ever run): PENDING;
    - after it, a tier that never ran, only ever failed, hangs, or whose timer
      is missing / disabled / stopped is a HIGH incident;
    - a tier in disabled_tiers is ACCEPTED (deliberate, shown, never alerted).
  The heartbeat reads only the monitor's own SQLite store and `systemctl show`,
  so it keeps working while PostgreSQL is down.
* notifications: per channel, a permanent delivery failure (HIGH) or a
  delivery that is still retrying (WARNING). It clears only when a later
  message on that channel is actually SENT.
* unit_failed_result: the incident crash-notify opens when a monitor unit
  fails; the tier's next SUCCESSFUL pass resolves it (engine.close_unit_failure).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from ..model import ACCEPTED, FAIL, HIGH, OK, PENDING, SRC_MONITOR, WARNING, CheckResult

TIMER_OK_FILE_STATES = ("enabled", "enabled-runtime", "static", "alias")


def _minutes(td: timedelta) -> int:
    return int(td.total_seconds() // 60)


def _timer_problem(ctx, timer: str) -> tuple[str | None, dict]:
    try:
        s = ctx.systemctl(timer)
    except Exception as e:  # noqa: BLE001 -- unknown, not a verdict either way
        return None, {"timer": timer, "error": f"{type(e).__name__}: {str(e)[:120]}"}
    ev = {"timer": timer, "LoadState": s.get("LoadState"), "ActiveState": s.get("ActiveState"),
          "UnitFileState": s.get("UnitFileState")}
    if s.get("LoadState") == "not-found":
        return "timer_missing", ev
    if s.get("UnitFileState") and s.get("UnitFileState") not in TIMER_OK_FILE_STATES:
        return "timer_disabled", ev
    if s.get("ActiveState") != "active":
        return "timer_inactive", ev
    return None, ev


def heartbeat(ctx) -> list[CheckResult]:
    m = ctx.cfg["monitor"]
    out = []
    first = ctx.store.first_run_at()
    first_at = datetime.fromisoformat(first) if first else ctx.now
    for tier, max_min in m["heartbeat_max_minutes"].items():
        if tier == ctx.tier:
            continue          # this run IS the heartbeat for its own tier; the other tier checks it
        timer = m["tier_timers"][tier]
        base = dict(source=SRC_MONITOR, check="heartbeat", qualifier=tier,
                    expected=f"a SUCCESSFUL '{tier}' run within {max_min} min; {timer} enabled "
                             f"and active")
        if tier in m["disabled_tiers"]:
            out.append(CheckResult(state=ACCEPTED, title=f"Monitor tier '{tier}' disabled by "
                                                         f"configuration", **base))
            continue
        grace_end = first_at + timedelta(minutes=max(int(m["install_grace_minutes"]), max_min))
        in_grace = ctx.now < grace_end
        last_ok = ctx.store.last_run(tier, ok_only=True)
        last_att = ctx.store.last_attempt(tier)
        timer_issue, tev = _timer_problem(ctx, timer)
        ev = {"timer": tev, "install_grace_until": grace_end.isoformat(timespec="seconds")}
        age = ctx.now - datetime.fromisoformat(last_ok["finished_at"]) if last_ok else None
        if last_ok:
            ev["last_success_at"] = last_ok["finished_at"]
        if last_att:
            ev["last_attempt"] = {"started_at": last_att["started_at"],
                                  "status": last_att["status"],
                                  "error": (last_att["error"] or "")[:200] or None}
        fresh = age is not None and age <= timedelta(minutes=max_min)
        if fresh and not timer_issue:
            out.append(CheckResult(state=OK, title=f"Monitor tier '{tier}' healthy",
                                   actual=f"last success {_minutes(age)} min ago", evidence=ev,
                                   **base))   # OK results carry no fingerprint
            continue
        if in_grace:
            out.append(CheckResult(
                state=PENDING, evidence=ev, **base,
                title=f"Monitor tier '{tier}': install grace until {grace_end:%H:%M} UTC"))
            continue
        out.append(_stale(tier, timer, age, last_att, timer_issue, fresh, max_min, ctx.now, ev,
                          base))
    return out


def _stale(tier, timer, age, last_att, timer_issue, fresh, max_min, now, ev, base) -> CheckResult:
    if fresh and timer_issue:
        category = timer_issue
        actual = (f"{timer} {timer_issue.replace('timer_', '')}: nothing will run this tier "
                  f"again (last success {ev.get('last_success_at')})")
    elif last_att is None:
        category, actual = "never_ran", "no run recorded since the monitor was installed"
    elif last_att["status"] == "RUNNING" and \
            now - datetime.fromisoformat(last_att["started_at"]) > timedelta(minutes=max_min):
        category = "stalled"
        actual = (f"the run started at {last_att['started_at']} never finished "
                  f"(hung or killed)")
    elif timer_issue:
        category, actual = timer_issue, f"{timer}: {timer_issue.replace('timer_', '')}"
    else:
        category = "monitor_stale"
        # Timestamps, not "N min ago": the fingerprint must not change every run.
        actual = ("never succeeded" if age is None else
                  f"last success {ev.get('last_success_at')}")
        if last_att and last_att["status"] in ("DEGRADED", "FAILED"):
            actual += f"; latest run {last_att['status']}: {(last_att['error'] or '')[:160]}"
    return CheckResult(
        state=FAIL, severity=HIGH, category=category,
        title=f"Monitor tier '{tier}' has not completed successfully", actual=actual,
        evidence=ev,
        actions=[f"`systemctl status laynes-monitor-{tier}.service {timer}`; "
                 f"`journalctl -u laynes-monitor-{tier} -n 200`.",
                 "Until fixed, the problems this tier detects are NOT being detected.",
                 "Re-enable or restart the timer only with approval."], **base)


def unit_failed_result(unit: str, err: str | None) -> CheckResult:
    base = dict(source=SRC_MONITOR, check="unit_failed", qualifier=unit,
                expected=f"{unit} exits 0")
    if err is None:
        return CheckResult(state=OK, title=f"{unit} completed successfully", **base)
    return CheckResult(
        state=FAIL, severity=HIGH, category="unit_failed",
        title=f"Monitor unit {unit} failed", actual=err[:300],
        actions=[f"`journalctl -u {unit} -n 200` (exit 1: a check crashed, was skipped, or an "
                 "alert could not be delivered; exit 2: the pass aborted).",
                 "Until fixed, part of the data monitoring is not running."], **base)


def notifications(ctx) -> list[CheckResult]:
    cfg_n = ctx.cfg["notify"]
    max_attempts = int(cfg_n["max_attempts"])
    db = ctx.store.db
    channels = sorted(set(cfg_n["channels"]) | {
        r[0] for r in db.execute("SELECT DISTINCT channel FROM notifications WHERE created_at >= ?",
                                 ((ctx.now - timedelta(days=7)).isoformat(timespec="seconds"),))})
    real = [c for c in cfg_n["channels"] if c != "log"]
    out = []
    for ch in channels:
        base = dict(source=SRC_MONITOR, check="notification_delivery", qualifier=ch,
                    expected=f"messages via '{ch}' delivered")
        last_final = db.execute(
            "SELECT id, status, last_error, last_attempt_at FROM notifications WHERE channel = ? "
            "AND status IN ('SENT','FAILED') ORDER BY id DESC LIMIT 1", (ch,)).fetchone()
        retrying = db.execute(
            "SELECT COUNT(*) AS n, MAX(attempts) AS a, MAX(last_error) AS err, "
            "MIN(next_attempt_at) AS nxt FROM notifications WHERE channel = ? "
            "AND status = 'RETRYING'", (ch,)).fetchone()
        stuck = db.execute(
            "SELECT COUNT(*) FROM notifications WHERE channel = ? AND status = 'QUEUED' "
            "AND created_at < ?", (ch, (ctx.now - timedelta(minutes=30)).isoformat(
                timespec="seconds"))).fetchone()[0]
        failed_n = db.execute("SELECT COUNT(*) FROM notifications WHERE channel = ? AND "
                              "status = 'FAILED'", (ch,)).fetchone()[0]
        ev = {"retrying": retrying["n"], "failed_total": failed_n, "queued_over_30min": stuck}
        if last_final is not None and last_final["status"] == "FAILED":
            ev["last_error"] = (last_final["last_error"] or "")[:200]
            out.append(CheckResult(
                state=FAIL, severity=HIGH, category="delivery_failed",
                title=f"Alert delivery via {ch} failing",
                actual=f"latest message permanently FAILED after {max_attempts} attempts "
                       f"(at {last_final['last_attempt_at']})",
                evidence=ev,
                # Alert through the OTHER channels if there are any; never re-send
                # through the one that is failing (that would only fail again).
                notify=len([c for c in real if c != ch]) > 0,
                actions=[f"Alerts via {ch} are NOT reaching you: watch the dashboard until fixed.",
                         "Check the channel config and the credentials file, and "
                         "`journalctl -u laynes-monitor-data`.",
                         "Resolves on the next message this channel actually delivers."], **base))
        elif retrying["n"]:
            ev["last_error"] = (retrying["err"] or "")[:200]
            out.append(CheckResult(
                state=FAIL, severity=WARNING, category="delivery_retrying",
                title=f"Alert delivery via {ch} retrying",
                actual=f"{retrying['n']} message(s) retrying",
                evidence={**ev, "max_attempt_so_far": retrying["a"], "of": max_attempts,
                          "next_attempt_at": retrying["nxt"]},
                actions=["Transient errors retry with backoff; if it persists the messages "
                         "become FAILED and this escalates to HIGH."], **base))
        elif stuck:
            out.append(CheckResult(
                state=FAIL, severity=HIGH, category="delivery_stuck",
                title=f"Alert delivery via {ch} not being attempted",
                actual=f"{stuck} message(s) queued for over 30 min", evidence=ev,
                actions=["Deliveries run at the end of every monitor pass: check that the "
                         "tiers are running (heartbeat) and `journalctl -u laynes-monitor-*`."],
                **base))
        else:
            out.append(CheckResult(state=OK, title=f"Notifications via {ch} delivered",
                                   evidence=ev, **base))
    return out
