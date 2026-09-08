import { ArrowRight, BarChart3, Clock, Layers, Network, TrendingUp, UtensilsCrossed } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { BarsArt, ClockArt, LayersArt, NetworkArt, ProductArt, TrendArt } from './CardArt'

interface Card {
  category: string
  title: string
  example: string
  icon: LucideIcon
  tile: string       // icon tile background
  ink: string        // icon + art colour
  Art: (p: { color: string }) => JSX.Element
}

/**
 * Six entry points into the assistant.
 *
 * Every `example` is a query the assistant can actually answer. Two of the
 * reference's captions were changed because they name capabilities this
 * backend does not have:
 *
 *  - "Which locations are outperforming forecast?" — the assistant has no
 *    forecast to compare against, so it becomes a highest-sales question.
 *  - "Compare delivery vs dine-in sales" — A8 established that dining_option
 *    codes do NOT prove a service mode; the only verified split is
 *    web-associated vs non-web-associated ordering. Shipping the reference's
 *    wording would have put a claim on screen the data cannot support.
 */
export const CARDS: Card[] = [
  { category: 'Sales Report', title: 'Sales performance',
    example: "Show me last week's sales by store",
    icon: BarChart3, tile: '#FEE4E2', ink: '#D6202F', Art: BarsArt },
  { category: 'Performance', title: 'Store performance',
    example: 'Which locations had the highest sales last week?',
    icon: TrendingUp, tile: '#DCFAE6', ink: '#079455', Art: TrendArt },
  { category: 'Network Analytics', title: 'Network insights',
    example: 'Compare sales across all 12 locations',
    icon: Network, tile: '#D1E9FF', ink: '#1570EF', Art: NetworkArt },
  { category: 'Channel Comparison', title: 'Channel analysis',
    example: 'Compare web-associated and non-web-associated orders this month',
    icon: Layers, tile: '#EBE9FE', ink: '#6938EF', Art: LayersArt },
  { category: 'Product Insights', title: 'Top products',
    example: 'What are the top 5 selling products?',
    icon: UtensilsCrossed, tile: '#FFEAD5', ink: '#E04F16', Art: ProductArt },
  { category: 'Operations', title: 'Operational efficiency',
    example: 'Show me kitchen times and order volume',
    icon: Clock, tile: '#CCFBEF', ink: '#0E9384', Art: ClockArt },
]

export function AnalyticsCards({ onPick }: { onPick: (prompt: string) => void }) {
  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
      {CARDS.map(({ category, title, example, icon: Icon, tile, ink, Art }, i) => (
        <button
          key={title}
          onClick={() => onPick(example)}
          style={{ animationDelay: `${i * 40}ms` }}
          className="group relative flex animate-pop-in flex-col overflow-hidden rounded-2xl
                     border border-line bg-white/86 p-5 text-left shadow-card backdrop-blur-sm
                     transition-all duration-200 ease-out
                     hover:-translate-y-0.5 hover:border-ink-400/35 hover:bg-white hover:shadow-lift
                     min-h-[176px]"
        >
          <Art color={ink} />

          <div className="relative z-10 mb-4 flex items-center gap-2.5">
            <span className="flex h-10 w-10 items-center justify-center rounded-xl"
                  style={{ backgroundColor: tile, color: ink }}>
              <Icon size={18} strokeWidth={2.2} />
            </span>
            <span className="text-[10.5px] font-semibold uppercase tracking-[0.11em] text-ink-400">
              {category}
            </span>
          </div>

          <div className="relative z-10 mt-auto">
            <h3 className="mb-1.5 flex items-center gap-1.5 text-[17px] font-semibold
                           tracking-[-0.015em] text-ink-900">
              {title}
              <ArrowRight size={15}
                className="text-ink-400 transition-transform duration-200 group-hover:translate-x-0.5" />
            </h3>
            <p className="max-w-[72%] text-[13px] leading-snug text-ink-500">
              “{example}”
            </p>
          </div>
        </button>
      ))}
    </div>
  )
}
