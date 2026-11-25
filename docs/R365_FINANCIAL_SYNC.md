# R365 financial synchronisation: permanent fix

## Root cause (confirmed 2026-11-25, read-only)

1. **The scheduled ingest was never installed.**
   - `systemd/r365-financial-ingest.{service,timer}` have been in the repo since PR #16
     (2026-10-16), but the rollout was stopped before the timer step and never resumed.
   - `/etc/systemd/system` has no financial units, and the journal has no entries.
   - All 33 `r365_financial_ingest_run` rows are `backfill`/`manual`, all from 2026-10-16. None
     is `scheduled`.
2. **The unit could not have run as written.**
   - It appends its output to `/var/lib/laynes/r365-financial/logs/…`, and that directory
     doesn't exist.
   - systemd opens `StandardOutput=append:` files before starting the process, so the first run
     would have failed with 209/STDOUT before any code ran.
   - **Correction (2026-11-25, found during the production install):** PR #42's
     `StateDirectory=…/logs` does **not** fix this. On systemd 259 the output file is opened
     *before* `StateDirectory=` directories are created, and before any `ExecStartPre=` (which
     has its own output to open). Transient units on a scratch path: the production layout
     (`state/` present, `logs/` absent) and a fully empty layout both exit **209**; with
     `logs/` pre-created, exit 0. The timer was disabled again before its first elapse.
   - **Fix:** an installation prerequisite,
     `systemd/r365-financial-ingest.tmpfiles.conf` → `/etc/tmpfiles.d/laynes-r365-financial.conf`.
     It creates `state/` and `logs/` (0700 root, never aged out) at install, through
     `systemd-tmpfiles --create`, and on every boot. Regression test:
     `tests/test_r365_financial_unit_startup.py` (static + isolated tmpfiles; live
     clean-directory start with `R365_UNIT_LIVE_TEST=1` as root).
3. **Nothing reported it.**
   - The monitor's financial check hardcoded `timer_installed: False` and only asked "loaded
     after close?".
   - P10, loaded once on 10-16 after its 10-07 close, passed that check while the source kept
     adding and re-approving lines for weeks.
4. **AI Chat was told data refreshed daily.**
   - The financial gate described the refresh schedule as `daily 07:00 America/Chicago`.
   - It had only a single global age warning; nothing per period showed that a closed period's
     figures were stale.

**What was not the cause:**
- **Period selection is correct.** It uses the AYG fiscal calendar (`ayg_business_periods`), not
  calendar months, and covers the last 42 days daily plus every elapsed period of the current
  and previous FY on Mondays.
- **Credentials and connectivity work.** `/etc/laynes/r365.env` is used by the read-only
  comparisons; 52 GET requests took 7.9 s.
- **Overlap prevention exists** (flock).
- **Failures were already transactional:** each period is one DB transaction, and schema or
  reconciliation errors roll back.

## Synchronisation (existing pipeline, now installable and safe)

