// Presentation logic for the data-monitoring dashboard. PURE: no fetching, no
// mutation of incident records. Grouping is display-only -- every underlying
// incident (id, status, audit trail) is kept and reachable from its group.
import type {
  MonitorIncident, MonitorOverview, MonitorStoreCell, OpState, Severity, StoreSource,
} from '../types/monitoring'

// ── vocabulary ─────────────────────────────────────────────────────────────
export const SECTIONS = ['revel', 'hme', 'r365', 'ai_chat', 'mcp', 'infrastructure', 'monitor'] as const
export type Section = typeof SECTIONS[number]

export const SECTION_LABEL: Record<Section, string> = {
  revel: 'Revel POS', hme: 'HME', r365: 'R365', ai_chat: 'AI Chat', mcp: 'MCP',
  infrastructure: 'Infrastructure', monitor: 'Monitor',
}

/** Incident source -> dashboard section (R365 operations + financials share "R365"). */
export function sectionOf(source: string): Section {
  if (source === 'r365_financial') return 'r365'
  return (SECTIONS as readonly string[]).includes(source) ? (source as Section) : 'infrastructure'
}

export const STORE_SOURCES: StoreSource[] = ['revel', 'hme', 'r365']

export const SEV_RANK: Record<Severity, number> = { CRITICAL: 3, HIGH: 2, WARNING: 1 }
const STATUS_RANK: Record<string, number> = { OPEN: 2, ACKNOWLEDGED: 1, RESOLVED: 0 }

// Worst-first ranking of operational states. Absence of evidence (UNKNOWN /
// STALE) outranks DEGRADED, so a roll-up can never show HEALTHY over it.
export const OP_RANK: Record<OpState, number> = {
  HEALTHY: 0, PENDING: 1, DEGRADED: 2, UNKNOWN: 3, STALE: 4, FAILED: 5,
}
export function worstState(states: (OpState | null | undefined)[]): OpState {
  const s = states.filter(Boolean) as OpState[]
  if (!s.length) return 'UNKNOWN'
  return s.reduce((a, b) => (OP_RANK[b] > OP_RANK[a] ? b : a))
}

export const CATEGORY_LABEL: Record<string, string> = {
  goal_threshold_mismatch: 'Goal reconciliation failure',
  store_reconciliation_mismatch: 'Store-day did not reconcile',
  reconciliation_failed: 'Reconciliation failed',
  reconciliation_mismatch: 'Sales vs payments mismatch',
  no_access: 'Store not visible to the HME account',
  no_data: 'No data for the day',
  missing_ingestion: 'Data not ingested',
  ingestion_failed: 'Ingestion failed',
  ingest_error: 'Store ingestion error',
  payment_timing: 'Payment recorded on another day',
  open_unpaid_tickets: 'Open or unpaid tickets',
  source_stale: 'Sync is stale',
  stale_data: 'Data not refreshed',
  stale_after_close: 'Period not refreshed after close',
  latest_refresh_failed: 'Latest refresh failed',
  incomplete: 'Week incomplete',
  coverage_gap: 'Missing store-days',
  job_failed: 'Scheduled job failed',
  job_failures: 'Scheduled job failures',
  unit_failed: 'Service unit failed',
  unit_missing: 'Service unit missing',
  timer_inactive: 'Timer not active',
  service_down: 'Service down',
  down: 'Service down',
  endpoint: 'Endpoint not responding',
  stuck_jobs: 'Stuck chat jobs',
  sql_errors: 'SQL errors',
  provider_errors: 'Model provider errors',
  trust_gate: 'Trust-guard refusals',
  tool_errors: 'MCP tool errors',
  auth_failures: 'Authentication failures',
  db_unreachable: 'Database unreachable',
  disk: 'Disk filling up',
  memory: 'Low memory',
  tls: 'TLS certificate expiring',
  stale_backup: 'Backup is stale',
  no_backup: 'No local backup found',
  backup_unrecognised: 'Backup file not recognised',
  delivery_failed: 'Alert delivery failed',
  delivery_retrying: 'Alert delivery retrying',
  delivery_stuck: 'Alert delivery stuck',
  check_crashed: 'Monitor check failing',
  never_ran: 'Monitor tier never ran',
  stalled: 'Monitor tier stalled',
}
export const categoryLabel = (c: string | null | undefined, fallback = '') =>
  (c && CATEGORY_LABEL[c]) || fallback || (c ? c.replace(/_/g, ' ') : '')

