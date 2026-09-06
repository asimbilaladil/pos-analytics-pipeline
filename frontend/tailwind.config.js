/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // AYG brand red, used sparingly: primary action, active accents.
        brand: {
          50: '#FDECED', 100: '#FBD5D8', 200: '#F6C9CD',
          500: '#D6202F', 600: '#C41C2A', 700: '#B0182A',
        },
      },
      fontFamily: { sans: ['Inter', 'ui-sans-serif', 'system-ui', 'sans-serif'] },
      keyframes: {
        'fade-up': { '0%': { opacity: '0', transform: 'translateY(4px)' },
                     '100%': { opacity: '1', transform: 'translateY(0)' } },
      },
      animation: { 'fade-up': 'fade-up .18s ease-out both' },
    },
  },
  plugins: [],
}
