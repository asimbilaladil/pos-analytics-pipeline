"""Laynes Intelligence data-quality monitor (Phase 1: detect and notify only).

The monitor READS production state and writes only to its own SQLite incident
store (``/var/lib/laynes/monitor/monitor.db`` by default). It never writes a
business table, never re-runs or retries an ingestion job, never repairs data
and never calls a language model.

Layout:
  config.py     settings, readiness windows, notification policy
  model.py      CheckResult / severities / dedup keys
  pg.py         bounded, read-only PostgreSQL sessions
  store.py      monitor-owned incident history (SQLite)
  engine.py     results -> incidents (open / unchanged / escalate / resolve)
  notify.py     dry-run log, Telegram and e-mail adapters + outbox
  checks/       one module per source (revel, hme, r365, chat, mcp, services,
                infra, monitor self-checks)
  cli.py        ``python -m monitoring run --tier ...`` and admin commands
"""

VERSION = "laynes_monitor/1.1.0"
