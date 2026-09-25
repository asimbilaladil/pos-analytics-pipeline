import type { AdminUser, AskResponse, Conversation, ConversationDetail, User } from '../types'

/**
 * Every call is credentialed so the HttpOnly session cookie travels with it.
 * No token is ever read or stored by JavaScript -- the browser holds it and
 * the server decides who you are on each request.
 */
export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

/**
 * Client-side ceiling for a long analysis call. The backend gives up on its own
 * deadline at 240s and nginx at 300s, so 250s sits between them: normally the
 * server answers first, and if the connection itself stalls the request still
 * ends with a real error instead of hanging forever behind a spinner.
 */
const ASK_TIMEOUT_MS = 250_000

async function request<T>(path: string, init: RequestInit = {},
                          timeoutMs?: number): Promise<T> {
  let res: Response
  const ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null
  const timer = ctrl && timeoutMs
    ? setTimeout(() => ctrl.abort(), timeoutMs)
    : null
  try {
    res = await fetch(path, {
      credentials: 'include',
      headers: init.body instanceof FormData
        ? {}
        : { 'Content-Type': 'application/json', ...(init.headers || {}) },
      ...(ctrl ? { signal: ctrl.signal } : {}),
      ...init,
    })
  } catch (err) {
    // fetch rejects for transport failures AND for our own abort. They need
    // different words: one is "you are offline", the other is "this took too
    // long", and telling a user the wrong one sends them chasing the wrong fix.
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw new ApiError(504, 'That request took too long. Please try again.')
    }
    throw new ApiError(0, 'Cannot reach the server. Check your connection and try again.')
  } finally {
    if (timer) clearTimeout(timer)
  }

  if (res.status === 204) return undefined as T
  const isJson = res.headers.get('content-type')?.includes('application/json')
  const body = isJson ? await res.json().catch(() => null) : null

  if (!res.ok) {
    // A gateway timeout or a crashed upstream answers with HTML, not JSON, so
    // body is null here and the fallback text is what the user sees.
    const detail = (body && (body.detail || body.message)) || null
    throw new ApiError(res.status, typeof detail === 'string' ? detail : httpFallback(res.status))
  }
  if (body === null && res.status !== 204) {
    // 2xx with a missing or unparseable body. Previously this returned null and
    // the caller dereferenced it, producing a confusing TypeError; name it.
    throw new ApiError(res.status, 'The server sent an unreadable response. Please try again.')
  }
  return body as T
}

function httpFallback(status: number): string {
  if (status === 401) return 'Your session has expired. Please sign in again.'
  if (status === 403) return 'You do not have access to that.'
  if (status === 404) return 'That conversation no longer exists.'
  if (status === 413) return 'That file is too large.'
  if (status === 503) return 'The assistant is not available right now.'
  if (status >= 500) return 'The server had a problem. Please try again.'
  return 'Something went wrong.'
}

export const api = {
  login: (email: string, password: string) =>
    request<User>('/api/auth/login', { method: 'POST', body: JSON.stringify({ email, password }) }),
  logout: () => request<void>('/api/auth/logout', { method: 'POST' }),
  me: () => request<User>('/api/auth/me'),

  models: () => request<{ models: string[]; default: string }>('/api/models'),
  status: () => request<{ assistant_configured: boolean }>('/api/status'),

  conversations: () => request<Conversation[]>('/api/conversations'),
  conversation: (id: number) => request<ConversationDetail>(`/api/conversations/${id}`),
  deleteConversation: (id: number) =>
    request<void>(`/api/conversations/${id}`, { method: 'DELETE' }),

  ask: (id: number, question: string, model: string) =>
    request<AskResponse>(`/api/conversations/${id}/messages`, {
      method: 'POST', body: JSON.stringify({ question, model }),
    }, ASK_TIMEOUT_MS),

  askWithFiles: (id: number, question: string, model: string, files: File[]) => {
    const fd = new FormData()
    fd.append('question', question)
    fd.append('model', model)
    files.forEach((f) => fd.append('files', f))
    return request<AskResponse>(`/api/conversations/${id}/messages/upload`, {
      method: 'POST', body: fd,
    }, ASK_TIMEOUT_MS)
  },

  importConversation: (file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return request<Conversation>('/api/conversations/import', { method: 'POST', body: fd })
  },
  exportUrl: (id: number) => `/api/conversations/${id}/export`,

  adminUsers: () => request<AdminUser[]>('/api/admin/users'),
  adminUserConversations: (uid: number) =>
    request<Conversation[]>(`/api/admin/users/${uid}/conversations`),
  adminConversation: (id: number) =>
    request<ConversationDetail>(`/api/admin/conversations/${id}`),
}
