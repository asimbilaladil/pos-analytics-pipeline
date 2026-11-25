"""Services, scheduled jobs, timers, public endpoints and the TLS certificate.

Read-only: `systemctl show` (never start/stop/restart/reset-failed), plain
HTTP GETs that expect the unauthenticated answers (200 for the site, 401 for
the API), and a TLS handshake to read the certificate's expiry.
"""
from __future__ import annotations

import socket
import ssl
import subprocess
from datetime import datetime, timezone

from ..model import CRITICAL, FAIL, HIGH, OK, SRC_INFRA, WARNING, CheckResult

PROPS = ("ActiveState", "SubState", "Result", "ExecMainStatus", "ExecMainExitTimestamp",
         "LoadState", "NextElapseUSecRealtime", "LastTriggerUSec", "UnitFileState")


def systemctl_show(unit: str) -> dict:
    cp = subprocess.run(["systemctl", "show", unit, "--no-pager",
                         "-p", ",".join(PROPS)], capture_output=True, text=True, timeout=15)
    out = {}
    for line in cp.stdout.splitlines():
        k, _, v = line.partition("=")
        out[k] = v
    return out


def units(ctx) -> list[CheckResult]:
    out = []
    for unit, sev in ctx.cfg["services"]["units"].items():
        s = ctx.systemctl(unit)
        base = dict(source=SRC_INFRA, check="service", qualifier=unit, expected="active (running)")
        if s.get("LoadState") == "not-found":
            out.append(CheckResult(state=FAIL, severity=WARNING, category="unit_missing",
                                   title=f"Unit {unit} not installed", actual="not-found", **base))
        elif s.get("ActiveState") != "active":
            out.append(CheckResult(
                state=FAIL, severity=sev, category="service_down",
                title=f"{unit} is {s.get('ActiveState')}",
                actual=f"{s.get('ActiveState')}/{s.get('SubState')} result={s.get('Result')}",
                evidence=s, actions=[f"`systemctl status {unit}`; `journalctl -u {unit} -n 200`.",
                                     "Restart only with approval (laynes-api / laynes-mcp: "
                                     "only with zero active jobs)."], **base))
        else:
            out.append(CheckResult(state=OK, title=f"{unit} active", **base))
    return out


def jobs(ctx) -> list[CheckResult]:
    out = []
    for unit, sev in ctx.cfg["services"]["job_units"].items():
        s = ctx.systemctl(unit)
        base = dict(source=SRC_INFRA, check="job", qualifier=unit,
                    expected="last run finished successfully")
        if s.get("LoadState") == "not-found":
            out.append(CheckResult(state=FAIL, severity=WARNING, category="unit_missing",
                                   title=f"Job {unit} not installed", actual="not-found", **base))
            continue
        failed = s.get("ActiveState") == "failed" or s.get("Result") not in ("success", "", None)
        if failed:
            out.append(CheckResult(
                state=FAIL, severity=sev, category="job_failed",
                title=f"{unit} last run failed",
                actual=f"result={s.get('Result')} exit={s.get('ExecMainStatus')} at "
                       f"{s.get('ExecMainExitTimestamp')}",
                evidence=s,
                actions=[f"`journalctl -u {unit} --since -1d`; read the job's own log.",
                         "The data checks for this source say whether data is actually missing.",
                         "Do not re-run the job from the monitor."], **base))
        else:
            out.append(CheckResult(state=OK, title=f"{unit} last run OK",
                                   actual=f"exit={s.get('ExecMainStatus')}", **base))
    return out


def timers(ctx) -> list[CheckResult]:
    out = []
    for t in ctx.cfg["services"]["timers"]:
        s = ctx.systemctl(t)
        base = dict(source=SRC_INFRA, check="timer", qualifier=t, expected="timer active")
        if s.get("ActiveState") != "active":
            out.append(CheckResult(
                state=FAIL, severity=HIGH, category="timer_inactive",
                title=f"{t} is not active -- its job will not run",
                actual=f"{s.get('ActiveState')} ({s.get('LoadState')})", evidence=s,
                actions=[f"`systemctl status {t}`; `systemctl list-timers`.",
                         "Re-enable only with approval."], **base))
        else:
            out.append(CheckResult(state=OK, title=f"{t} active", **base))
    return out


def http(ctx) -> list[CheckResult]:
    out = []
    timeout = ctx.cfg["services"]["http_timeout_s"]
    for h in ctx.cfg["services"]["http"]:
        code, err = ctx.http(h["url"], timeout)
        base = dict(source=SRC_INFRA, check="http", qualifier=h["name"],
                    expected=f"HTTP {'/'.join(map(str, h['expect']))}")
        if code in h["expect"]:
            out.append(CheckResult(state=OK, title=f"{h['name']} reachable", actual=str(code), **base))
        else:
            out.append(CheckResult(
                state=FAIL, severity=h.get("severity", HIGH), category="endpoint",
                title=f"{h['name']} not answering as expected", actual=err or f"HTTP {code}",
                evidence={"url": h["url"]},
                actions=["Check nginx (`systemctl status nginx`, `nginx -t`) and laynes-api.",
                         "An unexpected 200 on the API would mean authentication is not "
                         "enforced -- treat as a security incident."], **base))
    return out


def cert_expiry(host: str, timeout: float = 10) -> datetime:
    ctx = ssl.create_default_context()
    with socket.create_connection((host, 443), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as s:
            na = s.getpeercert()["notAfter"]
    return datetime.fromtimestamp(ssl.cert_time_to_seconds(na), tz=timezone.utc)


def tls(ctx) -> list[CheckResult]:
    host = ctx.cfg["services"]["tls_host"]
    base = dict(source=SRC_INFRA, check="tls_certificate", qualifier=host,
                expected=f"> {ctx.cfg['services']['tls_warn_days']} days to expiry")
    try:
        exp = ctx.cert_expiry(host)
    except Exception as e:  # noqa: BLE001
        return [CheckResult(state=FAIL, severity=HIGH, category="tls", title="TLS handshake failed",
                            actual=f"{type(e).__name__}: {str(e)[:120]}", **base)]
    days = (exp - ctx.now).days
    if days < 3:
        sev = CRITICAL
    elif days < ctx.cfg["services"]["tls_warn_days"]:
        sev = WARNING
    else:
        return [CheckResult(state=OK, title="TLS certificate valid",
                            actual=f"{days} days left", **base)]
    return [CheckResult(state=FAIL, severity=sev, category="tls",
                        title="TLS certificate expiring", actual=f"{days} days left ({exp:%Y-%m-%d})",
                        actions=["Check `systemctl status certbot.timer` and "
                                 "`certbot renew --dry-run`."], **base)]
