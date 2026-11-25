# Data-quality monitoring (Phase 1)

Phase 1 is for **detection and notification only**. The monitor does not:

- repair data or write business tables;
- retry or re-run ingestion jobs;
- start, stop or restart services;
- call a language model.

It reads production state, keeps its own incident history, and tells you:

- which data is failing, and where (source, store, business date);
- why (expected vs actual, category, evidence);
- whether the problem is still active.

## Architecture

```
systemd timers (proposed, not installed)
  laynes-monitor-health   every 5 min ─┐
  laynes-monitor-data     every 15 min ├─> python -m monitoring ...
  laynes-monitor-summary  07:00 Chicago┘        │
                                                 ├─ checks/*  (read-only)
                                                 │    PostgreSQL: read-only, bounded session
                                                 │    systemctl show, HTTP GET, TLS, disk, /proc
                                                 │    MCP usage log (private copy)
                                                 ├─ engine: results -> incidents (dedup)
                                                 ├─ store: /var/lib/laynes/monitor/monitor.db (SQLite)
                                                 └─ notify: outbox -> log (dry-run) | telegram | email
laynes-api  /api/admin/monitoring/*  (super_admin) -> reads monitor.db only
frontend    Admin menu -> "Data monitoring"
```

The monitor runs on its own, not through AI Chat or MCP:

- It is a separate process with separate units.
- Its state lives in its own SQLite file, so a PostgreSQL outage is still
  recorded and reported.
- The dashboard reads only that file; opening the page costs the business
  database nothing.

The monitor reuses one piece of AI Chat code: `chat_sql.revel_store_breakdown`,
imported read-only from the release checkout. That keeps the monitor and AI Chat
from disagreeing about whether a Revel store-day reconciles.

## Data sources and checks

| Source | Signal (authoritative) | Check | When a day is judged (Chicago) |
|---|---|---|---|
| Revel POS | `sync_state` (orders_v2 / payments_v2 per store) | `revel.freshness`: last success > 26h, or status not success | every 15 min |
| Revel POS | `chat_sql.revel_store_breakdown(D)`: orders vs payments, ±0.5% | `revel.reconciliation` per store and network, last 3 days | D+1 09:45 (after the 09:00 sync); hourly |
| HME | `hme_ingest_run` (network runs), `v_hme_store_day_trust_llm`, `hme_store_ingest_result` | `hme.days`: day reconciliation plus the 11 VERIFIED stores, last 7 days | settling after D+1 06:30; final after D+1 10:45 (post-retry) |
| R365 ops | `v_r365_completeness_llm`, `r365_store_daily`, `r365_ingest_run` | `r365.weekly`: week and per-store 7-day coverage, last 3 weeks | Thu 21:30 for the week ending Wed |
| R365 financial | `ayg_business_periods`, `r365_financial_ingest_run` | `r365.financial`: closed period loaded after close and reconciled | period end + 10 days; 6-hourly |
| AI Chat | `chat_jobs`, `chat_query_log` (status / error columns only) | `chat.stuck_jobs`, `chat.errors` (failed jobs, provider and SQL errors, trust-gate refusals) | 5 / 15 min |
| MCP | `/healthz`, `usage_log` outcomes | `mcp.health`, `mcp.tool_errors` (errors, auth-failure bursts) | 5 / 15 min |
| Infrastructure | `systemctl show`, public HTTPS, TLS, disk, memory, backups | `services.*`, `infra.*` | 5 / 15 min; TLS 12-hourly |
| Monitor | `monitor_runs`, the outbox | `monitor.heartbeat`, `monitor.notification_delivery`, per-check crash incidents | 5 min |

### Source settling vs accepted exclusions vs genuine failures

| State | Meaning | Incident? | Alert? |
|---|---|---|---|
| `PENDING` | Inside the source's readiness window (Revel before 09:45; HME Trend lag before the 10:15 retry; open fiscal period) | no; an existing one is left as is | no |
| `ACCEPTED` | Configured exclusion (store not open yet, accepted HME date, holiday closure) | resolves any open one | no |
| `FAIL` / WARNING | Real, but not corruption: payment timing, open tickets, chat trust-gate refusals, R365 period not refreshed | yes | dashboard and daily summary only |
| `FAIL` / HIGH, CRITICAL | Genuine failure: unexplained mismatch, missing or stale data, persistent HME mismatch after the retry, service down | yes | immediately |

How HME settles through the day:

1. **Morning run, settling checks only:** a Trend/Outliers mismatch is
   `PENDING`. No alert.
