"""Monitor settings.

Defaults below match the production schedules observed on 2026-11-25. A TOML
file (``/etc/laynes/monitor.toml`` or ``$LAYNES_MONITOR_CONFIG``) overrides any
of them; see ``deploy/monitoring/monitor.example.toml``.

Secrets never live in the TOML. Notification credentials are read from the
environment variables NAMED here (the systemd unit loads them from a 0600 env
file), so the config file can be world-readable and committed as an example.

Financial reconciliation thresholds are deliberately NOT configurable here: the
Revel ±0.5% tolerance is imported from chat_sql at run time so the monitor and
AI Chat can never disagree about what "reconciled" means.
"""
from __future__ import annotations

import copy
import os
import tomllib
from datetime import time
from zoneinfo import ZoneInfo

CHICAGO = ZoneInfo("America/Chicago")

DEFAULT_CONFIG_PATH = "/etc/laynes/monitor.toml"

DEFAULTS: dict = {
    "monitor": {
        "state_db": "/var/lib/laynes/monitor/monitor.db",
        "log_file": "/var/lib/laynes/monitor/monitor.log",
        # Code the Revel check imports its reconciliation from (chat_sql).
        "release_dir": "/root/pos-analytics-pipeline-release",
        # A tier whose last SUCCESSFUL run is older than this is itself an incident.
        "heartbeat_max_minutes": {"health": 15, "data": 45, "summary": 1500},
        # How often each tier's timer fires (minutes). Used for staleness on the
        # dashboard: a check whose last success is older than 3 periods is STALE.
        "tier_period_minutes": {"health": 5, "data": 15, "summary": 1440},
        # The systemd timer that drives each tier. The heartbeat checks it is
        # loaded, enabled and active.
        "tier_timers": {"health": "laynes-monitor-health.timer",
                        "data": "laynes-monitor-data.timer",
                        "summary": "laynes-monitor-summary.timer"},
        # Tiers deliberately not run here (reported ACCEPTED, never alerted).
        # Every other tier is EXPECTED: never having run is an incident once the
        # install grace has passed.
        "disabled_tiers": [],
        # Grace after the store's first ever run, so installing the units one by
        # one does not page. A tier is judged after max(grace, its heartbeat limit).
        "install_grace_minutes": 60,
        "check_timeout_seconds": 120,
    },
    "postgres": {
        # Names of environment variables, never the values.
        "host_env": "DB_HOST", "port_env": "DB_PORT", "dbname_env": "DB_NAME",
        "user_env": "MONITOR_DB_USER", "password_env": "MONITOR_DB_PASS",
        # The Revel reconciliation reuses chat_sql.revel_store_breakdown, but on a
        # connection from monitoring/pg.py (this role, same limits) -- see
        # checks/revel.py. DB_RO_USER / DB_RO_PASS are not used by the monitor.
        "statement_timeout_ms": 30000,
        "lock_timeout_ms": 2000,
        "connect_timeout_s": 10,
    },
    "revel": {
        # revel-daily-sync runs 09:00 Chicago and takes 20-25 min; business day
        # D is complete only after that run on D+1.
        "ready_after_local": "09:45",
        "lookback_days": 3,
        # sync_state.last_success_at older than this = a missed nightly sync.
        "sync_max_age_hours": 26,
        # A store-day failing on payment timing (payments dated another day) is
        # not corruption; it is reported, but not pushed as an alert.
        "payment_timing_severity": "WARNING",
        # Share of the delta that open/unpaid tickets must explain to call the
        # mismatch "open tickets" (a store-operations issue) instead of a sync fault.
        "open_ticket_explained_share": 0.9,
        # Stores (establishment ids) excluded from Revel checks, with a reason.
        "accepted_stores": {},
        # {"YYYY-MM-DD": "reason"}: days the stores did not trade (e.g. a holiday).
        # A no-data store-day on such a date is ACCEPTED, never alerted.
        "accepted_closed_dates": {},
    },
    "hme": {
        "morning_ready_local": "06:30",   # 05:30 run + up to ~17 min + randomized delay
        "retry_ready_local": "10:45",     # 10:15 retry + up to ~8 min
        "lookback_days": 7,
        # Checks that settle on their own (HME source lag); before the retry they
        # are "settling", not failures.
        "settling_checks": ["trend_total_cars_matches_pa_total_orders_per_store",
                            "pa_total_orders_vs_outliers_car_departures"],
        # {"YYYY-MM-DD": "reason"} days accepted as permanently unreconciled.
        "accepted_dates": {},
        "accepted_stores": {},
    },
    "r365": {
        # Weeks run Thursday-Wednesday; r365-weekly-ingest runs Thu 20:00 Chicago.
        "week_ready_local": "21:30",
        "lookback_weeks": 3,
        "financial_enabled": True,
        # A closed fiscal period should be refreshed at least once after close
        # within this many days.
        "financial_refresh_grace_days": 10,
        # The R365 source re-syncs postings for ~5 weeks after a period closes; the
        # orchestrator's scheduled mode re-pulls every period overlapping the last
        # 42 days. Inside this window a closed period must keep being refreshed;
        # after it, the last load must postdate the window to count as final.
        "financial_repair_window_days": 42,
        "financial_refresh_max_age_hours": 30,   # == r365_financial_freshness.MAX_REFRESH_AGE_HOURS
        "financial_timer": "r365-financial-ingest.timer",
        "financial_lookback_periods": 2,
    },
    "chat": {
        "error_window_minutes": 60,
        "provider_error_threshold": 3,
        "sql_error_threshold": 3,
        "stuck_job_minutes": 15,
        "trust_block_window_hours": 24,
    },
    "mcp": {
        "health_url": "http://127.0.0.1:8710/healthz",
        "usage_db": "/var/lib/laynes-mcp/mcp.sqlite3",
        "error_window_minutes": 60,
        "error_threshold": 1,
        "auth_failed_threshold": 20,
    },
    "services": {
        # unit -> severity when the unit is failed / inactive.
        "units": {
            "laynes-api": "CRITICAL",
            "laynes-mcp": "HIGH",
            "nginx": "CRITICAL",
            "postgresql": "CRITICAL",
            "laynes-sales-report": "WARNING",
        },
        # Oneshot ingestion jobs: a FAILED last run. Data checks own the
        # business verdict; this says the job itself did not finish cleanly.
        "job_units": {
            "revel-daily-sync": "HIGH",
            "revel-items-selfheal": "WARNING",
            "laynes-loyalty-labor-sync": "WARNING",
            "hme-daily-ingest": "WARNING",          # red on every source-lag morning
            "hme-reconciliation-retry": "HIGH",
            "r365-weekly-ingest": "HIGH",
        },
        "timers": ["revel-daily-sync.timer", "hme-daily-ingest.timer",
                   "hme-reconciliation-retry.timer", "r365-weekly-ingest.timer",
                   "laynes-loyalty-labor-sync.timer", "revel-items-selfheal.timer"],
        "http": [
            {"name": "public_site", "url": "https://intelligence.aygchicken.com/",
             "expect": [200], "severity": "CRITICAL"},
            {"name": "public_api_auth", "url": "https://intelligence.aygchicken.com/api/status",
             "expect": [401], "severity": "CRITICAL"},
        ],
        "http_timeout_s": 10,
        "tls_host": "intelligence.aygchicken.com",
        "tls_warn_days": 14,
    },
    "infra": {
        "disk_paths": ["/"],
        "disk_warn_pct": 85,
        "disk_critical_pct": 93,
        "mem_available_min_mb": 300,
        # No scheduled database backup was found on the host (2026-11-25). Point
        # this at the backup location once one exists.
        "backup_globs": ["/root/deploy-backups/*/*.dump", "/var/backups/laynes/*.dump"],
        "backup_max_age_hours": 26,
        "backup_severity": "HIGH",
        # Optional evidence from future jobs (docs/BACKUP_PROPOSAL.md): JSON status
        # files {"ok": bool, "completed_at": ISO, "dump": path}. Empty = not monitored.
        "offsite_status_glob": "",
        "offsite_max_age_hours": 30,
        "restore_test_status_glob": "",
        "restore_test_max_age_hours": 24 * 8,
    },
    "notify": {
        # "log" writes notifications to the log only. Real delivery needs
        # dry_run = false AND an approved channel.
        "channels": ["log"],
        "dry_run": True,
        "immediate_min_severity": "HIGH",
        "notify_resolutions": True,
        "max_attempts": 5,
        # Exponential backoff between attempts: 60s, 2m, 4m, 8m ... capped at 1h.
        "retry_base_seconds": 60,
        "retry_max_seconds": 3600,
        # A delivering process's lease on a row (longer than any send timeout).
        "lease_seconds": 300,
        "telegram": {"bot_token_env": "MONITOR_TELEGRAM_BOT_TOKEN",
                     "chat_id_env": "MONITOR_TELEGRAM_CHAT_ID",
                     "api_base": "https://api.telegram.org"},
        "email": {"smtp_host": "", "smtp_port": 587, "starttls": True,
                  "user_env": "MONITOR_SMTP_USER", "password_env": "MONITOR_SMTP_PASS",
                  "from_addr": "", "to_addrs": []},
        "dashboard_url": "https://intelligence.aygchicken.com/",
    },
    "summary": {"local_times": ["07:00"]},
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path: str | None = None, overrides: dict | None = None) -> dict:
    path = path or os.environ.get("LAYNES_MONITOR_CONFIG", DEFAULT_CONFIG_PATH)
    cfg = DEFAULTS
    if path and os.path.exists(path):
        with open(path, "rb") as fh:
            cfg = _merge(cfg, tomllib.load(fh))
    if overrides:
        cfg = _merge(cfg, overrides)
    validate(cfg)
    return cfg


