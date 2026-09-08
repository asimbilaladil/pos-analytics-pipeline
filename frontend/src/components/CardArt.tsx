/**
 * Decorative motifs for the analytics cards.
 *
 * Inline SVG rather than images: they inherit the card's tint, cost nothing to
 * load, and stay crisp at any size. Each sits low-opacity behind the card text
 * and is aria-hidden — it carries no information, so a screen reader must not
 * announce it.
 *
 * Sizing is deliberately secondary to the text. `box` keeps every motif inside
 * roughly 25-30% of the card width and steps down on smaller screens, so the
 * art never competes with the title or crowds the example query. Each SVG also
 * carries a group opacity so the motif reads as a wash, not as content.
 */
const wrap = 'pointer-events-none absolute select-none'

/** mobile 78x66 -> laptop 94x80 -> desktop 110x94 (CSS px). */
const box = 'h-[62px] w-[72px] md:h-[76px] md:w-[88px] xl:h-[86px] xl:w-[100px]'

const ART = `${wrap} ${box}`
const FADE = 0.68

export function BarsArt({ color }: { color: string }) {
  return (
    <svg aria-hidden className={`${ART} bottom-3 right-3`} viewBox="0 0 128 108" fill="none"
         opacity={FADE} preserveAspectRatio="xMaxYMax meet">
      {[[8, 38], [34, 58], [60, 29], [86, 76], [108, 49]].map(([x, h], i) => (
        <rect key={i} x={x} y={96 - h} width="16" height={h} rx="4" fill={color}
              opacity={0.26 + i * 0.13} />
      ))}
      <rect x="4" y="98" width="120" height="2" rx="1" fill={color} opacity=".22" />
    </svg>
  )
}

export function TrendArt({ color }: { color: string }) {
  return (
    <svg aria-hidden className={`${ART} bottom-3 right-3`} viewBox="0 0 144 112" fill="none"
         opacity={FADE} preserveAspectRatio="xMaxYMax meet">
      <path d="M4 96 L36 70 L64 82 L96 40 L138 18" stroke={color} strokeWidth="4"
            strokeLinecap="round" strokeLinejoin="round" opacity=".5" />
      <path d="M4 96 L36 70 L64 82 L96 40 L138 18 L138 112 L4 112 Z" fill={color} opacity=".11" />
      <circle cx="96" cy="40" r="8" fill={color} opacity=".3" />
      <circle cx="96" cy="40" r="4" fill={color} opacity=".7" />
    </svg>
  )
}

export function NetworkArt({ color }: { color: string }) {
  const nodes = [[26, 60], [58, 30], [58, 88], [96, 52], [126, 78], [120, 26]]
  return (
    <svg aria-hidden className={`${ART} bottom-3 right-3`} viewBox="0 0 152 112" fill="none"
         opacity={FADE} preserveAspectRatio="xMaxYMax meet">
      {[[0, 1], [0, 2], [1, 3], [2, 3], [3, 4], [3, 5]].map(([a, b], i) => (
        <line key={i} x1={nodes[a][0]} y1={nodes[a][1]} x2={nodes[b][0]} y2={nodes[b][1]}
              stroke={color} strokeWidth="2" opacity=".4" />
      ))}
      {nodes.map(([cx, cy], i) => (
        <g key={i}>
          <circle cx={cx} cy={cy} r="9" fill={color} opacity=".17" />
          <circle cx={cx} cy={cy} r="4" fill={color} opacity=".64" />
        </g>
      ))}
    </svg>
  )
}

/**
 * Channel comparison: paired columns on a baseline, i.e. two series measured
 * against each other. An earlier version used three long stacked pills, which
 * read as four oversized lozenges rather than as a chart.
 */
export function LayersArt({ color }: { color: string }) {
  const pairs = [[42, 27], [58, 40], [31, 49], [66, 36]]
  return (
    <svg aria-hidden className={`${ART} bottom-3 right-3`} viewBox="0 0 112 96" fill="none"
         opacity={FADE} preserveAspectRatio="xMaxYMax meet">
      {pairs.map(([a, b], i) => {
        const x = 10 + i * 25
        return (
          <g key={i}>
            <rect x={x} y={82 - a} width="9" height={a} rx="3" fill={color} opacity=".44" />
            <rect x={x + 11} y={82 - b} width="9" height={b} rx="3" fill={color} opacity=".22" />
          </g>
        )
      })}
      <rect x="6" y="84" width="100" height="2" rx="1" fill={color} opacity=".26" />
    </svg>
  )
}

export function ProductArt({ color }: { color: string }) {
  return (
    <svg aria-hidden className={`${ART} bottom-3 right-4`} viewBox="0 0 112 112" fill="none"
         opacity={FADE} preserveAspectRatio="xMaxYMax meet">
      {/* an abstract cup, not a photo -- keeps it elegant rather than childish */}
      <path d="M30 40 h52 l-7 62 a8 8 0 0 1-8 7 H45 a8 8 0 0 1-8-7 Z" fill={color} opacity=".2" />
      <path d="M30 40 h52" stroke={color} strokeWidth="4" strokeLinecap="round" opacity=".48" />
      <path d="M62 40 c0-16 6-24 16-28" stroke={color} strokeWidth="4" strokeLinecap="round" opacity=".42" />
      <ellipse cx="56" cy="34" rx="22" ry="9" fill={color} opacity=".3" />
      <circle cx="46" cy="28" r="7" fill={color} opacity=".25" />
      <circle cx="64" cy="26" r="9" fill={color} opacity=".2" />
    </svg>
  )
}

export function ClockArt({ color }: { color: string }) {
  return (
    <svg aria-hidden className={`${ART} bottom-3 right-4`} viewBox="0 0 112 112" fill="none"
         opacity={FADE} preserveAspectRatio="xMaxYMax meet">
      <circle cx="58" cy="56" r="40" fill={color} opacity=".14" />
      <circle cx="58" cy="56" r="40" stroke={color} strokeWidth="3.5" opacity=".42" />
      <path d="M58 32 V56 l17 11" stroke={color} strokeWidth="4.5"
            strokeLinecap="round" strokeLinejoin="round" opacity=".64" />
      {[0, 90, 180, 270].map((deg) => (
        <rect key={deg} x="57" y="20" width="2.5" height="7" rx="1.2" fill={color} opacity=".42"
              transform={`rotate(${deg} 58 56)`} />
      ))}
    </svg>
  )
}