/** One-sentence, plain-English description for operations staff. */
const SUMMARY: Record<string, string> = {
  goal_threshold_mismatch:
    'HME reported goal thresholds for this day that differ from the recorded goal history, so the day could not be reconciled. Affected store-days are excluded from AI answers until resolved.',
  store_reconciliation_mismatch:
    "This store's HME figures for the day did not pass reconciliation, so the store-day is treated as unverified and excluded from AI answers.",
  no_access:
    'HME did not return this store for the day because the account could not see it. The store-day is excluded from AI answers.',
  payment_timing:
    'Sales and payments for this store-day differ because a payment was dated on a different business day than its order.',
  open_unpaid_tickets:
    'Sales and payments for this store-day differ because some tickets were still open or unpaid.',
  reconciliation_mismatch:
    'Sales and payments for this store-day differ by more than the allowed tolerance, and the monitor found no recorded explanation.',
  stale_after_close:
    'The financial period has closed, but R365 data was last loaded before the close, so late postings may be missing.',
  job_failed: "A scheduled job's most recent run ended with an error.",
  source_stale: 'The source has not synced within its expected window.',
  no_data: 'No data was found for this store-day.',
  missing_ingestion: 'The scheduled ingestion has not produced data for this day yet.',
}
export function plainSummary(i: MonitorIncident): string {
  return (i.category && SUMMARY[i.category]) || i.title
}

/** Likely cause -- ONLY for categories the check assigns from recorded evidence. */
const CAUSE: Record<string, string> = {
  goal_threshold_mismatch: 'HME goal values for the day differ from goal history (recorded goal conflict).',
  payment_timing: 'A payment dated on another business day (recorded payment-date evidence).',
  open_unpaid_tickets: 'Open or unpaid tickets account for the gap (recorded ticket evidence).',
  no_access: 'HME reported NO_ACCESS for this store in the ingestion run.',
  stale_after_close: 'The last R365 load happened before the period close date.',
}
export const likelyCause = (i: MonitorIncident): string | null => (i.category && CAUSE[i.category]) || null

// ── dates ──────────────────────────────────────────────────────────────────
const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August',
  'September', 'October', 'November', 'December']
/** "2026-11-23" -> "November 23" (no time-zone shift: business dates are calendar dates). */
export function longDate(d: string | null | undefined): string {
  if (!d) return ''
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(d)
  return m ? `${MONTHS[Number(m[2]) - 1]} ${Number(m[3])}` : d
}
export function shortDate(d: string | null | undefined): string {
  if (!d) return '—'
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(d)
  return m ? `${MONTHS[Number(m[2]) - 1].slice(0, 3)} ${Number(m[3])}` : d
}

// ── grouping ───────────────────────────────────────────────────────────────
export type GroupStatus = 'NEW' | 'PARTLY_ACKNOWLEDGED' | 'ACKNOWLEDGED' | 'RESOLVED'

export interface IncidentGroup {
  key: string
  section: Section
  source: string
  title: string
  category: string | null
  business_date: string | null
  severity: Severity
  status: GroupStatus
  /** Network-level incident the group hangs off, when there is one. */
  parent: MonitorIncident | null
  /** Every underlying incident, parent first then stores. Never merged or dropped. */
  members: MonitorIncident[]
  storeIds: number[]
  first_detected_at: string
  last_checked_at: string
  last_changed_at: string
}

/** Which incidents describe ONE issue. Store-level HME reconciliation failures on a
 *  day are the same issue as that day's network reconciliation failure; otherwise
 *  incidents group when they share source, check, category, date and qualifier. */
export function groupKey(i: MonitorIncident): string {
  if (i.source === 'hme' && i.business_date &&
      (i.check_name === 'day_reconciliation' ||
       (i.check_name === 'store_day_trust' && i.category === 'store_reconciliation_mismatch'))) {
    return `hme|reconciliation|${i.business_date}`
  }
  return [i.source, i.check_name, i.category ?? '', i.business_date ?? '', i.qualifier ?? ''].join('|')
}

