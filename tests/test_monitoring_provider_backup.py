#!/usr/bin/env python3
"""Provider-managed (Hetzner Cloud) VPS backups vs local PostgreSQL dumps.

Run:  venv/bin/python tests/test_monitoring_provider_backup.py      (exit 0 = pass)

Pure: no PostgreSQL, no systemd, no Internet. The real Hetzner client is
exercised only against a fake HTTP server on 127.0.0.1; everything else uses an
injected status function. No real token is used (a fixed fake string).
"""
from __future__ import annotations

import http.server
import json
import os
import re
import sys
import threading
import urllib.parse
from datetime import datetime, timedelta, timezone

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

from monitoring import config as mcfg  # noqa: E402
from monitoring import engine, summary  # noqa: E402
from monitoring.checks import infra, provider_backup  # noqa: E402
from monitoring.checks.provider_backup import ProviderUnavailable  # noqa: E402
from monitoring.model import FAIL, HIGH, OK, UNKNOWN, WARNING  # noqa: E402
from monitoring.store import Store  # noqa: E402

results: list[tuple[str, bool]] = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {str(detail)[:400]}" if not ok and detail else ""))


# The user's console evidence (2026-11-25): 7 rolling backups, newest 2026-11-24 22:29 UTC, 10.65 GB.
NOW = datetime(2026, 11, 25, 9, 0, tzinfo=timezone.utc)
FAKE_TOKEN = "hcloud-FAKE-token-for-tests-0123456789"
SID = 164337898


def images(n=7, newest=datetime(2026, 11, 24, 22, 29, tzinfo=timezone.utc), status="available"):
    return [{"id": 1000 + i, "status": status, "created": (newest - timedelta(days=i)).isoformat(),
             "image_size_gb": 10.65, "bound_to": SID} for i in range(n)]


def status(backups=None, window="22-02", configured=True):
    if not configured:
        return {"configured": False}
    return {"configured": True, "server_id": SID, "server_name": "revel-system",
            "backup_window": window, "backups": images() if backups is None else backups}


def cfg(**infra_over):
    base = {"notify": {"channels": ["log"], "dry_run": True}, "monitor": {"state_db": ":memory:"},
            "infra": {"provider_backup": "hetzner", **infra_over}}
    return mcfg._merge(mcfg.DEFAULTS, base)


def ctx_for(st_fn, *, c=None, store=None, now=NOW, local_age_h=None):
    if local_age_h is None:
        newest = lambda g: (None, None)  # noqa: E731
    else:
        newest = lambda g: ("/root/deploy-backups/x/laynes-full.dump", now - timedelta(hours=local_age_h))  # noqa: E731
    return engine.Context(c or cfg(), store or Store(":memory:"), now, "data",
                          provider_backup_status=st_fn, newest_backup=newest,
                          backup_format=lambda p: "pg_dump custom archive")


def by_check(res):
    return {r.check: r for r in res}


def persist(ctx):
    for source, (ts, detail) in ctx.source_updates.items():
        ctx.store.set_source_status(source, ts, detail, ctx.now)


def raising(msg):
    def f(c):
        raise ProviderUnavailable(msg)
    return f


def all_text(res):
    return json.dumps([r.__dict__ for r in res], default=str)


# ═══════════════════════════════════════════════════════════════════════════
print("1. provider backups current (the console evidence) -> OK, separate from local dumps")
ctx = ctx_for(lambda c: status(), local_age_h=24 * 8)
res = by_check(infra.backups(ctx))
p = res["provider_backup"]
check("provider_backup OK with 7 available, newest 10.5h old", p.state == OK
      and p.evidence["available_count"] == 7 and p.evidence["newest_available"]["age_hours"] == 10.5, p)
check("provider result says not restore-verified", "not restore-verified" in p.title
      and p.evidence["restore_verified"] is False)
loc = res["backup_freshness"]
check("stale LOCAL dump with a current provider backup -> WARNING stale_local_dump (not HIGH)",
      loc.state == FAIL and loc.severity == WARNING and loc.category == "stale_local_dump", loc)
check("local-dump title names the provider backup as current, never 'no backup'",
      "provider VPS backup is current" in loc.title and "No local database backup" not in loc.title
      and "no backup" not in loc.title.lower())
