import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertCircle, ArrowDown, X } from 'lucide-react'
import { api, ApiError } from '../api/client'
import { Composer } from '../components/Composer'
import { QuickChips } from '../components/QuickChips'
import { WelcomeState } from '../components/WelcomeState'
import { Header } from '../components/Header'
import { MessageList, ThinkingMessage, UserMessage } from '../components/ChatMessage'
import { Sidebar } from '../components/Sidebar'
import type { AskAccepted, ChatJob, Conversation, Message, ModelOption, ModelsInfo, ProviderInfo, User } from '../types'
import { modelLabel, providerOfId } from '../lib/format'
import { loadResume, mayHaveReachedServer, saveResume, type InflightSend } from '../lib/chatResume'

export function Chat({ user, onSignedOut, onOpenAdmin }: {
  user: User; onSignedOut: () => void; onOpenAdmin?: () => void
}) {
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [activeId, setActiveId] = useState<number | null>(null)
  const [messages, setMessages] = useState<Message[]>([])
  const [pending, setPending] = useState<string | null>(null)
  const [models, setModels] = useState<ModelOption[]>([])
  const [providers, setProviders] = useState<ProviderInfo[]>([])
  const [model, setModel] = useState('')
  const [defaultModel, setDefaultModel] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [failed, setFailed] = useState<{ question: string; retryable: boolean; key?: string } | null>(null)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [atBottom, setAtBottom] = useState(true)
  const scrollRef = useRef<HTMLDivElement>(null)
  // The job this page is waiting on. Held in a ref as well so the visibility
  // listener and the poll timer always see the current value without being
  // re-registered on every render.
  const [job, setJob] = useState<ChatJob | null>(null)
  const jobRef = useRef<ChatJob | null>(null)
  const pollRef = useRef<number | null>(null)
  jobRef.current = job

  const handleError = useCallback((err: unknown) => {
    if (err instanceof ApiError && err.status === 401) { onSignedOut(); return }
    setError(err instanceof ApiError ? err.message : 'Something went wrong.')
  }, [onSignedOut])

  const refreshList = useCallback(async () => {
    try { setConversations(await api.conversations()) } catch (e) { handleError(e) }
  }, [handleError])

  useEffect(() => {
    refreshList()
    api.models().then((m: ModelsInfo) => {
      // An older server sends ids only; derive labels for those.
      setModels(m.options ?? m.models.map((id) => ({
        id, label: modelLabel(id), provider: providerOfId(id),
      })))
      setProviders(m.providers ?? [])
      setModel(m.default); setDefaultModel(m.default)
    }).catch(handleError)
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

  const stopPolling = useCallback(() => {
    if (pollRef.current !== null) { clearInterval(pollRef.current); pollRef.current = null }
  }, [])

  // Persisted IDs are the source of truth after any interruption: a job is
  // settled at most once, and only one status request is in flight at a time,
  // however many timers or wake-up events fire together.
  const settledRef = useRef<Set<string>>(new Set())
  const pollBusyRef = useRef(false)
  const inflightRef = useRef<InflightSend | null>(null)
  const recoveringRef = useRef(false)
  const resumingRef = useRef(false)
  const activeIdRef = useRef<number | null>(activeId)
  activeIdRef.current = activeId

  const rememberConversation = useCallback((id: number | null) => {
    saveResume(user.id, { activeId: id })
  }, [user.id])

  // One place that turns a finished job into UI. Reloads the transcript from
  // the server rather than trusting anything the client kept, so the answer
  // shown is exactly what was persisted.
  const settleJob = useCallback(async (j: ChatJob) => {
    stopPolling()
    if (settledRef.current.has(j.job_id)) return
    settledRef.current.add(j.job_id)
    setJob(null)
    jobRef.current = null
    setPending(null)
    try {
      const d = await api.conversation(j.conversation_id)
      setActiveId(d.id); setMessages(d.messages)
      rememberConversation(d.id)
      refreshList()
    } catch (e) { handleError(e) }
    if (j.status === 'failed') {
      setError(j.error_message || 'Something went wrong while analyzing the data. Please try again.')
    }
  }, [handleError, refreshList, stopPolling, rememberConversation])

  const checkJob = useCallback(async () => {
    const current = jobRef.current
    if (!current || pollBusyRef.current) return
    pollBusyRef.current = true
    try {
      const j = await api.job(current.job_id)
      if (jobRef.current?.job_id !== j.job_id) return   // superseded meanwhile
      setJob(j)
      if (j.status === 'completed' || j.status === 'failed') await settleJob(j)
    } catch (e) {
      // A polling hiccup must not discard the job: the answer is still being
      // generated server-side and the next tick can pick it up. Only a 404
      // (job genuinely gone) stops the watch.
      if (e instanceof ApiError && e.status === 404) {
        stopPolling(); setJob(null); jobRef.current = null; setPending(null)
      }
    } finally {
      pollBusyRef.current = false
    }
  }, [settleJob, stopPolling])

  const startPolling = useCallback((j: ChatJob) => {
    setJob(j)
    jobRef.current = j
    stopPolling()
    if (document.visibilityState === 'visible') {
      pollRef.current = window.setInterval(checkJob, 2500)
    }
  }, [checkJob, stopPolling])

  // The server accepted (or already had) this submission. Reload the
  // transcript from the server so the question appears exactly once -- also
  // when this is a re-send of a question that was persisted, or even answered,
  // while the phone slept -- then watch or settle the job it points to.
  const acceptSubmission = useCallback(async (res: AskAccepted) => {
    inflightRef.current = null
    saveResume(user.id, { inflight: null, activeId: res.conversation_id })
    setFailed(null)
    setActiveId(res.conversation_id)
    const j: ChatJob = {
      job_id: res.job_id, conversation_id: res.conversation_id,
      status: res.status, attempt: 1, created_at: null, completed_at: null,
      duration_ms: null, error_code: null, error_message: null,
    }
    if (res.status === 'completed' || res.status === 'failed') {
      settledRef.current.delete(j.job_id)
      await settleJob(j)
      return
    }
    try {
      const d = await api.conversation(res.conversation_id)
      setMessages(d.messages)
    } catch { /* the job watch below still delivers the answer */ }
    setPending(null)
    refreshList()
    startPolling(j)
  }, [user.id, settleJob, refreshList, startPolling])

  // Re-send an unacknowledged submission with its ORIGINAL key. The server
  // treats that as a lookup: it returns the existing job (running, or already
  // finished while the phone slept) and creates nothing new.
  const recoverSubmission = useCallback(async () => {
    const inf = inflightRef.current
    if (!inf || recoveringRef.current || document.visibilityState !== 'visible') return
    recoveringRef.current = true
    try {
      for (let attempt = 0; attempt < 3; attempt++) {
        try {
          const res = await api.ask(inf.conversationId, inf.question, inf.model, inf.key)
          await acceptSubmission(res)
          return
        } catch (e) {
          if (!mayHaveReachedServer(e)) {
            // A real rejection: nothing is generating for this question.
            inflightRef.current = null
            saveResume(user.id, { inflight: null })
            setPending(null)
            setFailed({ question: inf.question, retryable: true, key: inf.key })
            handleError(e)
            return
          }
          if (typeof navigator !== 'undefined' && navigator.onLine === false) break
          await new Promise((r) => setTimeout(r, 1500 * (attempt + 1)))
        }
      }
      // Still unreachable. Keep the send (and its key) so the next wake-up or
      // reconnect resumes it automatically; Retry re-sends the same key too.
      setPending(null)
      setFailed({ question: inf.question, retryable: true, key: inf.key })
      setError('Cannot reach the server. Your question is safe -- it will be picked up when you reconnect.')
    } finally {
      recoveringRef.current = false
    }
  }, [acceptSubmission, handleError, user.id])

  // Waking up. Timers from before the sleep are not trusted: stop them, read
  // the persisted state, and continue from that. Single-flight, because
  // visibilitychange, pageshow, focus and online often fire together.
  const resume = useCallback(async () => {
    if (document.visibilityState !== 'visible' || resumingRef.current) return
    resumingRef.current = true
    stopPolling()
    try {
      if (inflightRef.current) { await recoverSubmission(); return }
      const current = jobRef.current
      if (current) {
        try {
          const j = await api.job(current.job_id)
          if (j.status === 'completed' || j.status === 'failed') await settleJob(j)
          else startPolling(j)
        } catch (e) {
          if (e instanceof ApiError && e.status === 404) { setJob(null); jobRef.current = null }
          else if (e instanceof ApiError && e.status === 401) handleError(e)
          else startPolling(current)            // offline for now: keep watching
        }
        return
      }
      const cid = activeIdRef.current
      if (cid) {
        try {
          const aj = await api.activeJob(cid)
          if (aj) startPolling(aj)
          else { const d = await api.conversation(cid); setMessages(d.messages) }
        } catch (e) {
          if (e instanceof ApiError && e.status === 401) handleError(e)
        }
      }
    } finally {
      resumingRef.current = false
    }
  }, [stopPolling, recoverSubmission, settleJob, startPolling, handleError])

  // Backgrounding pauses POLLING only; the server job is untouched. A locked
  // phone may also freeze or discard the page, so every way back in resumes.
  useEffect(() => {
    const onVisibility = () => {
      if (document.visibilityState === 'visible') resume()
      else stopPolling()
    }
    const onResume = () => { resume() }
    document.addEventListener('visibilitychange', onVisibility)
    document.addEventListener('resume', onResume)        // Page Lifecycle API
    document.addEventListener('freeze', stopPolling)
    window.addEventListener('pageshow', onResume)         // bfcache restore
    window.addEventListener('focus', onResume)
    window.addEventListener('online', onResume)
    return () => {
      document.removeEventListener('visibilitychange', onVisibility)
      document.removeEventListener('resume', onResume)
      document.removeEventListener('freeze', stopPolling)
      window.removeEventListener('pageshow', onResume)
      window.removeEventListener('focus', onResume)
      window.removeEventListener('online', onResume)
      stopPolling()
    }
  }, [resume, stopPolling])

  const openConversation = async (id: number) => {
    setSidebarOpen(false)
    try {
      const d = await api.conversation(id)
      setActiveId(d.id); setMessages(d.messages)
      rememberConversation(d.id)
      // Keep the conversation's model only while the server still offers it
      // (an OpenAI conversation after OpenAI was disabled falls back to the
      // default rather than leaving the picker on an unsendable value).
      if (d.model) {
        const offered = models.length === 0 || models.some((m) => m.id === d.model)
        setModel(offered || !defaultModel ? d.model : defaultModel)
      }
      // A freshly opened conversation always starts at its newest message.
      setAtBottom(true)
      requestAnimationFrame(() => scrollToBottom('auto'))
      // Reattach to any answer still being generated for this conversation, so
      // a reload (or a different device) picks the question back up instead of
      // losing it. No client state is required for this to work.
      const activeJob = await api.activeJob(id)
      if (activeJob) {
        setPending(null)
        startPolling(activeJob)
      } else {
        stopPolling(); setJob(null); jobRef.current = null
      }
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) rememberConversation(null)
      handleError(e)
    }
  }

  // A page the phone discarded (or an app that was killed) comes back here.
  // Reopen where the user was; an unacknowledged send is re-sent with its key.
  const restoredRef = useRef(false)
  useEffect(() => {
    if (restoredRef.current) return
    restoredRef.current = true
    const saved = loadResume(user.id)
    if (saved.inflight) {
      inflightRef.current = saved.inflight
      setPending(saved.inflight.shown)
      recoverSubmission()
    } else if (saved.activeId) {
      openConversation(saved.activeId)
    }
    // Runs once per mount; later changes are handled by the events above.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const newChat = () => {
    stopPolling(); setJob(null); jobRef.current = null
    inflightRef.current = null
    saveResume(user.id, { activeId: null, inflight: null })
    setActiveId(null); setMessages([]); setPending(null)
    setError(null); setFailed(null); setSidebarOpen(false); setAtBottom(true)
  }

  const send = async (question: string, files: File[] = [], reuseKey?: string) => {
    // Mirrors how the server stores the turn, so a reload looks the same.
    const shown = question + files.map((f) => `\n\u{1F4CE} ${f.name}`).join('')
    setError(null); setFailed(null); setPending(shown)
    // Sending is an explicit action, so it always returns you to the end.
    setAtBottom(true)
    // One key per logical submission. A double-tapped Send, a re-send after the
    // phone slept through the acknowledgement, and Retry all reuse it, and the
    // server hands back the SAME job instead of generating twice.
    const key = reuseKey
      ?? (crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`)
    const inflight: InflightSend = {
      key, question, shown, conversationId: activeId ?? 0, model, at: Date.now(),
    }
    // Files cannot be re-sent from storage, so only text sends are resumable.
    if (!files.length) {
      inflightRef.current = inflight
      saveResume(user.id, { inflight })
    }
    try {
      const res = files.length
        ? await api.askWithFiles(activeId ?? 0, question, model, files, key)
        : await api.ask(activeId ?? 0, question, model, key)
      await acceptSubmission(res)
    } catch (e) {
      if (!files.length && mayHaveReachedServer(e)) {
        // The phone may have locked mid-send: the question can already be on
        // the server. Keep it on screen and re-send with the same key now (or
        // as soon as the page is visible / back online).
        await recoverSubmission()
        return
      }
      // Only the SUBMISSION failed here, so nothing is generating and the
      // question is genuinely lost unless we keep it.
      inflightRef.current = null
      saveResume(user.id, { inflight: null })
      setPending(null)
      setFailed({ question, retryable: files.length === 0, key })
      handleError(e)
    }
  }

  const retry = () => {
    const q = failed?.question
    if (!q) return
    const k = failed?.key
    setFailed(null)
    send(q, [], k)
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
          models={models} providers={providers} model={model} onModelChange={setModel}
          onNewChat={newChat} onSignOut={signOut} onOpenAdmin={onOpenAdmin}
          onOpenSidebar={() => setSidebarOpen(true)}
        />

        {error && (
          <div role="alert"
            className="mx-auto mt-4 flex w-full max-w-4xl items-start gap-2.5 rounded-xl border
                       border-amber-200/80 bg-amber-50 px-4 py-3 text-[13px] text-amber-900
                       shadow-card">
            <AlertCircle size={15} className="mt-px shrink-0" />
            <div className="flex-1">
              <span>{error}</span>
              {failed && (
                <div className="mt-1.5 text-[12.5px] text-amber-800">
                  <span className="opacity-80">Your question was kept: </span>
                  <span className="italic">“{failed.question}”</span>
                  {failed.retryable && (
                    <button
                      onClick={retry}
                      className="ml-2 rounded-md border border-amber-300 bg-white px-2 py-0.5
                                 font-medium text-amber-900 hover:bg-amber-100"
                    >Retry</button>
                  )}
                </div>
              )}
            </div>
            <button onClick={() => { setError(null); setFailed(null) }} aria-label="Dismiss"><X size={14} /></button>
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
                  {/* `pending` covers the brief moment before the submission
                      is accepted, when the question is not yet in `messages`.
                      Once a job is being watched the question has already been
                      persisted and rendered, so only the thinking indicator
                      belongs here. */}
                  {pending && (
                    <>
                      <UserMessage content={pending} />
                      <ThinkingMessage />
                    </>
                  )}
                  {!pending && job && <ThinkingMessage />}
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
