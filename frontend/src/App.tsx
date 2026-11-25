import { useEffect, useState } from 'react'
import { Loader2 } from 'lucide-react'
import { api } from './api/client'
import { AdminChats } from './pages/AdminChats'
import { AdminMonitoring } from './pages/AdminMonitoring'
import { Chat } from './pages/Chat'
import { Login } from './pages/Login'
import type { User } from './types'

export default function App() {
  const [user, setUser] = useState<User | null>(null)
  const [checking, setChecking] = useState(true)
  const [view, setView] = useState<'chat' | 'admin' | 'monitoring'>('chat')

  // The session lives in an HttpOnly cookie, so the only way to know whether
  // we are signed in is to ask the server. That also means a refresh restores
  // the session without any token ever touching JavaScript.
  useEffect(() => {
    api.me().then(setUser).catch(() => setUser(null)).finally(() => setChecking(false))
  }, [])

  if (checking) {
    return (
      <div className="flex h-full items-center justify-center">
        <Loader2 className="animate-spin text-slate-300" size={22} />
      </div>
    )
  }
  if (!user) return <Login onSignedIn={setUser} />
  const signedOut = () => { setUser(null); setView('chat') }
  // The server enforces super_admin on every admin/monitoring call; these
  // checks only decide what to draw.
  if (view === 'admin' && user.is_admin)
    return <AdminChats onBack={() => setView('chat')} onSignedOut={signedOut} />
  if (view === 'monitoring' && user.is_admin)
    return <AdminMonitoring onBack={() => setView('chat')} onSignedOut={signedOut} />
  return <Chat user={user} onSignedOut={signedOut}
               onOpenAdmin={user.is_admin ? () => setView('admin') : undefined}
               onOpenMonitoring={user.is_admin ? () => setView('monitoring') : undefined} />
}
