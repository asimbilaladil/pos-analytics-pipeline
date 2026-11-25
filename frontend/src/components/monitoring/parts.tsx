import { useState } from 'react'
import { ChevronDown, ChevronRight, Clock, MapPin, RotateCcw } from 'lucide-react'
import type {
  MonitorFreshness, MonitorIncident, MonitorIncidentDetail, MonitorOverview,
} from '../../types/monitoring'
import {
  affectedLocations, categoryLabel, cellView, healthSummary, incidentStatusLabel, latestMonitorSuccess,
  likelyCause, longDate, plainSummary, SECTION_LABEL, shortDate, STORE_SOURCES,
  type CellState, type IncidentGroup, type Section, type VisibleGroup,
} from '../../lib/monitoringView'

// ── badges & formatting ────────────────────────────────────────────────────
const STYLE: Record<string, string> = {
  CRITICAL: 'bg-red-600 text-white',
  HIGH: 'bg-red-100 text-red-800',
  WARNING: 'bg-amber-100 text-amber-900',
  HEALTHY: 'bg-emerald-100 text-emerald-800',
  OK: 'bg-emerald-100 text-emerald-800',
  PENDING: 'bg-slate-100 text-slate-600',
  DEGRADED: 'bg-amber-100 text-amber-900',
  FAILED: 'bg-red-100 text-red-800',
  FAIL: 'bg-red-100 text-red-800',
  UNKNOWN: 'bg-slate-200 text-slate-700',
  STALE: 'bg-orange-100 text-orange-900',
  NOT_APPLICABLE: 'bg-white text-ink-400 ring-1 ring-inset ring-line',
  NEW: 'bg-brand-50 text-brand-700 ring-1 ring-inset ring-brand-200',
  ACKNOWLEDGED: 'bg-slate-100 text-slate-600',
  PARTLY_ACKNOWLEDGED: 'bg-slate-100 text-slate-600',
  RESOLVED: 'bg-emerald-50 text-emerald-700',
  SUCCESS: 'bg-emerald-100 text-emerald-800',
  RUNNING: 'bg-slate-100 text-slate-600',
  QUEUED: 'bg-slate-100 text-slate-600',
  RETRYING: 'bg-amber-100 text-amber-900',
  SENT: 'bg-emerald-100 text-emerald-800',
  DRY_RUN: 'bg-slate-100 text-slate-600',
}
const LABEL: Record<string, string> = {
  NOT_APPLICABLE: 'N/A', PARTLY_ACKNOWLEDGED: 'PARTLY ACK', DRY_RUN: 'DRY RUN',
}

export function Badge({ v, title }: { v: string; title?: string }) {
  return (
    <span title={title} data-badge={v}
      className={`inline-flex shrink-0 items-center whitespace-nowrap rounded px-1.5 py-0.5 text-[10.5px] font-semibold leading-4 tracking-wide ${STYLE[v] || 'bg-slate-100 text-slate-600'}`}>
      {LABEL[v] ?? v}
    </span>
  )
}

export const when = (s: string | null | undefined) =>
  s ? new Date(s).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }) : '—'
export const ago = (s: string | null | undefined) => {
  if (!s) return 'never'
  const m = Math.round((Date.now() - new Date(s).getTime()) / 60000)
  if (m < 1) return 'just now'
  if (m < 60) return `${m} min ago`
  if (m < 48 * 60) return `${Math.round(m / 60)} h ago`
  return `${Math.round(m / 1440)} d ago`
}
const show = (v: unknown) => (v === null || v === undefined || v === '' ? '—'
  : typeof v === 'string' ? v : typeof v === 'number' || typeof v === 'boolean' ? String(v) : JSON.stringify(v))

const H2 = ({ children, id }: { children: React.ReactNode; id?: string }) => (
  <h2 id={id} className="text-[11px] font-semibold uppercase tracking-wide text-ink-400">{children}</h2>
)

