import { Download, MessageSquarePlus, Search, Upload, X } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import clsx from 'clsx'
import { ConversationList } from './ConversationList'
import logo from '../assets/ayg-logo.png'
import type { Conversation } from '../types'

/**
 * Fixed-height rail: brand, search, primary action, scrolling list, anchored footer.
 *
 * The five-part structure is load-bearing. h-[100dvh] + overflow-hidden keeps
 * the rail exactly viewport height whether there are 3 conversations or 500;
 * everything except the list is shrink-0 and only the list scrolls. Changing
 * that reintroduces the bug where the rail grew with the conversation count
 * and pushed its own footer off-screen.
 *
 * The reference also shows Home, Explore, Pinned, Settings, Team and Profile.
 * None of those exist in this backend, so none are drawn -- a nav item that
 * does nothing is worse than an absent one.
 */
export function Sidebar({
  conversations, activeId, onSelect, onNew, onDelete, onImport, onExport, open, onClose,
}: {
  conversations: Conversation[]
  activeId: number | null
  onSelect: (id: number) => void
  onNew: () => void
  onDelete: (id: number) => void
  onImport: (file: File) => void
  onExport: () => void
  open: boolean
  onClose: () => void
}) {
  const fileRef = useRef<HTMLInputElement>(null)
  const searchRef = useRef<HTMLInputElement>(null)
  const [query, setQuery] = useState('')

  // The reference shows a ⌘K badge on the search field. It is drawn only
  // because the shortcut is actually wired up here.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        searchRef.current?.focus()
        searchRef.current?.select()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  return (
    <>
      {open && (
        <div className="fixed inset-0 z-30 bg-ink-900/25 backdrop-blur-[1px] lg:hidden"
             onClick={onClose} />
      )}

      <aside
        className={clsx(
          'z-40 flex h-[100dvh] w-[320px] shrink-0 flex-col overflow-hidden',
          'border-r border-line bg-rail',
          'max-lg:fixed max-lg:inset-y-0 max-lg:left-0 max-lg:w-[300px] max-lg:shadow-float',
          'max-lg:transition-transform max-lg:duration-200',
          !open && 'max-lg:-translate-x-full',
        )}
      >
        {/* ── brand ── */}
        <div className="flex shrink-0 items-center gap-3 px-5 pb-4 pt-5">
          <img src={logo} alt="AYG Food Services" className="h-9 w-auto shrink-0" />
          <span className="min-w-0 text-[15px] font-semibold leading-[1.15] tracking-[-0.015em] text-ink-900">
            Laynes<br />Intelligence
          </span>
          <button onClick={onClose} aria-label="Close sidebar"
            className="ml-auto flex h-8 w-8 items-center justify-center rounded-lg text-ink-400
                       hover:bg-white hover:text-ink-700 lg:hidden">
            <X size={16} />
          </button>
        </div>

        {/* ── search ── */}
        <div className="shrink-0 px-4">
          <div className="relative">
            <Search size={14.5}
              className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-400" />
            <input
              ref={searchRef}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search conversations..."
              aria-label="Search conversations"
              className="h-10 w-full rounded-xl border border-line bg-white pl-9 pr-14 text-[13.5px]
                         text-ink-900 shadow-card placeholder:text-ink-400 transition-colors
                         focus:border-ink-400/40 focus:outline-none"
            />
            {query ? (
              <button onClick={() => setQuery('')} aria-label="Clear search"
                className="absolute right-2.5 top-1/2 flex h-6 w-6 -translate-y-1/2 items-center
                           justify-center rounded-md text-ink-400 hover:bg-app hover:text-ink-700">
                <X size={13} />
              </button>
            ) : (
              <kbd className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2
                              rounded-md border border-line bg-app px-1.5 py-0.5 font-sans
                              text-[10.5px] font-medium text-ink-400">⌘K</kbd>
            )}
          </div>
        </div>

        {/* ── primary action ── */}
        <div className="shrink-0 px-4 pb-1 pt-3">
          <button
            onClick={onNew}
            className="flex h-11 w-full items-center gap-3 rounded-xl bg-brand-50 px-3.5
                       text-[14px] font-semibold text-brand-500 transition-colors
                       hover:bg-brand-100 active:scale-[.99]"
          >
            <MessageSquarePlus size={17} />
            New chat
          </button>
        </div>

        {/* ── the ONLY scrolling region in the rail ── */}
        <nav className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-2.5 pb-2 pt-2 scroll-thin">
          <ConversationList
            conversations={conversations} activeId={activeId} query={query}
            onSelect={onSelect} onDelete={onDelete}
          />
        </nav>

        {/* ── footer: real features only ── */}
        <div className="flex shrink-0 items-center gap-1 border-t border-line px-3 py-2.5">
          <input
            ref={fileRef} type="file" accept="application/json,.json" className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0]
              if (f) onImport(f)
              e.target.value = ''
            }}
          />
          <button
            onClick={() => fileRef.current?.click()}
            className="flex flex-1 flex-col items-center gap-1 rounded-lg py-2 text-[11.5px]
                       font-medium text-ink-500 transition-colors hover:bg-white hover:text-ink-900"
          >
            <Upload size={15} /> Import
          </button>
          <button
            onClick={onExport}
            disabled={!activeId}
            title={activeId ? 'Export this conversation' : 'Open a conversation to export it'}
            className="flex flex-1 flex-col items-center gap-1 rounded-lg py-2 text-[11.5px]
                       font-medium text-ink-500 transition-colors
                       enabled:hover:bg-white enabled:hover:text-ink-900
                       disabled:cursor-not-allowed disabled:text-ink-400/55"
          >
            <Download size={15} /> Export
          </button>
        </div>
      </aside>
    </>
  )
}
