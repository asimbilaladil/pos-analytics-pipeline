import { ChevronDown, Menu, Plus } from 'lucide-react'
import logo from '../assets/ayg-logo.png'

/**
 * 64px bar spanning the full main column. It is deliberately OUTSIDE the
 * centred content column so it runs edge to edge, with the actions as one
 * tight right-aligned group rather than spread across the width.
 */
export function Header({
  models, model, onModelChange, onNewChat, onSignOut, onOpenSidebar,
}: {
  models: string[]
  model: string
  onModelChange: (m: string) => void
  onNewChat: () => void
  onSignOut: () => void
  onOpenSidebar: () => void
}) {
  return (
    <header className="flex h-16 shrink-0 items-center justify-between border-b border-slate-200
                       bg-white/80 px-6 backdrop-blur-md">
      <div className="flex min-w-0 items-center gap-3">
        <button onClick={onOpenSidebar} aria-label="Open sidebar"
          className="-ml-1 flex h-8 w-8 items-center justify-center rounded-lg text-slate-500
                     hover:bg-slate-100 lg:hidden">
          <Menu size={17} />
        </button>
        <img src={logo} alt="AYG Food Services" className="h-6 w-auto shrink-0" />
        <span className="truncate text-base font-semibold text-slate-900">
          <span className="hidden sm:inline">Laynes Intelligence</span>
          <span className="sm:hidden">Laynes</span>
        </span>
      </div>

      <div className="flex items-center gap-3">
        <div className="relative hidden sm:block">
          <select
            value={model}
            onChange={(e) => onModelChange(e.target.value)}
            aria-label="Model"
            className="cursor-pointer appearance-none rounded-full border border-slate-200 bg-slate-100
                       py-1.5 pl-3 pr-8 text-xs font-medium text-slate-700 transition-colors
                       hover:bg-slate-200/70 focus:outline-none"
          >
            {models.map((m) => <option key={m} value={m}>{m}</option>)}
          </select>
          <ChevronDown size={11}
            className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-slate-400" />
        </div>

        <button
          onClick={onNewChat}
          className="flex items-center gap-1.5 rounded-full bg-brand-500 px-4 py-1.5 text-xs
                     font-semibold text-white shadow-sm shadow-brand-200 transition-all
                     hover:bg-brand-700 active:scale-95"
        >
          <Plus size={13} strokeWidth={2.5} />
          <span className="hidden sm:inline">New chat</span>
        </button>

        <div className="hidden h-4 w-px bg-slate-200 sm:block" />

        <button onClick={onSignOut}
          className="whitespace-nowrap text-xs font-medium text-slate-500 transition-colors
                     hover:text-slate-800">
          Sign out
        </button>
      </div>
    </header>
  )
}
