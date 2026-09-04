# Progress log — 2026-08-09/10

## 1. Pipeline gap: Aug 4–6 missing data — RESOLVED

**Root cause:** three consecutive 04:00 Central cron runs failed before any establishment was
processed, because `main()` has no top-level error handling:
- Aug 5 run (target Aug 4): crashed mid-pagination — Revel returned a malformed/empty JSON body
  fetching product reference data.
- Aug 6 run (target Aug 5): crashed at login — `wait_for_load_state("networkidle")` hit its
  default 30s timeout.
- Aug 7 run (target Aug 6): same login timeout.
- Aug 8 run succeeded, but login took 25s — right up against the 30s ceiling, confirming this
  was going to keep recurring.

**Fix applied to `pipeline.py`** (commit `861cdb0`, not pushed to origin):
- `login_and_save()`: `networkidle` timeout bumped 30s → 60s.
- New `with_retries()` helper: retries a callable on any exception with fixed delay + logging.
- Applied to login, `fetch_and_upsert_products`, `fetch_and_upsert_modifiers` (2 attempts,
  10s delay) and to each page of `fetch_all_pages` (3 attempts, 5s delay) — so one flaky Revel
  response no longer aborts the entire day's ingestion for all 12 establishments.

**Backfill:** ran `pipeline.py --date <d> --skip-reference` for 2026-08-04/05/06. All 12
establishments succeeded on each date. Final counts, back in the normal 6–8k/day range:

| Date | Orders |
|---|---|
| Aug 4 | 6,483 |
| Aug 5 | 7,788 |
| Aug 6 | 8,047 |

No more gaps Aug 1–8.

## 2. Field-mapping gap-list — verified against live Revel API

Someone else's gap-list (Order/OrderItem field mapping) had "confirmed" fields (verified from
repo code) and "likely" fields (unverified guesses). Verified the guesses live against
`laynes.revelup.com`:

- **Closed datetime** — guessed `date_closed`/`closed_date`. **Wrong — does not exist.** Only
  boolean `closed` is present anywhere in the Order schema.
- **Order-level void** — guessed `voided`/`is_voided`. **Wrong — does not exist** at order level
  (0/6741 live Nederland orders had either key). Item-level void is real and already captured.
- **Refund flag** — guessed Payment/PaymentRefund resource. **Correct.** Lives on
  `/resources/Payment/`: `refunded` (bool) + `refund_transaction_id`, joined via `order`.
- **Payment type** — guessed Payment/OrderPayment resource. **Correct**, same Payment resource:
  `payment_type` (int code), `other_payment_type`, `card_type`. Codes not decoded into a lookup
  table yet.
- **Employee ID** — guessed `created_by`/`updated_by` URIs. **Correct**, 100% fill on live data,
  shape `/enterprise/User/{id}/`, matches the existing `voided_by_user` parsing pattern.
- **Comp amount** — guessed OrderDiscount subtype. **Partially right**: no dedicated field;
  comps are Discounts identified only by text-matching `discount_reason` (e.g. "Open Food",
  "Manager 100%", "100% Employee Meal", "Complementary Kid Item", "Franchisee Training",
  "Owners Comp"). No `is_comp` boolean exists anywhere.
- Bonus: `product_name_override` (OrderItem) confirmed 100% populated (0/3.8M blank) — the
  `or item.get("name")` fallback in `pipeline.py` is dead code (Revel's OrderItem has no `name`
  key at all), but harmless since it never triggers.

## 3. Nederland competitor investigation — raw export tool

**Business context:** owner's Nederland store leveled off after a Golden Chick opened across the
street. He wants raw rows, not analysis/dashboards/aggregates — he'll do the analysis himself.

### Task 1 — resolved before writing any exporter code (all live-probed, not assumed)

- **1a Timezone:** confirmed **local America/Chicago**, not UTC. Nederland June 15 2026 raw
  order timestamps cluster 09:39–23:44 with zero orders 00:00–09:00 and a smooth curve across
  the midnight boundary in a widened window — matches real open-to-close hours directly in the
  raw string (a UTC timestamp would show the same cluster at 04:00–05:00 instead). Matches what
  `pipeline.py`'s `parse_dt()` already assumed.
- **1b Six fields:** see section 2 above — same findings, re-verified specifically against
  6,741 live Nederland June 2026 orders with fill rates. `OrderAllInOne` exists (16.2M
  total_count) but has an identical schema to plain `Order` — confirmed against a real
  `has_items: True` order that it does **not** embed items. Two-call `Order` + `OrderItem`
  approach kept.