det = ctx.source_updates["backup"][1]
check("dashboard detail separates provider and local", det["provider"]["state"] == "OK"
      and det["local"]["path"].endswith(".dump") and det["restore_verified"] is False)
check("provider status recorded with newest backup time",
      ctx.source_updates["provider_backup"][0].startswith("2026-11-24T22:29"))
ctx = ctx_for(lambda c: status(), local_age_h=None)
loc = by_check(infra.backups(ctx))["backup_freshness"]
check("NO local dump with a current provider backup -> WARNING no_local_dump",
      loc.category == "no_local_dump" and loc.severity == WARNING, loc)

print("2. provider definitively bad -> HIGH, local stays HIGH")
ctx = ctx_for(lambda c: status(window=None), local_age_h=24 * 8)
res = by_check(infra.backups(ctx))
check("backup_window null -> HIGH provider_backups_disabled",
      res["provider_backup"].category == "provider_backups_disabled" and res["provider_backup"].severity == HIGH)
check("local stale with provider not current -> HIGH", res["backup_freshness"].severity == HIGH
      and "NOT current" in res["backup_freshness"].title)
ctx = ctx_for(lambda c: status(backups=images(2, status="creating") + images(1, status="unavailable")))
res = by_check(infra.backups(ctx))
check("only creating/unavailable images -> HIGH provider_backup_none_available",
      res["provider_backup"].category == "provider_backup_none_available")
ctx = ctx_for(lambda c: status(backups=[]))
check("empty backup list (window set) -> HIGH none_available",
      by_check(infra.backups(ctx))["provider_backup"].category == "provider_backup_none_available")
old = images(7, newest=NOW - timedelta(hours=40))
ctx = ctx_for(lambda c: status(backups=old), local_age_h=24 * 8)
res = by_check(infra.backups(ctx))
check("newest available 40h old (> 30h) -> HIGH provider_backup_stale",
      res["provider_backup"].category == "provider_backup_stale" and res["provider_backup"].severity == HIGH)
check("...and the stale local dump stays HIGH", res["backup_freshness"].severity == HIGH)
mixed = images(1, newest=NOW - timedelta(hours=1), status="creating") + images(6, newest=NOW - timedelta(hours=12))
ctx = ctx_for(lambda c: status(backups=mixed))
p = by_check(infra.backups(ctx))["provider_backup"]
check("a newer 'creating' image is ignored; the newest AVAILABLE one decides (OK)",
      p.state == OK and p.evidence["creating_count"] == 1 and p.evidence["newest_available"]["age_hours"] == 12.0, p)
ctx = ctx_for(lambda c: status(backups=images(3)), c=cfg(provider_min_available=7), local_age_h=24 * 8)
res = by_check(infra.backups(ctx))
check("fewer than provider_min_available (3 < 7), newest fresh -> WARNING provider_backup_few",
      res["provider_backup"].category == "provider_backup_few" and res["provider_backup"].severity == WARNING)
check("...a fresh backup still exists, so the local dump is WARNING", res["backup_freshness"].severity == WARNING)

print("3. API unavailable / not configured -> UNKNOWN, never 'no backups'")
for msg in ("HTTP 401 (unauthorized)", "HTTP 403 (forbidden)", "HTTP 429 (rate_limit_exceeded)",
            "HTTP 503", "unreachable: URLError", "invalid JSON response"):
    ctx = ctx_for(raising(msg), local_age_h=24 * 8)
    res = by_check(infra.backups(ctx))
    p = res["provider_backup"]
    check(f"{msg} -> provider UNKNOWN (no FAIL, no 'none available')",
          p.state == UNKNOWN and p.category is None and "none" not in p.title.lower())
check("UNKNOWN provider: local stale stays HIGH but says status unknown, not 'no backups'",
      res["backup_freshness"].severity == HIGH and "status unknown" in res["backup_freshness"].title)
ctx = ctx_for(lambda c: status(configured=False), local_age_h=2)
res = by_check(infra.backups(ctx))
check("hetzner selected but no token -> UNKNOWN 'not monitored (no API token)'",
      res["provider_backup"].state == UNKNOWN and "no API token" in res["provider_backup"].title)
