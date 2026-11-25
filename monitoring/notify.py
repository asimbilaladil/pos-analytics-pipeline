"""Notification formatting and delivery.

Channels: "log" (always dry-run), "telegram", "email". Real delivery happens
ONLY when notify.dry_run is false; otherwise every channel writes the exact
message it would have sent to the log and marks it DRY_RUN, without opening a
network connection.

Messages are built only from incident fields the monitor itself produced
(store, source, date, title, difference, status, first action). They never
contain credentials, customer data, chat questions/answers or raw SQL rows.
Errors are scrubbed of the bot token before they are stored or logged.
"""
from __future__ import annotations

import json
import logging
import os
import smtplib
import ssl
import urllib.request
from email.message import EmailMessage

from .model import SOURCE_LABELS

log = logging.getLogger("laynes.monitor")

MAX_LEN = 3500          # Telegram's hard limit is 4096


class DeliveryError(RuntimeError):
    pass


# ── formatting ──────────────────────────────────────────────────────────────
def _src(source: str) -> str:
    return SOURCE_LABELS.get(source, source)


def format_group(kind: str, incidents: list[dict], dashboard_url: str | None = None) -> str:
    """One message for a group of incidents sharing kind/source/check/date/title."""
    first = incidents[0]
    status = {"OPENED": "OPEN", "ESCALATED": "OPEN (escalated)",
              "RESOLVED": "RESOLVED"}.get(kind, kind)
    head = "RESOLVED" if kind == "RESOLVED" else first["severity"]
    lines = [f"Laynes Intelligence — {head}"
             + (f" ({len(incidents)} items)" if len(incidents) > 1 else ""), ""]
    stores = [i["store_name"] or f"store {i['store_id']}" for i in incidents if i.get("store_id")]
    if len(incidents) == 1 and stores:
        lines.append(f"Store: {stores[0]}")
    elif stores:
        lines.append(f"Stores: {', '.join(stores)}")
    elif first.get("qualifier"):
        lines.append(f"Item: {first['qualifier']}")
    lines.append(f"Source: {_src(first['source'])}")
    if first.get("business_date"):
        lines.append(f"Date: {first['business_date']}")
    lines.append(f"Issue: {first['title']}")
    if len(incidents) == 1 and first.get("difference"):
        lines.append(f"Difference: {first['difference']}")
    elif len(incidents) > 1:
        diffs = [f"{i['store_name'] or i.get('qualifier') or '-'}: {i['difference']}"
                 for i in incidents if i.get("difference")]
        if diffs:
            lines.append("Differences: " + "; ".join(diffs))
    lines.append(f"Status: {status}")
    ids = [i["id"] for i in incidents]
    lines.append(("Incident: " if len(ids) == 1 else "Incidents: ") + ", ".join(ids))
    if kind == "RESOLVED":
        lines += ["", "The issue cleared on the latest check."]
    else:
        actions = first.get("actions") or []
        if isinstance(actions, str):
            actions = json.loads(actions)
        if actions:
            lines += ["", f"Recommended action: {actions[0]}"]
    if dashboard_url:
        lines += ["", f"Dashboard: {dashboard_url}"]
    return _clip("\n".join(lines))


def _clip(s: str) -> str:
    return s if len(s) <= MAX_LEN else s[:MAX_LEN - 20] + "\n…(truncated)"


# ── channels ────────────────────────────────────────────────────────────────
class Channel:
    name = "base"

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def send(self, body: str, subject: str) -> None:
        raise NotImplementedError


class LogChannel(Channel):
    name = "log"

    def send(self, body, subject):
        log.warning("[notification:log] %s\n%s", subject, body)


class TelegramChannel(Channel):
    name = "telegram"

    def _creds(self):
        t = self.cfg["notify"]["telegram"]
        token, chat = os.environ.get(t["bot_token_env"]), os.environ.get(t["chat_id_env"])
        if not token or not chat:
            raise DeliveryError(f"telegram not configured ({t['bot_token_env']} / "
                                f"{t['chat_id_env']} unset)")
        return token, chat

    def send(self, body, subject):
        token, chat = self._creds()
        url = f"{self.cfg['notify']['telegram']['api_base']}/bot{token}/sendMessage"
        data = json.dumps({"chat_id": chat, "text": body,
                           "disable_web_page_preview": True}).encode()
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                payload = json.loads(r.read() or b"{}")
                if not payload.get("ok", False):
                    raise DeliveryError(f"telegram rejected message: "
                                        f"{payload.get('description', '')[:120]}")
        except DeliveryError:
            raise
        except Exception as e:  # noqa: BLE001
            raise DeliveryError(scrub(f"{type(e).__name__}: {e}", token)) from None


class EmailChannel(Channel):
    name = "email"

    def send(self, body, subject):
        e = self.cfg["notify"]["email"]
        if not e["smtp_host"] or not e["from_addr"] or not e["to_addrs"]:
            raise DeliveryError("email not configured (smtp_host / from_addr / to_addrs)")
        user, pw = os.environ.get(e["user_env"]), os.environ.get(e["password_env"])
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = e["from_addr"]
        msg["To"] = ", ".join(e["to_addrs"])
        msg.set_content(body)
        try:
            with smtplib.SMTP(e["smtp_host"], int(e["smtp_port"]), timeout=20) as s:
                if e["starttls"]:
                    s.starttls(context=ssl.create_default_context())
                if user:
                    s.login(user, pw or "")
                s.send_message(msg)
        except Exception as ex:  # noqa: BLE001
            raise DeliveryError(scrub(f"{type(ex).__name__}: {ex}", pw)) from None


CHANNELS = {"log": LogChannel, "telegram": TelegramChannel, "email": EmailChannel}


def scrub(text: str, *secrets_: str | None) -> str:
    for s in secrets_:
        if s:
            text = text.replace(s, "<redacted>")
    return text[:300]


def subject_for(body: str) -> str:
    first = body.splitlines()[0] if body else "Laynes Intelligence"
    issue = next((l[7:] for l in body.splitlines() if l.startswith("Issue: ")), "")
    return f"{first}: {issue}"[:150] if issue else first


def deliver_pending(store, cfg: dict, now, channels: dict | None = None) -> dict:
    """Send every PENDING outbox row. Returns counts. Never raises."""
    channels = channels or {name: cls(cfg) for name, cls in CHANNELS.items()}
    dry = bool(cfg["notify"]["dry_run"])
    max_attempts = int(cfg["notify"]["max_attempts"])
    stats = {"sent": 0, "dry_run": 0, "failed": 0, "retrying": 0}
    for n in store.pending_notifications(max_attempts):
        ch = channels.get(n["channel"])
        subject = subject_for(n["body"])
        if ch is None:
            store.mark_notification(n["id"], "FAILED", now, f"unknown channel {n['channel']}")
            stats["failed"] += 1
            continue
        if dry or n["channel"] == "log":
            log.warning("[notification:%s DRY-RUN] %s\n%s", n["channel"], subject, n["body"])
            store.mark_notification(n["id"], "DRY_RUN", now)
            stats["dry_run"] += 1
            continue
        try:
            ch.send(n["body"], subject)
        except Exception as e:  # noqa: BLE001
            err = str(e)[:300]
            if n["attempts"] + 1 >= max_attempts:
                store.mark_notification(n["id"], "FAILED", now, err)
                stats["failed"] += 1
            else:
                store.bump_attempt(n["id"], err)
                stats["retrying"] += 1
            log.error("notification %s via %s failed: %s", n["id"], n["channel"], err)
            continue
        store.mark_notification(n["id"], "SENT", now)
        stats["sent"] += 1
    return stats
