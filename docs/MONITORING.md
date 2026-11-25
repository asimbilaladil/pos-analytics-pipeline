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

That function normally opens its own connection (`chat_sql._ro_conn`, the
analytics role, with no connect or lock timeout). In the monitor's process only,
`checks/revel.py: monitor_connection` points `_ro_conn` at
`monitoring.pg.connect` for the duration of each call:

- the same SQL and verdict code runs on a monitor session (role
  `laynes_monitor`);
- the session is read-only on the server and verified before the first query;
- it is bounded by connect_timeout 10 s, lock_timeout 2 s, an
  idle-in-transaction timeout of 60 s, and statement_timeout (chat_sql lowers it
  to its own 15 s);
- it is opened and closed per call.

`chat_sql` is not modified, and the ±0.5% tolerance is untouched.

`laynes-api` imports the check registry only for metadata (names, tiers and
sources, to compute dashboard states). It never calls `engine.run_tier` or a
check, so the swap cannot happen in AI Chat's process. A test asserts this. The cost is 1 call per lookback day (3) per hour, about
7 s each, plus one small open-ticket query per failing store.

## Data sources and checks

| Source | Signal (authoritative) | Check | When a day is judged (Chicago) |
|---|---|---|---|
| Revel POS | `sync_state` (orders_v2 / payments_v2 per store) | `revel.freshness`: last success > 26h, or status not success | every 15 min |
| Revel POS | `chat_sql.revel_store_breakdown(D)`: orders vs payments, ±0.5% | `revel.reconciliation` per store and network, last 3 days | D+1 09:45 (after the 09:00 sync); hourly |
| HME | `hme_ingest_run` (network runs), `v_hme_store_day_trust_llm`, `hme_store_ingest_result` | `hme.days`: day reconciliation plus the 11 VERIFIED stores, last 7 days | settling after D+1 06:30; final after D+1 10:45 (post-retry) |
| R365 ops | `v_r365_completeness_llm`, `r365_store_daily`, `r365_ingest_run` | `r365.weekly`: week and per-store 7-day coverage, last 3 weeks | Thu 21:30 for the week ending Wed |
| R365 financial | `ayg_business_periods`, `r365_financial_ingest_run`, `systemctl show r365-financial-ingest.timer` | `r365.financial`: per closed period, **our copy** (`period_coverage`: never loaded / loaded before close / `refresh_overdue` inside the source's 42-day repair window / `not_refreshed_after_repair_window` / reconciliation failed / final) and, separately, **source freshness** (`period_source_sync`: the source's own sync flag at the last load). Reports whether the scheduled financial ingest is installed | period end + 10 days; refresh at most 72 h old inside the repair window; 6-hourly |
| AI Chat | `chat_jobs`, `chat_query_log` (status / error columns only) | `chat.stuck_jobs`, `chat.errors` (failed jobs, provider and SQL errors, trust-gate refusals) | 5 / 15 min |
| MCP | `/healthz`, `usage_log` outcomes | `mcp.health`, `mcp.tool_errors` (errors, auth-failure bursts) | 5 / 15 min |
| Infrastructure | `systemctl show`, public HTTPS, TLS, disk, memory | `services.*`, `infra.*` | 5 / 15 min; TLS 12-hourly |
| Backups | newest dump file (mtime, size, pg_dump header); optional offsite and restore-test status files | `infra.backups`: **freshness only**, never "recoverable" without a recorded restore test | hourly |
| Monitor | `monitor_runs` (SUCCESS only), the monitor timers, the outbox | `monitor.heartbeat` (in both tiers), `monitor.notification_delivery` per channel, per-check crash and per-unit failure incidents | 5 / 15 min |

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

## Monitor execution status

Each tier run is a `monitor_runs` row. Its `status` follows the CLI exit code,
and the dashboard shows the same value:

| status | exit | Meaning |
|---|---|---|
| `SUCCESS` | 0 | Every due check ran, and every delivery attempted in this run succeeded. |
| `DEGRADED` | 1 | The pass completed with at least one fault: a check crashed, a SQL check was skipped (PostgreSQL unavailable), or a delivery failed or is retrying. |
| `FAILED` | 2 | The pass itself aborted (store error, bad config). |
| `RUNNING` | — | In progress; if it never finishes, the run hung or was killed. |

Rules:

- `ok = 1` only for `SUCCESS`. `last_run(ok_only=True)` and the heartbeat count
  `SUCCESS` runs only.
- Data-quality failures are incidents, not monitor faults. A pass that finds 20
  failing stores is still `SUCCESS`.
