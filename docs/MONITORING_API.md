# Data-monitor API

## 1. Current: the admin dashboard API (implemented, in PR #38)

The API is mounted by `backend/api.py` and served by `laynes-api`.

Access:

- Every route requires a session cookie (401 otherwise) and role `super_admin`
  (403 otherwise).
- Routes read only the monitor's SQLite store (`monitoring/store.py`), never the
  business database.

| Method and path | Returns | Store access |
|---|---|---|
| `GET /api/admin/monitoring/overview` | The overview below | read-only (`mode=ro`) |
| `GET /api/admin/monitoring/incidents?status=&source=&limit=` | Incident rows, newest change first (limit ≤ 1000) | read-only |
| `GET /api/admin/monitoring/incidents/{id}` | `{incident, events[]}` | read-only |
| `POST /api/admin/monitoring/incidents/{id}/ack` with body `{note?: string ≤ 500}` | `{acknowledged: id}`; 409 if not OPEN or missing | ack-only writer |
| `GET /api/admin/monitoring/notifications?limit=` | Outbox rows (limit ≤ 500) | read-only |

Errors: 503 means the store is missing, unreadable, corrupt, locked past
10 seconds, or on an unknown schema version, or the config is invalid. The
dashboard shows the message; it never renders an empty all-clear.

### Overview shape

```jsonc
{
  "generated_at": "2026-11-25T16:00:00+00:00",
  "states": ["HEALTHY","PENDING","DEGRADED","UNKNOWN","STALE","FAILED"],
  "monitor": {                                   // the MONITOR's own health
    "state": "HEALTHY",
    "tiers": {
      "health":  {"state": "HEALTHY", "disabled": false, "max_minutes": 15,
                  "last_success_at": "...", "last_attempt_at": "...",
                  "last_attempt_status": "SUCCESS", "last_attempt_error": null,
                  "last_run_id": 812},
      "data":    {...}, "summary": {...}
    },
    "notifications": {"state": "HEALTHY", "QUEUED": 0, "RETRYING": 0, "SENT": 12,
                      "DRY_RUN": 0, "FAILED": 0, "failed_24h": 0,
                      "oldest_undelivered_at": null}
  },
  "sources": [{                                   // every known source, always
    "source": "revel", "label": "Revel POS",
    "state": "FAILED",                            // combined (see docs/MONITORING.md)
    "data_state": "FAILED",                       // source-data health
    "monitoring_state": "HEALTHY",                // are the checks of it running?
    "worst_open_severity": "HIGH", "open_incidents": 3,
    "counts": {"OK": 30, "FAIL": 3},
    "last_success_at": "...",                     // the SOURCE's own latest sync
    "detail": {...},
    "last_successful_check_at": "...", "oldest_successful_check_at": "...",
    "last_attempted_check_at": "...", "checks": ["revel.freshness", "revel.reconciliation"]
  }],
  "checks": [{"check": "revel.reconciliation", "source": "revel", "state": "HEALTHY",
              "reason": null, "last_success_at": "...", "last_attempt_at": "...",
              "last_error": null, "last_duration_s": 20.3, "stale_after_minutes": 180}],
  "stores": [...], "open_incidents": [...], "open_counts": {...},
  "reconciliation_differences": [...], "last_runs": {...},
  // Presentation helpers derived from the latest results (no extra queries):
  "freshness": [{"source": "hme", "label": "HME drive-thru",
                 "last_sync_at": "...", "last_check_at": "...", "monitoring_state": "HEALTHY",
                 "expected_date": "2026-11-23",                   // due by now per the readiness schedule
                 "expected_rule": "each day is due 06:30 Chicago the next day",   // null/None: no schedule (R365 financials)
                 "expected_counts": {"OK": 2, "FAIL": 9},         // results ON expected_date; {} = not evaluated yet
                 "latest_evaluated_date": "2026-11-24",           // newest date the monitor evaluated (NOT proof of ingestion)
                 "latest_evaluated_label": null,                  // e.g. "FY2026 P12" for R365 financials
                 "latest_evaluated_counts": {"PENDING": 12},
                 "latest_checked_date": "2026-11-23",             // newest with nothing PENDING
                 "latest_checked_label": null,
                 "latest_checked_counts": {"OK": 2, "FAIL": 9},
                 "latest_verified_date": "2026-11-22",            // newest where EVERY result is OK/ACCEPTED; null if none
                 "latest_verified_label": null,
                 "retry_pending": false,                          // a PENDING "settling" result awaits the retry
                 "pending": ["Awaiting HME morning ingestion"]}],
  // Authoritative store list per source, as the source's own check last read it
  // (Revel: active establishments; HME, R365: active VERIFIED mappings), stored in
  // the monitor's source_status. null = not known yet (cells then show UNKNOWN, never N/A).
  "coverage": {"hme": {"basis": "mapping", "as_of": "...", "store_ids": [6, 7, ...],
                       "stores": [{"id": 6, "name": "LCF Katy"}, ...]},
               "revel": {...}, "r365": null}
}
```

## 2. Future: a read-only status API for n8n (designed here, NOT built)

The plan is for n8n to schedule checks after each Revel, HME and R365
ingestion, and to send e-mail. It needs a machine contract that does not depend
on an administrator's browser session.

