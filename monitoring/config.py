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
        # A tier whose last successful run is older than this is itself an incident.
        "heartbeat_max_minutes": {"health": 15, "data": 45, "summary": 1500},
        "check_timeout_seconds": 120,
    },
    "postgres": {
        # Names of environment variables, never the values.
        "host_env": "DB_HOST", "port_env": "DB_PORT", "dbname_env": "DB_NAME",
        "user_env": "MONITOR_DB_USER", "password_env": "MONITOR_DB_PASS",
        # The Revel reconciliation reuses chat_sql, which connects as the
        # read-only analytics role (DB_RO_USER / DB_RO_PASS).
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
    },
    "notify": {
        # "log" writes notifications to the log only. Real delivery needs
        # dry_run = false AND an approved channel.
        "channels": ["log"],
        "dry_run": True,
        "immediate_min_severity": "HIGH",
        "notify_resolutions": True,
        "max_attempts": 5,
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


def parse_hhmm(s: str) -> time:
    hh, mm = s.split(":")
    return time(int(hh), int(mm))
