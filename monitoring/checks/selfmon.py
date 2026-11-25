"""The monitor watching itself.

* heartbeat: each tier's last SUCCESSFUL run must be recent. Run by the
  5-minute health tier, so a dead 15-minute data tier (crash loop, disabled
  timer, hung query) is reported within minutes. The health tier itself is
  covered by systemd: its unit has OnFailure= the crash notifier, and its timer
  is listed in services.timers once installed.
* notifications: deliveries that exhausted their retries.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from ..model import FAIL, HIGH, OK, SRC_MONITOR, CheckResult


def heartbeat(ctx) -> list[CheckResult]:
    out = []
    for tier, max_min in ctx.cfg["monitor"]["heartbeat_max_minutes"].items():
        if tier == ctx.tier:
            continue          # this run IS the heartbeat for its own tier
        last = ctx.store.last_run(tier, ok_only=True)
        base = dict(source=SRC_MONITOR, check="heartbeat", qualifier=tier,
                    expected=f"successful '{tier}' run within {max_min} min")
        if last is None:
            if ctx.store.last_run(tier) is None:
                continue      # tier never ran (not yet installed): nothing to compare
            age = None
        else:
            age = ctx.now - datetime.fromisoformat(last["finished_at"])
        if age is None or age > timedelta(minutes=max_min):
            out.append(CheckResult(
                state=FAIL, severity=HIGH, category="monitor_stale",
                title=f"Monitor tier '{tier}' has not completed successfully",
                actual="never succeeded" if age is None else f"last success {int(age.total_seconds() // 60)} min ago",
                actions=[f"`systemctl status laynes-monitor-{tier}.service "
                         f"laynes-monitor-{tier}.timer`; `journalctl -u laynes-monitor-{tier}`.",
                         "Until fixed, data problems in this tier are NOT being detected."], **base))
        else:
            out.append(CheckResult(state=OK, title=f"Monitor tier '{tier}' healthy",
                                   actual=f"{int(age.total_seconds() // 60)} min ago", **base))
    return out


def notifications(ctx) -> list[CheckResult]:
    max_attempts = int(ctx.cfg["notify"]["max_attempts"])
    failed = ctx.store.db.execute(
        "SELECT COUNT(*) AS n, MAX(last_error) AS err FROM notifications "
        "WHERE status = 'FAILED' AND created_at >= ?",
        ((ctx.now - timedelta(days=1)).isoformat(timespec="seconds"),)).fetchone()
    retrying = ctx.store.db.execute(
        "SELECT COUNT(*) AS n FROM notifications WHERE status='PENDING' AND attempts > 0").fetchone()
    base = dict(source=SRC_MONITOR, check="notification_delivery",
                expected="all notifications delivered")
    if failed["n"]:
        return [CheckResult(
            state=FAIL, severity=HIGH, category="delivery_failed",
            title="Alert delivery failing", actual=f"{failed['n']} undelivered in 24h "
                                                   f"(after {max_attempts} attempts)",
            evidence={"last_error": (failed["err"] or "")[:200], "retrying": retrying["n"]},
            actions=["Alerts are NOT reaching you: check the dashboard directly until fixed.",
                     "Check the channel config and credentials file, and "
                     "`journalctl -u laynes-monitor-data`."],
            notify=False, **base)]
    return [CheckResult(state=OK, title="Notifications delivered",
                        evidence={"retrying": retrying["n"]}, **base)]
