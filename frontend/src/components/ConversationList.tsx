import { MessageSquare, Trash2 } from 'lucide-react'
import clsx from 'clsx'
import { BUCKET_ORDER, dayBucket } from '../lib/format'
import type { Conversation } from '../types'

/**
 * The scrolling half of the sidebar.
 *
 * Conversations are grouped by recency because a flat list of fifty titles is
 * hard to scan — the grouping is derived from updated_at, which the API
 * already returns, so it costs no extra request.
 */
export function ConversationList({
  conversations, activeId, query, onSelect, onDelete,
}: {
  conversations: Conversation[]
  activeId: number | null
  query: string
  onSelect: (id: number) => void
  onDelete: (id: number) => void
}) {
  const q = query.trim().toLowerCase()
  const filtered = q
    ? conversations.filter((c) => c.title.toLowerCase().includes(q))
    : conversations

  if (filtered.length === 0) {
    return (
      <p className="px-3 py-6 text-center text-[13px] leading-relaxed text-ink-400">
        {q ? `No conversations match “${query}”.`
           : 'No conversations yet. Start a new chat and it will appear here.'}
      </p>
    )
  }

  const groups = new Map<string, Conversation[]>()
  for (const c of filtered) {
    const b = dayBucket(c.updated_at)
    ;(groups.get(b) ?? groups.set(b, []).get(b)!).push(c)
  }

  return (
    <>
      {BUCKET_ORDER.filter((b) => groups.has(b)).map((bucket, gi) => (
        <section key={bucket} className="mb-1.5">
          <h3 className="px-3 pb-1.5 pt-3 text-[10.5px] font-semibold uppercase
                         tracking-[0.11em] text-ink-400">
            {/* The reference labels this block "Pinned"; pinning does not exist
                here, so the first group is named for what it actually is. */}
            {gi === 0 && !query ? 'Recent conversations' : bucket}
          </h3>
          <div className="space-y-px">
            {groups.get(bucket)!.map((c) => {
              const active = c.id === activeId
              return (
                <div key={c.id} className="group relative">
                  <button
                    onClick={() => onSelect(c.id)}
                    title={c.title}
                    aria-current={active ? 'true' : undefined}
                    className={clsx(
                      'flex h-10 w-full items-center gap-3 rounded-xl pl-3 pr-9 text-left',
                      'text-[13.5px] transition-colors duration-150',
                      active
                        ? 'bg-white font-medium text-ink-900 shadow-card'
                        : 'text-ink-500 hover:bg-white/70 hover:text-ink-900',
                    )}
                  >
                    <MessageSquare
                      size={14.5}
                      className={clsx('shrink-0', active ? 'text-brand-500' : 'text-ink-400')}
                    />
                    <span className="min-w-0 flex-1 truncate">{c.title}</span>
                  </button>
                  {/* Revealed on hover or keyboard focus only, so the list stays calm */}
                  <button
                    onClick={() => onDelete(c.id)}
                    aria-label={`Delete ${c.title}`}
                    className="absolute right-1 top-1/2 flex h-6 w-6 -translate-y-1/2 items-center
                               justify-center rounded-md text-ink-400 opacity-0 transition
                               hover:bg-app hover:text-ink-700
                               focus-visible:opacity-100 group-hover:opacity-100"
                  >
                    <Trash2 size={12.5} />
                  </button>
                </div>
              )
            })}
          </div>
        </section>
      ))}
    </>
  )
}
