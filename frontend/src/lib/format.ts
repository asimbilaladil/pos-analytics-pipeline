/** Shared formatting helpers, so grouping/labels are defined in one place. */

/** Buckets a conversation by how recently it was touched. */
export function dayBucket(iso: string): string {
  const then = new Date(iso)
  const now = new Date()
  const startOf = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime()
  const days = Math.round((startOf(now) - startOf(then)) / 86_400_000)
  if (days <= 0) return 'Today'
  if (days === 1) return 'Yesterday'
  if (days < 7) return 'Previous 7 days'
  if (days < 30) return 'Previous 30 days'
  return 'Older'
}

/** Bucket order is fixed rather than alphabetical, so "Older" never leads. */
export const BUCKET_ORDER = ['Today', 'Yesterday', 'Previous 7 days', 'Previous 30 days', 'Older']

/**
 * Short display label for a model id: "claude-haiku-4-5-20251001" -> "Haiku 4.5".
 *
 * Derived rather than looked up in a table, so a model the backend adds later
 * still renders sensibly without a frontend change. The option value stays the
 * full id -- only the label is shortened, so what we send the API is unchanged.
 */
export function modelLabel(id: string): string {
  // OpenAI ids: "gpt-6-luna" -> "GPT-6 Luna", "gpt-6.1-sol" -> "GPT-6.1 Sol".
  const g = /^gpt-([\d.]+)(?:-([a-z]+))?$/.exec(id)
  if (g) return `GPT-${g[1]}${g[2] ? ' ' + g[2][0].toUpperCase() + g[2].slice(1) : ''}`
  const bare = id.replace(/-\d{8}$/, '') // drop a trailing date stamp
  // Both id shapes ship today: family-then-version, and the older reverse.
  const m = /^claude-([a-z]+)-((?:\d+-)*\d+)$/.exec(bare)
         ?? /^claude-((?:\d+-)*\d+)-([a-z]+)$/.exec(bare)
  if (!m) return id
  const [family, ver] = /^\d/.test(m[1]) ? [m[2], m[1]] : [m[1], m[2]]
  return `${family[0].toUpperCase() + family.slice(1)} ${ver.split('-').join('.')}`
}

/** Provider of a model id, for display only (the server decides routing). */
export function providerOf(id: string | null | undefined): 'anthropic' | 'openai' | null {
  if (!id) return null
  return /^(gpt-|o\d)/.test(id) ? 'openai' : 'anthropic'
}

/** Provider of a model id the server sent without one (older API). */
export function providerOfId(id: string): 'anthropic' | 'openai' {
  return providerOf(id) ?? 'anthropic'
}

export const PROVIDER_NAMES: Record<'anthropic' | 'openai', string> = {
  anthropic: 'Anthropic',
  openai: 'OpenAI',
}
