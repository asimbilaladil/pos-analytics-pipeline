import { Download, MessageSquare, Plus, Trash2, Upload, X } from 'lucide-react'
import { useRef } from 'react'
import clsx from 'clsx'
import type { Conversation } from '../types'

export function Sidebar({
  conversations, activeId, onSelect, onNew, onDelete, onImport, onExport,
  open, onClose,
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

  return (
    <>
      {/* Mobile scrim: the rail becomes a drawer below lg */}
      {open && (
        <div className="fixed inset-0 z-30 bg-slate-900/20 lg:hidden" onClick={onClose} />
      )}
      <aside
        className={clsx(
          // h-[100dvh] + overflow-hidden: the rail is always exactly viewport
          // height, whether there are 3 conversations or 500.
          'z-40 flex h-[100dvh] w-72 shrink-0 flex-col overflow-hidden border-r border-slate-200 bg-white',
          'max-lg:fixed max-lg:inset-y-0 max-lg:left-0 max-lg:transition-transform',
          !open && 'max-lg:-translate-x-full',
        )}
      >
        <div className="flex shrink-0 items-center justify-between px-4 pb-2 pt-4">
          <span className="text-[11px] font-semibold uppercase tracking-[0.07em] text-slate-400">
            Conversations
          </span>
          <div className="flex items-center gap-1">
            <button onClick={onNew} aria-label="New chat" title="New chat"
              className="flex h-6 w-6 items-center justify-center rounded-md text-slate-400
                         transition-colors hover:bg-slate-100 hover:text-slate-700">
              <Plus size={15} />
            </button>
            <button onClick={onClose} aria-label="Close sidebar"
              className="flex h-6 w-6 items-center justify-center rounded-md text-slate-400
                         hover:bg-slate-100 hover:text-slate-700 lg:hidden">
              <X size={15} />
            </button>
          </div>
        </div>

        {/* The list is the only thing that scrolls; header and footer stay put */}
        <nav className="min-h-0 flex-1 space-y-0.5 overflow-y-auto overscroll-contain px-2 pb-2 scroll-thin">
          {conversations.length === 0 ? (
            <p className="px-3 py-2 text-[13px] leading-relaxed text-slate-400">
              No conversations yet. Start a new chat and it will appear here.
            </p>
          ) : (
            conversations.map((c) => {
              const active = c.id === activeId
              return (
                <div key={c.id} className="group relative">
                  <button
                    onClick={() => onSelect(c.id)}
                    title={c.title}
                    className={clsx(
                      'flex h-10 w-full items-center gap-2.5 rounded-lg px-3 pr-8 text-left text-[14px] transition-colors',
                      active
                        ? 'bg-slate-100 font-medium text-slate-900'
                        : 'text-slate-600 hover:bg-slate-50 hover:text-slate-900',
                    )}
                  >
                    <MessageSquare size={14} className={active ? 'text-slate-500' : 'text-slate-400'} />
                    <span className="min-w-0 flex-1 truncate">{c.title}</span>
                  </button>
                  <button
                    onClick={() => onDelete(c.id)}
                    aria-label={`Delete ${c.title}`}
                    className="absolute right-1.5 top-1/2 hidden h-6 w-6 -translate-y-1/2 items-center
                               justify-center rounded-md text-slate-400 hover:bg-slate-200
                               hover:text-slate-700 group-hover:flex focus-visible:flex"
                  >
                    <Trash2 size={13} />
                  </button>
                </div>
              )
            })
          )}
        </nav>

        {/* Import / Export share one row, as in the reference */}
        <div className="flex shrink-0 items-center gap-2 border-t border-slate-100 p-3">
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
            className="flex flex-1 items-center justify-center gap-2 rounded-lg px-3 py-2 text-xs
                       font-medium text-slate-600 transition-colors hover:bg-slate-100"
          >
            <Upload size={13} /> Import
          </button>
          <button
            onClick={onExport}
            disabled={!activeId}
            className="flex flex-1 items-center justify-center gap-2 rounded-lg px-3 py-2 text-xs
                       font-medium text-slate-600 transition-colors enabled:hover:bg-slate-100
                       disabled:cursor-not-allowed disabled:text-slate-300"
          >
            <Download size={13} /> Export
          </button>
        </div>
      </aside>
    </>
  )
}