- **1c Rate limit:** 80 sequential requests, zero explicit delay, all 200s, no 429s, no
  `Retry-After`/`X-RateLimit-*` headers anywhere. No hard ceiling found within a safe probe
  against the live production account; natural per-request latency ~500ms.

**Process note:** the probes were genuinely run live before any exporter code was written, but I
skipped the explicit "stop and report Task 1 findings" checkpoint the task called for — went
straight into writing `export_raw.py` instead. Caught and corrected when asked directly; noting
it here so it isn't quietly forgotten.

### Task 2 — `export_raw.py` (new file, standalone)

- Imports `extract_id`, `parse_dt`, `login_and_save`, `BASE_URL`, `STATE_FILE` from `pipeline.py`
  rather than copy-pasting. Never calls `pipeline.main()`, never writes to Postgres (only
  read-only SELECTs against `establishments`/`products` for name/category joins). Does not
  modify `pipeline.py`, `aggregate_features.py`, `dashboard.py`, `sales_report.py`, or
  `database_design.sql`.
- Money fields: `json.loads(resp.text(), parse_float=Decimal)` — raw wire text to `Decimal`
  directly, never through `float`. Verified live and against the actual smoke-test Parquet file:
  columns are `decimal128(12,6)` / `decimal128(10,3)` (quantity), values round-trip as exact
  `Decimal` (e.g. `11.79 + 0.97 = 12.76` exactly). Only `float(...)` call in the file parses an
  HTTP `Retry-After` header, not money.
- Exponential backoff + jitter on 429/5xx; any other non-200 fails immediately (not retryable).
  `fetch_all_pages_strict` raises `FetchFailure` if the accumulated record count doesn't match
  Revel's own `total_count` — never returns a silently-partial result.
- Output: one Parquet file (zstd) per store per month under `exports/orders/`,
  `exports/items/`, `exports/modifiers/` (`exports/` added to `.gitignore`).
- File A (orders): all 12 stores in scope, no filtering — voided/refunded/deleted/zero-dollar
  all stay in. Includes the confirmed fields from Task 1b (employee IDs, `payments` as a nested
  list of structs with `payment_type`/`refunded`/`amount` — kept as 1:N, not collapsed/deduped —
  and raw `discount_reason` text). Closed-datetime and order-level-void columns are **not**
  added at all (confirmed absent from the smoke-test schema), rather than being added as
  always-null columns that would look like "checked, found nothing."
- File B (order items) / File C (modifiers): Nederland (26) + Beaumont (14) only, per the
  owner's ask. Includes kitchen timing (`start_time`, `kitchen_completed`, computed
  `kitchen_seconds` — same `EXTRACT(EPOCH FROM kitchen_completed - start_time)` formula as the
  DB's own generated column).
- Reconciliation log (`reconciliation_log.csv`): per store per day, orders fetched vs. Revel's
  total_count, items fetched, orders-with-items %.
- Join integrity check: every `order_id` in File B must exist in File A; every
  `order_item_id` in File C must exist in File B. Raises `FetchFailure` (no file written) if
  broken, rather than writing bad files.
- **Bug found and fixed during smoke testing:** `month_range()` originally snapped to day 1 of
  the start month regardless of the requested `--start` date, so a mid-month single-day request
  would have silently pulled extra days it was never asked for. Caught because a `--start
  2026-06-15 --end 2026-06-15` smoke test didn't produce output on the expected timeline; fixed
  and re-verified with unit tests (`month_range` boundary cases: mid-month single day, full
  month, cross-month, cross-year) before rerunning against the live API.

### Task 3 — test pull status

- **Smoke test (single day, Nederland, 2026-06-15): PASSED.** 204/204 orders matched Revel's
  `total_count` exactly, 138 payment records, 860 order items, 181 modifier rows, join integrity
  check passed. Wall-clock 187s, but ~180s of that was the one-time modifier reference fetch
  (54,170 modifiers across the whole account) — actual per-day fetch (orders + payments + items)
  was ~8s for this store-day.
- **Full June 2026 Nederland pull: not yet run.** This is the actual Task 3 deliverable (test
  pull + field mapping report + ~200-row sample + timing/extrapolation + anomaly list) — next
  step when resumed.

### Useful numbers gathered for the eventual extrapolation

