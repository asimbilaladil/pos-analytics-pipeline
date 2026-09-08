import { AnalyticsCards } from './AnalyticsCards'

export function WelcomeState({ onPick }: { onPick: (prompt: string) => void }) {
  return (
    <div className="py-9">
      <div className="mb-8 text-center">
        <p className="mb-3 text-[11px] font-semibold uppercase tracking-[0.16em] text-ink-400">
          Your AI analytics assistant
        </p>
        <h1 className="mx-auto max-w-3xl text-[clamp(30px,3.4vw,46px)] font-bold leading-[1.08]
                       tracking-[-0.035em] text-ink-900">
          What would you like to know?
        </h1>
        <p className="mx-auto mt-3.5 max-w-2xl text-[15.5px] leading-relaxed text-ink-500">
          Ask about sales, orders, products, kitchen times, weather, and performance
          across all <span className="font-semibold text-ink-700">12 AYG Food Services</span> locations.
        </p>
      </div>
      <AnalyticsCards onPick={onPick} />
    </div>
  )
}
