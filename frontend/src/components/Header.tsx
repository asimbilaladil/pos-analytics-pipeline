import { useEffect, useRef, useState } from 'react'
import { ChevronDown, LogOut, Menu, Plus, Sparkles } from 'lucide-react'
import type { User } from '../types'
import { modelLabel } from '../lib/format'

/**
 * Light 64px bar. The sidebar owns the branding, so the header carries only
 * controls — matching the reference, where the left side is empty on desktop.
 *
 * shrink-0 inside the main column's flex layout is what keeps it visible:
 * because the column itself never scrolls, the header simply never moves.
 */
export function Header({
  user, models, model, onModelChange, onNewChat, onSignOut, onOpenSidebar,
}: {
  user: User | null
  models: string[]
  model: string
  onModelChange: (m: string) => void
  onNewChat: () => void
  onSignOut: () => void
  onOpenSidebar: () => void
}) {
  // Real data only: initials come from the signed-in account, never invented.
  const name = user?.full_name?.trim() || user?.email?.split('@')[0] || null
  const initials = name
    ? name.split(/[\s._-]+/).filter(Boolean).slice(0, 2).map((p) => p[0]!.toUpperCase()).join('')
    : null

  return (
    <header className="z-20 flex h-16 shrink-0 items-center justify-between gap-3
                       border-b border-line bg-white/70 px-4 backdrop-blur-md sm:px-6">
      <button onClick={onOpenSidebar} aria-label="Open sidebar"
        className="flex h-9 w-9 items-center justify-center rounded-lg text-ink-500
                   transition-colors hover:bg-app hover:text-ink-900 lg:hidden">
        <Menu size={18} />
      </button>
      <div className="hidden lg:block" />

      <div className="flex shrink-0 items-center gap-2.5">
        <div className="relative hidden sm:block">
          <Sparkles size={13}
            className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-brand-500" />
          <select
            value={model}
            onChange={(e) => onModelChange(e.target.value)}
            aria-label="Assistant model"
            title={model}
            className="h-10 cursor-pointer appearance-none rounded-xl border border-line bg-white
                       pl-8 pr-9 text-[13px] font-medium text-ink-900 shadow-card
                       transition-colors hover:border-ink-400/40 focus:outline-none"
          >
            {models.map((m) => <option key={m} value={m}>{modelLabel(m)}</option>)}
          </select>
          <ChevronDown size={14}
            className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-ink-400" />
        </div>

        <button
          onClick={onNewChat}
          className="flex h-10 items-center gap-1.5 rounded-xl bg-brand-500 px-4 text-[13px]
                     font-semibold text-white shadow-[0_1px_2px_rgba(214,32,47,.28)]
                     transition-all hover:bg-brand-600 active:scale-[.97]"
        >
          <Plus size={15} strokeWidth={2.6} />
          <span className="hidden sm:inline">New chat</span>
        </button>

        <span className="hidden h-6 w-px bg-line sm:block" aria-hidden />

        {initials ? (
          <ProfileMenu name={name!} email={user?.email ?? null} initials={initials}
                       onSignOut={onSignOut} />
        ) : (
          <button onClick={onSignOut}
            className="whitespace-nowrap rounded-lg px-2.5 py-2 text-[13px] font-medium
                       text-ink-500 transition-colors hover:bg-app hover:text-ink-900">
            Sign out
          </button>
        )}
      </div>
    </header>
  )
}

/**
 * Avatar button + dropdown. The trigger only toggles the menu; signing out is
 * a deliberate second click on the menu item, which reuses the same `onSignOut`
 * handler the header already owns — there is no second copy of that logic.
 */
function ProfileMenu({ name, email, initials, onSignOut }: {
  name: string
  email: string | null
  initials: string
  onSignOut: () => void
}) {
  const [open, setOpen] = useState(false)
  const root = useRef<HTMLDivElement>(null)

  // Close on outside click and on Escape. Bound only while open so the app is
  // not listening on document for the whole session.
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (!root.current?.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  return (
    <div ref={root} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="Account menu"
        className={`flex items-center gap-2.5 rounded-xl py-1.5 pl-1.5 pr-2.5 transition-colors
                    focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2
                    focus-visible:outline-brand-500 hover:bg-app ${open ? 'bg-app' : ''}`}
      >
        <span className="flex h-8 w-8 items-center justify-center rounded-full bg-ink-900/[.06]
                         text-[11.5px] font-semibold text-ink-700">
          {initials}
        </span>
        <span className="hidden text-[13px] font-medium text-ink-700 md:inline">{name}</span>
        <ChevronDown size={13}
          className={`hidden text-ink-400 transition-transform md:inline ${open ? 'rotate-180' : ''}`} />
      </button>

      {open && (
        <div
          role="menu"
          aria-label="Account"
          className="absolute right-0 top-[calc(100%+8px)] z-50 w-[236px] max-w-[calc(100vw-2rem)]
                     overflow-hidden rounded-xl border border-line bg-white p-1.5 shadow-float"
        >
          <div className="px-2.5 py-2">
            <p className="truncate text-[13px] font-semibold text-ink-900">{name}</p>
            {email && <p className="truncate text-[12px] text-ink-500">{email}</p>}
          </div>
          <div className="my-1 h-px bg-line" role="separator" />
          <button
            type="button"
            role="menuitem"
            autoFocus
            onClick={() => { setOpen(false); onSignOut() }}
            className="flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-[13px]
                       font-medium text-ink-700 transition-colors hover:bg-app hover:text-ink-900
                       focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-[-2px]
                       focus-visible:outline-brand-500"
          >
            <LogOut size={14} className="text-ink-400" />
            Sign out
          </button>
        </div>
      )}
    </div>
  )
}
