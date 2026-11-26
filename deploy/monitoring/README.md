# Deploying the data monitor

Every step needs explicit approval. Nothing here was run in production.

Order:

1. merge;
2. read-only role (optional but recommended);
3. dry-run units;
4. dashboard (API restart plus frontend);
5. live notifications.

Each stage can be rolled back on its own.

## 0. Preconditions

- PR merged.
- The release checkout moved to the merge commit:
  `git -C /root/pos-analytics-pipeline-release checkout --detach <sha>`.
- Moving the checkout restarts nothing. The running API keeps its loaded code
  until stage 3.

## 1. Read-only role (recommended; a database change, so approval required)

- `proposed_monitor_role.sql` creates `laynes_monitor` with:
  - SELECT only on the relations the checks read, including the four that
    `chat_sql.revel_store_breakdown` reads (`orders_v2`, `v_orders_classified`,
    `v_payments_daily_v2`, `v_labor_daily_context`);
    - the Revel reconciliation now runs on this role;
    - the views run with their owner's rights (checked in `pg_class`
      2026-11-25), so a grant on the view is enough;
  - column-level access to `chat_jobs` and `chat_query_log`, so no
    question/answer text;
  - forced read-only sessions, statement and lock timeouts; CONNECTION LIMIT 4.
- `tests/test_monitoring_isolated_db.py` applies this file's GRANT and ALTER
  ROLE statements verbatim, then runs the whole data tier on that role. A
  missing grant fails the test.
- Rollback: `DROP OWNED BY laynes_monitor; DROP ROLE laynes_monitor;`.

If this stage is declined, set `MONITOR_DB_USER/PASS` to the application role
instead. The monitor still forces `default_transaction_read_only=on`, verifies
it, and refuses to query otherwise. It is weaker, though: the credential itself
could write.

`monitor.env` needs only `MONITOR_DB_USER/PASS` (plus `DB_HOST/PORT/NAME`).
The analytics-role credentials (`DB_RO_*`) are no longer used by the monitor.

## 2. Install the units in dry-run (no alerts leave the host)

```
install -d -m 750 /etc/laynes /var/lib/laynes/monitor
install -m 644 deploy/monitoring/monitor.example.toml /etc/laynes/monitor.toml        # review first
install -m 600 deploy/monitoring/monitor.env.example  /etc/laynes/monitor.env          # then fill in values
install -m 644 deploy/monitoring/laynes-monitor-*.service deploy/monitoring/laynes-monitor-*.timer \
        'deploy/monitoring/laynes-monitor-crash@.service' /etc/systemd/system/
systemctl daemon-reload
systemctl start laynes-monitor-data.service      # one manual pass; read the journal
systemctl start laynes-monitor-health.service
journalctl -u laynes-monitor-data -n 200         # DRY-RUN notification text appears here
python -m monitoring status                      # from the release checkout, with the env loaded
systemctl enable --now laynes-monitor-health.timer laynes-monitor-data.timer laynes-monitor-summary.timer
```

Things to know:

- **Install grace.** The heartbeat gives 60 minutes from the store's first run
  (longer for a tier whose own limit is longer; the summary gets 25 h). After
  that:
  - a tier that never ran, or whose timer is missing, disabled or inactive, is
    a HIGH incident;
  - so enable the timers within an hour of the first manual pass;
  - or list a tier you deliberately do not run in `[monitor].disabled_tiers`.
- **Exit codes** equal the recorded run status: 0 SUCCESS, 1 DEGRADED (a check
  crashed or was skipped, or a delivery failed or is retrying), 2 FAILED.
  - Any non-zero exit triggers `OnFailure=` crash-notify.
  - Crash-notify opens one deduplicated `unit_failed` incident per unit; the
    tier's next SUCCESS resolves it.
- **Permissions:**
  - Units run as root with `UMask=0027`, `StateDirectory=laynes/monitor`
    (0750) and `ProtectSystem=strict`.
  - The store file and its -wal and -shm are 0640, owner root, never
    world-readable.
- The timers use new `OnCalendar` entries and touch no existing timer.
- The summary timer's `Persistent=true` only catches up a 07:00 run missed
  while the host was down. It replays nothing on first install.
- Expected first-run incidents, from the 2026-11-25 smoke run on production
  (read-only, dry-run, state kept outside production):

| Area | Incident | Severity |
|---|---|---|
| Monitor | Heartbeat for the tiers not yet run: `PENDING` during the 60-minute grace (no alert) | — |
| HME | 2026-11-23 network unreconciled after the retry: goal D mismatch at 9 stores | HIGH |
| HME | The 9 store-days on 2026-11-23 | HIGH |
| HME | Rosenberg 2026-11-19 NO_ACCESS | HIGH |
| HME | `hme-reconciliation-retry` unit failed | HIGH |
| HME | `hme-daily-ingest` unit red | WARNING |
| Infrastructure | No backup newer than 26h; the newest is the 2026-11-17 pre-deploy dump | HIGH |
| Revel | Missouri City 11-21 and 11-22: payment timing | WARNING |
| Revel | Shepherd 11-22 and 11-23: open tickets | WARNING |
| R365 financial | P11 last loaded before close | WARNING |

