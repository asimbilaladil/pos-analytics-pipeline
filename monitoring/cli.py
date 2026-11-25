"""Command line.

  python -m monitoring run --tier health|data     one monitor pass (timers call this)
  python -m monitoring summary                    daily readiness summary
  python -m monitoring status [--json]            what the dashboard shows
  python -m monitoring ack INCIDENT_ID --by NAME  acknowledge an incident
  python -m monitoring crash-notify UNIT          OnFailure= hook for the monitor units
  python -m monitoring test-notify                one test message per channel

Exit codes for `run`: 0 = completed; 1 = completed but a check crashed or an
alert could not be delivered; 2 = the pass itself failed. Data problems found
by the checks are NOT exit failures -- they are incidents.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from . import VERSION, config
from .clock import utcnow


def _setup_logging(cfg: dict | None):
    handlers = [logging.StreamHandler(sys.stderr)]
    path = (cfg or {}).get("monitor", {}).get("log_file")
    if path:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            handlers.append(logging.FileHandler(path))
        except OSError:
            pass
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="monitoring", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None)
    ap.add_argument("--state-db", default=None, help="override monitor.state_db")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--tier", choices=("health", "data"), required=True)
    sub.add_parser("summary")
    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")
    a = sub.add_parser("ack")
    a.add_argument("incident_id")
    a.add_argument("--by", required=True)
    a.add_argument("--note", default=None)
    c = sub.add_parser("crash-notify")
    c.add_argument("unit")
    sub.add_parser("test-notify")
    args = ap.parse_args(argv)

    try:
        cfg = config.load(args.config, {"monitor": {"state_db": args.state_db}}
                          if args.state_db else None)
    except Exception as e:  # noqa: BLE001
        _setup_logging(None)
        logging.getLogger("laynes.monitor").error("bad monitor config: %s", e)
        if args.cmd == "crash-notify":
            return 0
        return 2
    _setup_logging(cfg)
    log = logging.getLogger("laynes.monitor")
    from .store import Store

    if args.cmd == "run":
        from .engine import run_tier
        try:
            out = run_tier(cfg, args.tier)
        except Exception:  # noqa: BLE001
            log.exception("monitor tier %s failed", args.tier)
            return 2
        log.info("tier=%s ok=%s %s", args.tier, out["ok"],
                 json.dumps({k: v for k, v in out.items() if k != "ok"}, default=str))
        return 0 if out["ok"] else 1

    store = Store(cfg["monitor"]["state_db"])
    now = utcnow()
    if args.cmd == "summary":
        from .notify import deliver_pending
        from .summary import send_daily_summary
        rid = store.start_run("summary", now, VERSION)
        send_daily_summary(store, cfg, now)
        stats = deliver_pending(store, cfg, now)
        store.finish_run(rid, utcnow(), True, 0, 0, 0,
                         None if not stats["failed"] else f"delivery_failed={stats['failed']}")
        return 0 if not stats["failed"] else 1
    if args.cmd == "status":
        from .summary import daily_summary_text, overview
        if args.json:
            print(json.dumps(overview(store, now), default=str, indent=2))
        else:
            print(daily_summary_text(store, now))
        return 0
    if args.cmd == "ack":
        store.begin()
        ok = store.acknowledge(args.incident_id, now, args.by, args.note)
        store.commit()
        print("acknowledged" if ok else "not found or not OPEN")
        return 0 if ok else 1
    if args.cmd in ("crash-notify", "test-notify"):
        from .notify import deliver_pending
        if args.cmd == "crash-notify":
            body = (f"Laynes Intelligence — HIGH\n\nSource: Monitor\nIssue: monitor unit "
                    f"{args.unit} failed\nStatus: OPEN\n\nRecommended action: "
                    f"`journalctl -u {args.unit} -n 100`. Until fixed, part of the data "
                    f"monitoring is not running.")
            kind = "MONITOR"
        else:
            body = ("Laynes Intelligence — TEST\n\nThis is a test notification from the data "
                    "monitor. No action needed.")
            kind = "TEST"
        try:
            store.begin()
            for ch in cfg["notify"]["channels"]:
                store.enqueue(now, kind, "HIGH" if kind == "MONITOR" else None, [], ch, body)
            store.commit()
            deliver_pending(store, cfg, now)
        except Exception:  # noqa: BLE001
            log.exception("%s could not record/deliver", args.cmd)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
