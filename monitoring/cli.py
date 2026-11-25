"""Command line.

  python -m monitoring run --tier health|data     one monitor pass (timers call this)
  python -m monitoring summary                    daily readiness summary
  python -m monitoring status [--json]            what the dashboard shows
  python -m monitoring ack INCIDENT_ID --by NAME  acknowledge an incident
  python -m monitoring crash-notify UNIT          OnFailure= hook for the monitor units
  python -m monitoring test-notify                one test message per channel

Exit codes for `run` and `summary` are the recorded monitor_runs.status:
  0 = SUCCESS   every due check ran and every delivery attempted succeeded
  1 = DEGRADED  completed, but a check crashed, a check was skipped (PostgreSQL
                unavailable) or a delivery failed / is retrying
  2 = FAILED    the pass itself aborted (or the config is invalid)
Data problems found by the checks are NOT exit failures -- they are incidents.
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


EXIT_CODES = {"SUCCESS": 0, "DEGRADED": 1, "FAILED": 2}


def _summary(cfg: dict, log) -> int:
    """The daily summary, recorded as a 'summary' tier run with an honest status."""
    from .engine import close_unit_failure, run_status
    from .notify import deliver_pending
    from .store import RUN_FAILED, RUN_SUCCESS, Store
    from .summary import send_daily_summary
    store = Store(cfg["monitor"]["state_db"])
    now = utcnow()
    rid = store.start_run("summary", now, VERSION)
    try:
        send_daily_summary(store, cfg, now)
        stats = deliver_pending(store, cfg, now)
        status, reason = run_status({"crashed": [], "skipped": [], "delivery": stats})
        store.finish_run(rid, utcnow(), status, 1, 0, 0, reason)
        if status == RUN_SUCCESS:
            close_unit_failure(store, cfg, "laynes-monitor-summary.service", now)
    except Exception as e:  # noqa: BLE001
        log.exception("daily summary failed")
        store.rollback()
        try:
            store.finish_run(rid, utcnow(), RUN_FAILED, 1, 1, 0, f"{type(e).__name__}: {e}"[:300])
        except Exception:  # noqa: BLE001
            log.exception("could not record the failed summary run")
        return 2
    finally:
        store.close()
    return EXIT_CODES[status]


def _crash_notify(cfg: dict, unit: str, log) -> int:
    """OnFailure= hook: open (or keep) ONE monitor:unit_failed incident per unit.

    Deduplicated like every incident: a unit failing every 5 minutes alerts once,
    and the tier's next SUCCESSFUL pass resolves it. Best effort: never fails.
    """
    from .checks.selfmon import unit_failed_result
    from .engine import apply_results, enqueue_events
    from .notify import deliver_pending
    from .store import Store
    now = utcnow()
    try:
        store = Store(cfg["monitor"]["state_db"])
    except Exception:  # noqa: BLE001 -- e.g. disk full: the journal is all we have
        log.exception("crash-notify: monitor store unavailable; %s failed", unit)
        return 0
    try:
        tier = unit.removeprefix("laynes-monitor-").removesuffix(".service")
        last = store.last_attempt(tier)
        err = f"{unit} exited non-zero" + (
            f"; latest run {last['status']}: {(last['error'] or '')[:200]}" if last else "")
        store.begin()
        ev = apply_results(store, cfg, [unit_failed_result(unit, err)], now)
        enqueue_events(store, cfg, ev, now)
        store.commit()
        deliver_pending(store, cfg, now)
    except Exception:  # noqa: BLE001
        store.rollback()
        log.exception("crash-notify could not record/deliver for %s", unit)
    finally:
        store.close()
    return 0


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
        log.info("tier=%s status=%s %s", args.tier, out["status"],
                 json.dumps({k: v for k, v in out.items() if k not in ("ok", "status")},
                            default=str))
        return EXIT_CODES[out["status"]]

    if args.cmd == "summary":
        return _summary(cfg, log)
    if args.cmd == "crash-notify":
        return _crash_notify(cfg, args.unit, log)

    now = utcnow()
    if args.cmd == "status":
        from .summary import daily_summary_text, overview
        try:
            store = Store.open_readonly(cfg["monitor"]["state_db"])   # a status read never writes
        except Exception as e:  # noqa: BLE001
            print(f"monitor store unavailable: {type(e).__name__}: {e}")
            return 2
        if args.json:
            print(json.dumps(overview(store, now, cfg), default=str, indent=2))
        else:
            print(daily_summary_text(store, now, cfg=cfg))
        return 0
    store = Store(cfg["monitor"]["state_db"])
    if args.cmd == "ack":
        store.begin()
        ok = store.acknowledge(args.incident_id, now, args.by, args.note)
        store.commit()
        print("acknowledged" if ok else "not found or not OPEN")
        return 0 if ok else 1
    if args.cmd == "test-notify":
        from .notify import deliver_pending
        body = ("Laynes Intelligence — TEST\n\nThis is a test notification from the data "
                "monitor. No action needed.")
        store.begin()
        for ch in cfg["notify"]["channels"]:
            store.enqueue(now, "TEST", None, [], ch, body)
        store.commit()
        stats = deliver_pending(store, cfg, now)
        print(json.dumps({k: v for k, v in stats.items()}, default=str))
        return 0 if not (stats["failed"] or stats["retrying"]) else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
