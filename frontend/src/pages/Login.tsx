import { useState } from 'react'
import { Loader2 } from 'lucide-react'
import { api, ApiError } from '../api/client'
import logo from '../assets/ayg-logo.png'
import type { User } from '../types'

export function Login({ onSignedIn }: { onSignedIn: (u: User) => void }) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (busy) return
    setBusy(true); setError(null)
    try {
      onSignedIn(await api.login(email, password))
    } catch (err) {
      // Whatever the cause, the message is the server's single generic one --
      // this screen never reveals whether an account exists.
      setError(err instanceof ApiError ? err.message : 'Sign in failed.')
      setBusy(false)
    }
  }

  return (
    <div className="flex min-h-full items-center justify-center bg-slate-50 px-4 py-12">
      <div className="w-full max-w-[400px]">
        <div className="mb-8 flex flex-col items-center gap-4">
          <img src={logo} alt="AYG Food Services" className="h-11 w-auto" />
          <div className="text-center">
            <h1 className="text-2xl font-bold tracking-tight text-slate-900">Laynes Intelligence</h1>
            <p className="mt-1.5 text-sm text-slate-500">Sales &amp; operations, in plain English.</p>
          </div>
        </div>

        <form onSubmit={submit}
          className="rounded-2xl border border-slate-200 bg-white p-7 shadow-sm">
          <label className="mb-1.5 block text-xs font-semibold uppercase tracking-wide text-slate-500">
            Email
          </label>
          <input
            type="email" required autoFocus autoComplete="username"
            value={email} onChange={(e) => setEmail(e.target.value)}
            placeholder="you@aygfoods.com"
            className="mb-4 w-full rounded-lg border border-slate-300 px-3.5 py-2.5 text-[15px]
                       text-slate-900 placeholder:text-slate-400 transition-colors
                       focus:border-brand-500 focus:outline-none focus:ring-4 focus:ring-brand-50"
          />
          <label className="mb-1.5 block text-xs font-semibold uppercase tracking-wide text-slate-500">
            Password
          </label>
          <input
            type="password" required autoComplete="current-password"
            value={password} onChange={(e) => setPassword(e.target.value)}
            className="w-full rounded-lg border border-slate-300 px-3.5 py-2.5 text-[15px]
                       text-slate-900 transition-colors focus:border-brand-500
                       focus:outline-none focus:ring-4 focus:ring-brand-50"
          />

          {error && (
            <p role="alert" className="mt-4 rounded-lg bg-brand-50 px-3 py-2 text-[13px] text-brand-700">
              {error}
            </p>
          )}

          <button type="submit" disabled={busy}
            className="mt-5 flex w-full items-center justify-center gap-2 rounded-lg bg-brand-500
                       py-2.5 text-[15px] font-semibold text-white transition-colors
                       hover:bg-brand-700 disabled:opacity-70">
            {busy && <Loader2 size={15} className="animate-spin" />}
            {busy ? 'Signing in…' : 'Sign in'}
          </button>
        </form>

        <p className="mt-5 text-center text-xs text-slate-400">
          Authorised users only · access is logged
        </p>
      </div>
    </div>
  )
}
