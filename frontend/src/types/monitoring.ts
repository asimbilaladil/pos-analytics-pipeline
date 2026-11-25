// Shapes returned by /api/admin/monitoring/* (backend/monitoring_api.py).

export type Severity = 'CRITICAL' | 'HIGH' | 'WARNING'
export type IncidentStatus = 'OPEN' | 'ACKNOWLEDGED' | 'RESOLVED'
export type ResultState = 'OK' | 'FAIL' | 'PENDING' | 'ACCEPTED' | 'UNKNOWN'

export interface MonitorIncident {
  id: string
  dedup_key: string
  source: string
  check_name: string
  store_id: number | null
  store_name: string | null
  business_date: string | null
  qualifier: string | null
  severity: Severity
  status: IncidentStatus
  title: string
  category: string | null
  expected: unknown
  actual: unknown
  difference: string | null
  evidence: Record<string, unknown>
  actions: string[]
  first_detected_at: string
  last_checked_at: string
  last_changed_at: string
  check_count: number
  acknowledged_at: string | null
  acknowledged_by: string | null
  ack_note: string | null
  resolved_at: string | null
  resolution: string | null
}

export interface MonitorEvent {
  id: number
  at: string
  event: string
  actor: string
  detail: Record<string, unknown>
}

export interface MonitorIncidentDetail {
  incident: MonitorIncident
  events: MonitorEvent[]
}

// Machine-readable operational states (monitoring/summary.py). Absence of
// evidence is UNKNOWN or STALE -- never HEALTHY.
export type OpState = 'HEALTHY' | 'PENDING' | 'DEGRADED' | 'FAILED' | 'UNKNOWN' | 'STALE'

export interface MonitorSourceRow {
  source: string
  label: string
  /** Combined: FAILED if the data fails; otherwise UNKNOWN/STALE if the monitor has no current evidence. */
  state: OpState
  /** Source-data health from incidents and results. */
  data_state: OpState
  /** Whether the monitor's checks of this source are running (operational health). */
  monitoring_state: OpState
  worst_open_severity: Severity | null
  open_incidents: number
  counts: Partial<Record<ResultState, number>>
  /** The source's own latest successful sync. */
  last_success_at: string | null
  detail: Record<string, unknown> | null
  last_successful_check_at: string | null
  oldest_successful_check_at: string | null
  last_attempted_check_at: string | null
  checks: string[]
}

export interface MonitorCheckState {
  check: string
  source: string
  tiers: string[]
  state: OpState
  reason: string | null
  last_success_at: string | null
  last_attempt_at: string | null
  last_error: string | null
  last_duration_s: number | null
  stale_after_minutes: number
}

export interface MonitorTierState {
  state: OpState
  disabled: boolean
  max_minutes: number
  last_success_at: string | null
  last_attempt_at: string | null
  last_attempt_status: 'RUNNING' | 'SUCCESS' | 'DEGRADED' | 'FAILED' | null
  last_attempt_error: string | null
  last_run_id: number | null
}

export interface MonitorSelfStatus {
  state: OpState
  tiers: Record<string, MonitorTierState>
  notifications: {
    state: OpState
    QUEUED: number; RETRYING: number; SENT: number; DRY_RUN: number; FAILED: number
    failed_24h: number
    oldest_undelivered_at: string | null
  }
}

export interface MonitorStoreCell {
  check?: string
  business_date?: string
  state?: ResultState
  severity?: Severity
  title?: string
  difference?: string | null
  open_incidents?: number
  worst_open?: Severity
}

export interface MonitorStoreRow {
  store_id: number
  store_name: string | null
  sources: Partial<Record<'revel' | 'hme' | 'r365', MonitorStoreCell>>
}

export interface MonitorRun {
  tier: string
  started_at: string
  finished_at: string | null
  status: 'RUNNING' | 'SUCCESS' | 'DEGRADED' | 'FAILED'
  ok: number | null
  checks_run: number | null
  checks_failed: number | null
  error: string | null
}

export interface MonitorOverview {
  generated_at: string
  sources: MonitorSourceRow[]
  stores: MonitorStoreRow[]
  open_incidents: MonitorIncident[]
  open_counts: Partial<Record<Severity, number>>
  reconciliation_differences: {
    id: string; source: string; store_name: string | null; business_date: string | null
    difference: string; category: string | null; severity: Severity; status: IncidentStatus
  }[]
  last_runs: Record<string, MonitorRun>
  checks: MonitorCheckState[]
  monitor: MonitorSelfStatus
  states: OpState[]
  /** Loaded vs verified per source (derived from latest results). */
  freshness?: MonitorFreshness[]
  /** store_ids each store-level source evaluated; absent = not covered by that source. */
  coverage?: Partial<Record<StoreSource, number[]>>
}

export type StoreSource = 'revel' | 'hme' | 'r365'

export interface MonitorFreshness {
  source: string
  label: string
  /** The source's own latest successful sync / ingestion. */
  last_sync_at: string | null
  /** The monitor's latest successful check of this source. */
  last_check_at: string | null
  monitoring_state: OpState | null
  latest_expected_date: string | null
  latest_expected_label: string | null
  latest_expected_counts: Partial<Record<ResultState, number>>
  latest_checked_date: string | null
  latest_checked_label: string | null
  latest_checked_counts: Partial<Record<ResultState, number>>
  latest_verified_date: string | null
  latest_verified_label: string | null
  retry_pending: boolean
  pending: string[]
}

export interface MonitorNotification {
  id: number
  created_at: string
  kind: string
  severity: Severity | null
  channel: string
  body: string
  status: 'QUEUED' | 'RETRYING' | 'SENT' | 'DRY_RUN' | 'FAILED'
  attempts: number
  next_attempt_at: string | null
  last_attempt_at: string | null
  last_error: string | null
  sent_at: string | null
}
