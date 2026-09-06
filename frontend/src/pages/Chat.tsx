import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertCircle, X } from 'lucide-react'
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

  // Keep the newest turn in view as the transcript grows.
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages, pending])

  const openConversation = async (id: number) => {
    setSidebarOpen(false)
    try {
      const d = await api.conversation(id)
      setActiveId(d.id); setMessages(d.messages)
      if (d.model) setModel(d.model)
    } catch (e) { handleError(e) }
  }

  const newChat = () => {
    setActiveId(null); setMessages([]); setPending(null)
    setError(null); setSidebarOpen(false)
  }

  const send = async (question: string) => {
    setError(null); setPending(question)
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
    <div className="grid h-full lg:grid-cols-[288px_minmax(0,1fr)]">
      <Sidebar
        conversations={conversations} activeId={activeId}
        onSelect={openConversation} onNew={newChat} onDelete={remove}
        onImport={doImport} onExport={doExport}
        open={sidebarOpen} onClose={() => setSidebarOpen(false)}
      />

      {/* min-w-0 stops a wide table inside the transcript widening the column */}
      <div className="flex h-full min-w-0 flex-col">
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

        {/* Only this region scrolls; the composer below never moves. */}
        <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto scroll-thin">
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

        <div className="shrink-0 pb-4">
          <div className="mx-auto w-full max-w-4xl px-6">
            <Composer onSend={send} disabled={!!pending} />
          </div>
        </div>
      </div>
    </div>
  )
}