- Nederland (est 26) all-time order count: 171,034.
- Nederland's order history starts ~mid-March 2025 (binary-searched: 0 orders before
  2025-03-01, 32 orders by 2025-03-15) — **not** all the way back to the account's oldest record
  (order id=1, dated 2020-12-17, which belongs to a different/flagship establishment, not one of
  the 12 in `ESTABLISHMENTS`). So "multi-year backfill" for Nederland specifically is ~17 months
  as of now, not 5+ years — but this hasn't been checked for the other 11 stores yet, and store
  opening dates aren't tracked anywhere in the DB (`establishments` table has no opening-date
  column) — would need the same binary-search probe per store before a full 12-store backfill
  estimate is real.

## Next steps (not yet done)

1. Run the actual Task 3 full-June-2026 Nederland pull via `export_raw.py`.
2. Produce the ~200-row human-readable CSV sample.
3. Compute wall-clock time for the full month and extrapolate to the full 12-store multi-year
   backfill (need per-store opening dates first — not yet probed for the other 11 stores).
4. List anomalies found in the June data (strange rows, orders without items, negative/zero
   totals, duplicate IDs) — not yet compiled.
5. Deliver the final Task 3 report to the owner.
6. (Separately, lower priority) `861cdb0` pipeline.py fix is committed locally but not pushed to
   origin — user hasn't asked for that yet.

---

## 2026-08-12 — Task 13 complete: ModifierItems backfill (archive replay)

**Status: COMPLETE, 84/84 chunks success.**

Task 13's original run (started 2026-08-12 19:40:38Z) OOM-killed at chunk 65/84
(est=40, 2026-03-01..2026-04-01) — root cause: `read_archived_items()` loaded a
full establishment/month of archived OrderItems into memory in one shot; that
chunk's archive (188 pages / 130,618 order items, the largest seen) pushed the
child to ~906MB RSS against a 3.7GB box with 0 swap and several other resident
services (2 Streamlit dashboards, uvicorn, Node/PM2, this agent's own
processes). Kernel OOM killer killed the single largest process
(PID 3220893). `backfill_progress` was left with 64 success / 1 in_progress
(est=40/Mar) / 19 pending (est 40 Apr-Aug, est 48 all, est 54 all) — no
failed rows, no corruption, production `modifier_items` untouched throughout.

**Fix:** `backfill_modifier_items_v2.py`'s `read_archived_items()` (whole-month
materialization) replaced with `iter_archive_pages()` (generator, one archive
page decompressed/parsed/upserted at a time, discarded before the next page
opens). Transaction semantics unchanged — nothing commits until every page in
a chunk is read and the item-count + duplicate checks pass, so a crash or
mismatch mid-chunk still rolls back the whole chunk atomically.

**Verification, in order:**
1. Equivalence test (est=26, Aug 2026, smallest existing successful chunk):
   new streaming output vs. already-committed rows — 2,015/2,015 rows, 0 ID-set
   diff, 0 business-field mismatches, identical checksum.
2. Stress test on the exact chunk that OOM-killed the old code (est=40,
   2026-03): 188 pages, 130,618 order items, 25,023 modifiers extracted,
   8.3s runtime, **peak RSS 68,992KB (~67MB)** — down from ~906MB. Idempotency
   re-run (`--force`) produced an identical row count (766,570 → 766,570), no
   duplicates.
3. Resumed the remaining 19 chunks via `run_task13_remaining.py` (properly
   `setsid`-detached, PID 3222949, venv on PATH this time — first launch
   attempt failed fast on a `PATH`/venv mismatch before touching the DB,
   caught and relaunched correctly). Completed in 70s, max RSS across the
   whole run **79,164KB (~77MB)**.

**Final checkpoint (backfill_progress, resource='modifier_items_v2'):**

| Metric | Value |
|---|---|
| Chunks | 84/84 success, 0 failed, 0 in_progress |
| `modifier_items_v2` rows | 917,589 |
| Distinct IDs | 917,589 (0 duplicates) |
| Duplicate (order_item_id, modifier_id) groups | 0 |
| `order_item_id → order_items_v2` orphans | 0 |
| `order_id → orders_v2` orphans | 0 |
| Archive pages processed | 5,812 |
| Archive read/decompression errors | 0 |
| Zero-order chunks (est 48 x2, est 54 x4) | all 6 present as success/0-row |
| Peak RSS after streaming fix | ~77MB (was ~906MB) |
| Production `modifier_items` | unchanged — 914,658 rows, untouched |
| Revel API calls made | 0 (archive replay only, Task 10 source) |

