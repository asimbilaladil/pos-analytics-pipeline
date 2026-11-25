# 2026-11-23 HME day unreconciled: goal values differ from goal history

This was a read-only investigation on 2026-11-25, around 00:50 Chicago, against
production `c6d743c`. No data was edited, no ingestion was re-run, and goal
history was not touched.

## Current state

There were two runs for 2026-11-23, both `partial`, with `reconciliation_ok = false`:

| Run | Kind | Started (UTC) | Store count | Verified-store count |
|---|---|---|---|---|
| 597 | 05:30 morning run | 11-24 11:32 | 11 | 11 |
| 598 | 10:15 retry | 11-24 16:17 | 11 | 11 |

- **Exactly one reconciliation check fails in both runs:**
  `goal_d_matches_pa_threshold`, with 9 stores differing. The other checks pass:
  PA totals vs Outliers departures 2230 = 2230, store row sums 2230 = 2230,
  Trend vs PA per store 0 differing.
- **The failure persists.** The retry did not change it. No later run exists for
  11-23; the next scheduled run (05:30 on 11-25) is for 11-24.
- **Other days:** 11-18 to 11-21 failed in the morning and reconciled on the
  retry (Trend settling). 11-22 reconciled in the morning.

## Affected stores (2026-11-23)

Observed goals are compared with the recorded goal history. Each existing goal
dates from 2026-09-08 and is open-ended. Rows come from `hme_goal_conflict`
61–69, all unresolved.

| Store (HME number) | Observed A/B/C/D | Recorded A/B/C/D |
|---|---|---|
| LCF Beaumont (00111) | 239 / 251 / 263 / **289** | 240 / 255 / 270 / 285 |
| LCF Rosenberg (1) | 236 / 248 / 261 / **290** | 240 / 255 / 270 / 285 |
| LCF Katy (108) | 240 / 252 / 263 / **288** | 240 / 255 / 270 / 285 |
| LCF Ella (109) | 240 / 252 / 276 / **311** | 240 / 255 / 270 / 285 |
| LCF Shepherd (115) | 239 / 251 / 263 / **334** | 240 / 255 / 270 / 345 |
| LCF Mission Bend (1158614) | 239 / 251 / 263 / **289** | 240 / 255 / 270 / 285 |
| LCF Missouri City (1163022) | 239 / 251 / 263 / **289** | 240 / 255 / 270 / 285 |
| LCF Pasadena (16) | 239 / 251 / 263 / **289** | 240 / 255 / 270 / 285 |
| LCF Airtex (26) | 239 / 251 / 263 / **289** | 240 / 255 / 270 / 285 |

LCF Nederland and LCF Cypress reconciled and are VERIFIED.

### Reading of the evidence

- Seven of the nine stores report the same unusual set: 239 / 251 / 263 / 289.
  None of A, B or C is a round goal. That looks like a value the source computed
  over the day, for example an average across a mid-day goal change or a
  Goals-report aggregation, rather than a new goal someone set.
- Ella and Shepherd differ again. Rosenberg is close to the common set.
- The source cannot tell which reading is right. The loader's four-case
  goal-history rule therefore correctly records a conflict instead of
  overwriting history.
- **It is not corruption.** Car counts, totals and per-store rows all reconcile;
  only the goal thresholds are in question.

## Trust protections (verified)

- `v_hme_store_day_trust_llm` for 11-23: 9 stores `UNVERIFIED`
  (`PARTIAL` / `RECONCILIATION_MISMATCH`), 2 `VERIFIED`.
- `hme_trust_sql.confine_to_verified` was applied to
  `SELECT … FROM v_hme_store_daily_llm WHERE business_date = '2026-11-23'` and
  run read-only:
  - the raw query returns 11 stores;
  - the confined query returns only **26 and 54** (Cypress, Nederland).
- MCP uses a byte-identical copy of `hme_trust_sql.py`, so AI Chat and MCP both
  exclude the 9 unverified store-days.
- Rosenberg on 2026-11-19 (`NO_ACCESS` in both runs) has no trust row at all.
  The guard's positive `EXISTS … VERIFIED` predicate excludes it.

## How the monitor classifies it

| Incident | Severity | Category |
|---|---|---|
| `hme:day_reconciliation:-:2026-11-23` | HIGH | `goal_threshold_mismatch`. A non-settling check still failing after the 10:45 retry window. |
| `hme:store_day_trust:<store>:2026-11-23` for the 9 stores | HIGH | `store_reconciliation_mismatch` |
| `hme:store_day_trust:40:2026-11-19` | HIGH | `no_access` |
| `infrastructure:job:hme-reconciliation-retry` | HIGH | Unit exited non-zero. That is the retry exit-code issue, fixed by `fix/hme-retry-pending-exit` (not deployed). |

This is correct: a goal check is not a source-lag "settling" check, and it
failed after the retry. Notifications group the nine store-days into one
message.

**Lookback note:** 11-23 leaves the 7-day HME lookback on 2026-11-30. After
that, its incidents stay OPEN with an old `last_checked_at`, because nothing
proved the day fixed. Acknowledge them with a note once a decision is made.

## Decision needed (owner; not done here)

- Confirm in the HME portal whether the goals changed on 2026-11-23, or whether
  the report aggregated them.
- Then either:
  - resolve conflicts 61–69 through the established goal-history process (a
    reviewed, audited correction like the Mission Bend one); or
  - accept 11-23 as unverified for those stores.

The monitor resolves its incidents on its own once the trust view shows the
store-days VERIFIED.