- A check that is not yet due (`min_interval_minutes`) is not counted as a fault.
- Crash-notify (`OnFailure=`) opens one `monitor:unit_failed:<unit>` incident
  per unit. It is deduplicated like any other incident and resolved by that
  tier's next `SUCCESS`.

## Watching the monitor itself

`monitor.heartbeat` runs in both the health tier and the data tier. Each tier
checks the others, and both check the daily summary. It reads only the SQLite
store and `systemctl show`, so it keeps working while PostgreSQL is down.

| Situation | Result |
|---|---|
| Inside the install grace (60 min, or the tier's own limit if larger, from the store's first run) | `PENDING`, no alert |
| Tier never ran after the grace | HIGH `never_ran` |
| No `SUCCESS` within the limit (health 15 min, data 45 min, summary 25 h) | HIGH `monitor_stale`, citing the latest run's status and error |
| A run started more than the limit ago and never finished | HIGH `stalled` |
| Timer missing, disabled or inactive (even if the last run was recent) | HIGH `timer_missing` / `timer_disabled` / `timer_inactive` |
| Tier listed in `monitor.disabled_tiers` | `ACCEPTED` (deliberate; shown, never alerted) |
| Recent `SUCCESS` and the timer is enabled and active | `OK` |

A `DEGRADED` or `FAILED` run never clears a heartbeat incident; only a `SUCCESS`
run does.

## Operational states (dashboard and API)

Every known source is always listed. Having no evidence is never reported as
healthy.

| State | Meaning |
|---|---|
| `HEALTHY` | Fresh successful checks and nothing failing |
| `PENDING` | Only in-window results (the source is not due yet) |
| `DEGRADED` | WARNING incidents, or the latest attempt of a check crashed or was skipped (its incidents frozen) |
| `FAILED` | An open HIGH or CRITICAL incident |
| `UNKNOWN` | The monitor has never completed a check of this source |
| `STALE` | A check's last success is older than 3 of its intervals (data checks: 45 min; Revel reconciliation: 3 h; R365 financial: 18 h) |

Each source row has three states:

- `data_state`: the health of the source data;
- `monitoring_state`: whether the monitor's checks of it are running;
- `state`: the combined value. FAILED wins; otherwise UNKNOWN or STALE override
  any data verdict.

Each source row also reports `last_successful_check_at`,
`oldest_successful_check_at` and `last_attempted_check_at`. `checks[]` gives the
same per check. `monitor` gives the per-tier state, the last success, the last
attempt with its status and error, and the outbox counts.

## Incidents

Monitor-owned SQLite tables:

| Table | What it holds |
|---|---|
| `incidents` | id `INC-YYYYMMDD-XXXXXX`; `dedup_key`; source, check, store_id/name, business_date, qualifier; severity CRITICAL / HIGH / WARNING; status OPEN / ACKNOWLEDGED / RESOLVED; title, category, expected, actual, difference; evidence (JSON); actions (manual steps); first_detected_at, last_checked_at, last_changed_at, check_count; ack and resolution fields |
| `incident_events` | Timeline: OPENED, UPDATED, ESCALATED, DEESCALATED, ACKNOWLEDGED, RESOLVED |
| `latest_results` | Latest state of every evaluated key, including OK, PENDING and ACCEPTED (the per-store grid) |
| `source_status` | Latest successful sync per source |
| `monitor_runs` | Each tier run: status RUNNING / SUCCESS / DEGRADED / FAILED (the heartbeat reads SUCCESS only) |
| `check_runs` | Per-check last attempt, last success, last error, duration |
| `notifications` | The outbox: QUEUED, RETRYING, SENT, DRY_RUN or FAILED, with attempts, next_attempt_at, last_attempt_at, the delivery lease and the last error |

Schema version: `PRAGMA user_version = 2`. The monitor migrates a v1 store in
place; a v1 run marked "ok" but with an error becomes `DEGRADED`, never
`SUCCESS`. The dashboard refuses a store whose version it does not know.

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
- **Delivery lifecycle:**

  | From | Event | To |
  |---|---|---|
  | `QUEUED` | send succeeds | `SENT` |
  | `QUEUED` | dry-run, or the log channel | `DRY_RUN` (`send()` is never called) |
  | `QUEUED` / `RETRYING` | send fails, attempts remain | `RETRYING`, `next_attempt_at` = now + backoff |
  | `RETRYING` | send fails, attempts exhausted | `FAILED` (permanent) |
  | any | unknown channel | `FAILED` at once (a config error is not transient) |

  - Backoff is 60 s × 2ⁿ⁻¹, capped at 1 h: 1, 2, 4 and 8 min between the 5
    attempts. A row is never attempted before its `next_attempt_at`.
  - The state is durable in SQLite. A restart continues where it stopped.
  - Delivery is leased. A process claims due rows with one atomic UPDATE (5 min
    lease) and may record an outcome only while it holds the lease. Concurrent
    tiers, the summary and crash-notify therefore never send the same row twice.
    A sender that dies mid-send loses the lease and the row becomes due again: at
    least once, never silently dropped.
  - Telegram counts as sent only on HTTP 200 with `ok: true`. E-mail counts as
    sent only if SMTP accepted every recipient. Anything else is a failure.
  - Any failed or retrying attempt makes the run `DEGRADED` (exit 1).
  - `monitor:notification_delivery:<channel>` reports a permanent failure
    (HIGH), retries in progress (WARNING), or messages queued for over 30 min
    with no attempt (HIGH).
    - It clears only when a later message on that channel is actually `SENT`.
    - It alerts through the other channels when one exists, never through the
      failing one.