ctx = ctx_for(lambda c: status(), c=cfg(provider_backup=""), local_age_h=24 * 8)
res = infra.backups(ctx)
check("provider_backup = '' -> no provider result at all (not monitored)",
      "provider_backup" not in by_check(res) and ctx.source_updates["backup"][1]["provider"]["state"].startswith("not monitored"))
check("...local stale is HIGH and the action text says unknown != none",
      by_check(res)["backup_freshness"].severity == HIGH
      and any("does not mean no backups exist" in a for a in by_check(res)["backup_freshness"].actions))

print("4. engine: UNKNOWN never resolves or opens; prolonged UNKNOWN -> WARNING, not 'missing'")
c = cfg()
st = Store(":memory:")
ctx = ctx_for(lambda c_: status(window=None), c=c, store=st)
engine.apply_results(st, c, infra.backups(ctx), NOW)
persist(ctx)
key = by_check(infra.backups(ctx_for(lambda c_: status(window=None), c=c, store=st)))["provider_backup"].key
check("provider disabled opened a HIGH incident", st.active_incident(key)["severity"] == HIGH)
t1 = NOW + timedelta(hours=1)
ctx = ctx_for(raising("HTTP 503"), c=c, store=st, now=t1)
ev = engine.apply_results(st, c, infra.backups(ctx), t1)
persist(ctx)
check("API then unavailable: the open incident is left untouched (not resolved)",
      st.active_incident(key) is not None and st.active_incident(key)["status"] == "OPEN")
st2 = Store(":memory:")
first = NOW
for h in (0, 6, 23):
    t = first + timedelta(hours=h)
    ctx = ctx_for(raising("unreachable: URLError"), c=c, store=st2, now=t)
    res = by_check(infra.backups(ctx))
    engine.apply_results(st2, c, list(res.values()), t)
    persist(ctx)
check("unavailable < 24h: status check UNKNOWN, no incident",
      res["provider_backup_status"].state == UNKNOWN and not st2.active_incidents(check="provider_backup_status"))
check("unavailable_since persists across runs",
      ctx.source_updates["provider_backup"][1]["unavailable_since"] == first.isoformat(timespec="seconds"))
t = first + timedelta(hours=25)
ctx = ctx_for(raising("unreachable: URLError"), c=c, store=st2, now=t)
res = by_check(infra.backups(ctx))
engine.apply_results(st2, c, list(res.values()), t)
persist(ctx)
s = res["provider_backup_status"]
check("unavailable >= 24h -> WARNING provider_backup_status_unknown",
      s.state == FAIL and s.severity == WARNING and s.category == "provider_backup_status_unknown")
check("...worded as 'could not be verified' and 'does NOT mean backups are missing'",
      "could not be verified" in s.title and "NOT mean backups are missing" in s.evidence["note"])
t = first + timedelta(hours=26)
ctx = ctx_for(lambda c_: status(), c=c, store=st2, now=t)
engine.apply_results(st2, c, infra.backups(ctx), t)
persist(ctx)
check("API readable again -> status warning RESOLVED, unavailable_since cleared",
      not st2.active_incidents(check="provider_backup_status")
      and "unavailable_since" not in ctx.source_updates["provider_backup"][1])

print("5. local-dump incident: provider evidence changes wording/severity, never resolves it")
st3 = Store(":memory:")
ctx = ctx_for(raising("HTTP 503"), c=c, store=st3, local_age_h=30)
engine.apply_results(st3, c, infra.backups(ctx), NOW)
lkey = by_check(infra.backups(ctx_for(raising("x"), c=c, store=Store(":memory:"), local_age_h=30)))["backup_freshness"].key
inc = st3.active_incident(lkey)
check("stale local dump, provider unknown -> HIGH incident", inc and inc["severity"] == HIGH)
t = NOW + timedelta(hours=1)
ctx = ctx_for(lambda c_: status(), c=c, store=st3, now=t, local_age_h=31)
ev = engine.apply_results(st3, c, infra.backups(ctx), t)
inc = st3.active_incident(lkey)
check("provider verified current -> same incident DEESCALATED to WARNING, still OPEN, no alert",
      inc and inc["severity"] == WARNING and inc["status"] == "OPEN" and not [e for e in ev if e[0] != "RESOLVED"], ev)
check("...only a fresh LOCAL dump resolves it",
      st3.active_incident(lkey) is not None)