// ── 1. summary strip ───────────────────────────────────────────────────────
export function SummaryStrip({ ov, groups }: { ov: MonitorOverview; groups: IncidentGroup[] }) {
  const h = healthSummary(ov)
  const open = ov.open_incidents
  const fresh = open.filter((i) => i.status === 'OPEN').length
  const acked = open.filter((i) => i.status === 'ACKNOWLEDGED').length
  const loc = affectedLocations(open)
  const last = latestMonitorSuccess(ov)
  const bySev = (['CRITICAL', 'HIGH', 'WARNING'] as const).filter((s) => ov.open_counts[s])
  const fr = (src: string) => ov.freshness?.find((f) => f.source === src)
  const srcState = (src: string) => ov.sources.find((s) => s.source === src)?.state ?? 'UNKNOWN'
  return (
    <section aria-label="Summary" data-testid="summary"
      className="grid grid-cols-2 gap-px overflow-hidden rounded-xl border border-line bg-line shadow-card lg:grid-cols-[1.25fr_1fr_1fr_1fr_1.6fr]">
      <div className="col-span-2 bg-white p-3.5 lg:col-span-1">
        <p className="text-[11px] font-medium text-ink-500">Overall</p>
        <div className="mt-1 flex items-center gap-2" data-testid="overall"><Badge v={h.overall} />
          <span className="text-[12px] text-ink-500">{h.overall === 'HEALTHY' ? 'All checks passing' : 'Needs attention'}</span></div>
        <dl className="mt-2 grid grid-cols-[auto_1fr] items-center gap-x-2 gap-y-1 text-[12px]">
          <dt className="text-ink-500">Business data</dt><dd data-testid="data-health"><Badge v={h.data} /></dd>
          <dt className="text-ink-500">Monitoring</dt><dd data-testid="monitoring-health"><Badge v={h.monitoring} /></dd>
        </dl>
        {h.monitoringReason && <p className="mt-1.5 text-[11px] leading-snug text-amber-800">{h.monitoringReason}</p>}
      </div>
      <div className="bg-white p-3.5">
        <p className="text-[11px] font-medium text-ink-500">Unresolved incidents</p>
        <p className="mt-0.5 text-[22px] font-semibold leading-7 text-ink-900" data-testid="unresolved-count">{open.length}</p>
        <p className="text-[12px] text-ink-500" data-testid="issue-count">
          {groups.length} issue{groups.length === 1 ? '' : 's'} · {fresh} new · {acked} acknowledged</p>
        <div className="mt-1.5 flex flex-wrap gap-1" data-testid="severity-counts">
          {bySev.map((s) => (
            <span key={s} className="inline-flex items-center gap-1 text-[11px] text-ink-600">
              <Badge v={s} /><b className="font-semibold" data-sev={s}>{ov.open_counts[s]}</b></span>))}
        </div>
      </div>
      <div className="bg-white p-3.5">
        <p className="text-[11px] font-medium text-ink-500">Affected locations</p>
        <p className="mt-0.5 text-[22px] font-semibold leading-7 text-ink-900" data-testid="affected-count">{loc.stores}</p>
        <p className="text-[12px] text-ink-500">stores{loc.networkLevel ? ` · ${loc.networkLevel} network-level` : ''}</p>
      </div>
      <div className="col-span-2 bg-white p-3.5 lg:col-span-1">
        <p className="text-[11px] font-medium text-ink-500">Last successful monitor run</p>
        <p className="mt-1 text-[14px] font-semibold text-ink-900" data-testid="last-run">{ago(last)}</p>
        <p className="text-[11px] text-ink-400">{when(last)}</p>
        <div className="mt-1 flex flex-wrap gap-1">
          {Object.entries(ov.monitor.tiers).map(([t, s]) => (
            <span key={t} className="flex items-center gap-1 text-[11px] text-ink-500">{t} <Badge v={s.state} /></span>))}
        </div>
      </div>
      <div className="col-span-2 bg-white p-3.5 lg:col-span-1">
        <p className="text-[11px] font-medium text-ink-500">Data freshness</p>
        <ul className="mt-1 space-y-1" data-testid="freshness-chips">
          {(['revel', 'hme', 'r365'] as const).map((src) => {
            const f = fr(src)
            return (
              <li key={src} className="flex items-center gap-2 text-[12px]">
                <span className="w-[68px] shrink-0 font-medium text-ink-700">{SECTION_LABEL[src as Section]}</span>
                <Badge v={srcState(src)} />
                <span className="truncate text-ink-500">
                  synced {ago(f?.last_sync_at)} · verified {f?.latest_verified_date ? shortDate(f.latest_verified_date) : 'none recent'}
                </span>
              </li>
            )
          })}
        </ul>
      </div>
    </section>
  )
}