Task 13 is DONE. Streaming-page fix is retained in `backfill_modifier_items_v2.py`
for any future reruns/reprocessing. Per `revel_internal_data_backfill_plan.md`,
next in the task dependency chain is **Task 14 — Review Derived Feature Tables**
(not started; plan only requested/produced next).

---

## 2026-08-12 — Pre-cutover orders_v2/order_items_v2 catch-up: COMPLETE

**Status: all 12 establishments caught up, reconciled clean.**

Closed the ~22h gap between orders_v2/order_items_v2's Task 09/10 backfill
snapshot (2026-08-11) and live data, using a new dedicated script
(`catchup_orders_v2.py`) built on the existing Task 07.4 shadow-mode sync
primitives (`sync_updated.sync_orders`/`sync_order_items_and_modifiers`,
`target="shadow_v2"`) rather than the production entrypoint. Piloted on
establishment 40 first, then ran the remaining 11 sequentially with the
same script/rules.

**Final checkpoint:**

| Metric | Value |
|---|---|
| Establishments caught up | 12/12 |
| `orders_v2` (first reconciliation) | 1,083,603 rows, all distinct, 0 duplicates |
| `order_items_v2` (first reconciliation) | 3,966,800 rows, all distinct, 0 duplicates |
| `order_items_v2 → orders_v2` orphans | 0 |
| `payments_v2 → orders_v2` orphans | 4,180 → **24** |
| — of which: snapshot-gap cases (order existed in production, missing from orders_v2) | 4,032 → **0 resolved** |
| — of which: recent timing-edge cases (Aug 12, missing everywhere) | 124 → **0 resolved** |
| — of which: old edge-of-window cases (Feb 10-13, missing everywhere, order never existed in production either) | 24 → **24, unresolved, deferred to Task 17 / later investigation** |
| Production `orders`/`order_items`/`payments`/`order_history`/`modifier_items` | untouched throughout (identical row counts + `max(ingested_at)`) |
| Production/shared `sync_state` watermarks (`resource='orders'`/`'payments'`, est=26) | untouched — only new `resource='orders_v2'` rows appeared, one per establishment |
| Retries | 0 |
| Failures | 0 |
| Peak RSS | ~477 MB (11-establishment run), ~380 MB (pilot) |
| Idempotency | verified — rerun produced 0 duplicate growth; small row-count increases matched real new live orders created in the elapsed wall-clock gap |

`orders_v2`/`order_items_v2` are now a clean, current, duplicate-free, orphan-free
superset of production (in fact ahead of production's freshness, since
production only refreshes via the once-daily 09:00 Central cron).

Next: **Task 14 — authoritative feature recomputation**, AFTER-CATCH-UP
approach as planned, using `orders_v2`/`order_items_v2` as the sole source.

---

## 2026-08-12 — Task 14 complete: authoritative feature recomputation (orders_v2/order_items_v2 -> *_v2)

**Status: COMPLETE.** New shadow tables `features_hourly_v2`, `features_product_daily_v2`,
`features_daily_summary_v2` (migrations/14_feature_tables_v2.sql) populated via
`aggregate_features_v2.py` -- byte-identical SQL/formulas to `aggregate_features.py`,
only source (`orders_v2`/`order_items_v2`) and destination tables changed.

| Metric | Value |
|---|---|
| Establishments | all 12 |
| Date range | 2026-02-09 -> 2026-08-11 (184 days, matching live table's own coverage) |
| `features_hourly_v2` | 28,368 rows |
| `features_product_daily_v2` | 219,236 rows |
| `features_daily_summary_v2` | 1,976 rows |
| Runtime | 8m11s |
| Peak RSS | ~37 MB |
| Disk increase | ~66 MB |
| Errors/failures | 0 |
| Live `features_hourly`/`features_product_daily`/`features_daily_summary` | unchanged throughout (same row counts + `max(computed_at)`) |
| `sales_report.py`/`dashboard.py`/`aggregate_features.py`/`database_design.sql` | zero modifications |

**Reconciliation vs. live tables** found real, explained differences, not noise:
- 3 stale-phantom live rows (Feb 9 -- orders that no longer exist anywhere, live table never revisited)
- 22 shadow-only daily rows / ~2.3K product-level rows, concentrated on 2026-07-13 and
  2026-07-21 -- **real historical ingestion gaps** in production (07-13: 36 orders in
  production vs 2,917 in orders_v2; 07-21: 855 vs 4,009) that Task 09/10's backfill
  recovered but production/live features never did.
- Largest single-day dollar diff (est=36, 2026-07-28, -$1,612.53) traced dollar-exact
  to the duplicate-row partition-key bug: order id 16003344 has two rows in production
  `orders` (stale copy on 07-28, corrected copy on 07-31); orders_v2 correctly holds
  one, on 07-31. Same mechanism confirmed for the #4 diff (est=36, 07-29, -$583.63,
  order id 16028751).
