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
}

export interface ConversationDetail {
  id: number
  title: string
  model: string | null
  messages: Message[]
}

export interface AskResponse {
  conversation_id: number
  title: string
  answer: string
  duration_ms: number
}
