/**
 * Decorative motifs for the analytics cards.
 *
 * Inline SVG rather than images: they inherit the card's tint, cost nothing to
 * load, and stay crisp at any size. Each sits low-opacity behind the card text
 * and is aria-hidden — it carries no information, so a screen reader must not
 * announce it.
 */
const wrap = 'pointer-events-none absolute select-none'

export function BarsArt({ color }: { color: string }) {
  return (
    <svg aria-hidden className={`${wrap} bottom-0 right-3 h-24 w-32`} viewBox="0 0 128 96" fill="none">
      {[[8, 34], [34, 52], [60, 26], [86, 68], [108, 44]].map(([x, h], i) => (
        <rect key={i} x={x} y={96 - h} width="16" height={h} rx="4" fill={color}
              opacity={0.18 + i * 0.09} />
      ))}
    </svg>
  )
}

export function TrendArt({ color }: { color: string }) {
  return (
    <svg aria-hidden className={`${wrap} bottom-0 right-2 h-28 w-36`} viewBox="0 0 144 112" fill="none">
      <path d="M4 96 L36 70 L64 82 L96 40 L138 18" stroke={color} strokeWidth="3"
            strokeLinecap="round" strokeLinejoin="round" opacity=".35" />
      <path d="M4 96 L36 70 L64 82 L96 40 L138 18 L138 112 L4 112 Z" fill={color} opacity=".08" />
      <circle cx="96" cy="40" r="7" fill={color} opacity=".22" />
      <circle cx="96" cy="40" r="3.5" fill={color} opacity=".5" />
    </svg>
  )
}

export function NetworkArt({ color }: { color: string }) {
  const nodes = [[26, 60], [58, 30], [58, 88], [96, 52], [126, 78], [120, 26]]
  return (
    <svg aria-hidden className={`${wrap} bottom-0 right-1 h-28 w-40`} viewBox="0 0 160 112" fill="none">
      {[[0, 1], [0, 2], [1, 3], [2, 3], [3, 4], [3, 5]].map(([a, b], i) => (
        <line key={i} x1={nodes[a][0]} y1={nodes[a][1]} x2={nodes[b][0]} y2={nodes[b][1]}
              stroke={color} strokeWidth="1.5" opacity=".28" />
      ))}
      {nodes.map(([cx, cy], i) => (
        <g key={i}>
          <circle cx={cx} cy={cy} r="9" fill={color} opacity=".12" />
          <circle cx={cx} cy={cy} r="4" fill={color} opacity=".45" />
        </g>
      ))}
    </svg>
  )
}

export function LayersArt({ color }: { color: string }) {
  return (
    <svg aria-hidden className={`${wrap} bottom-0 right-3 h-24 w-32`} viewBox="0 0 128 96" fill="none">
      {[0, 1, 2].map((i) => (
        <rect key={i} x={16 + i * 6} y={26 + i * 18} width="92" height="16" rx="5"
              fill={color} opacity={0.3 - i * 0.08} />
      ))}
      <rect x="74" y="14" width="18" height="76" rx="6" fill={color} opacity=".16" />
      <rect x="98" y="30" width="18" height="60" rx="6" fill={color} opacity=".24" />
    </svg>
  )
}

export function ProductArt({ color }: { color: string }) {
  return (
    <svg aria-hidden className={`${wrap} bottom-0 right-4 h-28 w-28`} viewBox="0 0 112 112" fill="none">
      {/* an abstract cup, not a photo -- keeps it elegant rather than childish */}
      <path d="M30 40 h52 l-7 62 a8 8 0 0 1-8 7 H45 a8 8 0 0 1-8-7 Z" fill={color} opacity=".14" />
      <path d="M30 40 h52" stroke={color} strokeWidth="3" strokeLinecap="round" opacity=".34" />
      <path d="M62 40 c0-16 6-24 16-28" stroke={color} strokeWidth="3" strokeLinecap="round" opacity=".3" />
      <ellipse cx="56" cy="34" rx="22" ry="9" fill={color} opacity=".22" />
      <circle cx="46" cy="28" r="7" fill={color} opacity=".18" />
      <circle cx="64" cy="26" r="9" fill={color} opacity=".14" />
    </svg>
  )
}

export function ClockArt({ color }: { color: string }) {
  return (
    <svg aria-hidden className={`${wrap} bottom-1 right-3 h-28 w-28`} viewBox="0 0 112 112" fill="none">
      <circle cx="58" cy="56" r="40" fill={color} opacity=".10" />
      <circle cx="58" cy="56" r="40" stroke={color} strokeWidth="2.5" opacity=".3" />
      <path d="M58 32 V56 l17 11" stroke={color} strokeWidth="3.5"
            strokeLinecap="round" strokeLinejoin="round" opacity=".45" />
      {[0, 90, 180, 270].map((deg) => (
        <rect key={deg} x="57" y="20" width="2.5" height="7" rx="1.2" fill={color} opacity=".3"
              transform={`rotate(${deg} 58 56)`} />
      ))}
    </svg>
  )
}
