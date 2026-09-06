# Daily auto-sync design — loyalty + labour (DESIGN ONLY, NOT SCHEDULED)

Both backfills are already resumable and idempotent, so the daily job is the
same code with a narrower window. Nothing below has been scheduled.

## Discovery axis

Historical backfill uses `created_date` (loyalty) and `clock_in` (labour),
because an `updated_date` window only finds later mutations and would miss a
record nobody has touched since creation. The *daily* job has the opposite
requirement — it must catch mutations — so it uses `updated_date__gte`.

## Overlap window

Run at 04:15 America/Chicago with `updated_date__gte = now - 48h`.

* 48h, not 24h: the existing cron's `TZ=America/Chicago` is **not honoured**
  (it runs 09:00 UTC = 04:00 Chicago), and a DST shift or a late-closing store
  moves the effective boundary. A 48h window absorbs both without gaps.
* Overlap is free: both writes are `ON CONFLICT ... DO UPDATE`, so re-seeing a
  record rewrites identical values.

## Why labour especially needs the overlap

`clock_out` is NULL for a shift still open when the window closed. That row is
stored with NULL `worked_seconds`/`estimated_labor_cost` rather than being
skipped or zero-filled, and the next run corrects it once the shift closes.
A 24h no-overlap window would leave overnight shifts permanently open.

## Loyalty PII constraint carries over

The daily loyalty job must keep `resource=None` on `fetch_all_pages`. Raw
archiving stays disabled for that path forever — `gift_reward_data` embeds
plaintext PII that cannot be stripped server-side.

## Checkpointing

Daily runs should use a distinct `backfill_progress` resource suffix
(`order_loyalty_v2_daily`, `timesheet_entries_v2_daily`) so a daily failure
never marks a historical chunk failed, and so `--force` on a backfill does not
replay daily windows.

## Failure handling

Per-chunk failures already log and continue. For the daily job, a chunk marked
`failed` should alert rather than silently retry next day, since a 48h window
will not reach back far enough to self-heal after two consecutive failures.