- **Detecting failures from outside:** the monitor cannot reliably report its own
  total failure. The design leaves that to an external dead-man check, for
  example n8n, that polls the read-only status API (`docs/MONITORING_API.md`). It
  alerts when the API is unreachable, `monitor.state` is not `HEALTHY`, or a
  tier's `last_success_at` is too old. Until then, systemd `OnFailure=` is the
  local path.
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

The dashboard is reached from the admin menu: **Data monitoring**. From top to
bottom (layout and rules: `docs/MONITORING_DASHBOARD.md`):

- **Summary.**
  - Overall state, with **business-data** health and **monitoring** health shown
    separately. The overall state is the worse of the two, so stale or missing
    checks are never shown as HEALTHY.
  - Unresolved incident and issue counts, split into new and acknowledged.
  - Affected locations.
  - Last successful monitor run.
  - Revel, HME and R365 freshness.
- **Data sources: loaded vs verified.** For each source:
  - last sync;
  - expected by now: the date due under the readiness schedule, with its results or
    "not evaluated yet";
  - latest evaluated date or period, which is not proof of ingestion;
  - latest date or period with a full verdict, with its counts;
  - latest fully verified date or period;
  - the monitor's last check;
  - whether a scheduled retry is pending.
- **Store health.** Each store's Revel, HME and R365 state:
  - HEALTHY, PENDING, DEGRADED, FAILED, UNKNOWN or STALE;
  - **N/A** only where the source's own store mapping excludes the store; a mapped
    store with no evidence, or any store while the mapping is not yet known, is
    UNKNOWN or STALE;
  - an unresolved incident on any date takes precedence;
  - clicking a store filters the incidents.
- **Incidents.**
  - Grouped by source; related incidents shown as one expandable issue. Example:
    the HME network reconciliation failure for a day plus its store-days.
  - Filters: status, source, severity, store, business date, and search.
    - Counts show matching records only.
    - A network-level parent shown for context is labelled "context" and
      counted separately.
  - Priority sort puts unresolved CRITICAL and HIGH first.
  - Notification outbox, including dry-run messages.
- **Detail.**
  - A plain-English summary, location and business date.
  - Expected vs actual, and the difference.
  - Likely cause, only for categories derived from recorded evidence.
  - Recommended investigation steps, first detected and last checked times, and
    a timeline.
  - Technical evidence, collapsed by default.
  - Acknowledge, with a note.

Grouping is presentation only: every incident keeps its own ID, status and
history, and acknowledging never changes a verdict or resolves anything.

Access is enforced on the server:

- every `/api/admin/monitoring/*` route requires a session (401 otherwise) and
  role `super_admin` (403 otherwise);
- the page is part of the authenticated app, behind the existing login.

Storage access:

- The store is `/var/lib/laynes/monitor/monitor.db`: mode 0640 in a 0750
  directory, owner root. The -wal and -shm files take the database file's mode,
  and the units set `UMask=0027`.
- Dashboard GETs open it with SQLite `mode=ro`: no schema creation, migration or
  PRAGMA writes. A test checks that the file is byte-identical after the GETs.
- Acknowledge opens it read-write but cannot create or migrate anything, and runs
  only `Store.acknowledge`.
- A missing, locked, corrupt or wrong-version store returns 503, never an empty
  all-clear.
- The page says explicitly when "no unresolved incidents" is not an all-clear
  because the monitor itself is not `HEALTHY`.
- API reference: `docs/MONITORING_API.md`.

## Running it

```
python -m monitoring run --tier health|data    # one pass; exit 0 SUCCESS / 1 DEGRADED / 2 FAILED
python -m monitoring summary                   # daily readiness summary (same exit codes)
python -m monitoring status [--json]
python -m monitoring ack INC-... --by you@example.com --note "..."
```

Deployment, rollback and the notification go-live plan are in
`deploy/monitoring/README.md`.
