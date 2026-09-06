import { Bot } from 'lucide-react'
import { Markdown } from './Markdown'
import type { Message } from '../types'

/**
 * The two roles are distinguished by SHAPE, not by two competing bubbles:
 * the user turn is a compact tinted bubble that does not span the column, the
 * assistant turn is an open surface so a long answer reads as a document.
 */
export function UserMessage({ content }: { content: string }) {
  return (
    <div className="flex justify-end animate-fade-up">
      <div className="max-w-[72%] rounded-2xl rounded-br-md border border-brand-200 bg-brand-50
                      px-4 py-2.5 text-[15px] leading-relaxed text-slate-800">
        {content}
      </div>
    </div>
  )
}

export function AssistantMessage({ content }: { content: string }) {
  return (
    <div className="flex gap-3 animate-fade-up">
      <div className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full
                      border border-slate-200 bg-white text-slate-500">
        <Bot size={14} />
      </div>
      <div className="min-w-0 flex-1 pt-0.5">
        <Markdown>{content}</Markdown>
      </div>
    </div>
  )
}

/** Honest waiting state: it names what is happening, with no fake progress. */
export function ThinkingMessage() {
  return (
    <div className="flex gap-3">
      <div className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full
                      border border-slate-200 bg-white text-slate-500">
        <Bot size={14} />
      </div>
      <div className="flex items-center gap-2 pt-1.5 text-[14px] text-slate-500">
        <span className="flex gap-1">
          {[0, 150, 300].map((d) => (
            <span key={d}
              className="h-1.5 w-1.5 animate-bounce rounded-full bg-slate-300"
              style={{ animationDelay: `${d}ms`, animationDuration: '1s' }} />
          ))}
        </span>
        Analyzing your data…
      </div>
    </div>
  )
}

export function MessageList({ messages }: { messages: Message[] }) {
  return (
    <>
      {messages.map((m, i) =>
        m.role === 'user'
          ? <UserMessage key={i} content={m.content} />
          : <AssistantMessage key={i} content={m.content} />,
      )}
    </>
  )
}
