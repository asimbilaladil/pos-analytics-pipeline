import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Activity, AlertCircle, ArrowLeft, Loader2, RefreshCw, Search, X } from 'lucide-react'
import { api, ApiError } from '../api/client'
import type {
  MonitorIncident, MonitorIncidentDetail, MonitorNotification, MonitorOverview, Severity,
} from '../types/monitoring'
import {
  bySection, countVisible, EMPTY_FILTERS, filterGroups, groupIncidents, SECTION_LABEL, SECTIONS, shortDate, sortGroups,
  type Filters, type IncidentGroup, type Section, type SortKey, type StatusFilter, type VisibleGroup,
} from '../lib/monitoringView'
import {
  Badge, GroupDetail, GroupRow, IncidentDetailView, SourceFreshness, StoreMatrix, SummaryStrip, when,
} from '../components/monitoring/parts'

/**
 * Super-admin data-quality monitoring. Everything shown comes from the data
 * monitor's own incident store; the server enforces super_admin on every call
 * (401/403 otherwise) and this page only decides what to draw. Grouping,
 * filtering and the store matrix are presentation only -- no incident record is
 * merged, hidden from its group, or changed (acknowledging is the one write, and
 * it never changes a verdict).
 */
type Tab = 'incidents' | 'notifications'
type Selection = { kind: 'group'; key: string } | { kind: 'incident'; id: string; fromGroup?: string } | null

const selectCls = 'h-8 rounded-lg border border-line bg-white px-2 text-[12.5px] text-ink-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-300'

