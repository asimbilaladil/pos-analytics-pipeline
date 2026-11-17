# HME store-scoped backfill (migration 64)

Purpose: load **one** HME store's missing dates without touching any other
store and without changing any day's network verdict. Written for the LCF
Mission Bend gap (HME `1158614` → establishment `25`, 2026-10-14 … 2026-11-15).
The store was invisible to the integration account until 2026-11-16.

## What changes

| Piece | Change |
|---|---|
| `migrations/64_hme_store_scoped_runs.sql` | `hme_ingest_run.store_scope text[]`, nullable. NULL = a network run; this covers every existing row and every daily or retry run. A CHECK requires a non-empty scope. `v_hme_day_completeness_llm` reads network runs only (`store_scope IS NULL`); its columns and current output are unchanged. |
| `hme_load.load(..., only_stores=)` | The full extract still passes every tenant/security check. After that, only the scoped store's rows, goals and per-store result are written. The run is recorded with `store_scope`. The scope must be an active VERIFIED mapping, and must be present in the extract (otherwise `StoreNotInExtract` and nothing is written). |
| `hme_daily_ingest.ingest_date(..., only_stores=)` | Passes the scope to the loader and to failed-run records. The normal path calls the loader exactly as before. |
| `hme_reconciliation_retry.latest_run` | Ignores store-scoped runs. It reads `store_scope` through `to_jsonb`, so it works before migration 64 too. |
| `hme_backfill.py --only-store N --expect-establishment E` | Verifies the mapping (also again under the lock), uses the shared ingest lock, the same extractor and loader checks, and its own checkpoint `backfill-store-N-<start>-<end>.json`. Skips dates the store already has, unless `--include-loaded`. `--plan` is read-only: no lock, no extraction, no writes. |

Unchanged: the ingest lock, timers and schedules, the mapping table, the reconciliation checks and tolerances, the daily and retry behaviour, and the chat/MCP code.

## Planner rules (`--plan` and every backfill)

A date is backfilled only when all of these hold:
1. It is **older than Chicago yesterday.** Yesterday belongs to `hme-daily-ingest` and `hme-reconciliation-retry`, which load every store, so a scoped run never lands on the one date the retry examines.
2. The day **already has a network-wide run.** A store is filled into days the network has judged, never ahead of them.
3. It is **not `loaded`** in this store's checkpoint.
4. The store has **no canonical row** for it (unless `--include-loaded`).

The current plan is in `docs/HME_MISSION_BEND_BACKFILL_PLAN.md`: 33 dates, all to backfill.

## Verified before review (tests/test_hme_store_scoped_backfill.py, section V)

1. **Independent of PR #34 (migrations 62/63).**
   - The two touch disjoint objects.
   - 64 was applied to today's schema, without 62/63.
   - Both migrations apply cleanly together in either order.
2. **Scheduled jobs keep working with scoped runs present.**
   - A later network run (as written by the daily ingest or the retry) is recorded network-wide and becomes the day's verdict.
   - The retry sees it as the latest, and a scoped run after it does not displace it.
   - The retry's eligibility decision is unchanged. Its coverage count now includes Mission Bend's loaded rows (10/11 → 11/11), which can only add rows, never flip the decision.
   - The daily ingest never reads `hme_ingest_run`.
3. **No upgrade of network trust.** Scoped runs on 10-14/10-15, even forced to `succeeded` / `reconciliation_ok = true` / 11 stores, leave every day verdict byte-identical. The chat HME gate still reports 10-14 and 10-15 as unreconciled, for Mission Bend and for the network.
4. **Rollback cannot reinterpret scoped runs.**
   - The schema rollback refuses while any scoped run exists, and the scoped runs keep their scope.
   - Code-only rollback is safe: the old retry would take a newer scoped run as the latest for a date, but it only ever examines Chicago yesterday, which the planner never schedules. Old code never writes `store_scope`, and the migration-64 view keeps ignoring scoped runs.
