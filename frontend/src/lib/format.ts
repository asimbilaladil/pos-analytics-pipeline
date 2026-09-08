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
  const bare = id.replace(/-\d{8}$/, '') // drop a trailing date stamp
  // Both id shapes ship today: family-then-version, and the older reverse.
  const m = /^claude-([a-z]+)-((?:\d+-)*\d+)$/.exec(bare)
         ?? /^claude-((?:\d+-)*\d+)-([a-z]+)$/.exec(bare)
  if (!m) return id
  const [family, ver] = /^\d/.test(m[1]) ? [m[2], m[1]] : [m[1], m[2]]
  return `${family[0].toUpperCase() + family.slice(1)} ${ver.split('-').join('.')}`
}