// ── 6. source freshness ────────────────────────────────────────────────────
const counts = (c: Partial<Record<string, number>>) =>
  Object.entries(c).filter(([, n]) => n).map(([k, n]) => `${n} ${k === 'FAIL' ? 'failing' : k.toLowerCase()}`).join(', ')
const dateWithLabel = (d: string | null, l: string | null) => (d ? `${shortDate(d)}${l ? ` (${l})` : ''}` : null)

export function SourceFreshness({ ov }: { ov: MonitorOverview }) {
  const rows: MonitorFreshness[] = ov.freshness ?? []
  if (!rows.length) return null
  return (
    <section aria-labelledby="fresh-h" className="rounded-xl border border-line bg-white shadow-card">
      <div className="flex items-baseline justify-between px-4 pb-1.5 pt-3"><H2 id="fresh-h">Data sources: loaded vs verified</H2>
        <span className="hidden text-[11px] text-ink-400 sm:inline">"Verified" = every evaluated store passed for that date</span></div>
      <ul className="divide-y divide-line border-t border-line sm:hidden" data-testid="freshness-cards">
        {rows.map((f) => {
          const st = ov.sources.find((s) => s.source === f.source)
          return (
            <li key={f.source} className="px-4 py-2.5 text-[12.5px]">
              <div className="flex items-center justify-between gap-2"><span className="font-medium text-ink-900">{f.label}</span>
                {st && <Badge v={st.state} />}</div>
              <dl className="mt-1 grid grid-cols-[110px_minmax(0,1fr)] gap-x-2 gap-y-0.5 text-ink-700">
                <dt className="text-ink-500">Last sync</dt><dd>{ago(f.last_sync_at)}</dd>
                <dt className="text-ink-500">Latest expected</dt><dd>{dateWithLabel(f.latest_expected_date, f.latest_expected_label) ?? '—'}</dd>
                <dt className="text-ink-500">Latest checked</dt><dd>{dateWithLabel(f.latest_checked_date, f.latest_checked_label) ?? '—'}
                  {f.latest_checked_date && <span className="text-ink-400"> · {counts(f.latest_checked_counts)}</span>}</dd>
                <dt className="text-ink-500">Latest verified</dt><dd>{f.latest_verified_date
                  ? dateWithLabel(f.latest_verified_date, f.latest_verified_label) : <span className="text-amber-800">None in the monitored window</span>}</dd>
                <dt className="text-ink-500">Monitor check</dt><dd>{ago(f.last_check_at)}{f.retry_pending ? ' · retry pending' : ''}</dd>
              </dl>
            </li>
          )
        })}
      </ul>
      <div className="hidden overflow-x-auto scroll-thin sm:block">
        <table className="w-full min-w-[760px] text-left text-[12.5px]" data-testid="freshness-table">
          <thead><tr className="border-y border-line bg-app/60 text-[10.5px] uppercase tracking-wide text-ink-400">
            <th scope="col" className="px-4 py-1.5 font-semibold">Source</th>
            <th scope="col" className="px-3 py-1.5 font-semibold">Last sync</th>
            <th scope="col" className="px-3 py-1.5 font-semibold">Latest expected</th>
            <th scope="col" className="px-3 py-1.5 font-semibold">Latest checked</th>
            <th scope="col" className="px-3 py-1.5 font-semibold">Latest verified</th>
            <th scope="col" className="px-3 py-1.5 font-semibold">Monitor check</th>
            <th scope="col" className="px-3 py-1.5 font-semibold">Retry</th>
          </tr></thead>
          <tbody>
            {rows.map((f) => {
              const st = ov.sources.find((s) => s.source === f.source)
              return (
                <tr key={f.source} className="border-b border-line last:border-0 align-top" data-source={f.source}>
                  <th scope="row" className="px-4 py-2 font-medium text-ink-900">
                    <div className="flex items-center gap-2">{f.label}{st && <Badge v={st.state} />}</div></th>
                  <td className="px-3 py-2 text-ink-700">{ago(f.last_sync_at)}<div className="text-[11px] text-ink-400">{when(f.last_sync_at)}</div></td>
                  <td className="px-3 py-2 text-ink-700">{dateWithLabel(f.latest_expected_date, f.latest_expected_label) ?? '—'}
                    {!!f.pending.length && <div className="text-[11px] text-ink-400">{f.pending[0]}</div>}</td>
                  <td className="px-3 py-2 text-ink-700">{dateWithLabel(f.latest_checked_date, f.latest_checked_label) ?? '—'}
                    {f.latest_checked_date && <div className="text-[11px] text-ink-400">{counts(f.latest_checked_counts)}</div>}</td>
                  <td className="px-3 py-2" data-testid={`verified-${f.source}`}>
                    {f.latest_verified_date ? <span className="text-ink-700">{dateWithLabel(f.latest_verified_date, f.latest_verified_label)}</span>
                      : <span className="text-amber-800">None in the monitored window</span>}</td>
                  <td className="px-3 py-2 text-ink-700">{ago(f.last_check_at)}
                    {f.monitoring_state && f.monitoring_state !== 'HEALTHY' && <div className="mt-0.5"><Badge v={f.monitoring_state} /></div>}</td>
                  <td className="px-3 py-2">{f.retry_pending
                    ? <span className="inline-flex items-center gap-1 text-[12px] text-ink-700"><RotateCcw size={12} /> pending</span>
                    : <span className="text-ink-400">—</span>}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </section>
  )
}

// ── 5. per-store matrix ────────────────────────────────────────────────────
const CELL_HELP: Record<CellState, string> = {
  HEALTHY: 'Verified on the latest checked date', PENDING: 'Awaiting data or a scheduled retry',
  DEGRADED: 'Warning-level issue', FAILED: 'Unresolved high-severity issue', UNKNOWN: 'No current evidence',
  STALE: 'Monitor checks are stale', NOT_APPLICABLE: 'This source does not cover this store',
}

export function StoreMatrix({ ov, selected, onSelect }: {
  ov: MonitorOverview; selected: number | null; onSelect: (id: number | null) => void
}) {
  const stores = [...ov.stores].sort((a, b) => (a.store_name || '').localeCompare(b.store_name || ''))
  const covered = (src: 'revel' | 'hme' | 'r365') => ov.coverage?.[src]?.length
  return (
    <section aria-labelledby="stores-h" className="rounded-xl border border-line bg-white shadow-card">
      <div className="flex flex-wrap items-baseline justify-between gap-2 px-4 pb-1.5 pt-3">
        <H2 id="stores-h">Store health</H2>
        <span className="text-[11px] text-ink-400">
          {STORE_SOURCES.map((s) => `${SECTION_LABEL[s]} ${covered(s) ?? '?'} stores`).join(' · ')} · click a store to filter incidents</span>
      </div>
      <div className="overflow-x-auto scroll-thin">
        <table className="w-full min-w-[520px] text-left text-[12.5px]" data-testid="store-matrix">
          <thead><tr className="border-y border-line bg-app/60 text-[10.5px] uppercase tracking-wide text-ink-400">
            <th scope="col" className="px-4 py-1.5 font-semibold">Store</th>
            {STORE_SOURCES.map((s) => <th key={s} scope="col" className="px-3 py-1.5 font-semibold">{SECTION_LABEL[s]}</th>)}
          </tr></thead>
          <tbody>
            {stores.map((s) => {
              const active = selected === s.store_id
              return (
                <tr key={s.store_id} data-store={s.store_id}
                  className={`border-b border-line last:border-0 ${active ? 'bg-brand-50' : ''}`}>
                  <th scope="row" className="px-4 py-1.5 font-medium">
                    <button type="button" onClick={() => onSelect(active ? null : s.store_id)} aria-pressed={active}
                      className="rounded text-left text-ink-900 underline-offset-2 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-300">
                      {s.store_name || `Store #${s.store_id}`}</button>
                  </th>
                  {STORE_SOURCES.map((src) => {
                    const c = cellView(ov, s.store_id, src)
                    return (
                      <td key={src} className="px-3 py-1.5" data-cell={`${s.store_id}:${src}`} data-state={c.state}>
                        <div className="flex items-center gap-1.5" title={`${CELL_HELP[c.state]}${c.title ? ` — ${c.title}` : ''}`}>
                          <Badge v={c.state} />
                          {c.state !== 'NOT_APPLICABLE' && c.date && <span className="text-[11px] text-ink-400">{shortDate(c.date)}</span>}
                          {c.openIncidents > 0 && <span className="text-[11px] font-medium text-red-700">{c.openIncidents} open</span>}
                        </div>
                      </td>
                    )
                  })}
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <p className="px-4 py-2 text-[11px] text-ink-400">
        N/A = the source does not cover that store (for example, an HME-unmapped location). Unresolved incidents
        on any date take precedence over the newest checked date.</p>
    </section>
  )
}

// ── 3. incident list ───────────────────────────────────────────────────────
export function GroupRow({ g, selectedId, expanded, onToggle, onOpen }: {
  g: VisibleGroup; selectedId: string | null; expanded: boolean
  onToggle: () => void; onOpen: (target: { group: VisibleGroup } | { incident: MonitorIncident }) => void
}) {
  const multi = g.members.length > 1
  const single = g.members[0]
  const storeCount = g.storeIds.length
  const isSel = selectedId === g.key || (!multi && selectedId === single.id)
  const meta = [
    multi ? `${storeCount} store${storeCount === 1 ? '' : 's'}` : (single.store_name || single.qualifier || 'Network-level'),
    g.business_date ? longDate(g.business_date) : null,
    !multi && single.difference ? single.difference : null,
  ].filter(Boolean).join(' · ')
  const panelId = `grp-${g.key.replace(/[^a-z0-9]/gi, '-')}`
  return (
    <li className="border-b border-line last:border-0" data-group={g.key} data-members={g.members.length}>
      <div className={`flex items-stretch ${isSel ? 'bg-brand-50' : 'hover:bg-app'}`}>
        {multi ? (
          <button type="button" onClick={onToggle} aria-expanded={expanded} aria-controls={panelId}
            aria-label={`${expanded ? 'Collapse' : 'Expand'} ${g.members.length} incidents`}
            className="flex w-8 shrink-0 items-start justify-center pt-3 text-ink-400 hover:text-ink-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-300">
            {expanded ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
          </button>
        ) : <span className="w-8 shrink-0" aria-hidden />}
        <button type="button" onClick={() => onOpen(multi ? { group: g } : { incident: single })}
          className="min-w-0 flex-1 py-2.5 pr-3 text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-brand-300"
          data-testid="group-open">
          <div className="flex flex-wrap items-center gap-1.5">
            <Badge v={g.severity} />
            <Badge v={multi ? g.status : incidentStatusLabel(single.status)} />
            {multi && <span className="text-[11px] font-medium text-ink-500">{g.members.length} incidents</span>}
          </div>
          <p className="mt-1 text-[13px] font-medium leading-snug text-ink-900">{g.title}</p>
          <p className="truncate text-[12px] text-ink-500">{meta}</p>
          <p className="text-[11px] text-ink-400">first detected {ago(g.first_detected_at)} · checked {ago(g.last_checked_at)}</p>
        </button>
      </div>
      {multi && expanded && (
        <ul id={panelId} className="border-t border-line bg-app/50 py-1 pl-8" data-testid="group-members">
          {g.visible.map((m) => (
            <li key={m.id}>
              <button type="button" onClick={() => onOpen({ incident: m })} data-incident={m.id}
                className={`flex w-full items-center gap-2 px-3 py-1.5 text-left text-[12.5px] focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-brand-300 ${selectedId === m.id ? 'bg-brand-50' : 'hover:bg-white'}`}>
                <MapPin size={12} className="shrink-0 text-ink-400" />
                <span className="min-w-0 flex-1 truncate">
                  <span className="font-medium text-ink-900">{m.store_name || 'Network-level'}</span>
                  <span className="text-ink-500"> · {categoryLabel(m.category, m.title)}</span>
                </span>
                <Badge v={incidentStatusLabel(m.status)} />
              </button>
            </li>
          ))}
        </ul>
      )}
    </li>
  )
}

// ── 4. details ─────────────────────────────────────────────────────────────
const Field = ({ k, children }: { k: string; children: React.ReactNode }) => (
  <><dt className="text-ink-500">{k}</dt><dd className="min-w-0 break-words text-ink-900">{children}</dd></>
)

export function GroupDetail({ g, onOpen }: { g: IncidentGroup; onOpen: (i: MonitorIncident) => void }) {
  const head = g.parent ?? g.members[0]
  return (
    <div className="space-y-3 text-[13px]" data-testid="group-detail">
      <div className="flex flex-wrap items-center gap-1.5"><Badge v={g.severity} /><Badge v={g.status} />
        <span className="text-[12px] text-ink-500">{g.members.length} incident records</span></div>
      <h3 className="text-[16px] font-semibold leading-snug text-ink-900">{g.title}</h3>
      <p className="leading-relaxed text-ink-700">{plainSummary(head)}</p>
      <dl className="grid grid-cols-[120px_minmax(0,1fr)] gap-x-3 gap-y-1">
        <Field k="Affected stores">{g.storeIds.length}</Field>
        <Field k="Business date">{g.business_date ? longDate(g.business_date) : '—'}</Field>
        <Field k="First detected">{when(g.first_detected_at)}</Field>
        <Field k="Last checked">{when(g.last_checked_at)}</Field>
        {likelyCause(head) && <Field k="Likely cause">{likelyCause(head)}</Field>}
      </dl>
      <div>
        <p className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-ink-400">Incidents in this group</p>
        <ul className="divide-y divide-line rounded-lg border border-line">
          {g.members.map((m) => (
            <li key={m.id}>
              <button type="button" onClick={() => onOpen(m)} data-incident={m.id}
                className="flex w-full items-center gap-2 px-3 py-1.5 text-left hover:bg-app focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-brand-300">
                <span className="min-w-0 flex-1 truncate"><b className="font-medium">{m.store_name || 'Network-level'}</b>
                  <span className="text-ink-500"> · {categoryLabel(m.category, m.title)}</span></span>
                <Badge v={m.severity} /><Badge v={incidentStatusLabel(m.status)} />
              </button>
            </li>
          ))}
        </ul>
      </div>
      <p className="text-[11px] text-ink-400">Grouping is a display aid only: each incident above keeps its own ID, status and history.</p>
    </div>
  )
}

export function IncidentDetailView({ d, ackNote, setAckNote, onAck, acking }: {
  d: MonitorIncidentDetail; ackNote: string; setAckNote: (s: string) => void; onAck: () => void; acking: boolean
}) {
  const i = d.incident
  const [tech, setTech] = useState(false)
  const cause = likelyCause(i)
  return (
    <div className="space-y-3 text-[13px]" data-testid="incident-detail">
      <div className="flex flex-wrap items-center gap-1.5"><Badge v={i.severity} /><Badge v={incidentStatusLabel(i.status)} />
        <span className="ml-auto font-mono text-[11px] text-ink-400">{i.id}</span></div>
      <h3 className="text-[16px] font-semibold leading-snug text-ink-900">{i.title}</h3>
      <p className="leading-relaxed text-ink-700" data-testid="plain-summary">{plainSummary(i)}</p>
      <dl className="grid grid-cols-[120px_minmax(0,1fr)] gap-x-3 gap-y-1">
        <Field k="Location">{i.store_name || (i.qualifier ? i.qualifier : 'Network-level (all stores)')}</Field>
        <Field k="Source">{SECTION_LABEL[(i.source === 'r365_financial' ? 'r365' : i.source) as Section] ?? i.source}{i.source === 'r365_financial' ? ' (financials)' : ''}</Field>
        <Field k="Business date">{i.business_date ? longDate(i.business_date) : '—'}{i.qualifier && i.store_name ? ` · ${i.qualifier}` : ''}</Field>
        <Field k="Expected">{show(i.expected)}</Field>
        <Field k="Actual">{show(i.actual)}</Field>
        {i.difference && <Field k="Difference"><span className="font-medium">{i.difference}</span></Field>}
        {cause && <Field k="Likely cause">{cause}</Field>}
        <Field k="First detected">{when(i.first_detected_at)}</Field>
        <Field k="Last checked">{when(i.last_checked_at)} <span className="text-ink-400">({i.check_count} checks)</span></Field>
        {i.acknowledged_by && <Field k="Acknowledged">{i.acknowledged_by}, {when(i.acknowledged_at)}{i.ack_note ? ` — “${i.ack_note}”` : ''}</Field>}
        {i.resolved_at && <Field k="Resolved">{when(i.resolved_at)} — {i.resolution}</Field>}
      </dl>
      {!!i.actions.length && (
        <div>
          <p className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-ink-400">Recommended investigation</p>
          <ol className="list-decimal space-y-1 pl-5 text-ink-700">{i.actions.map((a, k) => <li key={k}>{a}</li>)}</ol>
        </div>
      )}
      <div>
        <p className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-ink-400">Timeline</p>
        <ol className="space-y-1 border-l border-line pl-3" data-testid="timeline">
          {d.events.map((e) => (
            <li key={e.id} className="text-[12px] text-ink-700"><span className="flex items-center gap-1 text-ink-400"><Clock size={11} />{when(e.at)}</span>
              <b className="font-medium">{e.event.toLowerCase().replace(/_/g, ' ')}</b> · {e.actor}</li>
          ))}
        </ol>
      </div>
      <div>
        <button type="button" onClick={() => setTech(!tech)} aria-expanded={tech}
          className="flex items-center gap-1 text-[12px] font-medium text-ink-500 hover:text-ink-900 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-300">
          {tech ? <ChevronDown size={13} /> : <ChevronRight size={13} />} Technical evidence</button>
        {tech && (
          <div className="mt-1.5 space-y-1.5" data-testid="technical">
            <p className="font-mono text-[11px] text-ink-500">source={i.source} · check={i.check_name} · category={i.category ?? '—'} · key={i.dedup_key}</p>
            <pre className="max-h-72 overflow-auto rounded-lg bg-app p-2 text-[11px] scroll-thin">{JSON.stringify(i.evidence, null, 2)}</pre>
          </div>
        )}
      </div>
      {i.status === 'OPEN' && (
        <div className="space-y-2 border-t border-line pt-3">
          <label htmlFor="ack-note" className="block text-[12px] font-medium text-ink-700">Acknowledge</label>
          <div className="flex flex-col gap-2 sm:flex-row">
            <input id="ack-note" value={ackNote} onChange={(e) => setAckNote(e.target.value)} maxLength={500}
              placeholder="Note (optional): who is looking at it, or why it is accepted"
              className="min-w-0 flex-1 rounded-lg border border-line px-3 py-1.5 text-[13px] focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-300" />
            <button type="button" onClick={onAck} disabled={acking}
              className="rounded-lg bg-brand-600 px-3 py-1.5 text-[13px] font-medium text-white hover:bg-brand-700 disabled:opacity-60">
              {acking ? 'Acknowledging…' : 'Acknowledge'}</button>
          </div>
        </div>
      )}
      <p className="text-[11px] leading-snug text-ink-400">Acknowledging records that someone is aware. It does not change the
        data-quality verdict or resolve the incident: it is still re-checked, escalations still alert, and it resolves
        only when the data passes. The monitor never repairs data.</p>
    </div>
  )
}