export const incidentStatusLabel = (s: string) =>
  s === 'OPEN' ? 'NEW' : s === 'ACKNOWLEDGED' ? 'ACKNOWLEDGED' : s

function groupStatus(members: MonitorIncident[]): GroupStatus {
  const open = members.filter((m) => m.status === 'OPEN').length
  const ack = members.filter((m) => m.status === 'ACKNOWLEDGED').length
  if (!open && !ack) return 'RESOLVED'
  if (open && ack) return 'PARTLY_ACKNOWLEDGED'
  return open ? 'NEW' : 'ACKNOWLEDGED'
}

const minIso = (xs: string[]) => xs.reduce((a, b) => (b < a ? b : a))
const maxIso = (xs: string[]) => xs.reduce((a, b) => (b > a ? b : a))

export function groupIncidents(incidents: MonitorIncident[]): IncidentGroup[] {
  const by = new Map<string, MonitorIncident[]>()
  for (const i of incidents) {
    const k = groupKey(i)
    const list = by.get(k)
    if (list) list.push(i)
    else by.set(k, [i])
  }
  const groups: IncidentGroup[] = []
  for (const [key, list] of by) {
    const members = [...list].sort((a, b) =>
      (a.store_id === null ? -1 : 0) - (b.store_id === null ? -1 : 0) ||
      (a.store_name || '').localeCompare(b.store_name || ''))
    const parent = members.find((m) => m.store_id === null && members.length > 1) ?? null
    const head = parent ?? members[0]
    const severity = members.reduce<Severity>((s, m) => (SEV_RANK[m.severity] > SEV_RANK[s] ? m.severity : s), 'WARNING')
    const storeIds = [...new Set(members.map((m) => m.store_id).filter((x): x is number => x !== null))]
    const section = sectionOf(head.source)
    let title = head.title
    if (members.length > 1) {
      const label = categoryLabel(head.category, head.title)
      title = `${SECTION_LABEL[section]}${head.business_date ? ` — ${longDate(head.business_date)}` : ''}: ${label}`
    }
    groups.push({
      key, section, source: head.source, title, category: head.category,
      business_date: head.business_date, severity, status: groupStatus(members), parent, members,
      storeIds,
      first_detected_at: minIso(members.map((m) => m.first_detected_at)),
      last_checked_at: maxIso(members.map((m) => m.last_checked_at)),
      last_changed_at: maxIso(members.map((m) => m.last_changed_at || m.last_checked_at)),
    })
  }
  return groups
}

// ── filtering & sorting ────────────────────────────────────────────────────
export type StatusFilter = 'unresolved' | 'new' | 'acknowledged' | 'resolved' | 'all'
export type SortKey = 'priority' | 'newest' | 'oldest' | 'business_date'

export interface Filters {
  section: Section | ''
  severity: Severity | ''
  storeId: number | null
  date: string
  status: StatusFilter
  q: string
}
export const EMPTY_FILTERS: Filters = { section: '', severity: '', storeId: null, date: '', status: 'unresolved', q: '' }

export function statusMatches(i: MonitorIncident, s: StatusFilter): boolean {
  switch (s) {
    case 'unresolved': return i.status !== 'RESOLVED'
    case 'new': return i.status === 'OPEN'
    case 'acknowledged': return i.status === 'ACKNOWLEDGED'
    case 'resolved': return i.status === 'RESOLVED'
    default: return true
  }
}

export function incidentMatches(i: MonitorIncident, f: Filters): boolean {
  if (!statusMatches(i, f.status)) return false
  if (f.section && sectionOf(i.source) !== f.section) return false
  if (f.severity && i.severity !== f.severity) return false
  if (f.storeId !== null && i.store_id !== f.storeId) return false
  if (f.date && i.business_date !== f.date) return false
  if (f.q) {
    const hay = [i.id, i.title, i.store_name, i.source, i.check_name, i.category,
      categoryLabel(i.category), i.business_date, i.qualifier, i.difference]
      .filter(Boolean).join(' ').toLowerCase()
    if (!f.q.toLowerCase().split(/\s+/).filter(Boolean).every((w) => hay.includes(w))) return false
  }
  return true
}

