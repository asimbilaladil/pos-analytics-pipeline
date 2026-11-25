# Data-monitoring dashboard

The page is `frontend/src/pages/AdminMonitoring.tsx`. It is built from:

- the components in `frontend/src/components/monitoring/parts.tsx`;
- the pure presentation logic in `frontend/src/lib/monitoringView.ts`.

It reads the existing admin monitoring API only. There are no new routes. The
overview gains two derived fields, `freshness` and `coverage`, documented in
`docs/MONITORING_API.md`.

## Layout

| Section | Answers |
|---|---|
| **Summary** | Is the system healthy? How many unresolved incidents, how many locations, how fresh is the monitor and each source? |
| **Data sources: loaded vs verified** | For each source: last sync, **expected by now** (from the readiness schedule, with its own results or "not evaluated yet"), latest *evaluated* date (not proof of ingestion), latest date with a full verdict, latest fully verified date, last monitor check, and whether a retry is pending. |
| **Store health** | Per store and per source: HEALTHY, PENDING, DEGRADED, FAILED, UNKNOWN, STALE or N/A. Clicking a store filters the incidents to it. |
| **Incidents** | Issues grouped by source, with filters, search and sort. |
| **Detail** | What happened in plain English, where, when, expected vs actual, likely cause (only when evidenced), what to check, timeline, technical evidence, and Acknowledge. |

On phones:

- the summary stacks;
- the freshness table becomes cards;
- the store table scrolls inside its card;
- choosing an issue replaces the list with its detail, with an "All incidents"
  back link (Escape also closes it).

## Rules that keep it honest

- **Health.** The overall state is the worse of business-data health and
  monitoring health. An UNKNOWN or STALE monitor tier or check can never
  produce "HEALTHY".
  - An empty incident list while the monitor is not healthy says "this is not an
    all-clear".
  - A failed first load says the status is unknown. A failed refresh keeps the
    last data and says it may be out of date.
- **Grouping is presentation only** (`groupIncidents`).
  - The HME network reconciliation failure for a day and its store-level
    reconciliation failures on that day form one issue.
  - Otherwise, incidents group when they share source, check, category,
    business date and qualifier.
  - Every underlying incident keeps its ID, status and timeline, and appears in
    the expanded group and the group detail.
  - Counts show both issues and incident records. A test asserts that the group
    members add up to the record count.
- **Severity.**
  - A group takes its most severe member.
  - Under priority sort, unresolved CRITICAL and HIGH come first, both across
    source sections and within them.
  - New (unacknowledged) incidents rank before acknowledged ones.
- **Expected vs evaluated.** "Expected by now" comes from the same readiness rules
  the checks use (`clock` + config, no database):
  - Revel: each day is due at `revel.ready_after_local` the next day;
  - HME: each day is due at `hme.morning_ready_local` the next day;
  - R365: the week ending Wednesday is due at `r365.week_ready_local` on Thursday;
  - R365 financials: there is no schedule here, so it shows "No schedule".
  If that date has no results, it says "not evaluated yet". An older or newer
  evaluated date never stands in for it.
- **Store cells.**
  - An unresolved incident on any date wins: HIGH or CRITICAL shows FAILED,
    WARNING shows DEGRADED.
  - N/A appears **only** when the source's authoritative store list is known and
    excludes the store. Each check already reads that list and records it in
    the monitor's own `source_status` as `covered_stores`:
    - `revel.freshness`: active establishments;
    - `hme.days`: active VERIFIED `hme_store_mapping`;
    - `r365.weekly`: active VERIFIED `r365_store_mapping`.
  - The dashboard reads only that record; it never queries business tables.
  - If the list is not known yet, for example before the first run after deploy,
    or R365 with no loaded week, every missing cell is UNKNOWN.
  - A mapped store with no evidence is UNKNOWN, or STALE when the source's checks
    are stale. It still gets a row.
  - Otherwise the cell shows the newest result.
- **Filtered counts.**
  - Filters apply per incident record. A group appears when at least one member
    matches.
  - Its count, severity and status come from the **matching** members only, for
    example "1 of 10 incidents match" or "1 of 9 stores match".
  - A network-level parent that does not match itself is shown, labelled
    **context**, and counted separately, for example "3 matching incident records
    (+1 network-level shown for context)".
- **Likely cause** appears only for categories that a check assigns from
  recorded evidence: goal conflict, payment timing, open tickets, NO_ACCESS,
  and a load before the period close.
- **Acknowledge** is the page's only write. It records who is aware and never
  changes the verdict, the severity or the status beyond OPEN → ACKNOWLEDGED.

## Cost

One overview read per minute while the page is open, from the monitor's SQLite
store (`mode=ro`). It never queries PostgreSQL, and a test asserts that.
Resolved incidents are fetched only when the Resolved or All filter is chosen,
and notifications only when that tab is opened.

## Tests

- `tests/test_monitoring_dashboard_frontend.py` runs the real build in Chromium
  against a mocked API. It covers:
  - grouping and expansion, counts, filters, search and sort;
  - store states including N/A, freshness, and acknowledgement;
  - UNKNOWN and STALE, empty states, 401, 403 and 503;
  - refresh failure, the mobile layout (no overflow) and keyboard use.
  - Screenshots are saved when `DASHBOARD_SHOTS` is set.
- `tests/test_monitoring_dashboard_api.py` covers:
  - expected vs evaluated dates against the schedule at different times of day;
  - coverage from the recorded mapping (unknown when absent);
  - each check recording its mapping;
  - no SQL or PostgreSQL in the overview, route authorization, and no new routes.

## Deploy (needs approval)

The changes are:
- the frontend;
- `monitoring/summary.py`, used by `laynes-api` and the daily summary text;
- three checks that now also record their already-read store list in
  `source_status.detail.covered_stores`. No new query, and no change to any
  verdict.

There is no migration, no monitoring-unit change, and no change to ingestion,
reconciliation, thresholds or timers.

Until the first data run after deploy records `covered_stores`, the coverage is
unknown, so missing cells show UNKNOWN, not N/A. That takes up to 15 minutes.

1. Merge the PR into `sidebar-redesign` (after review).
2. Back up the web root:
   `cp -a /var/www/laynes-intelligence /root/deploy-backups/<TS>-frontend-before-<sha>/www`.
3. Move the release checkout (restarts nothing):
   `git -C /root/pos-analytics-pipeline-release checkout --detach <merge-sha>`.
4. Run the go/no-go check (0 queued/processing chat jobs, 0 MCP ask jobs, no
   ingestion, outside the HME and Revel windows), then
   `systemctl restart laynes-api`. MCP needs no restart.
5. Publish: `/root/pos-analytics-pipeline-release/deploy-frontend.sh`.
6. Verify:
   - signed out, `/api/admin/monitoring/overview` returns 401;
   - signed in as super_admin, the page loads, the counts match
     `python -m monitoring status`, and the HME Nov 23 issue shows as one group
     of 10;
   - AI Chat answers one question.

The monitor timers pick up the new `summary.py` on their next run. It only adds
fields to the overview, which the daily summary text ignores.

## Rollback

1. Run the go/no-go check.
2. `git -C /root/pos-analytics-pipeline-release checkout --detach 52c2f6f` (or
   the previous SHA), then `systemctl restart laynes-api`.
3. Restore the web root:
   `rsync -a --delete <backup>/www/ /var/www/laynes-intelligence/`, then
   `chown -R root:www-data` and `chmod 750` on directories and 640 on files.

No data migration needs reverting. Incident records are untouched in both
directions.