def validate(cfg: dict) -> None:
    from .model import SEVERITIES
    for section, key in (("revel", "ready_after_local"), ("hme", "morning_ready_local"),
                         ("hme", "retry_ready_local"), ("r365", "week_ready_local")):
        parse_hhmm(cfg[section][key])
    if cfg["hme"]["morning_ready_local"] >= cfg["hme"]["retry_ready_local"]:
        raise ValueError("hme.morning_ready_local must be before retry_ready_local")
    for unit, sev in {**cfg["services"]["units"], **cfg["services"]["job_units"]}.items():
        if sev not in SEVERITIES:
            raise ValueError(f"services: {unit} has bad severity {sev!r}")
    if cfg["notify"]["immediate_min_severity"] not in SEVERITIES:
        raise ValueError("notify.immediate_min_severity must be a severity")
    unknown = set(cfg["notify"]["channels"]) - {"log", "telegram", "email"}
    if unknown:
        raise ValueError(f"notify.channels: unknown {sorted(unknown)}")
    for t in cfg["summary"]["local_times"]:
        parse_hhmm(t)
    m = cfg["monitor"]
    tiers = set(m["heartbeat_max_minutes"])
    if tiers != {"health", "data", "summary"}:
        raise ValueError("monitor.heartbeat_max_minutes must cover health, data and summary")
    if not tiers <= set(m["tier_period_minutes"]) or not tiers <= set(m["tier_timers"]):
        raise ValueError("monitor.tier_period_minutes / tier_timers must cover every tier")
    if set(m["disabled_tiers"]) - tiers:
        raise ValueError(f"monitor.disabled_tiers: unknown {sorted(set(m['disabled_tiers']) - tiers)}")
    r = cfg["r365"]
    if not (1 <= int(r["financial_refresh_grace_days"]) <= int(r["financial_repair_window_days"]) <= 120):
        raise ValueError("r365: need 1 <= financial_refresh_grace_days <= financial_repair_window_days <= 120")
    if not (1 <= int(r["financial_refresh_max_age_hours"]) <= 24 * 14):
        raise ValueError("r365.financial_refresh_max_age_hours must be 1..336")
    n = cfg["notify"]
    if not (1 <= int(n["max_attempts"]) <= 20):
        raise ValueError("notify.max_attempts must be 1..20")
    if not (1 <= int(n["retry_base_seconds"]) <= int(n["retry_max_seconds"]) <= 86400):
        raise ValueError("notify: need 1 <= retry_base_seconds <= retry_max_seconds <= 86400")


def parse_hhmm(s: str) -> time:
    hh, mm = s.split(":")
    return time(int(hh), int(mm))