export interface VisibleGroup extends IncidentGroup {
  /** Members that match EVERY filter. Counts, severity and status shown for a
   *  filtered group come from these only. */
  matched: MonitorIncident[]
  /** The network-level parent, shown for context when it does not match itself.
   *  Never counted as a match. */
  context: MonitorIncident[]
  /** context + matched, in display order. */
  visible: MonitorIncident[]
  /** Status / severity of the matched members (equal to the group's when unfiltered). */
  shownStatus: GroupStatus
  shownSeverity: Severity
  matchedStoreIds: number[]
}

/** Group first over the whole pool (so an issue is never split by a filter), then
 *  keep groups with at least one matching member. */
export function filterGroups(incidents: MonitorIncident[], f: Filters): VisibleGroup[] {
  return groupIncidents(incidents).flatMap((g) => {
    const matched = g.members.filter((m) => incidentMatches(m, f))
    if (!matched.length) return []
    const context = g.parent && !matched.includes(g.parent) ? [g.parent] : []
    return [{
      ...g, matched, context, visible: [...context, ...matched],
      shownStatus: groupStatus(matched),
      shownSeverity: matched.reduce<Severity>((s, m) => (SEV_RANK[m.severity] > SEV_RANK[s] ? m.severity : s), 'WARNING'),
      matchedStoreIds: [...new Set(matched.map((m) => m.store_id).filter((x): x is number => x !== null))],
    }]
  })
}

export function countVisible(groups: VisibleGroup[]) {
  return {
    issues: groups.length,
    matched: groups.reduce((n, g) => n + g.matched.length, 0),
    context: groups.reduce((n, g) => n + g.context.length, 0),
  }
}

const GROUP_STATUS_RANK: Record<GroupStatus, number> = { NEW: 3, PARTLY_ACKNOWLEDGED: 2, ACKNOWLEDGED: 1, RESOLVED: 0 }

const sev = (g: IncidentGroup) => ('shownSeverity' in g ? (g as VisibleGroup).shownSeverity : g.severity)
const stat = (g: IncidentGroup) => ('shownStatus' in g ? (g as VisibleGroup).shownStatus : g.status)

export function sortGroups<T extends IncidentGroup>(groups: T[], key: SortKey): T[] {
  const unresolved = (g: IncidentGroup) => (stat(g) === 'RESOLVED' ? 0 : 1)
  const cmp: Record<SortKey, (a: T, b: T) => number> = {
    // Unresolved CRITICAL/HIGH first, then new before acknowledged, then most recent change.
    priority: (a, b) => unresolved(b) - unresolved(a) || SEV_RANK[sev(b)] - SEV_RANK[sev(a)] ||
      GROUP_STATUS_RANK[stat(b)] - GROUP_STATUS_RANK[stat(a)] || b.last_changed_at.localeCompare(a.last_changed_at),
    newest: (a, b) => b.first_detected_at.localeCompare(a.first_detected_at),
    oldest: (a, b) => a.first_detected_at.localeCompare(b.first_detected_at),
    business_date: (a, b) => (b.business_date || '').localeCompare(a.business_date || '') ||
      SEV_RANK[sev(b)] - SEV_RANK[sev(a)],
  }
  return [...groups].sort(cmp[key])
}

/** Groups bucketed by source section. With byPriority, sections holding unresolved
 *  CRITICAL/HIGH issues come first (then the fixed Revel, HME, R365... order), so
 *  the most urgent work is never below a list of warnings. */
export function bySection<T extends IncidentGroup>(groups: T[], byPriority = false): [Section, T[]][] {
  const out = SECTIONS.map((s) => [s, groups.filter((g) => g.section === s)] as [Section, T[]])
    .filter(([, gs]) => gs.length > 0)
  if (!byPriority) return out
  const urgency = (gs: T[]) => Math.max(...gs.map((g) => (stat(g) === 'RESOLVED' ? 0 : SEV_RANK[sev(g)])))
  return out.map((x, idx) => ({ x, idx })).sort((a, b) => urgency(b.x[1]) - urgency(a.x[1]) || a.idx - b.idx).map(({ x }) => x)
}

export const sortIncidents = (xs: MonitorIncident[]) => [...xs].sort((a, b) =>
  (STATUS_RANK[b.status] ?? 0) - (STATUS_RANK[a.status] ?? 0) || SEV_RANK[b.severity] - SEV_RANK[a.severity])

