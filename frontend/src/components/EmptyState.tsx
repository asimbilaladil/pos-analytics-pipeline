import { BarChart3, Car, Clock, Network, Trophy, UtensilsCrossed } from 'lucide-react'

const CARDS = [
  { icon: BarChart3,        label: 'Sales Report',       prompt: "Last week's sales for Cypress" },
  { icon: Trophy,           label: 'Performance',        prompt: 'Which location had the highest revenue yesterday?' },
  { icon: Network,          label: 'Network Analytics',  prompt: 'Network revenue by location for the last 7 days' },
  { icon: Car,              label: 'Channel Comparison', prompt: 'Airtex drive-through vs in-store split this month' },
  { icon: UtensilsCrossed,  label: 'Product Insights',   prompt: 'Top 5 selling products across all locations last week' },
  { icon: Clock,            label: 'Operations',         prompt: 'Which locations had the slowest kitchen times yesterday?' },
]

export function EmptyState({ onPick }: { onPick: (prompt: string) => void }) {
  return (
    // Reference: main py-10 (40px) + hero mt-4 (16px) = 56px below the header.
    <div className="pt-14">
      <div className="space-y-3 text-center">
        <h1 className="text-3xl font-extrabold tracking-tight text-slate-900">
          What would you like to know?
        </h1>
        <p className="mx-auto max-w-md text-sm text-slate-500">
          Ask about sales, orders, products, kitchen times or weather — across all{' '}
          <span className="font-semibold text-slate-700">12 locations</span>.
        </p>
      </div>

      <div className="my-8 grid grid-cols-1 gap-3 md:grid-cols-2">
        {CARDS.map(({ icon: Icon, label, prompt }) => (
          <button
            key={label}
            onClick={() => onPick(prompt)}
            className="group rounded-xl border border-slate-200 bg-white p-4 text-left transition-all
                       hover:border-brand-200 hover:shadow-md hover:shadow-slate-100"
          >
            <div className="mb-1.5 flex items-center gap-2 text-xs font-medium text-slate-400
                            transition-colors group-hover:text-brand-600">
              <Icon size={13} />
              <span>{label}</span>
            </div>
            <p className="text-sm font-medium text-slate-700">{prompt}</p>
          </button>
        ))}
      </div>
    </div>
  )
}
