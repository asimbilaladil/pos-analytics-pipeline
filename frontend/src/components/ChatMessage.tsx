import { Sparkles } from 'lucide-react'
import { Markdown } from './Markdown'
import type { Message } from '../types'

/**
 * The two roles are told apart by SHAPE, not by two competing bubbles.
 *
 * A user turn is a short question, so it gets a compact tinted bubble that
 * does not span the column. An assistant turn is an analytical answer — often
 * several paragraphs, a table and a list of caveats — so it gets an open
 * surface and reads as a document. Putting a long answer in a bubble is what
 * makes analytics chat UIs hard to read.
 */
export function UserMessage({ content }: { content: string }) {
  return (
    <div className="flex animate-fade-up justify-end">
      {/* Tinted rather than solid red: a long thread has many user turns, and
          filling each one with the brand colour turns the transcript into a
          wall of red. Red stays reserved for actions; the bubble is
          distinguished by shape, tint and alignment instead. */}
      <div className="max-w-[72%] rounded-2xl rounded-br-md border border-brand-100 bg-brand-50 px-4 py-3 text-[14.5px] leading-relaxed text-ink-900">
        {content}
      </div>
    </div>
  )
}

export function AssistantMessage({ content }: { content: string }) {
  return (
    <div className="flex animate-fade-up gap-3">
      <Avatar />
      <div className="min-w-0 flex-1 rounded-2xl rounded-tl-md border border-line bg-white/90 px-5 py-4 shadow-card">
        <Markdown>{content}</Markdown>
      </div>
    </div>
  )
}

/** Honest waiting state: it names what is happening, with no invented progress. */
export function ThinkingMessage() {
  return (
    <div className="flex animate-fade-up gap-3">
      <Avatar />
      <div className="flex items-center gap-2.5 rounded-2xl rounded-tl-md border border-line bg-white/90 px-5 py-4 shadow-card">
        <span className="flex gap-1">
          {[0, 160, 320].map((d) => (
            <span key={d}
              className="h-1.5 w-1.5 animate-bounce rounded-full bg-brand-300"
              style={{ animationDelay: `${d}ms`, animationDuration: '1s' }} />
          ))}
        </span>
        <span className="text-[13.5px] text-ink-500">Analyzing your data…</span>
      </div>
    </div>
  )
}

function Avatar() {
  return (
    <div className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg
                    border border-line bg-white text-brand-500 shadow-card">
      <Sparkles size={14} />
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
