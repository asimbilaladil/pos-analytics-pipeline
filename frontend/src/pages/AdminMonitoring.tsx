import { useCallback, useEffect, useState } from 'react'
import { Activity, AlertCircle, ArrowLeft, CheckCircle2, Clock, RefreshCw } from 'lucide-react'
import { api, ApiError } from '../api/client'
import type {
  MonitorIncident, MonitorIncidentDetail, MonitorNotification, MonitorOverview, MonitorStoreCell,
} from '../types/monitoring'

/**
 * Super-admin data-quality monitoring. Everything shown comes from the data
 * monitor's own incident store; the server enforces super_admin on every call
 * (401/403 otherwise) and this page only decides what to draw.
 */
const SEV_STYLE: Record<string, string> = {
  CRITICAL: 'bg-red-600 text-white',
  HIGH: 'bg-red-100 text-red-800',
  WARNING: 'bg-amber-100 text-amber-900',
  OK: 'bg-emerald-100 text-emerald-800',
  PENDING: 'bg-slate-100 text-slate-600',
  ACCEPTED: 'bg-slate-100 text-slate-500',
  FAIL: 'bg-red-100 text-red-800',
}

function Badge({ v }: { v: string }) {
  return (
    <span className={`inline-block rounded px-1.5 py-0.5 text-[11px] font-semibold ${SEV_STYLE[v] || 'bg-slate-100'}`}>
      {v}
    </span>
  )
}

const when = (s: string | null | undefined) => (s ? new Date(s).toLocaleString() : '—')
const ago = (s: string | null | undefined) => {
  if (!s) return '—'
  const m = Math.round((Date.now() - new Date(s).getTime()) / 60000)
  if (m < 60) return `${m} min ago`
  if (m < 48 * 60) return `${Math.round(m / 60)} h ago`
  return `${Math.round(m / 1440)} d ago`
}
const show = (v: unknown) => (v === null || v === undefined ? '—' : typeof v === 'string' ? v : JSON.stringify(v))

type Tab = 'open' | 'history' | 'notifications'