5. **Checkpointed and resumable.** An interrupt (SIGINT) after 10 of 33 dates leaves exactly those 10 `loaded`. The checkpoint is written atomically (0600, no temp file). Resume processes exactly the 23 remaining dates, then exit 0. A checkpoint belonging to another store scope is refused. Every date runs under the shared ingest lock, with the mapping re-verified under it.

## Deployment sequence (each step needs approval)

1. **Back up** `hme_ingest_run` and the view definition: `pg_dump -t hme_ingest_run` plus `pg_get_viewdef('v_hme_day_completeness_llm')`.
2. **Apply migration 64.** Run it as `laynes_user` (the owner), outside the HME timer windows (05:30 and 10:15 CT):
   `sudo -u postgres psql -v ON_ERROR_STOP=1 -d laynes -c 'SET ROLE laynes_user' -f - < migrations/64_hme_store_scoped_runs.sql`.
   - The `ADD COLUMN` is metadata-only. The CHECK scans about 150 rows. Both take a brief exclusive lock on `hme_ingest_run`.
3. **Verify:** the column exists; `SELECT count(*) FROM hme_ingest_run WHERE store_scope IS NOT NULL` = 0; the day view's output equals the pre-migration backup.
4. **Merge the PR, then move the release checkout** (`/root/pos-analytics-pipeline-release`) to the merge commit.
   - The HME timers run from it, and its HME code is safe with or without migration 64.
   - laynes-api and laynes-mcp don't import these modules, so no restart is needed.
5. **Plan (read-only):** `hme_backfill.py --only-store 1158614 --expect-establishment 25 --start 2026-10-14 --end 2026-11-15 --plan`. Expect the 33 dates in `docs/HME_MISSION_BEND_BACKFILL_PLAN.md`.
6. **Optional dry run of one date** (rolled back): add `--dry-run --start 2026-10-20 --end 2026-10-20`.
7. **Backfill**, with no other HME job running (the lock enforces this). It takes about 5–6 minutes per date, so about 3 hours for 33 dates, and is restartable at any point.
8. **Verify:**
   - Mission Bend has rows for every successful date;
   - the other ten stores' rows and every `v_hme_day_completeness_llm` row are unchanged against the pre-backfill snapshot;
   - 2026-10-14/15 and 2026-11-13 are still `reconciliation_ok = false` at network level;
   - website AI Chat and MCP (read-only, mocked model) report Mission Bend per store, and still exclude the unreconciled days.

## Rollback

- **Code:** move the release checkout back to `a86366d`. Store-scoped runs then simply stop being created. Migration 64 stays harmless: old code never writes `store_scope`, and the view still ignores scoped runs.
- **Data:** a scoped run only adds Mission Bend rows, outliers, goal observations and per-store results, all carrying that run's `run_id`.
  - They can be removed by `run_id` (`hme_ingest_run.store_scope IS NOT NULL`), in this order: results, outliers, daily, goal history opened by the run, run rows.
  - Do this only on explicit approval: they are the correct data.
- **Schema:** `docs/rollback/64_hme_store_scoped_runs_rollback.sql` restores the original view and drops the column. It refuses while any store-scoped run exists. Without the column such runs would look network-wide, so in that case keep migration 64 and roll back code only.

## Risks

- **Reconciliation on Oct 14/15:** the scoped run is checked with the same reconciliation, over Mission Bend's own rows. If Mission Bend's Oct 14/15 source shows the same Outliers decay as the other stores, those two dates load as PARTIAL (`RECONCILIATION_MISMATCH`) and stay excluded by chat/MCP, which is correct.
- **Extraction is network-wide:** the extractor still pulls all stores for each date. Only Mission Bend is written, but each date costs a full extraction, about 5–6 minutes.
- **Goal history:** applied in date order (`apply_goal_observation`). Backfilling older dates after newer ones can record goal conflicts in `hme_goal_conflict` instead of rewriting history. That's the existing policy, now visible for Mission Bend only.
- **Ordering with PR #34:** migration 64 is independent of 62/63 (different objects). The numbers are only an ordering convention.
