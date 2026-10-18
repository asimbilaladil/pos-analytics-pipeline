import { ApiError } from '../api/client'

/**
 * What a phone that went to sleep needs in order to find its answer again.
 *
 * A locked phone freezes the page (JS, timers, fetch) and may discard it
 * entirely. The server job never depends on the page, so all the client must
 * keep is an ADDRESS: the conversation it was looking at, and -- while a send
 * has not been acknowledged -- the idempotency key of that send. Re-sending
 * the same key is a lookup on the server, never a second generation.
 *
 * localStorage, scoped per user, so it survives the app being killed. Every
 * access is guarded: private mode or blocked storage must never break chat,
 * only lose the convenience of reopening where you were.
 */
export interface InflightSend {
  key: string
  question: string
  shown: string
  conversationId: number   // 0 = a new chat whose id the client never learned
  model: string
  at: number
}

interface Stored { activeId: number | null; inflight: InflightSend | null }

/** An unacknowledged send older than this is not resumed automatically. */
export const INFLIGHT_MAX_AGE_MS = 24 * 60 * 60 * 1000

const storeKey = (userId: number) => `laynes.chat.v1.${userId}`

export function loadResume(userId: number): Stored {
  try {
    const v = JSON.parse(localStorage.getItem(storeKey(userId)) || 'null')
    if (v && typeof v === 'object') {
      const inf = v.inflight
      const inflight = inf && typeof inf.key === 'string' && typeof inf.question === 'string'
        && typeof inf.at === 'number' && Date.now() - inf.at < INFLIGHT_MAX_AGE_MS
        ? inf as InflightSend : null
      return { activeId: typeof v.activeId === 'number' ? v.activeId : null, inflight }
    }
  } catch { /* unreadable or blocked storage: start fresh */ }
  return { activeId: null, inflight: null }
}

export function saveResume(userId: number, patch: Partial<Stored>): void {
  try {
    localStorage.setItem(storeKey(userId), JSON.stringify({ ...loadResume(userId), ...patch }))
  } catch { /* storage unavailable: resume-after-kill is lost, chat still works */ }
}

/**
 * True when a failed SUBMISSION may nevertheless have reached the server: the
 * connection died (status 0), our own timer gave up (504 -- on a frozen phone
 * that timer fires the instant it wakes), or a gateway dropped the reply. Then
 * the question must stay on screen and be re-sent with the same key. A real
 * rejection (4xx, 503 not configured) means nothing is generating.
 */
export function mayHaveReachedServer(err: unknown): boolean {
  return err instanceof ApiError && (err.status === 0 || err.status === 502 || err.status === 504)
}