export function AdminMonitoring({ onBack, onSignedOut }: { onBack: () => void; onSignedOut: () => void }) {
  const [ov, setOv] = useState<MonitorOverview | null>(null)
  const [tab, setTab] = useState<Tab>('open')
  const [history, setHistory] = useState<MonitorIncident[]>([])
  const [notes, setNotes] = useState<MonitorNotification[]>([])
  const [detail, setDetail] = useState<MonitorIncidentDetail | null>(null)
  const [ackNote, setAckNote] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  const handleError = useCallback((err: unknown) => {
    if (err instanceof ApiError && err.status === 401) { onSignedOut(); return }
    setError(err instanceof ApiError ? err.message : 'Something went wrong.')
  }, [onSignedOut])

  const load = useCallback(async () => {
    setLoading(true); setError(null)
    try {
      setOv(await api.monitorOverview())
      if (tab === 'history') setHistory(await api.monitorIncidents())
      if (tab === 'notifications') setNotes(await api.monitorNotifications())
    } catch (e) { handleError(e) } finally { setLoading(false) }
  }, [tab, handleError])

  useEffect(() => { load() }, [load])
  useEffect(() => {                 // refresh every 60 s while open
    const t = setInterval(load, 60_000)
    return () => clearInterval(t)
  }, [load])

  const openIncident = async (id: string) => {
    try { setDetail(await api.monitorIncident(id)); setAckNote('') } catch (e) { handleError(e) }
  }
  const acknowledge = async () => {
    if (!detail) return
    try {
      await api.monitorAck(detail.incident.id, ackNote)
      await openIncident(detail.incident.id)
      await load()
    } catch (e) { handleError(e) }
  }

  const cell = (c?: MonitorStoreCell) => {
    if (!c || (!c.state && !c.open_incidents)) return <span className="text-ink-300">—</span>
    return (
      <div className="space-y-0.5">
        <div className="flex items-center gap-1.5">
          {c.state && <Badge v={c.state === 'FAIL' && c.severity ? c.severity : c.state} />}
          {c.business_date && <span className="text-[11px] text-ink-400">{c.business_date}</span>}
        </div>
        {c.difference && <div className="text-[11px] text-ink-600">{c.difference}</div>}
        {!!c.open_incidents && (
          <div className="text-[11px] text-red-700">{c.open_incidents} open ({c.worst_open})</div>
        )}
      </div>
    )
  }

  const incidentRow = (i: MonitorIncident) => (
    <button key={i.id} onClick={() => openIncident(i.id)}
      className={`grid w-full grid-cols-[86px_minmax(0,1fr)] gap-2 border-b border-line px-4 py-2.5 text-left
                  transition-colors hover:bg-app ${detail?.incident.id === i.id ? 'bg-brand-50' : ''}`}>
      <div className="space-y-1"><Badge v={i.severity} />
        {i.status !== 'OPEN' && <div className="text-[10px] text-ink-400">{i.status}</div>}</div>
      <div className="min-w-0">
        <p className="truncate text-[13px] font-medium text-ink-900">{i.title}</p>
        <p className="truncate text-[12px] text-ink-500">
          {i.store_name || i.qualifier || 'network'} · {i.source}{i.business_date ? ` · ${i.business_date}` : ''}
          {i.difference ? ` · ${i.difference}` : ''}
        </p>
        <p className="text-[11px] text-ink-400">first seen {ago(i.first_detected_at)} · checked {ago(i.last_checked_at)}</p>
      </div>
    </button>
  )

  return (
    <div className="flex h-[100dvh] flex-col overflow-hidden bg-canvas-50">
      <header className="flex h-16 shrink-0 items-center gap-3 border-b border-line bg-white px-4 sm:px-6">
        <button onClick={onBack}
          className="flex h-9 items-center gap-1.5 rounded-lg px-2.5 text-[13px] font-medium text-ink-700 transition-colors hover:bg-app">
          <ArrowLeft size={15} /> Back to chat
        </button>
        <span className="h-6 w-px bg-line" aria-hidden />
        <h1 className="flex items-center gap-2 text-[15px] font-semibold text-ink-900">
          <Activity size={16} className="text-brand-500" /> Data monitoring
        </h1>
        <button onClick={load} disabled={loading}
          className="ml-auto flex h-9 items-center gap-1.5 rounded-lg px-2.5 text-[13px] text-ink-600 hover:bg-app">
          <RefreshCw size={14} className={loading ? 'animate-spin' : ''} /> Refresh
        </button>
      </header>

      {error && (
        <div role="alert" className="mx-4 mt-3 flex items-start gap-2 rounded-xl border border-amber-200 bg-amber-50 px-4 py-2.5 text-[13px] text-amber-900">
          <AlertCircle size={15} className="mt-px shrink-0" /><span className="flex-1">{error}</span>
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-y-auto scroll-thin">
        <div className="mx-auto w-full max-w-[1400px] space-y-5 px-4 py-5 sm:px-6">
          {/* Sources + monitor heartbeat */}
          <section className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
            {ov?.sources.map((s) => (
              <div key={s.source} className="rounded-xl border border-line bg-white p-3.5">
                <div className="flex items-center justify-between">
                  <p className="text-[13px] font-semibold text-ink-900">{s.label}</p>
                  <Badge v={s.health} />
                </div>
                <p className="mt-1 text-[12px] text-ink-500">
                  ok {s.counts.OK ?? 0} · pending {s.counts.PENDING ?? 0} · failing {s.counts.FAIL ?? 0}
                  {s.open_incidents ? ` · ${s.open_incidents} open` : ''}
                </p>
                <p className="mt-0.5 flex items-center gap-1 text-[11px] text-ink-400">
                  <Clock size={11} /> latest successful sync {s.last_success_at ? `${when(s.last_success_at)} (${ago(s.last_success_at)})` : '—'}
                </p>
              </div>
            ))}
            <div className="rounded-xl border border-line bg-white p-3.5">
              <p className="text-[13px] font-semibold text-ink-900">Monitor runs</p>
              {ov && Object.values(ov.last_runs).map((r) => (
                <p key={r.tier} className="mt-1 flex items-center gap-1.5 text-[12px] text-ink-600">
                  {r.ok && !r.error ? <CheckCircle2 size={12} className="text-emerald-600" />
                    : <AlertCircle size={12} className="text-red-600" />}
                  {r.tier}: {ago(r.finished_at)}{r.error ? ` — ${r.error}` : ''}
                </p>
              ))}
              {ov && Object.keys(ov.last_runs).length === 0 && <p className="text-[12px] text-ink-400">No runs yet.</p>}
            </div>
          </section>

          {/* Per-store status */}
          <section className="overflow-x-auto rounded-xl border border-line bg-white">
            <h2 className="px-4 pb-2 pt-3.5 text-[11px] font-semibold uppercase tracking-wide text-ink-400">Per-store status (latest checked business day)</h2>
            <table className="w-full min-w-[640px] text-left text-[13px]">
              <thead><tr className="border-y border-line text-[11px] uppercase text-ink-400">
                <th className="px-4 py-2">Store</th><th className="px-4 py-2">Revel POS</th>
                <th className="px-4 py-2">HME</th><th className="px-4 py-2">R365</th></tr></thead>
              <tbody>
                {ov?.stores.map((s) => (
                  <tr key={s.store_id} className="border-b border-line align-top">
                    <td className="px-4 py-2 font-medium text-ink-900">{s.store_name || `#${s.store_id}`}</td>
                    <td className="px-4 py-2">{cell(s.sources.revel)}</td>
                    <td className="px-4 py-2">{cell(s.sources.hme)}</td>
                    <td className="px-4 py-2">{cell(s.sources.r365)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>

          {/* Reconciliation differences */}
          {!!ov?.reconciliation_differences.length && (
            <section className="rounded-xl border border-line bg-white">
              <h2 className="px-4 pb-2 pt-3.5 text-[11px] font-semibold uppercase tracking-wide text-ink-400">Unresolved reconciliation differences</h2>
              {ov.reconciliation_differences.map((r) => (
                <button key={r.id} onClick={() => openIncident(r.id)}
                  className="flex w-full items-center gap-3 border-t border-line px-4 py-2 text-left text-[13px] hover:bg-app">
                  <Badge v={r.severity} />
                  <span className="w-40 truncate font-medium">{r.store_name || 'network'}</span>
                  <span className="w-24 text-ink-500">{r.business_date}</span>
                  <span className="flex-1 truncate">{r.difference}</span>
                  <span className="hidden text-[12px] text-ink-400 sm:inline">{r.category}</span>
                </button>
              ))}
            </section>
          )}

          {/* Incidents + detail */}
          <section className="grid min-h-[420px] grid-cols-1 overflow-hidden rounded-xl border border-line bg-white lg:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)]">
            <div className="min-h-0 border-r border-line">
              <div className="flex gap-1 border-b border-line px-3 py-2">
                {(['open', 'history', 'notifications'] as Tab[]).map((t) => (
                  <button key={t} onClick={() => setTab(t)}
                    className={`rounded-lg px-2.5 py-1 text-[12px] font-medium ${tab === t ? 'bg-brand-50 text-brand-700' : 'text-ink-500 hover:bg-app'}`}>
                    {t === 'open' ? `Unresolved (${ov?.open_incidents.length ?? 0})` : t === 'history' ? 'History' : 'Notifications'}
                  </button>
                ))}
              </div>
              <div className="max-h-[640px] overflow-y-auto scroll-thin">
                {tab === 'open' && (ov?.open_incidents.length
                  ? ov.open_incidents.map(incidentRow)
                  : <p className="p-6 text-center text-[13px] text-ink-400">No unresolved incidents.</p>)}
                {tab === 'history' && history.map(incidentRow)}
                {tab === 'notifications' && notes.map((n) => (
                  <div key={n.id} className="border-b border-line px-4 py-2.5">
                    <p className="text-[11px] text-ink-400">{when(n.created_at)} · {n.kind} · {n.channel} · <b>{n.status}</b>
                      {n.last_error ? ` · ${n.last_error}` : ''}</p>
                    <pre className="mt-1 whitespace-pre-wrap text-[12px] text-ink-700">{n.body}</pre>
                  </div>
                ))}
              </div>
            </div>
            <div className="min-h-0 overflow-y-auto p-4 scroll-thin">
              {!detail ? (
                <p className="p-6 text-center text-[13px] text-ink-400">Select an incident for evidence and investigation steps.</p>
              ) : (
                <div className="space-y-3 text-[13px]">
                  <div className="flex items-center gap-2"><Badge v={detail.incident.severity} />
                    <span className="text-[12px] text-ink-500">{detail.incident.status}</span>
                    <span className="ml-auto font-mono text-[11px] text-ink-400">{detail.incident.id}</span></div>
                  <h3 className="text-[15px] font-semibold text-ink-900">{detail.incident.title}</h3>
                  <dl className="grid grid-cols-[130px_minmax(0,1fr)] gap-x-3 gap-y-1">
                    <dt className="text-ink-400">Store</dt><dd>{detail.incident.store_name || '—'}</dd>
                    <dt className="text-ink-400">Source / check</dt><dd>{detail.incident.source} / {detail.incident.check_name}</dd>
                    <dt className="text-ink-400">Business date</dt><dd>{detail.incident.business_date || '—'}{detail.incident.qualifier ? ` · ${detail.incident.qualifier}` : ''}</dd>
                    <dt className="text-ink-400">Expected</dt><dd>{show(detail.incident.expected)}</dd>
                    <dt className="text-ink-400">Actual</dt><dd className="break-words">{show(detail.incident.actual)}</dd>
                    <dt className="text-ink-400">Difference</dt><dd>{detail.incident.difference || '—'}</dd>
                    <dt className="text-ink-400">Category</dt><dd>{detail.incident.category || '—'}</dd>
                    <dt className="text-ink-400">First detected</dt><dd>{when(detail.incident.first_detected_at)}</dd>
                    <dt className="text-ink-400">Last checked</dt><dd>{when(detail.incident.last_checked_at)} ({detail.incident.check_count} checks)</dd>
                    {detail.incident.acknowledged_by && (<><dt className="text-ink-400">Acknowledged</dt>
                      <dd>{detail.incident.acknowledged_by}, {when(detail.incident.acknowledged_at)}{detail.incident.ack_note ? ` — ${detail.incident.ack_note}` : ''}</dd></>)}
                    {detail.incident.resolved_at && (<><dt className="text-ink-400">Resolved</dt>
                      <dd>{when(detail.incident.resolved_at)} — {detail.incident.resolution}</dd></>)}
                  </dl>
                  <div>
                    <p className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-ink-400">Manual investigation</p>
                    <ol className="list-decimal space-y-1 pl-5">{detail.incident.actions.map((a, k) => <li key={k}>{a}</li>)}</ol>
                  </div>
                  <details>
                    <summary className="cursor-pointer text-[12px] text-ink-500">Evidence</summary>
                    <pre className="mt-1 overflow-x-auto rounded-lg bg-app p-2 text-[11px]">{JSON.stringify(detail.incident.evidence, null, 2)}</pre>
                  </details>
                  <div>
                    <p className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-ink-400">Timeline</p>
                    {detail.events.map((e) => (
                      <p key={e.id} className="text-[12px] text-ink-600">{when(e.at)} · <b>{e.event}</b> · {e.actor}</p>
                    ))}
                  </div>
                  {detail.incident.status === 'OPEN' && (
                    <div className="flex flex-col gap-2 border-t border-line pt-3 sm:flex-row">
                      <input value={ackNote} onChange={(e) => setAckNote(e.target.value)} maxLength={500}
                        placeholder="Note (optional): who is looking at it, why it is accepted…"
                        className="flex-1 rounded-lg border border-line px-3 py-1.5 text-[13px]" />
                      <button onClick={acknowledge}
                        className="rounded-lg bg-brand-600 px-3 py-1.5 text-[13px] font-medium text-white hover:bg-brand-700">
                        Acknowledge
                      </button>
                    </div>
                  )}
                  <p className="text-[11px] text-ink-400">Acknowledging stops nothing: the issue is still re-checked, escalations still alert, and it resolves itself when the data clears. The monitor never repairs data.</p>
                </div>
              )}
            </div>
          </section>
        </div>
      </div>
    </div>
  )
}