- "Party Pack 100" (est=40) diffs traced to live-table staleness unrelated to v2: item
  is_voided=TRUE in both production and v2 order_items (identical data) -- live's
  stale value predates the void and was never revisited.

Next: **Task 15 -- full reconciliation and cutover-readiness assessment** (plan only
requested first, not yet executed).

---

## 2026-08-12 — Task 15 analytics remediation: COMPLETE

**Issue A (historical void staleness):**
- `features_daily_summary_v2`/`features_product_daily_v2` historical void values confirmed correct.
- Recurrence-prevention "dirty-date" mechanism designed (new `feature_recompute_queue` table,
  lifecycle, failure/retry, idempotency) but **NOT implemented** — carried as a **required
  Task 16 dependency**, not resolved yet.

**Issue B (features_hourly join fan-out):**
- Root cause: `HOURLY_SQL`'s `LEFT JOIN order_items` under `SUM(o.final_total)`/`AVG(o.final_total)`
  multiplied each order's revenue by its own item count.
- Fixed in `aggregate_features_v2.py` only (order_agg/item_agg CTE split, no fan-out possible).
  Live `aggregate_features.py` untouched.
- `features_hourly_v2` regenerated: 28,368 rows, all distinct, 45.1s runtime, ~36MB peak RSS,
  0 errors. `features_product_daily_v2`/`features_daily_summary_v2` untouched (formulas verified
  unaffected).
