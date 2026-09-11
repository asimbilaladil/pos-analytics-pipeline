import { useCallback, useEffect, useState } from 'react'
import { AlertCircle, ArrowLeft, Eye, MessageSquare, Users } from 'lucide-react'
import { api, ApiError } from '../api/client'
import { MessageList } from '../components/ChatMessage'
import type { AdminUser, Conversation, ConversationDetail } from '../types'

/**
 * Super-admin, read-only view of every user's conversations.
 * Three columns: users → that user's conversations → the transcript.
 * The server enforces the super_admin role on every call; this page only
 * decides what to draw.
 */
export function AdminChats({ onBack, onSignedOut }: { onBack: () => void; onSignedOut: () => void }) {
  const [users, setUsers] = useState<AdminUser[]>([])
  const [userId, setUserId] = useState<number | null>(null)
  const [convos, setConvos] = useState<Conversation[]>([])
  const [detail, setDetail] = useState<ConversationDetail | null>(null)
  const [error, setError] = useState<string | null>(null)

  const handleError = useCallback((err: unknown) => {
    if (err instanceof ApiError && err.status === 401) { onSignedOut(); return }
    setError(err instanceof ApiError ? err.message : 'Something went wrong.')
  }, [onSignedOut])

  useEffect(() => { api.adminUsers().then(setUsers).catch(handleError) }, [handleError])

  const pickUser = async (id: number) => {
    setUserId(id); setDetail(null); setConvos([])
    try { setConvos(await api.adminUserConversations(id)) } catch (e) { handleError(e) }
  }

  const pickConvo = async (id: number) => {
    try { setDetail(await api.adminConversation(id)) } catch (e) { handleError(e) }
  }

  const selected = users.find((u) => u.id === userId)
  const when = (s: string | null) => (s ? new Date(s).toLocaleString() : '—')

  return (
    <div className="flex h-[100dvh] flex-col overflow-hidden bg-canvas-50">
      <header className="flex h-16 shrink-0 items-center gap-3 border-b border-line bg-white px-4 sm:px-6">
        <button onClick={onBack}
          className="flex h-9 items-center gap-1.5 rounded-lg px-2.5 text-[13px] font-medium
                     text-ink-700 transition-colors hover:bg-app">
          <ArrowLeft size={15} /> Back to chat
        </button>
        <span className="h-6 w-px bg-line" aria-hidden />
        <h1 className="flex items-center gap-2 text-[15px] font-semibold text-ink-900">
          <Eye size={16} className="text-brand-500" /> All users' chats
        </h1>
        <span className="ml-auto hidden text-[12px] text-ink-400 sm:inline">Read-only · super admin</span>
      </header>

      {error && (
        <div role="alert" className="mx-4 mt-3 flex items-start gap-2 rounded-xl border border-amber-200
                                     bg-amber-50 px-4 py-2.5 text-[13px] text-amber-900">
          <AlertCircle size={15} className="mt-px shrink-0" />
          <span className="flex-1">{error}</span>
        </div>
      )}

      <div className="grid min-h-0 flex-1 grid-cols-1 overflow-hidden md:grid-cols-[260px_300px_minmax(0,1fr)]">
        {/* Users */}
        <section className={`min-h-0 overflow-y-auto border-r border-line bg-white scroll-thin
                             ${userId ? 'hidden md:block' : ''}`}>
          <h2 className="flex items-center gap-1.5 px-4 pb-2 pt-4 text-[11px] font-semibold uppercase
                         tracking-wide text-ink-400"><Users size={12} /> Users</h2>
          {users.map((u) => (
            <button key={u.id} onClick={() => pickUser(u.id)}
              className={`block w-full px-4 py-2.5 text-left transition-colors hover:bg-app
                          ${u.id === userId ? 'bg-brand-50' : ''}`}>
              <p className="truncate text-[13px] font-medium text-ink-900">
                {u.full_name || u.email}
                {!u.is_active && <span className="ml-1.5 text-[11px] text-ink-400">(inactive)</span>}
              </p>
              <p className="truncate text-[12px] text-ink-500">{u.email} · {u.role}</p>
              <p className="text-[11px] text-ink-400">
                {u.conversation_count} chats · last {when(u.last_chat_at)}
              </p>
            </button>
          ))}
        </section>

        {/* Conversations of the selected user */}
        <section className={`min-h-0 overflow-y-auto border-r border-line bg-white/60 scroll-thin
                             ${!userId || detail ? 'hidden md:block' : ''}`}>
          <h2 className="flex items-center gap-1.5 px-4 pb-2 pt-4 text-[11px] font-semibold uppercase
                         tracking-wide text-ink-400">
            <MessageSquare size={12} /> {selected ? (selected.full_name || selected.email) : 'Conversations'}
          </h2>
          {userId && (
            <button onClick={() => setUserId(null)}
              className="px-4 pb-2 text-[12px] text-brand-600 md:hidden">← Users</button>
          )}
          {!userId && <p className="px-4 text-[13px] text-ink-400">Pick a user.</p>}
          {userId && convos.length === 0 && <p className="px-4 text-[13px] text-ink-400">No chats yet.</p>}
          {convos.map((c) => (
            <button key={c.id} onClick={() => pickConvo(c.id)}
              className={`block w-full px-4 py-2.5 text-left transition-colors hover:bg-app
                          ${detail?.id === c.id ? 'bg-brand-50' : ''}`}>
              <p className="truncate text-[13px] text-ink-900">{c.title}</p>
              <p className="text-[11px] text-ink-400">{when(c.updated_at)}</p>
            </button>
          ))}
        </section>

        {/* Transcript */}
        <section className={`min-h-0 overflow-y-auto canvas-wash scroll-thin ${detail ? '' : 'hidden md:block'}`}>
          {detail ? (
            <div className="mx-auto w-full max-w-[900px] px-5 py-6 sm:px-8">
              <button onClick={() => setDetail(null)}
                className="mb-3 text-[12px] text-brand-600 md:hidden">← Conversations</button>
              <h2 className="mb-6 text-[16px] font-semibold text-ink-900">{detail.title}</h2>
              <div className="space-y-7"><MessageList messages={detail.messages} /></div>
            </div>
          ) : (
            <p className="p-8 text-center text-[13px] text-ink-400">Select a conversation to read it.</p>
          )}
        </section>
      </div>
    </div>
  )
}
