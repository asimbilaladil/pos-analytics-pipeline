/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        brand: {  // AYG red — primary actions, active accents
          50: '#FEF2F3', 100: '#FCE3E5', 200: '#F7CBCF', 300: '#EFA6AC',
          500: '#D6202F', 600: '#C41C2A', 700: '#A81826',
        },
        // Neutral ramp from the reference spec.
        app:    '#F6F7F9',   // application background
        rail:   '#F4F6F8',   // sidebar
        line:   '#E4E7EC',   // borders
        ink:    { 900: '#101828', 700: '#344054', 500: '#667085', 400: '#98A2B3' },
        // Accent families used ONLY inside analytics card icon tiles, so the
        // interface never reads as rainbow-coloured.
        tile: {
          red:    '#FEE4E2', redInk:    '#D6202F',
          green:  '#DCFAE6', greenInk:  '#079455',
          blue:   '#D1E9FF', blueInk:   '#1570EF',
          violet: '#EBE9FE', violetInk: '#6938EF',
          orange: '#FFEAD5', orangeInk: '#E04F16',
          teal:   '#CCFBEF', tealInk:   '#0E9384',
        },
      },
      fontFamily: { sans: ['Inter', 'ui-sans-serif', 'system-ui', 'sans-serif'] },
      boxShadow: {
        card:  '0 1px 2px rgba(16,24,40,.04), 0 1px 3px rgba(16,24,40,.03)',
        lift:  '0 2px 4px rgba(16,24,40,.04), 0 10px 24px -10px rgba(16,24,40,.14)',
        float: '0 4px 10px -2px rgba(16,24,40,.06), 0 20px 40px -14px rgba(16,24,40,.18)',
      },
      keyframes: {
        'fade-up': { '0%': { opacity: '0', transform: 'translateY(6px)' },
                     '100%': { opacity: '1', transform: 'translateY(0)' } },
        'pop-in':  { '0%': { opacity: '0', transform: 'translateY(8px) scale(.985)' },
                     '100%': { opacity: '1', transform: 'translateY(0) scale(1)' } },
      },
      animation: {
        'fade-up': 'fade-up .2s cubic-bezier(.22,1,.36,1) both',
        'pop-in':  'pop-in .22s cubic-bezier(.22,1,.36,1) both',
      },
    },
  },
  plugins: [],
}
