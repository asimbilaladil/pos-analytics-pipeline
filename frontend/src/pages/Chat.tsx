import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertCircle, ArrowDown, X } from 'lucide-react'
import { api, ApiError } from '../api/client'
import { Composer } from '../components/Composer'
import { EmptyState } from '../components/EmptyState'
import { Header } from '../components/Header'
import { MessageList, ThinkingMessage, UserMessage } from '../components/Messages'
import { Sidebar } from '../components/Sidebar'
import type { Conversation, Message } from '../types'

export function Chat({ onSignedOut }: { onSignedOut: () => void }) {
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [activeId, setActiveId] = useState<number | null>(null)
  const [messages, setMessages] = useState<Message[]>([])
  const [pending, setPending] = useState<string | null>(null)
  const [models, setModels] = useState<string[]>([])
  const [model, setModel] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [atBottom, setAtBottom] = useState(true)
  const scrollRef = useRef<HTMLDivElement>(null)

  const handleError = useCallback((err: unknown) => {
    if (err instanceof ApiError && err.status === 401) { onSignedOut(); return }
    setError(err instanceof ApiError ? err.message : 'Something went wrong.')
  }, [onSignedOut])

  const refreshList = useCallback(async () => {
    try { setConversations(await api.conversations()) } catch (e) { handleError(e) }
  }, [handleError])

  useEffect(() => {
    refreshList()
    api.models().then((m) => { setModels(m.models); setModel(m.default) }).catch(handleError)
  }, [refreshList, handleError])

  /* Auto-follow, but only when the reader is already at the end.
     Yanking someone back to the bottom while they are reading an earlier
     answer is the single most irritating thing a chat UI can do, so the
     transcript follows new content only if they were within FOLLOW_PX of the
     bottom. Sending your own message always scrolls once -- that is an
     explicit action, not an interruption. */
  const FOLLOW_PX = 120

  const scrollToBottom = useCallback((behavior: ScrollBehavior = 'auto') => {
    const el = scrollRef.current
    if (el) el.scrollTo({ top: el.scrollHeight, behavior })
  }, [])

  const onTranscriptScroll = useCallback(() => {
    const el = scrollRef.current
    if (!el) return
    setAtBottom(el.scrollHeight - el.scrollTop - el.clientHeight <= FOLLOW_PX)
  }, [])

  // New content arrives: follow only if the reader had not scrolled away.
  useEffect(() => {
    if (atBottom) scrollToBottom('smooth')
    // atBottom is intentionally omitted: this must react to new content, not
    // to the flag flipping while the reader scrolls.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [messages, pending, scrollToBottom])

  const openConversation = async (id: number) => {
    setSidebarOpen(false)
    try {
      const d = await api.conversation(id)
      setActiveId(d.id); setMessages(d.messages)
      if (d.model) setModel(d.model)
      // A freshly opened conversation always starts at its newest message.
      setAtBottom(true)
      requestAnimationFrame(() => scrollToBottom('auto'))
    } catch (e) { handleError(e) }
  }

  const newChat = () => {
    setActiveId(null); setMessages([]); setPending(null)
    setError(null); setSidebarOpen(false); setAtBottom(true)
  }

  const send = async (question: string) => {
    setError(null); setPending(question)
    // Sending is an explicit action, so it always returns you to the end.
    setAtBottom(true)
    try {
      const res = await api.ask(activeId ?? 0, question, model)
      setMessages((prev) => [...prev, { role: 'user', content: question },
                                       { role: 'assistant', content: res.answer }])
      setActiveId(res.conversation_id)
      refreshList()
    } catch (e) {
      handleError(e)
    } finally {
      setPending(null)
    }
  }

  const remove = async (id: number) => {
    try {
      await api.deleteConversation(id)
      if (id === activeId) newChat()
      refreshList()
    } catch (e) { handleError(e) }
  }

  const doImport = async (file: File) => {
    try {
      const c = await api.importConversation(file)
      await refreshList()
      openConversation(c.id)
    } catch (e) { handleError(e) }
  }

  const doExport = () => {
    if (activeId) window.location.href = api.exportUrl(activeId)
  }

  const signOut = async () => {
    try { await api.logout() } finally { onSignedOut() }
  }

  const empty = messages.length === 0 && !pending

  return (
    // 100dvh (not 100vh) so mobile browser chrome does not push the composer
    // off-screen. overflow-hidden keeps the shell exactly viewport-sized, which
    // is what stops the sidebar growing with the conversation count.
    <div className="grid h-[100dvh] overflow-hidden lg:grid-cols-[288px_minmax(0,1fr)]">
      <Sidebar
        conversations={conversations} activeId={activeId}
        onSelect={openConversation} onNew={newChat} onDelete={remove}
        onImport={doImport} onExport={doExport}
        open={sidebarOpen} onClose={() => setSidebarOpen(false)}
      />

      {/* min-w-0 stops a wide table inside the transcript widening the column */}
      <div className="flex h-full min-w-0 flex-col overflow-hidden">
        <Header
          models={models} model={model} onModelChange={setModel}
          onNewChat={newChat} onSignOut={signOut}
          onOpenSidebar={() => setSidebarOpen(true)}
        />

        {error && (
          <div role="alert"
            className="mx-auto mt-4 flex w-full max-w-4xl items-start gap-2 rounded-lg border
                       border-amber-200 bg-amber-50 px-4 py-2.5 text-[13px] text-amber-800">
            <AlertCircle size={15} className="mt-px shrink-0" />
            <span className="flex-1">{error}</span>
            <button onClick={() => setError(null)} aria-label="Dismiss"><X size={14} /></button>
          </div>
        )}

        {/* The transcript is the ONLY scroller here. The composer is its
            sibling, not its child, so it can never scroll out of reach --
            that was the whole bug. min-h-0 on both is what lets the flex
            child actually shrink instead of forcing the column taller. */}
        <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
          <div
            ref={scrollRef}
            onScroll={onTranscriptScroll}
            className="min-h-0 flex-1 overflow-y-auto overscroll-contain scroll-thin"
          >
            <div className="mx-auto w-full max-w-4xl px-6">
              {empty ? (
                <EmptyState onPick={send} />
              ) : (
                <div className="space-y-6 py-8">
                  <MessageList messages={messages} />
                  {pending && (
                    <>
                      <UserMessage content={pending} />
                      <ThinkingMessage />
                    </>
                  )}
                </div>
              )}
            </div>
          </div>

          <div className="relative shrink-0 bg-slate-50 px-6 pb-4 pt-3">
            {/* Only offered when you have actually scrolled away from the end,
                so it never covers the newest answer you are already reading. */}
            {!atBottom && !empty && (
              <button
                onClick={() => scrollToBottom('smooth')}
                className="absolute -top-11 left-1/2 flex -translate-x-1/2 items-center gap-1.5
                           rounded-full border border-slate-200 bg-white px-3 py-1.5 text-xs
                           font-medium text-slate-600 shadow-md transition-colors hover:bg-slate-50"
              >
                <ArrowDown size={13} /> Newest
              </button>
            )}
            <div className="mx-auto w-full max-w-4xl">
              <Composer onSend={send} disabled={!!pending} />
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