Rollback:

```
systemctl disable --now laynes-monitor-health.timer laynes-monitor-data.timer laynes-monitor-summary.timer
rm /etc/systemd/system/laynes-monitor-*  && systemctl daemon-reload
```

Optionally keep or delete `/var/lib/laynes/monitor` (monitor-owned data only).
No business data needs reverting.

The store schema is version 2. A v1 store (from the Phase-1 smoke run) is
migrated in place on first open. Rolling the code back to v1 needs a fresh
store: move `monitor.db*` aside.

## 3. Dashboard (laynes-api restart plus frontend build)

- `backend/monitoring_api.py` is mounted by `backend/api.py`.
- **Store access (checked on production, read-only, 2026-11-25):**
  - `laynes-api` runs as **root** with no sandbox. User=, ProtectSystem=no,
    ProtectHome=no.
  - It can therefore read `/var/lib/laynes/monitor/monitor.db` (0640 root) with
    no permission change.
  - The API does not rely on root to limit what it does:
    - every GET opens the store `mode=ro` (it cannot write, create or migrate);
    - acknowledge uses an ack-only opener that runs one UPDATE plus one event
      INSERT;
    - a missing, corrupt or wrong-version store returns 503.
  - **If `laynes-api` is ever moved off root:**
    - create group `laynes-monitor-ro` and add the API user to it;
    - set the units to `Group=laynes-monitor-ro`, keeping the store 0640 and
      the directory 0750.
    - Acknowledgement would then need write access. Give the group 0660 on the
      store files and directory write, or move acks to a separate
      `acks.db`/table that the monitor merges.
    - This needs no change while the API runs as root.
  - Nothing else reads the store. `laynes-mcp` (User=laynes-mcp,
    ProtectSystem=strict, ReadWritePaths=/var/lib/laynes-mcp) cannot see it.
    The monitor never writes MCP's files; it reads a private copy of the usage
    log.
- Restart `laynes-api` **only with zero queued/processing `chat_jobs`**, as for
  every API deploy.
- Then build and publish the frontend:
  `cd frontend && npm run build && rsync -a --delete dist/ /var/www/laynes-intelligence/`.
  Back up the current directory first.
- Rollback:
  - API: `git -C /root/pos-analytics-pipeline-release checkout --detach c6d743c`,
    then restart under the same zero-jobs rule.
  - Frontend: restore the backed-up `/var/www/laynes-intelligence`.
- Verify:
  - signed out: `/api/admin/monitoring/overview` returns 401;
  - non-admin: 403;
  - super_admin: 200, and the menu shows **Data monitoring**.

## 4. Live notifications (needs your approval of destination and config)

Choose one or both.

**Telegram:**

1. Create a bot with BotFather.
2. Message it once from your account, then read your chat id with `getUpdates`.
3. Put `MONITOR_TELEGRAM_BOT_TOKEN` and `MONITOR_TELEGRAM_CHAT_ID` in
   `/etc/laynes/monitor.env` (0600).
4. In `monitor.toml`, set `channels = ["log", "telegram"]`.

**E-mail:**

1. Fill in `[notify.email]` in `monitor.toml`: SMTP host, port, from address and
   recipients.
2. Put `MONITOR_SMTP_USER` and `MONITOR_SMTP_PASS` in `monitor.env`.
3. Add `"email"` to `channels`.

**Then:**

1. Run `python -m monitoring test-notify` while `dry_run` is still true. It
   only logs.
2. Set `dry_run = false`.
3. Run `python -m monitoring test-notify` again. It sends one TEST message per
   channel.
4. Confirm you received it.

To switch delivery off, set `dry_run = true`. It takes effect on the next run;
no restart is needed.

Recommended starting policy: `immediate_min_severity = "HIGH"`, resolution
notices on, and WARNING via the 07:00 summary only.

## Before go-live

- If stores close on holidays (e.g. Thanksgiving, 2026-11-26), add the date to
  `[revel].accepted_closed_dates`. Otherwise each closed store raises a
  no-data incident.
- Decide on a scheduled database backup. Until one exists, the backup incident
  stays open by design.
- The monitor's own timers are watched by the heartbeat; they do not need to be
  added to `[services].timers`.
- **External dead-man check (recommended with n8n).** The monitor cannot report
  its own total failure: host down, store unwritable, all units dead. Poll the
  status API from outside (`docs/MONITORING_API.md`). Alert if it is
  unreachable, if `monitor.state` is not HEALTHY, or if a tier's
  `last_success_at` is too old.
- Backups: see `docs/DATABASE_BACKUP.md` (scheduled job, restore test, monitor settings). The backup check reports freshness only
  until offsite and restore-test status files exist.