ctx = ctx_for(lambda c_: status(), c=c, store=st3, now=t, local_age_h=1)
engine.apply_results(st3, c, infra.backups(ctx), t)
check("fresh local dump -> resolved", st3.active_incident(lkey) is None)

print("6. overview: provider UNKNOWN makes infrastructure UNKNOWN, never HEALTHY")
st4 = Store(":memory:")
ctx = ctx_for(raising("HTTP 503"), c=c, store=st4, local_age_h=1)
engine.apply_results(st4, c, infra.backups(ctx), NOW)
o = summary.overview(st4, NOW, c)
infra_row = next(s for s in o["sources"] if s["source"] == "infrastructure")
check("infrastructure data_state UNKNOWN while the provider API is unreadable",
      infra_row["data_state"] == "UNKNOWN", infra_row)
st5 = Store(":memory:")
ctx = ctx_for(lambda c_: status(), c=c, store=st5, local_age_h=1)
engine.apply_results(st5, c, infra.backups(ctx), NOW)
infra_row = next(s for s in summary.overview(st5, NOW, c)["sources"] if s["source"] == "infrastructure")
check("provider OK + fresh local dump -> infrastructure data HEALTHY", infra_row["data_state"] == "HEALTHY", infra_row)

print("7. never RESTORE VERIFIED without a restore test")
texts = []
for fn in (lambda c_: status(), lambda c_: status(window=None), raising("HTTP 503"),
           lambda c_: status(configured=False)):
    ctx = ctx_for(fn, local_age_h=1)
    r = infra.backups(ctx)
    texts.append(all_text(r))
    d = ctx.source_updates["backup"][1]
    check("no 'newest_verified_backup' and restore_verified False without a restore-test file",
          "newest_verified_backup" not in d and d["restore_verified"] is False
          and d["provider"].get("restore_verified", False) is False)
check("no title or evidence claims 'restore verified' / 'recoverable'",
      not any(("restore verified" in t.lower().replace("not restore-verified", "").replace("restore_verified", ""))
              or "recoverable" in t.lower() for t in texts))

print("8. real Hetzner client against a local fake API (GET only; token never leaked)")
seen = []