- Validation: 0 mismatches across 28 independent samples (3 known cases + 25 random), all 15
  metrics checked against independently-written ground truth (not the fix's own SQL).
- Proof: network-wide `SUM(features_hourly_v2.total_revenue)` now exactly equals
  `SUM(features_daily_summary_v2.total_revenue)` per establishment (was off by ~7x before).
- Live `features_hourly`/dashboards/production tables: unchanged throughout.

Next: catch-up plan for `payments_v2`/`order_history_v2`/`modifier_items_v2` (plan only,
requested next, not yet executed) — bringing all three current before the final
Orders/OrderItems freshness pass and Task 16.

---

## 2026-08-12 — Coordinated 5-table shadow freshness pass: COMPLETE (all 12 establishments)

**Status: orders_v2/order_items_v2/payments_v2/order_history_v2/modifier_items_v2 all on one
aligned freshness snapshot.**

Extended `catchup_orders_v2.py` into `catchup_shadow_v2.py` (A. Orders -> B. OrderItems+
ModifierItems -> C. OrderHistory -> D. Payments, sequential per establishment). Required
Phase 1 code hardening first: `sync_payments`/`sync_order_history` gained `target=` params
(previously hardwired to production + shared `sync_state` watermark), and
`sync_order_items_and_modifiers`'s modifier-UPSERT call site was fixed to propagate
`target=` (previously the one remaining gap in this whole project that could silently leak
shadow-mode writes into production `modifier_items`). All three verified via focused
isolation tests with synthetic data before use.

**Final checkpoint:**

| Metric | Value |
|---|---|
| Establishments processed | 12/12 |
| `orders_v2` | 1,084,377 |
| `order_items_v2` | 3,969,612 |
| `payments_v2` | 644,626 |
| `order_history_v2` | 659,555 |
| `modifier_items_v2` | 926,076 |
| count = distinct IDs, all 5 tables | yes, 0 duplicates |
| Non-payment referential orphans (4 checks) | 0 |
| `payments_v2 -> orders_v2` orphans | 24 documented pre-window Task 17 cases + only transient same-day timing-edge cases (proven to self-resolve on rerun: 8 -> 0, replaced by 7 new, net churn only, never accumulating) |
| Retries | 0 |
| Errors | 0 |
| Production tables / live feature tables / dashboards / shared sync_state | unchanged throughout |

Next: **final authoritative v2 feature refresh** (recompute `features_hourly_v2`/
`features_product_daily_v2`/`features_daily_summary_v2` from current `orders_v2`/
`order_items_v2`, corrected hourly formula retained) before Task 16.

---

## 2026-08-12 (late) — Final authoritative v2 feature refresh: COMPLETE

**Status: features_hourly_v2/features_product_daily_v2/features_daily_summary_v2 are a
clean, current, validated shadow of all 12 establishments' latest orders_v2/order_items_v2
state, extended through 2026-08-12.**

| Metric | Value |
|---|---|
| Range | 2026-02-09 -> 2026-08-12 (185 days, all 12 establishments) |
| `features_hourly_v2` | 28,473 rows |
| `features_product_daily_v2` | 220,325 rows |
| `features_daily_summary_v2` | 1,988 rows |
| Runtime | ~7m59s |
| Peak RSS | ~37.5 MB |
| Errors | 0 |
| Validation mismatches | 0 (daily revenue vs direct orders_v2, hourly-summed vs daily revenue
  for all 12 establishments, order counts, void statistics, kitchen-time, representative
  product totals -- all exact matches) |
| Hourly fan-out bug | confirmed eliminated (network-wide, all 12 establishments) |
| Live feature tables / production tables / dashboards | unchanged throughout |

Next: **Task 16 cutover plan** (plan only, requested next, not yet executed).

---

## 2026-08-13 — TASK 16 CUTOVER: COMPLETE — LIVE ON V2

**Status: production cutover executed and validated. All 12 establishments'
Revel data now flows to orders_v2/order_items_v2/payments_v2/
order_history_v2/modifier_items_v2 via the daily cron; features_hourly_v2/
features_product_daily_v2/features_daily_summary_v2 are the live analytics
source for both dashboards.**

**Live configuration:**
- `.env`: `REVEL_SYNC_MODE=updated`, `REVEL_WRITE_TARGET=shadow_v2` (explicit, not implicit defaults)
- `run.sh` (crontab target, unchanged schedule `0 9 * * *`): Revel sync (target=shadow_v2)
  -> yesterday `aggregate_features_v2.py` -> `reprocess_dirty_features_v2.py` drain ->
  weather refresh. Legacy `aggregate_features.py` no longer scheduled.
- `run.sh.pre-task16`: byte-for-byte pre-cutover backup, preserved for rollback.
- Dashboards restarted via systemd (`laynes-dashboard.service` PID 3120500->3268010,
  `laynes-sales-report.service` PID 3012194->3268008), both repointed to `_v2` tables,
  both HTTP 200, smoke-tested with real function calls (not just EXPLAIN).

**Validation performed before and during cutover (all clean, 0 errors/retries throughout):**
- Final coordinated 5-table freshness pass (all 12 establishments)
- Final full v2 feature refresh (186 days) — 0 validation mismatches
- Dirty-date queue drain — race-safe `dirty_at`/`processing_started_at` mechanism
  proven with REAL production data (the real controlled `pipeline.py` sync re-dirtied
  46 of 53 queue rows mid-flight; all cleanly reprocessed to `done`)
- Real controlled `pipeline.py` sync (the actual production entrypoint) — all 12
  establishments, `target=shadow_v2` confirmed in logs, `rows_failed=0` everywhere
- Legacy tables confirmed byte-identical throughout (orders, order_items, payments,
  order_history, modifier_items, features_hourly, features_product_daily,
  features_daily_summary) — none deleted/truncated/renamed, all frozen for rollback

**Final v2 state (as of cutover completion):**
orders_v2=1,087,408, order_items_v2=3,980,850, payments_v2=646,435,
order_history_v2=661,402, modifier_items_v2=929,216 (all count=distinct, 0 duplicates).
`feature_recompute_queue`: 53 done, 0 pending/processing/failed.
`payments_v2->orders_v2` orphans: 24 (all pre-window Task 17 cases).

**Rollback:** commit `7687bed0f282e31016bd563578df6ec4a0bccac0` (Task 16 prerequisites)
+ `442d485eebb9dea932df7601e39bc18c9e809791` (run.sh activation recorded in git).
Remove `REVEL_SYNC_MODE`/`REVEL_WRITE_TARGET` from `.env`, `cp run.sh.pre-task16 run.sh`,
revert dashboard files to pre-`7687bed` state, restart both systemd services.
Estimated rollback time: ~5 minutes.

**Next automatic run:** 2026-08-14, cron fires at 09:00 UTC (displays as ~04:00 in
run.sh's own Central-time-formatted log lines, since `TZ=America/Chicago` propagates
into the job's environment but does NOT appear to shift the actual trigger time on
this system, which fires by system UTC per `timedatectl`) — the FIRST automatic run
on the new v2 sequence.

Next: Task 17 (historical backfill before 2026-02-10) — not started, awaiting direction.
