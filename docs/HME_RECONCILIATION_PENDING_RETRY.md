# HME daily ingest: exit 5 "reconciliation pending scheduled retry"

`hme_daily_ingest/1.4.0`. This changes the exit code of the scheduled morning run only.
Data, loader, reconciliation checks, tolerances, trust grading and schedules are unchanged.

## Problem

`hme-daily-ingest.service` has been red every morning since 2026-11-16, although each
day's 10:15 Chicago retry reconciled it.

- At 05:30 Chicago, HME's Trend dashboard (and, in September, the Outliers report) is
  often still behind Performance Analysis (PA) for one or more stores.
- `trend_total_cars_matches_pa_total_orders_per_store` (exact match) fails, so the run is
  `partial` / `reconciliation_ok=false` and exits 2. That is correct.
- `hme-reconciliation-retry` re-extracts the same day at 10:15 Chicago and normally
  reconciles it. That run belongs to a different unit, so the daily unit stays red until
  a morning reconciles on its own, which has not happened in a week.

A unit that is red every day no longer separates a real problem from a lag.

## Evidence (raw extracts `/var/lib/laynes/hme/raw`, `hme_ingest_run`, retry logs)

**Mornings:**
- On 24 network mornings where extraction succeeded, a store's Trend or Outliers figures
  lagged PA on 15. Store 115 (LCF Shepherd) lagged on all 15.
- From 2026-11-12 to 11-18, Shepherd lagged on 6 of 7 mornings. Other stores (109, 26,
  16, 1, 1163022 …) lag less often, by fewer cars, and settle sooner. On 11-17 only
  Shepherd was still behind at 05:47; on 11-16 all ten lagging stores had settled by 06:04.

**Shepherd's gap at ~05:36:**
- 15–77 cars.
- 11-18: PA 292 vs Trend 272.

**PA is stable:**
- Between the morning and 10:22 extracts, PA and every non-lagging store are identical.
- Only the lagging Trend/Outliers figures move, catching up to an exact match. This is
  HME's upstream aggregation settling, not our extraction or the business day.

**When it settles:**
- Still behind at 05:47.
- Reconciled at 10:21–10:23 in 9 of 11 eligible retries.
- Not reconciled by the retry:
  - 2026-10-06: account-wide, 24 stores.
  - 2026-11-13: Shepherd 1 car short, settled within 4 days.
- 2026-10-11 and 10-12: the retry extraction itself failed.
- There is no observation between 06:05 and 10:20, so the settle point cannot be narrowed
  further from existing data.

**Schedule:** moving the morning run later is not supported by this evidence.
- No observed settle time exists before ~10:20.
- A run that late would hold back all 10 other stores' verified data, about 5 hours a day,
  for one lagging store.
- 09:00–09:25 Chicago is occupied by `revel-daily-sync` (1.1–1.3 GB peak); HME peaks at
  0.9–1.0 GB on a 3.8 GB host.

The schedule stays at 05:30 (daily) and 10:15 (retry).

## Change

`hme_daily_ingest.main()` returns **5** instead of 2 only when every condition holds
(`assess_pending_retry`, fail-closed):

1. It is the scheduled path: no `--date`, no `--dry-run`.
2. The load is `partial` with `reconciliation_ok=false`.
3. The only failed checks are the settling checks
   (`trend_total_cars_matches_pa_total_orders_per_store`,
   `pa_total_orders_vs_outliers_car_departures`). A goal-D or row-sum failure keeps 2.
4. Every non-SUCCESS store result is `PARTIAL / RECONCILIATION_MISMATCH`. No FAILED,
   NO_ACCESS or missing VERIFIED store is allowed.
5. The latest network run for the date is this run, and
   `hme_reconciliation_retry.assess_retry_eligibility()` accepts it. This is the retry's
   own decision, not a copy of it.
6. Chicago time is the morning after the business date and before 10:00.
7. `hme-reconciliation-retry.timer` is active.

Any error while deciding keeps 2.

The unit adds 5 to `SuccessExitStatus` (`0 3 5 75`); 2 and 4 stay red.

The database is untouched by this decision:
- The run stays `partial` / `reconciliation_ok=false`.
- The lagging store-day stays PARTIAL / RECONCILIATION_MISMATCH, so
  `v_hme_store_day_trust_llm` grades it UNVERIFIED.
- AI Chat and MCP exclude it until a reconciled run exists.

The retry owns the verdict:
- It reconciles and exits 0 or 3, or
- it fails and exits 2, turning `hme-reconciliation-retry.service` red. That is now the
  actionable alert.

`classify_store_results`, the retry and the backfill are unchanged (they still see 2).

**Replay over every historical unreconciled network run:**
- Exit 5 for each pure lag morning: 208, 390, 392, 413, 416, 418, 420, 422, 423, 424,
  551, 586, 588.
- Exit 2 kept for:
  - the Mission Bend NO_ACCESS period (528–532);
  - backfills, and runs finishing after 10:00 (414, 500, 501, 531).

## Tests

- `tests/test_hme_pending_retry.py`: 64 checks. Pure, with no DB, network or extractor.
- Existing HME suites, run on `c6d743c` and on this branch against an isolated
  copy (port 55432; no network; production socket hidden), give identical check results.

## Deploy (needs approval; no service restart; no timer change)

1. Gate:
   - lock free;
   - not within 05:25–06:30 or 10:10–10:45 Chicago;
   - `hme-reconciliation-retry.timer` active.
2. Move the release checkout to the merged commit:
   `git -C /root/pos-analytics-pipeline-release checkout --detach <merged-sha>`.
   - laynes-api does not import `hme_daily_ingest`, so no restart is needed.
   - MCP is unaffected.
3. Install the drop-in:
   `install -m 644 systemd/dropins/hme-daily-ingest-20-pending-retry.conf /etc/systemd/system/hme-daily-ingest.service.d/20-pending-retry.conf`
   - The live unit is a symlink into the development tree, which must not be edited.
4. `systemctl daemon-reload`.
   - No `OnCalendar` changes, so `Persistent=` replays nothing.
5. Verify `systemctl show hme-daily-ingest -p SuccessExitStatus` lists 3 5 75.
   - Systemd omits 0 from the listing.
   - `systemctl list-timers` should be unchanged.
6. The unit stays red from its last exit-2 run until the next scheduled run.
   - `systemctl reset-failed hme-daily-ingest` is optional and cosmetic.
7. Next morning, confirm the log shows `exit 5` or `exit 0`, the DB row is still
   partial/unreconciled on a lag day, and the 10:15 retry reconciles it.

Either half alone is safe:
- New code without the drop-in: exit 5 is red, as today.
- Drop-in without the new code: 5 is never produced.

## Rollback

- Code: `rm /etc/systemd/system/hme-daily-ingest.service.d/20-pending-retry.conf && systemctl daemon-reload`.
- Release checkout: `git -C /root/pos-analytics-pipeline-release checkout --detach c6d743ca561092eeb25cb0260239d25ac97d8092`.
- No data to revert.

## Residual risks and follow-ups

- On exit-5 days, the morning alert moves to 10:15 Chicago. That was already when the day
  could first be resolved.
- A retry that exits 2 is red only until the next retry exits 0, 3 or 75. As before, the
  database (`v_hme_day_completeness_llm`, `hme_ingest_run`) is authoritative.
  - Follow-up: a read-only alert on network days unreconciled for more than 1 day.
- Optional: a second retry slot (about 13:45 Chicago, clear of the 11:30/15:30
  `revel-items-selfheal`) would likely have reconciled 11-13 and the retry extraction
  failures of 10-11 and 10-12. This is not proposed until it recurs.
