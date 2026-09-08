import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertCircle, ArrowDown, X } from 'lucide-react'
import { api, ApiError } from '../api/client'
import { Composer } from '../components/Composer'
import { QuickChips } from '../components/QuickChips'
import { WelcomeState } from '../components/WelcomeState'
import { Header } from '../components/Header'
import { MessageList, ThinkingMessage, UserMessage } from '../components/ChatMessage'
import { Sidebar } from '../components/Sidebar'
import type { Conversation, Message, User } from '../types'

export function Chat({ user, onSignedOut }: { user: User; onSignedOut: () => void }) {
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
  // Skipped entirely on the empty state — there is nothing to follow there,
  // and scrolling to the "bottom" of the welcome screen pushed the hero off
  // the top of the viewport.
  useEffect(() => {
    if (messages.length === 0 && !pending) return
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
          user={user}
          models={models} model={model} onModelChange={setModel}
          onNewChat={newChat} onSignOut={signOut}
          onOpenSidebar={() => setSidebarOpen(true)}
        />

        {error && (
          <div role="alert"
            className="mx-auto mt-4 flex w-full max-w-4xl items-start gap-2.5 rounded-xl border
                       border-amber-200/80 bg-amber-50 px-4 py-3 text-[13px] text-amber-900
                       shadow-card">
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
            className="canvas-wash relative min-h-0 flex-1 overflow-y-auto overscroll-contain scroll-thin"
          >
            {/* Wider than the old 896px reading column so three analytics cards
                fit a row; the transcript keeps its own narrower measure below. */}
            <div className={`relative z-10 mx-auto w-full px-5 sm:px-8
                             ${empty ? 'max-w-[1240px]' : 'max-w-[900px]'}`}>
              {empty ? (
                <WelcomeState onPick={send} />
              ) : (
                <div className="space-y-7 py-8">
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

          <div className="relative shrink-0 bg-canvas-50 px-6 pb-5 pt-3">
            {/* Only offered when you have actually scrolled away from the end,
                so it never covers the newest answer you are already reading. */}
            {!atBottom && !empty && (
              <button
                onClick={() => scrollToBottom('smooth')}
                className="absolute -top-12 left-1/2 flex -translate-x-1/2 animate-pop-in
                           items-center gap-1.5 rounded-full border border-canvas-200 bg-white
                           px-3.5 py-2 text-[12px] font-medium text-ink-700 shadow-float
                           transition-colors hover:bg-canvas-50"
              >
                <ArrowDown size={13} /> Newest
              </button>
            )}
            <Composer onSend={send} disabled={!!pending} />
            {/* Starter chips belong to the empty state only — once a
                conversation exists they are noise under a real transcript. */}
            {empty ? (
              <QuickChips onPick={send} />
            ) : (
              <p className="mt-2.5 text-center text-[11px] text-ink-400">
                Answers are generated from live data · every question is logged
              </p>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