| When | What | Mechanism |
|---|---|---|
| Daily 07:00 America/Chicago (12:00 UTC in CDT, 13:00 in CST), ±2 min | Chart of accounts; the current period; every period whose end is within the last 42 days | `r365-financial-ingest.timer`, `scheduled_periods()` |
| Mondays, same time | Every elapsed period of the current and previous FY (weekly revalidation, after the source's Monday 05:30 repair) | `scheduled_periods()` (`REPAIR_WEEKDAY`) |
| Period close | Automatic: the just-closed period stays in the daily set for 42 days (> the source's 35-day correction window), then is revalidated weekly | the same |
| Missed run | `Persistent=true` catches up after downtime; the monitor raises `scheduled_run_missed` after 36 h | timer + monitor |
| Overlap | A second run exits 75 without touching anything | flock on `/var/lib/laynes/r365-financial/state/ingest.lock` |
| Retries | Bounded HTTP retries per request (`MAX_ATTEMPTS`); a failed target is recorded FAILED and the others continue | orchestrator |
| Load | ~15 GET requests on a normal day, ~150 on Mondays; `Nice=10`, idle I/O, `MemoryMax=600M` | unit |

The schedule doesn't overlap HME (11:32 and 16:17 UTC) or Revel (15:00 UTC).

## Financial integrity (unchanged checks plus one new guard)

Before a period is written, the existing checks still apply:
- schema validation;
- the source's echoed filters;
- no truncated or paginated summary;
- summary totals reconciled with GL line detail per bucket;
- mapping checks for unmapped locations and unaccounted stores;
- duplicate line IDs refused.

Each period is replaced in **one transaction**, and every run row is kept. Additions, updates
and deletions are counted in the run row.

**New: deletion safety (`r365_financial_load.deletion_decision`).**
- Rows the source no longer reports are deleted **only when the source reports the period
  COMPLETE**.
- On a PARTIAL response the write is upsert-only, and the deferred deletions are recorded in
  the run's `reconciliation.deletion_policy`.
- Even on a COMPLETE response, a refresh **fails the target** and rolls back the whole period
  when, for any table's rows in the period, it would:
  - **empty the scope**: every existing row gone and nothing returned (any size, even 1 row);
  - delete a **near-total** share: more than 80% of a scope of 2+ rows, even if replacements
    were returned (a wholesale re-key is suspicious);
  - delete more than `max(5, 25%)` of the scope.
- An empty scope (no rows yet) has nothing to delete and loads normally.
- That stops a truncated but self-consistent response from wiping a period, including a small
  one (the earlier `max(50, 25%)` let a table of 50 rows or fewer be emptied).
- The known P11 changes pass unchanged: 449 of 4,279 transaction lines (10.5%), 1 of 5
  candidate rows, 1 of 413 account_period rows.
- **Intentional mass deletion** (for example the source really removed a whole period):
  re-run that one period with `r365_financial_ingest.py --period FY P --allow-mass-delete`,
  after checking the dry run. The flag requires `--period`, never applies to the scheduled
  run, and is **ignored on a PARTIAL source** (deletions stay deferred). Every other check
  (schema, reconciliation, mapping, duplicates) still applies, and the run records
  `deletion_policy.mass_delete_override=true`.
- The limits are set by `R365_FIN_DELETION_MAX_FRACTION` (0.25), `R365_FIN_DELETION_MIN_ROWS`
  (5) and `R365_FIN_DELETION_NEAR_TOTAL` (0.8).
- Lines moved to another period are not duplicated: the transaction key is the source line ID,
  and the other period's upsert moves the row.

## One shared freshness rule: `r365_financial_freshness.period_refresh_state()`

| State | Meaning | What AI Chat and MCP are told |
|---|---|---|
| FINAL | closed, with a COMPLETE, reconciled load on or after the 42-day correction window's last day | nothing extra |
| PROVISIONAL | the current period refreshed within 30 h, or a closed period inside its window refreshed within 30 h | "label the figures provisional; they can still change" |
| STALE | loaded before close; the window refresh is overdue; never re-pulled after the window; or the current period's last load is older than 30 h | "NOT current; give the last load date; never present as final" |
| UNVERIFIED | never loaded; reconciliation failed **or unknown**; the latest refresh failed (open **or** closed period); or a closed period's source is incomplete **or of unknown status** | "do not present as verified" |

**Fail closed.** Nothing is FINAL or PROVISIONAL without `reconciliation_ok = true`, and a
closed period is never FINAL or PROVISIONAL without a confirmed COMPLETE source. A failed
latest refresh is checked before anything else, so a current period that loaded fine earlier
shows the failure, not PROVISIONAL. A PARTIAL source is expected for the **current** period
and is not a defect; for a **closed** period it is UNVERIFIED.

**AI Chat and MCP:**
- `chat_sql._r365_fin_block` adds `refresh_state`, `refresh_state_reason` and `loaded_at` to
  each period in scope.
- `_r365_fin_gate` adds a warning per non-FINAL period. It never blocks, so the trust gate is
  unchanged.
- `r365_fin_result_note` puts the label next to the result rows.
- MCP gets the same through the shared gate. It needs a redeploy of `/opt/laynes-mcp/app`.

## Monitoring (`monitoring/checks/r365.py: financial`)

| Result | Detects |
|---|---|
| `scheduled_ingest` | timer missing or disabled (`scheduled_ingest_missing`), inactive, no successful scheduled run in 36 h (`scheduled_run_missed`), failed targets in 36 h (`scheduled_run_failed`) |
| `period_coverage` | the shared rule, per period: `missing_ingestion`, `stale_after_close`, `refresh_overdue`, `not_refreshed_after_repair_window`, `current_period_stale`, `refresh_failed`, `source_incomplete`, `source_status_unknown`, `reconciliation_failed`, `reconciliation_unknown` (HIGH), `source_changed_final_period` (a scheduled refresh changed rows of a FINAL period). PROVISIONAL and FINAL are OK, so an incident resolves once the sync catches up. |
| `period_source_sync` | the **source server's** own freshness flag, kept separate from our latest local load. Evidence carries `source_freshness` and `last_load` separately. |

Expected incidents on today's data once deployed:
- `scheduled_ingest_missing` (HIGH), until the timer is installed;
- P10 `not_refreshed_after_repair_window`;
- P11 `stale_after_close`;
- P12 `missing_ingestion` (the current period has never been loaded).

All of these are genuine.

## Deploy (each step needs approval)

1. Merge the PR into `sidebar-redesign`.
2. **Code:**
   - move the release checkout to the merge commit, after recording the current SHA for
     rollback;
   - restart `laynes-api` with 0 chat jobs, for the chat labels;
   - redeploy the MCP app (`/opt/laynes-mcp/app`, git-archive) with 0 ask jobs;
   - the monitor picks the change up on its next run.
3. **Install the prerequisite, then the units:**
   ```bash
   # PREREQUISITE (before the unit can ever start): the log directory systemd opens before exec
   install -m 644 $REL/systemd/r365-financial-ingest.tmpfiles.conf /etc/tmpfiles.d/laynes-r365-financial.conf
   systemd-tmpfiles --create /etc/tmpfiles.d/laynes-r365-financial.conf
   stat -c '%n %a %U:%G' /var/lib/laynes/r365-financial/state /var/lib/laynes/r365-financial/logs   # 700 root:root
   install -m 644 $REL/systemd/r365-financial-ingest.service $REL/systemd/r365-financial-ingest.timer /etc/systemd/system/
   systemctl daemon-reload
   systemd-analyze calendar --iterations=2 '*-*-* 07:00:00 America/Chicago'   # 12:00/13:00 UTC
   ```
   Every later release that touches these units re-installs all three files. The tmpfiles file
   is the only thing that creates `logs/`; don't remove it while the unit is installed.
4. **First run (manual, observed),** in a window clear of HME and Revel:
   - `systemctl start r365-financial-ingest.service`;
   - expect P11 and P12 refreshed (P10 too on a Monday);
   - read `r365_financial_ingest_run` and `/var/lib/laynes/r365-financial/logs/financial-ingest.service.log`.
   - **This is the production refresh of the current data.** It is covered by the backup and
     rollback in the 2026-11-25 recovery plan; take that backup first.
5. `systemctl enable --now r365-financial-ingest.timer`.
   - `Persistent=true`: if the timer's stamp
     (`/var/lib/systemd/timers/stamp-r365-financial-ingest.timer`) is older than the most recent
     07:00 Chicago elapse, enabling starts one **catch-up run immediately**. Enable before the
     next elapse after the stamp, or treat the catch-up as the observed first run.

**Rollback:**
- `systemctl disable --now r365-financial-ingest.timer`, remove the two unit files, then
  `daemon-reload`;
- remove `/etc/tmpfiles.d/laynes-r365-financial.conf` only together with the units. Leave
  `/var/lib/laynes/r365-financial/{state,logs}` in place; they hold the lock and the log
  history;
- restore the recorded release SHA;
- data: the scoped P10/P11 rollback from the backup.

## How to confirm next month runs by itself (no manual step)

The next period is FY2026 P13, starting 2026-12-03. P12 closes 2026-12-02.

| By | Expect (read-only queries / dashboard) |
|---|---|
| Every day | `scheduled_ingest` OK; a `trigger='scheduled'` run row for each targeted period |
| 2026-12-03, after 13:00 UTC | a scheduled COMPLETE or PARTIAL run for **FY2026 P13** with no manual action; P13 `refresh_state` PROVISIONAL |
| 2026-12-03 .. 2027-01-13 | P12 re-pulled **daily** after its close (`refresh_state` PROVISIONAL, its monitor result OK) |
| 2027-01-13 (P12 end + 42) | the last daily pull makes P12 **FINAL**; nothing stays STALE |
| Any day | `SELECT max(started_at) FROM r365_financial_ingest_run WHERE trigger = 'scheduled'` is less than 36 h old; otherwise `scheduled_run_missed` (HIGH) is already open |

A missing scheduled run, a STALE period, or a failed target shows up as a monitor incident
within one data-tier run (15 min). Nobody has to remember to look.
