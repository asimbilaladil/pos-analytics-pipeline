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
