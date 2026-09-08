/**
 * Starter chips under the composer on an empty conversation only.
 * Each maps to a query the assistant genuinely answers.
 */
const CHIPS: { label: string; prompt: string }[] = [
  { label: "Last week's sales",       prompt: "What were last week's sales across all locations?" },
  { label: 'Sales by store',          prompt: 'Show me last week’s sales by store' },
  { label: 'Top products',            prompt: 'What are the top 5 selling products?' },
  { label: 'Order times',             prompt: 'Show me kitchen times by location for last week' },
  { label: "This month's performance", prompt: 'How did each location perform this month?' },
]

export function QuickChips({ onPick }: { onPick: (prompt: string) => void }) {
  return (
    <div className="mt-3 flex flex-wrap items-center justify-center gap-2">
      {CHIPS.map(({ label, prompt }) => (
        <button
          key={label}
          onClick={() => onPick(prompt)}
          className="rounded-full border border-line bg-white/80 px-3.5 py-1.5 text-[12.5px]
                     font-medium text-ink-500 transition-all duration-150
                     hover:border-ink-400/40 hover:bg-white hover:text-ink-900 hover:shadow-card"
        >
          {label}
        </button>
      ))}
    </div>
  )
}