// ── health roll-ups ────────────────────────────────────────────────────────
const BUSINESS_SOURCES = ['revel', 'hme', 'r365', 'r365_financial', 'ai_chat', 'mcp', 'infrastructure']

export interface HealthSummary {
  overall: OpState
  data: OpState
  monitoring: OpState
  monitoringReason: string | null
}
/** Business-data health and monitoring health, kept separate; the overall state
 *  is the worse of the two, so stale or missing monitoring is never "HEALTHY". */
export function healthSummary(ov: MonitorOverview): HealthSummary {
  const biz = ov.sources.filter((s) => BUSINESS_SOURCES.includes(s.source))
  const data = worstState(biz.map((s) => s.data_state))
  const monitoring = worstState([ov.monitor.state, ...biz.map((s) => s.monitoring_state)])
  let reason: string | null = null
  if (monitoring !== 'HEALTHY') {
    const tiers = Object.entries(ov.monitor.tiers).filter(([, t]) => t.state !== 'HEALTHY' && !t.disabled)
    const srcs = biz.filter((s) => s.monitoring_state !== 'HEALTHY')
    reason = [...tiers.map(([n, t]) => `${n} tier ${t.state.toLowerCase()}`),
      ...srcs.map((s) => `${s.label} checks ${s.monitoring_state.toLowerCase()}`)].join(' · ') || null
  }
  return { overall: worstState([data, monitoring]), data, monitoring, monitoringReason: reason }
}

/** Distinct stores with an unresolved incident, plus network-level incident count. */
export function affectedLocations(incidents: MonitorIncident[]) {
  const open = incidents.filter((i) => i.status !== 'RESOLVED')
  const stores = new Set(open.map((i) => i.store_id).filter((x) => x !== null))
  return { stores: stores.size, networkLevel: open.filter((i) => i.store_id === null).length }
}

export function latestMonitorSuccess(ov: MonitorOverview): string | null {
  const ts = ['health', 'data'].map((t) => ov.monitor.tiers[t]?.last_success_at).filter(Boolean) as string[]
  return ts.length ? maxIso(ts) : null
}

// ── per-store matrix ───────────────────────────────────────────────────────
export type CellState = OpState | 'NOT_APPLICABLE'

export interface CellView {
  state: CellState
  date: string | null
  title: string | null
  openIncidents: number
  difference: string | null
}

/** Display state of one store x source cell. Open incidents win (a store with an
 *  unresolved HIGH issue is FAILED whatever its newest date says); a store the
 *  source does not cover is NOT_APPLICABLE; no current evidence is UNKNOWN/STALE. */
export function cellView(ov: MonitorOverview, storeId: number, src: StoreSource): CellView {
  const row = ov.stores.find((s) => s.store_id === storeId)
  const c: MonitorStoreCell | undefined = row?.sources[src]
  const open = ov.open_incidents.filter((i) => i.store_id === storeId && i.source === src)
  const worstOpen = open.reduce<Severity | null>((w, i) =>
    (!w || SEV_RANK[i.severity] > SEV_RANK[w] ? i.severity : w), null)
  const base = { date: c?.business_date ?? null, title: c?.title ?? null,
    openIncidents: open.length, difference: c?.difference ?? null }
  if (worstOpen) return { ...base, state: worstOpen === 'WARNING' ? 'DEGRADED' : 'FAILED' }
  // N/A only when the source's AUTHORITATIVE store list is known and excludes the
  // store. An unknown list, or a covered store with no evidence, is UNKNOWN/STALE.
  const cov = ov.coverage?.[src]
  if (cov && !cov.store_ids.includes(storeId)) return { ...base, state: 'NOT_APPLICABLE' }
  const srcRow = ov.sources.find((s) => s.source === src)
  if (srcRow && (srcRow.monitoring_state === 'STALE' || srcRow.monitoring_state === 'UNKNOWN')) {
    return { ...base, state: srcRow.monitoring_state }
  }
  switch (c?.state) {
    case 'OK': case 'ACCEPTED': return { ...base, state: 'HEALTHY' }
    case 'PENDING': return { ...base, state: 'PENDING' }
    case 'FAIL': return { ...base, state: c.severity === 'WARNING' ? 'DEGRADED' : 'FAILED' }
    default: return { ...base, state: 'UNKNOWN' }
  }
}
