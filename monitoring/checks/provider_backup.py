"""Provider-managed VPS backups (Hetzner Cloud automatic backups).

These are whole-disk images that Hetzner takes and stores. They are NOT local
PostgreSQL dumps, and they are evaluated separately from them.

Read-only by construction: the client issues HTTP GET only, against
  GET /v1/servers/{id}                                  -> server.backup_window (null = disabled)
  GET /v1/images?type=backup&bound_to={id}&sort=created:desc
                                                        -> backup images: status, created, image_size
Use a Hetzner Cloud API token with the **Read** permission (Read & Write is never
needed). Hetzner tokens are scoped to a project, not a server.

The token is read from the environment variable named in config
(`infra.provider_token_env`, default MONITOR_HCLOUD_TOKEN), loaded by systemd
from the 0600 /etc/laynes/monitor.env. It is never logged, stored, or put in an
error message.

Verdicts:
  OK       newest AVAILABLE backup is within provider_max_age_hours
  FAIL     the API positively reports: backups disabled, no available backup, or
           the newest available backup is too old
  UNKNOWN  no token configured, or the API/metadata service could not be read.
           Never reported as "no backups". (After provider_unknown_alert_hours
           of continuous failure to read a CONFIGURED API, a WARNING says the
           status could not be verified -- still not "no backups".)
A provider backup is never "restore verified": only a recorded restore test is.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from ..model import FAIL, HIGH, OK, SRC_INFRA, UNKNOWN, WARNING, CheckResult

USER_AGENT = "laynes-monitor (read-only backup status)"


class ProviderUnavailable(Exception):
    """The provider API could not give an answer. The message is safe to log
    (it never contains the token or response bodies)."""


def _get_json(url: str, token: str | None, timeout: float) -> dict:
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310 (fixed https / link-local)
            body = r.read(2_000_000)
    except urllib.error.HTTPError as e:
        code = None
        try:
            code = (json.loads(e.read(20_000) or b"{}").get("error") or {}).get("code")
        except (ValueError, AttributeError):
            pass
        raise ProviderUnavailable(f"HTTP {e.code}" + (f" ({code})" if code else "")) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ProviderUnavailable(f"unreachable: {type(e).__name__}") from None
    try:
        return json.loads(body)
    except ValueError:
        raise ProviderUnavailable("invalid JSON response") from None


def _server_id(c: dict) -> int:
    sid = str(c.get("provider_server_id") or "auto").strip()
    if sid != "auto":
        return int(sid)
    req = urllib.request.Request(c["provider_metadata_url"], headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=3) as r:          # noqa: S310 (link-local metadata)
            raw = r.read(64).decode().strip()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ProviderUnavailable(f"metadata service unreachable: {type(e).__name__}") from None
    if not raw.isdigit():
        raise ProviderUnavailable("metadata service returned no instance id")
    return int(raw)


def hetzner_status(c: dict, env=os.environ) -> dict:
    """Read-only fetch. Returns {"configured": False} when no token is set, else
    {"configured": True, "server_id", "backup_window", "backups": [image, ...]}.
    Raises ProviderUnavailable when the API cannot be read."""
    token = env.get(c["provider_token_env"]) or ""
    if not token.strip():
        return {"configured": False}
    sid = _server_id(c)
    base, t = c["provider_api_url"].rstrip("/"), float(c["provider_timeout_seconds"])
    server = _get_json(f"{base}/servers/{sid}", token, t).get("server") or {}
    if not server:
        raise ProviderUnavailable("server object missing from response")
    q = urllib.parse.urlencode({"type": "backup", "bound_to": sid, "sort": "created:desc",
                                "per_page": 50})
    images = _get_json(f"{base}/images?{q}", token, t).get("images")
    if not isinstance(images, list):
        raise ProviderUnavailable("images list missing from response")
    return {"configured": True, "server_id": sid, "server_name": server.get("name"),
            "backup_window": server.get("backup_window"),
            "backups": [{"id": i.get("id"), "status": i.get("status"), "created": i.get("created"),
                         "image_size_gb": i.get("image_size"), "bound_to": i.get("bound_to")}
                        for i in images if i.get("type", "backup") == "backup"]}


def _parse(ts) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def evaluate(ctx) -> tuple[list[CheckResult], dict, str]:
    """Returns (results, detail, verdict) with verdict in OK / FAIL / UNKNOWN.
    detail is what the dashboard shows under "provider" (no secrets)."""
    c = ctx.cfg["infra"]
    if not c.get("provider_backup"):
        return [], {"state": "not monitored (infra.provider_backup not set)"}, UNKNOWN
    max_h = c["provider_max_age_hours"]
    base = dict(source=SRC_INFRA, check="provider_backup",
                expected=f"an AVAILABLE {c['provider_backup']} VPS backup newer than {max_h}h "
                         f"(provider-managed; not restore-verified)")
    prev = ctx.previous_source_status("provider_backup") or {}
    prev_detail = prev.get("detail") or {}
    try:
        st = ctx.provider_backup_status(c)
    except ProviderUnavailable as e:
        first = prev_detail.get("unavailable_since") or ctx.now.isoformat(timespec="seconds")
        detail = {"state": "UNKNOWN", "reason": f"provider API unavailable: {e}",
                  "unavailable_since": first,
                  "last_verified_at": prev_detail.get("verified_at") or prev_detail.get("last_verified_at"),
                  "newest_backup_at_last_verification": prev.get("last_success_at"),
                  "restore_verified": False}
        out = [CheckResult(state=UNKNOWN, title="Provider backup status unknown (API unavailable)",
                           actual=str(e), evidence=detail, **base)]
        out.append(_unknown_duration(ctx, c, first, str(e)))
        ctx.source_status("provider_backup", _parse(prev.get("last_success_at")), detail)
        return out, detail, UNKNOWN
    if not st.get("configured"):
        detail = {"state": "UNKNOWN", "reason": f"no API token configured ({c['provider_token_env']})",
                  "restore_verified": False}
        return ([CheckResult(state=UNKNOWN, title="Provider backups not monitored (no API token)",
                             actual="not configured", evidence=detail, **base),
                 _status_ok(c)], detail, UNKNOWN)

    backups = st["backups"]
    avail = [b for b in backups if b["status"] == "available" and _parse(b["created"])]
    avail.sort(key=lambda b: _parse(b["created"]), reverse=True)
    newest = avail[0] if avail else None
    detail = {"state": None, "provider": c["provider_backup"], "server_id": st["server_id"],
              "server_name": st.get("server_name"), "backup_window_utc": st["backup_window"],
              "available_count": len(avail),
              "creating_count": sum(b["status"] == "creating" for b in backups),
              "unavailable_count": sum(b["status"] == "unavailable" for b in backups),
              "newest_available": None, "restore_verified": False,
              "verified_at": ctx.now.isoformat(timespec="seconds"),
              "note": "provider-managed whole-disk image; crash-consistent, not restore-tested"}
    if newest:
        t = _parse(newest["created"])
        detail["newest_available"] = {"id": newest["id"], "created": newest["created"],
                                      "age_hours": round((ctx.now - t).total_seconds() / 3600, 1),
                                      "image_size_gb": newest["image_size_gb"]}
    acts = _actions(c)
    if st["backup_window"] in (None, ""):
        res = CheckResult(state=FAIL, severity=HIGH, category="provider_backups_disabled",
                          title="Provider (VPS) automatic backups are disabled",
                          actual="server.backup_window is null", evidence=detail, actions=acts, **base)
        verdict = FAIL
    elif newest is None:
        res = CheckResult(state=FAIL, severity=HIGH, category="provider_backup_none_available",
                          title="Provider reports no available VPS backup",
                          actual=f"{len(backups)} backup image(s), none available",
                          evidence=detail, actions=acts, **base)
        verdict = FAIL
    elif detail["newest_available"]["age_hours"] > max_h:
        res = CheckResult(state=FAIL, severity=HIGH, category="provider_backup_stale",
                          title="Newest provider VPS backup is too old",
                          actual=f"newest available created {newest['created']}",
                          evidence=detail, actions=acts, **base)
        verdict = FAIL
    elif len(avail) < c["provider_min_available"]:
        # The newest backup is current (so a recent backup exists), but the
        # rolling set is smaller than expected.
        res = CheckResult(state=FAIL, severity=WARNING, category="provider_backup_few",
                          title="Fewer provider VPS backups available than expected",
                          actual=f"{len(avail)} available (expected >= {c['provider_min_available']})",
                          evidence=detail, actions=acts, **base)
        verdict = OK
    else:
        res = CheckResult(state=OK, title="Provider VPS backup current (not restore-verified)",
                          actual=f"{detail['newest_available']['age_hours']}h old, "
                                 f"{len(avail)} available", evidence=detail, **base)
        verdict = OK
    detail["state"] = "OK" if verdict == OK else "FAIL"
    ctx.source_status("provider_backup",
                      _parse(newest["created"]) if newest else None, detail)
    return [res, _status_ok(c)], detail, verdict


def _status_ok(c) -> CheckResult:
    """Clears a 'status could not be verified' warning once the API answers
    (or once no API is configured any more)."""
    return CheckResult(state=OK, source=SRC_INFRA, check="provider_backup_status",
                       title="No prolonged provider-API outage", actual="none",
                       expected=f"provider API readable within {c['provider_unknown_alert_hours']}h")


def _unknown_duration(ctx, c, first_iso, reason) -> CheckResult:
    base = dict(source=SRC_INFRA, check="provider_backup_status",
                expected=f"provider API readable within {c['provider_unknown_alert_hours']}h")
    first = _parse(first_iso) or ctx.now
    hours = (ctx.now - first).total_seconds() / 3600
    if hours < c["provider_unknown_alert_hours"]:
        return CheckResult(state=UNKNOWN, title="Provider backup status unknown",
                           actual=f"unavailable for {hours:.1f}h", **base)
    return CheckResult(state=FAIL, severity=WARNING, category="provider_backup_status_unknown",
                       title="Provider backup status could not be verified",
                       actual=f"API unavailable since {first_iso} ({reason})",
                       evidence={"note": "this does NOT mean backups are missing"},
                       actions=["Check the Hetzner Cloud status page and that the read-only "
                                f"token in {c['provider_token_env']} is still valid.",
                                "Confirm the backups in the Hetzner Cloud Console meanwhile."],
                       **base)


def _actions(c) -> list[str]:
    return ["Hetzner Cloud Console -> server -> Backups: confirm backups are enabled and the "
            "latest one is available.",
            "The monitor only reads backup status (read-only token); it never creates, "
            "deletes or changes backups.",
            "Provider backups are whole-disk images; a database restore from one has not been "
            "tested unless a restore-test status file records it."]