2. **Retry reconciles it:** the result is `OK`. No incident is ever opened.
3. **Still failing after the retry:** it becomes HIGH and alerts once.
4. **Non-settling check failing in the morning** (goal D, row sums): it is a
   WARNING until the retry window, then it is escalated to HIGH.

## Incidents

Monitor-owned SQLite tables:

| Table | What it holds |
|---|---|
| `incidents` | id `INC-YYYYMMDD-XXXXXX`; `dedup_key`; source, check, store_id/name, business_date, qualifier; severity CRITICAL / HIGH / WARNING; status OPEN / ACKNOWLEDGED / RESOLVED; title, category, expected, actual, difference; evidence (JSON); actions (manual steps); first_detected_at, last_checked_at, last_changed_at, check_count; ack and resolution fields |
| `incident_events` | Timeline: OPENED, UPDATED, ESCALATED, DEESCALATED, ACKNOWLEDGED, RESOLVED |
| `latest_results` | Latest state of every evaluated key, including OK, PENDING and ACCEPTED (the per-store grid) |
| `source_status` | Latest successful sync per source |
| `monitor_runs` | Each tier run (the heartbeat) |
| `check_runs` | Per-check timing and errors |
| `notifications` | The outbox: PENDING, SENT, DRY_RUN or FAILED, with attempts and the error |

### Deduplication

`dedup_key` = `source:check:store:business_date[:qualifier]`. It is built from
what the problem is, never from what was measured. A partial unique index allows
one active incident per key.

| Re-check result | Effect |
|---|---|
| Unchanged | Only `last_checked_at` and `check_count` change. No alert. |
| Material change, same severity | UPDATED. No alert. |
| Severity rose | ESCALATED. Alerted. |
| OK | RESOLVED. A resolution notice is sent only if the incident had been alerted. |
| Same key fails again after resolution | A new incident; the history is kept. |

Rules that keep the monitor from hiding or inventing problems:

- A key that a check did not evaluate in a run is never touched.
- A crashed check opens a `monitor:check_error` incident and freezes, rather
  than resolves, the incidents it owns.
- If PostgreSQL is down, SQL checks are skipped, not failed. `infra.postgres`
  reports CRITICAL.

A business date that ages out of a check's lookback (for example HME, 7 days)
is no longer re-checked. Its open incident stays open, with an old
`last_checked_at`, because nothing proved the data was fixed. Acknowledge it
with a note once you have decided what to do with that day. To keep checking
those days, widen the lookback.

Notifications are grouped by kind, source, check, date and title. For example,
nine HME stores failing on one day send one message that lists all nine.

## Notifications

- **Default:** channel `log`, with `dry_run = true`. Messages go to the journal
  and the outbox, nothing leaves the host, and channel code is never called.
- **Telegram and e-mail adapters:** credentials come from environment variables
  named in the config. Transport errors are scrubbed of the token or password.
- **Push threshold:** HIGH and above go out immediately. WARNING appears in the
  07:00 daily summary and on the dashboard.
- **Retries:** failed deliveries are retried on each run up to 5 attempts, then
  marked FAILED. That opens a `monitor:notification_delivery` incident and makes
  the unit exit non-zero.
- **Message format:**

```
Laynes Intelligence — HIGH

Store: LCF Missouri City
Source: Revel POS
Date: 2026-11-22
Issue: Sales reconciliation mismatch (unexplained)
Difference: +5.42%
Status: OPEN
Incident: INC-20261125-1A2B3C

Recommended action: Compare orders vs payments for the store-day ...
```

Messages are built only from monitor-generated fields. They never contain
credentials, customer data, chat questions or answers, or raw rows.

## Dashboard

The dashboard is reached from the admin menu: **Data monitoring**. It shows:

- source health cards: state counts, latest successful sync, open incidents;
- monitor run ages;
- a per-store grid for Revel, HME and R365;
- unresolved reconciliation differences;
- unresolved incidents, history and the notification outbox, including dry-run
  messages;
- incident detail: expected vs actual, evidence, manual investigation steps,
  timeline, and an Acknowledge button with a note.

Access is enforced on the server:

- every `/api/admin/monitoring/*` route requires a session (401 otherwise) and
  role `super_admin` (403 otherwise);
- the page is part of the authenticated app, behind the existing login.

## Running it

```
python -m monitoring run --tier health|data    # one pass; exit 0 ok / 1 check crash or delivery failure / 2 fatal
python -m monitoring summary                   # daily readiness summary
python -m monitoring status [--json]
python -m monitoring ack INC-... --by you@example.com --note "..."
```

Deployment, rollback and the notification go-live plan are in
`deploy/monitoring/README.md`.
