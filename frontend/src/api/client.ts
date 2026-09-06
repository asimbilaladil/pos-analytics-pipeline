import type { AskResponse, Conversation, ConversationDetail, User } from '../types'

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

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let res: Response
  try {
    res = await fetch(path, {
      credentials: 'include',
      headers: init.body instanceof FormData
        ? {}
        : { 'Content-Type': 'application/json', ...(init.headers || {}) },
      ...init,
    })
  } catch {
    // fetch only rejects for transport failures, which is exactly the case
    // worth naming for the user rather than showing a generic error.
    throw new ApiError(0, 'Cannot reach the server. Check your connection and try again.')
  }

  if (res.status === 204) return undefined as T
  const isJson = res.headers.get('content-type')?.includes('application/json')
  const body = isJson ? await res.json().catch(() => null) : null

  if (!res.ok) {
    const detail = (body && (body.detail || body.message)) || null
    throw new ApiError(res.status, typeof detail === 'string' ? detail : httpFallback(res.status))
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
    }),

  importConversation: (file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return request<Conversation>('/api/conversations/import', { method: 'POST', body: fd })
  },
  exportUrl: (id: number) => `/api/conversations/${id}/export`,
}