export function AdminMonitoring({ onBack, onSignedOut }: { onBack: () => void; onSignedOut: () => void }) {
  const [ov, setOv] = useState<MonitorOverview | null>(null)
  const [loadedAt, setLoadedAt] = useState<string | null>(null)
  const [resolved, setResolved] = useState<MonitorIncident[] | null>(null)
  const [notes, setNotes] = useState<MonitorNotification[] | null>(null)
  const [tab, setTab] = useState<Tab>('incidents')
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS)
  const [sort, setSort] = useState<SortKey>('priority')
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [sel, setSel] = useState<Selection>(null)
  const [detail, setDetail] = useState<MonitorIncidentDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [ackNote, setAckNote] = useState('')
  const [acking, setAcking] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const incidentsRef = useRef<HTMLElement>(null)

  const handleError = useCallback((err: unknown) => {
    if (err instanceof ApiError && err.status === 401) { onSignedOut(); return }
    if (err instanceof ApiError && err.status === 403) {
      setError('Your account does not have access to data monitoring.'); return
    }
    setError(err instanceof ApiError ? err.message : 'Could not reach the server.')
  }, [onSignedOut])

  const needResolved = filters.status === 'resolved' || filters.status === 'all'

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const o = await api.monitorOverview()
      setOv(o); setLoadedAt(new Date().toISOString()); setError(null)
      if (needResolved) setResolved(await api.monitorIncidents('RESOLVED'))
      if (tab === 'notifications') setNotes(await api.monitorNotifications())
    } catch (e) { handleError(e) } finally { setLoading(false) }
  }, [needResolved, tab, handleError])

  useEffect(() => { load() }, [load])
  useEffect(() => {                 // one overview read per minute while the page is open
    const t = setInterval(load, 60_000)
    return () => clearInterval(t)
  }, [load])

  // ── derived ──────────────────────────────────────────────────────────────
  const pool = useMemo<MonitorIncident[]>(() => {
    const open = ov?.open_incidents ?? []
    return needResolved ? [...open, ...(resolved ?? [])] : open
  }, [ov, resolved, needResolved])
  const openGroups = useMemo<IncidentGroup[]>(() => groupIncidents(ov?.open_incidents ?? []), [ov])
  const groups = useMemo<VisibleGroup[]>(() => sortGroups(filterGroups(pool, filters), sort), [pool, filters, sort])
  const sections = useMemo(() => bySection(groups, sort === 'priority'), [groups, sort])
  const vis = countVisible(groups)
  const dates = useMemo(() => [...new Set(pool.map((i) => i.business_date).filter(Boolean) as string[])].sort().reverse(), [pool])
  const stores = useMemo(() => [...(ov?.stores ?? [])].sort((a, b) => (a.store_name || '').localeCompare(b.store_name || '')), [ov])
  const filtered = JSON.stringify({ ...filters, status: 'unresolved' }) !== JSON.stringify(EMPTY_FILTERS) || filters.status !== 'unresolved'

  const selGroup = sel?.kind === 'group' ? groups.find((g) => g.key === sel.key) ?? openGroups.find((g) => g.key === sel.key) ?? null : null

  // ── actions ──────────────────────────────────────────────────────────────
  const set = <K extends keyof Filters>(k: K, v: Filters[K]) => setFilters((f) => ({ ...f, [k]: v }))
  const openIncident = useCallback(async (id: string, fromGroup?: string) => {
    setSel({ kind: 'incident', id, fromGroup }); setAckNote(''); setDetailLoading(true)
    try { setDetail(await api.monitorIncident(id)) } catch (e) { handleError(e); setDetail(null) } finally { setDetailLoading(false) }
  }, [handleError])
  const onOpen = (t: { group: VisibleGroup } | { incident: MonitorIncident }) => {
    if ('group' in t) { setSel({ kind: 'group', key: t.group.key }); setDetail(null); return }
    const parent = groups.find((g) => g.members.length > 1 && g.members.some((m) => m.id === t.incident.id))
    openIncident(t.incident.id, parent?.key)
  }
  const toggle = (key: string) => setExpanded((s) => { const n = new Set(s); if (n.has(key)) n.delete(key); else n.add(key); return n })
  const acknowledge = async () => {
    if (!detail) return
    setAcking(true)
    try {
      await api.monitorAck(detail.incident.id, ackNote || undefined)
      await openIncident(detail.incident.id, sel?.kind === 'incident' ? sel.fromGroup : undefined)
      await load()
    } catch (e) { handleError(e) } finally { setAcking(false) }
  }
  const pickStore = (id: number | null) => {
    set('storeId', id); setTab('incidents')
    if (id !== null) incidentsRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }
  const closeDetail = () => { setSel(null); setDetail(null) }
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape' && sel) closeDetail() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [sel])
  useEffect(() => {
    if (tab === 'notifications' && notes === null) api.monitorNotifications().then(setNotes).catch(handleError)
  }, [tab, notes, handleError])

  // ── render ───────────────────────────────────────────────────────────────
  const header = (
    <header className="flex h-14 shrink-0 items-center gap-2 border-b border-line bg-white px-3 sm:h-16 sm:gap-3 sm:px-6">
      <button type="button" onClick={onBack}
        className="flex h-9 items-center gap-1.5 rounded-lg px-2 text-[13px] font-medium text-ink-700 transition-colors hover:bg-app">
        <ArrowLeft size={15} /> <span className="hidden sm:inline">Back to chat</span>
      </button>
      <span className="h-6 w-px bg-line" aria-hidden />
      <h1 className="flex items-center gap-2 text-[15px] font-semibold text-ink-900">
        <Activity size={16} className="text-brand-500" /> Data monitoring
      </h1>
      <span className="ml-auto hidden text-[11px] text-ink-400 md:inline">{loadedAt ? `Updated ${when(loadedAt)}` : ''}</span>
      <button type="button" onClick={load} disabled={loading} aria-label="Refresh"
        className="flex h-9 items-center gap-1.5 rounded-lg px-2.5 text-[13px] text-ink-600 hover:bg-app max-md:ml-auto">
        <RefreshCw size={14} className={loading ? 'animate-spin' : ''} /> <span className="hidden sm:inline">Refresh</span>
      </button>
    </header>
  )

  if (!ov) {
    return (
      <div className="flex h-[100dvh] flex-col bg-app">
        {header}
        <div className="flex flex-1 items-center justify-center p-6">
          {error ? (
            <div role="alert" className="max-w-md rounded-xl border border-amber-200 bg-amber-50 p-5 text-center text-[13px] text-amber-900" data-testid="load-error">
              <AlertCircle className="mx-auto mb-2" size={20} />
              <p className="font-medium">Monitoring data could not be loaded.</p>
              <p className="mt-1">{error}</p>
              <p className="mt-1 text-[12px] text-amber-800">This is not an all-clear: the status of the data is unknown.</p>
              <button type="button" onClick={load} className="mt-3 rounded-lg bg-white px-3 py-1.5 font-medium ring-1 ring-amber-300 hover:bg-amber-100">Try again</button>
            </div>
          ) : (
            <p className="flex items-center gap-2 text-[13px] text-ink-500" data-testid="loading"><Loader2 size={15} className="animate-spin" /> Loading monitoring status…</p>
          )}
        </div>
      </div>
    )
  }

  const detailOpen = sel !== null
  return (
    <div className="flex h-[100dvh] flex-col overflow-hidden bg-app">
      {header}
      {error && (
        <div role="alert" data-testid="refresh-error" className="mx-3 mt-3 flex items-start gap-2 rounded-xl border border-amber-200 bg-amber-50 px-4 py-2.5 text-[13px] text-amber-900 sm:mx-6">
          <AlertCircle size={15} className="mt-px shrink-0" />
          <span className="flex-1">{error} Showing data last loaded {when(loadedAt)} — it may be out of date.</span>
          <button type="button" onClick={() => setError(null)} aria-label="Dismiss"><X size={14} /></button>
        </div>
      )}
      <main className="min-h-0 flex-1 overflow-y-auto scroll-thin">
        <div className="mx-auto w-full max-w-[1440px] space-y-4 px-3 py-4 sm:px-6">
          <SummaryStrip ov={ov} groups={openGroups} />
          <SourceFreshness ov={ov} />
          <StoreMatrix ov={ov} selected={filters.storeId} onSelect={pickStore} />

          <section ref={incidentsRef} aria-label="Incidents"
            className="grid scroll-mt-4 grid-cols-1 overflow-hidden rounded-xl border border-line bg-white shadow-card lg:grid-cols-[minmax(0,1fr)_minmax(0,1.05fr)]">
            {/* list */}
            <div className={`min-w-0 lg:border-r lg:border-line ${detailOpen ? 'max-lg:hidden' : ''}`}>
              <div className="flex items-center gap-1 border-b border-line px-3 py-2" role="tablist">
                {(['incidents', 'notifications'] as Tab[]).map((t) => (
                  <button key={t} type="button" role="tab" aria-selected={tab === t} onClick={() => setTab(t)}
                    className={`rounded-lg px-2.5 py-1 text-[12.5px] font-medium ${tab === t ? 'bg-brand-50 text-brand-700' : 'text-ink-500 hover:bg-app'}`}>
                    {t === 'incidents' ? 'Incidents' : 'Notifications'}
                  </button>
                ))}
              </div>

              {tab === 'incidents' && (
                <>
                  <div className="space-y-2 border-b border-line px-3 py-2.5" data-testid="filters">
                    <div className="relative">
                      <Search size={14} className="pointer-events-none absolute left-2.5 top-2 text-ink-400" />
                      <input type="search" value={filters.q} onChange={(e) => set('q', e.target.value)}
                        placeholder="Search incidents, stores, IDs…" aria-label="Search incidents"
                        className="h-8 w-full rounded-lg border border-line pl-8 pr-2 text-[12.5px] focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-300" />
                    </div>
                    <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
                      <select aria-label="Filter by status" className={selectCls} value={filters.status} onChange={(e) => set('status', e.target.value as StatusFilter)}>
                        <option value="unresolved">Unresolved</option><option value="new">New</option>
                        <option value="acknowledged">Acknowledged</option><option value="resolved">Resolved</option><option value="all">All</option>
                      </select>
                      <select aria-label="Filter by source" className={selectCls} value={filters.section} onChange={(e) => set('section', e.target.value as Section | '')}>
                        <option value="">All sources</option>
                        {SECTIONS.map((s) => <option key={s} value={s}>{SECTION_LABEL[s]}</option>)}
                      </select>
                      <select aria-label="Filter by severity" className={selectCls} value={filters.severity} onChange={(e) => set('severity', e.target.value as Severity | '')}>
                        <option value="">All severities</option><option value="CRITICAL">Critical</option>
                        <option value="HIGH">High</option><option value="WARNING">Warning</option>
                      </select>
                      <select aria-label="Filter by store" className={selectCls} value={filters.storeId ?? ''} onChange={(e) => set('storeId', e.target.value ? Number(e.target.value) : null)}>
                        <option value="">All stores</option>
                        {stores.map((s) => <option key={s.store_id} value={s.store_id}>{s.store_name || `#${s.store_id}`}</option>)}
                      </select>
                      <select aria-label="Filter by business date" className={selectCls} value={filters.date} onChange={(e) => set('date', e.target.value)}>
                        <option value="">All dates</option>
                        {dates.map((d) => <option key={d} value={d}>{shortDate(d)}</option>)}
                      </select>
                      <select aria-label="Sort incidents" className={selectCls} value={sort} onChange={(e) => setSort(e.target.value as SortKey)}>
                        <option value="priority">Sort: priority</option><option value="newest">Sort: newest</option>
                        <option value="oldest">Sort: oldest</option><option value="business_date">Sort: business date</option>
                      </select>
                    </div>
                    <div className="flex items-center justify-between text-[11.5px] text-ink-500">
                      <span data-testid="list-count">{vis.issues} issue{vis.issues === 1 ? '' : 's'} · {vis.matched} {filtered ? 'matching ' : ''}incident record{vis.matched === 1 ? '' : 's'}
                        {vis.context > 0 && <span data-testid="context-count" className="text-ink-400"> (+{vis.context} network-level shown for context)</span>}</span>
                      {filtered && <button type="button" onClick={() => setFilters(EMPTY_FILTERS)} className="font-medium text-brand-700 hover:underline">Clear filters</button>}
                    </div>
                  </div>
                  <div className="max-h-[70vh] overflow-y-auto scroll-thin" data-testid="incident-list">
                    {needResolved && resolved === null && <p className="p-4 text-[12.5px] text-ink-500">Loading resolved incidents…</p>}
                    {sections.length === 0 ? (
                      <div className="p-8 text-center text-[13px] text-ink-500" data-testid="empty">
                        {filtered ? <>No incidents match these filters. <button type="button" className="font-medium text-brand-700 hover:underline" onClick={() => setFilters(EMPTY_FILTERS)}>Clear filters</button></>
                          : <>No unresolved incidents.
                            {ov.monitor.state !== 'HEALTHY' && <span className="mt-1 block text-amber-800">The monitor itself is {ov.monitor.state}: this is not an all-clear.</span>}</>}
                      </div>
                    ) : sections.map(([sec, gs]) => (
                      <section key={sec} aria-label={SECTION_LABEL[sec]} data-section={sec}>
                        <h3 className="sticky top-0 z-10 flex items-center justify-between border-b border-line bg-app/95 px-3 py-1.5 text-[11px] font-semibold uppercase tracking-wide text-ink-500 backdrop-blur">
                          <span>{SECTION_LABEL[sec]}</span>
                          <span className="font-normal normal-case tracking-normal">{gs.length} issue{gs.length === 1 ? '' : 's'}</span>
                        </h3>
                        <ul>
                          {gs.map((g) => (
                            <GroupRow key={g.key} g={g} expanded={expanded.has(g.key)} onToggle={() => toggle(g.key)}
                              selectedId={sel?.kind === 'group' ? sel.key : sel?.kind === 'incident' ? sel.id : null}
                              onOpen={onOpen} />
                          ))}
                        </ul>
                      </section>
                    ))}
                  </div>
                </>
              )}

              {tab === 'notifications' && (
                <div className="max-h-[70vh] overflow-y-auto scroll-thin" data-testid="notifications">
                  {notes === null ? <p className="p-4 text-[12.5px] text-ink-500">Loading…</p>
                    : notes.length === 0 ? <p className="p-8 text-center text-[13px] text-ink-500">No notifications yet.</p>
                    : notes.map((n) => (
                      <div key={n.id} className="border-b border-line px-4 py-2.5">
                        <p className="flex flex-wrap items-center gap-1.5 text-[11px] text-ink-500">{when(n.created_at)} · {n.kind} · {n.channel} <Badge v={n.status} />
                          {n.attempts ? ` · ${n.attempts} attempt(s)` : ''}{n.status === 'RETRYING' && n.next_attempt_at ? ` · next ${when(n.next_attempt_at)}` : ''}
                          {n.last_error ? ` · ${n.last_error}` : ''}</p>
                        <pre className="mt-1 whitespace-pre-wrap font-sans text-[12px] text-ink-700">{n.body}</pre>
                      </div>
                    ))}
                </div>
              )}
            </div>

            {/* detail */}
            <div className={`min-w-0 p-4 ${detailOpen ? '' : 'max-lg:hidden'}`} data-testid="detail-pane">
              {detailOpen && (
                <div className="mb-3 flex items-center gap-3 text-[12.5px]">
                  <button type="button" onClick={closeDetail} className="flex items-center gap-1 font-medium text-ink-600 hover:text-ink-900 lg:hidden" data-testid="back-to-list">
                    <ArrowLeft size={14} /> All incidents</button>
                  {sel?.kind === 'incident' && sel.fromGroup && (
                    <button type="button" onClick={() => { setSel({ kind: 'group', key: sel.fromGroup! }); setDetail(null) }}
                      className="font-medium text-brand-700 hover:underline">Back to group</button>
                  )}
                </div>
              )}
              {!detailOpen && <p className="p-8 text-center text-[13px] text-ink-400">Select an issue to see what happened, which stores are affected and what to check.</p>}
              {sel?.kind === 'group' && (selGroup
                ? <GroupDetail g={selGroup} onOpen={(m) => openIncident(m.id, selGroup.key)}
                    matchedIds={'matched' in selGroup ? new Set((selGroup as VisibleGroup).matched.map((m) => m.id)) : undefined} />
                : <p className="text-[13px] text-ink-500">This issue is no longer in the current list.</p>)}
              {sel?.kind === 'incident' && (detailLoading && !detail
                ? <p className="flex items-center gap-2 text-[13px] text-ink-500"><Loader2 size={14} className="animate-spin" /> Loading incident…</p>
                : detail ? <IncidentDetailView d={detail} ackNote={ackNote} setAckNote={setAckNote} onAck={acknowledge} acking={acking} />
                : <p className="text-[13px] text-ink-500">The incident could not be loaded.</p>)}
            </div>
          </section>
        </div>
      </main>
    </div>
  )
}
