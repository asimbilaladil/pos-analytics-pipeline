# Dry-run plan: Mission Bend store-scoped backfill, 2026-10-14 … 2026-11-15

Generated read-only on 2026-11-17 against production with
`hme_backfill.py --only-store 1158614 --expect-establishment 25 --start 2026-10-14 --end 2026-11-15 --plan`
(branch code; no lock, no extraction, no writes; migration 64 not required for `--plan`).

- Store: HME `1158614` → establishment `25` (LCF Mission Bend), active VERIFIED mapping.
- **33 dates, all `backfill`**: Mission Bend has no canonical row on any of them; each date's latest per-store grade is FAILED / NO_ACCESS.
- **Network verdicts that must stay as they are after the backfill:** `reconciliation_ok = false` on 2026-10-14, 2026-10-15 and 2026-11-13; every date `partial` (Mission Bend absent from the network runs).
- Re-run with `--end 2026-11-16`: 34 dates, 33 to backfill. 2026-11-16 (Chicago yesterday) is skipped: it belongs to the daily ingest / reconciliation retry.

| Date | MB row | MB latest grade | Network status | Network reconciled | Action |
|---|---|---|---|---|---|
| 2026-10-14 | no | FAILED/NO_ACCESS | partial | False | backfill |
| 2026-10-15 | no | FAILED/NO_ACCESS | partial | False | backfill |
| 2026-10-16 | no | FAILED/NO_ACCESS | partial | True | backfill |
| 2026-10-17 | no | FAILED/NO_ACCESS | partial | True | backfill |
| 2026-10-18 | no | FAILED/NO_ACCESS | partial | True | backfill |
| 2026-10-19 | no | FAILED/NO_ACCESS | partial | True | backfill |
| 2026-10-20 | no | FAILED/NO_ACCESS | partial | True | backfill |
| 2026-10-21 | no | FAILED/NO_ACCESS | partial | True | backfill |
| 2026-10-22 | no | FAILED/NO_ACCESS | partial | True | backfill |
| 2026-10-23 | no | FAILED/NO_ACCESS | partial | True | backfill |
| 2026-10-24 | no | FAILED/NO_ACCESS | partial | True | backfill |
| 2026-10-25 | no | FAILED/NO_ACCESS | partial | True | backfill |
| 2026-10-26 | no | FAILED/NO_ACCESS | partial | True | backfill |
| 2026-10-27 | no | FAILED/NO_ACCESS | partial | True | backfill |
| 2026-10-28 | no | FAILED/NO_ACCESS | partial | True | backfill |
| 2026-10-29 | no | FAILED/NO_ACCESS | partial | True | backfill |
| 2026-10-30 | no | FAILED/NO_ACCESS | partial | True | backfill |
| 2026-10-31 | no | FAILED/NO_ACCESS | partial | True | backfill |
| 2026-11-01 | no | FAILED/NO_ACCESS | partial | True | backfill |
| 2026-11-02 | no | FAILED/NO_ACCESS | partial | True | backfill |
| 2026-11-03 | no | FAILED/NO_ACCESS | partial | True | backfill |
| 2026-11-04 | no | FAILED/NO_ACCESS | partial | True | backfill |
| 2026-11-05 | no | FAILED/NO_ACCESS | partial | True | backfill |
| 2026-11-06 | no | FAILED/NO_ACCESS | partial | True | backfill |
| 2026-11-07 | no | FAILED/NO_ACCESS | partial | True | backfill |
| 2026-11-08 | no | FAILED/NO_ACCESS | partial | True | backfill |
| 2026-11-09 | no | FAILED/NO_ACCESS | partial | True | backfill |
| 2026-11-10 | no | FAILED/NO_ACCESS | partial | True | backfill |
| 2026-11-11 | no | FAILED/NO_ACCESS | partial | True | backfill |
| 2026-11-12 | no | FAILED/NO_ACCESS | partial | True | backfill |
| 2026-11-13 | no | FAILED/NO_ACCESS | partial | False | backfill |
| 2026-11-14 | no | FAILED/NO_ACCESS | partial | True | backfill |
| 2026-11-15 | no | FAILED/NO_ACCESS | partial | True | backfill |
