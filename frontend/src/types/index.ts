export interface User {
  id: number
  email: string
  full_name: string | null
  role: string
  is_admin: boolean
}

export interface Conversation {
  id: number
  title: string
  model: string | null
  updated_at: string
}

export interface Message {
  role: 'user' | 'assistant'
  content: string
  /** Assistant turns: the model and provider that wrote this answer. */
  model?: string | null
  provider?: string | null
}

export type ProviderId = 'anthropic' | 'openai'

/** One server-approved model the picker may offer. */
export interface ModelOption {
  id: string
  label: string
  provider: ProviderId
}

export interface ProviderInfo {
  id: ProviderId
  label: string
  enabled: boolean
}

export interface ModelsInfo {
  models: string[]
  default: string
  options?: ModelOption[]
  providers?: ProviderInfo[]
}

export interface ConversationDetail {
  id: number
  title: string
  model: string | null
  messages: Message[]
}

export interface AdminUser {
  id: number
  email: string
  full_name: string | null
  role: string
  is_active: boolean
  last_login_at: string | null
  conversation_count: number
  last_chat_at: string | null
}

/**
 * The submission is ACCEPTED, not answered. Generation outlives the HTTP
 * request that started it, so the answer arrives via the job, not this
 * response. conversation_id comes back up front so a brand-new conversation is
 * addressable even if this is the last response the client ever receives.
 */
export interface AskAccepted {
  conversation_id: number
  title: string
  job_id: string
  status: JobStatus
  user_message_id: number | null
}

export type JobStatus = 'queued' | 'processing' | 'completed' | 'failed'

export interface ChatJob {
  job_id: string
  conversation_id: number
  status: JobStatus
  attempt: number
  created_at: string | null
  completed_at: string | null
  duration_ms: number | null
  error_code: string | null
  error_message: string | null
}