class FakeHetzner(http.server.BaseHTTPRequestHandler):
    mode = "ok"

    def log_message(self, *a):
        pass

    def _send(self, code, obj, raw=None):
        body = raw if raw is not None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        u = urllib.parse.urlparse(self.path)
        seen.append(("GET", u.path, dict(urllib.parse.parse_qsl(u.query)),
                     self.headers.get("Authorization")))
        if u.path == "/hetzner/v1/metadata/instance-id":
            return self._send(200, None, raw=str(SID).encode())
        if self.mode == "401":
            return self._send(401, {"error": {"code": "unauthorized", "message": f"bad token {FAKE_TOKEN}"}})
        if self.mode == "badjson":
            return self._send(200, None, raw=b"<html>")
        if u.path == f"/v1/servers/{SID}":
            return self._send(200, {"server": {"id": SID, "name": "revel-system", "backup_window": "22-02"}})
        if u.path == "/v1/images":
            return self._send(200, {"images": [{"id": 1, "type": "backup", "status": "available",
                                                "created": "2026-11-24T22:29:00+00:00",
                                                "image_size": 10.65, "bound_to": SID}],
                                    "meta": {"pagination": {"page": 1}}})
        return self._send(404, {"error": {"code": "not_found"}})

    def do_POST(self):  # noqa: N802
        seen.append(("POST", self.path, None, None))
        self._send(405, {})
    do_PUT = do_DELETE = do_PATCH = do_POST


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeHetzner)
threading.Thread(target=srv.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{srv.server_address[1]}"
cc = {**cfg()["infra"], "provider_api_url": f"{url}/v1",
      "provider_metadata_url": f"{url}/hetzner/v1/metadata/instance-id", "provider_timeout_seconds": 3}
env = {"MONITOR_HCLOUD_TOKEN": FAKE_TOKEN}
out = provider_backup.hetzner_status(cc, env)
check("server id discovered from the metadata service ('auto')", out["server_id"] == SID)
check("parsed: backup_window and one available 10.65 GB backup",
      out["backup_window"] == "22-02" and out["backups"][0]["status"] == "available"
      and out["backups"][0]["image_size_gb"] == 10.65)
img = [s for s in seen if s[1] == "/v1/images"][0]
check("images query: type=backup, bound_to=<server>, sort=created:desc",
      img[2].get("type") == "backup" and img[2].get("bound_to") == str(SID) and img[2].get("sort") == "created:desc")
check("bearer token sent to the API only, not to the metadata service",
      all(s[3] == f"Bearer {FAKE_TOKEN}" for s in seen if s[1].startswith("/v1/"))
      and all(s[3] is None for s in seen if "metadata" in s[1]))
check("only GET requests were made", {s[0] for s in seen} == {"GET"})
check("no token -> configured False and NO request made",
      provider_backup.hetzner_status(cc, {}) == {"configured": False} and len(seen) == 3)
FakeHetzner.mode = "401"
try:
    provider_backup.hetzner_status({**cc, "provider_server_id": str(SID)}, env)
    err = None
except ProviderUnavailable as e:
    err = str(e)
check("401 -> ProviderUnavailable('HTTP 401 (unauthorized)')", err == "HTTP 401 (unauthorized)", err)
check("error message never contains the token (even when the API echoes it)", FAKE_TOKEN not in (err or ""))
FakeHetzner.mode = "badjson"
try:
    provider_backup.hetzner_status({**cc, "provider_server_id": str(SID)}, env)
    err = None
except ProviderUnavailable as e:
    err = str(e)
check("non-JSON body -> ProviderUnavailable", err == "invalid JSON response", err)
srv.shutdown()
try:
    provider_backup.hetzner_status({**cc, "provider_server_id": str(SID), "provider_timeout_seconds": 1}, env)
    err = None
except ProviderUnavailable as e:
    err = str(e)
check("server gone -> ProviderUnavailable('unreachable: ...')", err and err.startswith("unreachable"), err)
ctx = ctx_for(lambda c_: provider_backup.hetzner_status(cc, env))   # real client, dead server
r = by_check(infra.backups(ctx))
check("dead API through the check -> UNKNOWN; token absent from every result",
      r["provider_backup"].state == UNKNOWN and FAKE_TOKEN not in all_text(list(r.values()))
      and FAKE_TOKEN not in json.dumps(ctx.source_updates, default=str))

print("9. config validation and static safety")
for bad, label in (({"provider_backup": "aws"}, "unknown provider"),
                   ({"provider_server_id": "abc"}, "non-numeric server id"),
                   ({"provider_api_url": "http://api.hetzner.cloud/v1"}, "non-https API URL"),
                   ({"provider_token_env": "MY TOKEN=x"}, "token_env must be a NAME, not a value"),
                   ({"provider_max_age_hours": 0}, "zero max age"),
                   ({"local_dump_severity_when_provider_ok": "LOW"}, "bad severity")):
    try:
        mcfg.validate(cfg(**bad))
        ok = False
    except ValueError:
        ok = True
    check(f"config rejects {label}", ok)
mcfg.validate(cfg())
check("defaults validate; provider backups off by default",
      mcfg.DEFAULTS["infra"]["provider_backup"] == "" and mcfg.DEFAULTS["infra"]["provider_token_env"] == "MONITOR_HCLOUD_TOKEN")
src = open(os.path.join(ROOT, "monitoring", "checks", "provider_backup.py")).read()
check("client source issues GET only (no POST/PUT/DELETE/PATCH, no actions endpoints)",
      'method="GET"' in src and not any(m in src for m in ('"POST"', '"PUT"', '"DELETE"', '"PATCH"', "/actions")))
check("client source has no subprocess, file writes or logging of the token",
      not any(x in src for x in ("subprocess", "log.", "logging", "print("))
      and not re.search(r"(?<![A-Za-z_.])open\(", src))
ex = open(os.path.join(ROOT, "deploy", "monitoring", "monitor.env.example")).read()
check("monitor.env.example names MONITOR_HCLOUD_TOKEN without a value",
      "MONITOR_HCLOUD_TOKEN=" in ex and all(ln.strip().endswith("=") for ln in ex.splitlines()
                                            if "MONITOR_HCLOUD_TOKEN=" in ln))

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed" + (f"; FAILED: {failed}" if failed else ""))
sys.exit(1 if failed else 0)
