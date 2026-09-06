import { useEffect, useState } from 'react'
import { Loader2 } from 'lucide-react'
import { api } from './api/client'
import { Chat } from './pages/Chat'
import { Login } from './pages/Login'
import type { User } from './types'

export default function App() {
  const [user, setUser] = useState<User | null>(null)
  const [checking, setChecking] = useState(true)

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
  return user
    ? <Chat onSignedOut={() => setUser(null)} />
    : <Login onSignedIn={setUser} />
}