### Rules

1. **Separate credential.** A dedicated read-only API key (`X-Monitor-Key`),
   stored hashed, used for nothing else:
   - not an admin session;
   - not an MCP key;
   - not the PostgreSQL role.
   It allows `GET` only, on `/api/monitor/v1/*` only. It can be revoked on its
   own, and it is optionally restricted to n8n's source address at nginx.
2. **Read-only.** No acknowledgement, no re-run, no repair, no ingestion trigger.
   It reads the same SQLite store, opened `mode=ro`.
3. **HTTP 200 never means "data OK".** 200 means only "this document was
   produced". The answer is in `state`, which can be `FAILED`, `UNKNOWN` or
   `STALE` under a 200.
   - n8n must branch on `state` and on `monitor.state`.
   - An unreachable API, a non-200, or a `generated_at` older than 10 minutes
     is itself an alert in n8n. That is the dead-man check: it does not rely on
     the monitor's own delivery.
4. **Stable identifiers.**
   - `incident_id` and `dedup_key` never change for an issue's lifetime.
   - A resolution is the same `incident_id` with `status: RESOLVED`, so n8n
     sends at most one alert and one resolution per incident.
   - A new failure of the same issue after a resolution gets a new
     `incident_id`.
5. **Freshness is explicit.** Every source object carries the monitor's last
   successful check time and the source's last ingestion time. The consumer
   never infers freshness from the time of the HTTP request.

### Endpoints (proposed)

| Endpoint | Purpose |
|---|---|
| `GET /api/monitor/v1/status` | Everything below in one document (n8n's main poll) |
| `GET /api/monitor/v1/sources/{source}?business_date=YYYY-MM-DD` | One source, for one business date or financial period (`?period=FY2026-P11` for R365 financial) |
| `GET /api/monitor/v1/incidents?status=OPEN,ACKNOWLEDGED&changed_since=ISO` | Incremental incident feed, for alert and resolution notices |

### `GET /api/monitor/v1/status` (proposed shape)

```jsonc
{
  "schema": "laynes-monitor-status/1",
  "generated_at": "2026-11-26T11:02:11Z",
  "monitor": {
    "state": "HEALTHY",                               // HEALTHY|PENDING|DEGRADED|FAILED|UNKNOWN|STALE
    "version": "laynes_monitor/1.1.0",
    "tiers": {"health": {"state": "HEALTHY", "last_success_at": "…", "last_attempt_at": "…",
                         "last_attempt_status": "SUCCESS", "max_minutes": 15}, "data": {…}, "summary": {…}},
    "notifications": {"state": "HEALTHY", "failed_24h": 0, "retrying": 0}
  },
  "sources": [
    {
      "source": "hme",
      "state": "FAILED",                              // combined
      "data_state": "FAILED", "monitoring_state": "HEALTHY",
      "expected": {"business_date": "2026-11-25", "ready_after": "2026-11-26T12:30:00Z",
                   "final_after": "2026-11-26T16:45:00Z"},
      "ingestion": {
        "latest_run_id": "598",                       // source-native id, as text
        "completion_status": "partial",               // the source's own status, verbatim
        "reconciliation_ok": false,
        "finished_at": "2026-11-24T16:23:00Z",
        "last_success_at": "2026-11-23T11:37:06Z"
      },
      "stores": {"expected": 11, "loaded": 11, "verified": 2},
      "store_status": [
        {"store_id": 6, "store_name": "LCF Katy", "state": "FAILED",
         "category": "store_reconciliation_mismatch", "incident_id": "INC-20261125-…"}
      ],
      "incidents": {"open": ["INC-…"], "acknowledged": []},
      "monitor_checked_at": "2026-11-26T11:00:04Z",
      "stale_after": "2026-11-26T11:45:04Z"
    }
    // revel: expected.business_date, ingestion.latest_run_id = sync window, per-store reconciliation
    // r365:  expected.week_start/week_end, ingestion.latest_run_id = r365_ingest_run.id
    // r365_financial: expected.period = "FY2026 P11", period_end, loaded_after_close
  ],
  "incidents": [
    {"incident_id": "INC-20261125-1A2B3C", "dedup_key": "hme:store_day_trust:6:2026-11-23",
     "source": "hme", "check": "store_day_trust", "store_id": 6, "business_date": "2026-11-23",
     "severity": "HIGH", "status": "OPEN", "category": "store_reconciliation_mismatch",
     "first_detected_at": "…", "last_checked_at": "…", "last_changed_at": "…",
     "resolved_at": null}
  ]
}
```

### What exists today to support it

- `summary.overview()` already computes every `state`, `data_state`,
  `monitoring_state`, per-check freshness, tier status and outbox health.
- `incidents` already carries a stable `id`, `dedup_key`, status timestamps and
  `resolved_at`.

Not built yet:

- the per-source `ingestion` block (latest run ID, completion status, expected
  and loaded store counts);
- the API key;
- the route.

The checks already read these values. They would need to be recorded in
`source_status.detail` in a fixed shape. That is a small change, scheduled with
the n8n work.

### Not in scope for the n8n API

- triggering ingestion or retries;
- acknowledging incidents (admin UI only);
- any free-form SQL or shell;
- any business figures beyond the reconciliation difference already shown on
  the dashboard.
