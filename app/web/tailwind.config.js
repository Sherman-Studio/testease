import twColors from 'tailwindcss/colors'
import plugin from 'tailwindcss/plugin'

// ── Theming (#1822) ──────────────────────────────────────────────────
// The whole UI is keyed to SEMANTIC scales: ink-50 is always "the
// quietest surface", ink-900 "the highest-contrast text", brand-600
// "the one action colour" — regardless of theme. Both themes ship as
// CSS-variable sets on :root[data-theme=…] (generated below); the
// utilities resolve through rgb(var(--…)) so a theme switch is one
// attribute flip, no per-view styling.
//
// Dark ("control room", the default): ink runs near-black → white.
// Light ("daylight ops"): the same scale runs paper → near-black.
//
// Accent families (red/amber/emerald/…): the dark theme uses Tailwind's
// pastel 200/300/400 shades for text on dark washes; the light theme
// swaps those same utilities to the deep 800/700/600 shades so
// `text-red-300` is readable in both. Shades ≥500 (used for /10 washes
// and /30 rings) work on both themes and stay literal.

const ACCENTS = [
  'red', 'amber', 'emerald', 'rose', 'violet', 'sky', 'teal', 'indigo',
  'fuchsia', 'lime', 'orange', 'cyan', 'slate', 'green', 'yellow',
]

function rgbTriplet(hex) {
  const h = hex.replace('#', '')
  return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16)).join(' ')
}

const DARK = {
  '--te-void': rgbTriplet('#07090e'),
  '--te-panel': rgbTriplet('#10151f'),
  '--te-hairline': '255 255 255',
  '--te-bloom': '39 194 228',
  '--te-grid-dot': '173 196 230',
  '--te-ink-50': rgbTriplet('#0b0f16'),
  '--te-ink-100': rgbTriplet('#131927'),
  '--te-ink-200': rgbTriplet('#1e2738'),
  '--te-ink-300': rgbTriplet('#2d394e'),
  '--te-ink-400': rgbTriplet('#4d5c75'),
  '--te-ink-500': rgbTriplet('#7585a0'),
  '--te-ink-600': rgbTriplet('#94a3bb'),
  '--te-ink-700': rgbTriplet('#b4c1d4'),
  '--te-ink-800': rgbTriplet('#d2dcea'),
  '--te-ink-900': rgbTriplet('#edf2f9'),
  '--te-ink-950': '255 255 255',
  '--te-brand-50': rgbTriplet('#082a35'),
  '--te-brand-100': rgbTriplet('#0b3a49'),
  '--te-brand-200': rgbTriplet('#0e4c60'),
  '--te-brand-300': rgbTriplet('#0f617c'),
  '--te-brand-400': rgbTriplet('#11819f'),
  '--te-brand-500': rgbTriplet('#16a3c4'),
  '--te-brand-600': rgbTriplet('#27c2e4'),
  '--te-brand-700': rgbTriplet('#5cd5ef'),
  '--te-brand-800': rgbTriplet('#93e4f6'),
  '--te-brand-900': rgbTriplet('#cdf3fb'),
}
const LIGHT = {
  '--te-void': rgbTriplet('#eef1f6'),
  '--te-panel': '255 255 255',
  '--te-hairline': '15 23 42',
  '--te-bloom': '14 138 171',
  '--te-grid-dot': '30 41 59',
  '--te-ink-50': rgbTriplet('#f4f6fa'),
  '--te-ink-100': rgbTriplet('#e9edf4'),
  '--te-ink-200': rgbTriplet('#d9e0ea'),
  '--te-ink-300': rgbTriplet('#c2cbda'),
  '--te-ink-400': rgbTriplet('#8d9ab1'),
  '--te-ink-500': rgbTriplet('#64748b'),
  '--te-ink-600': rgbTriplet('#4a5871'),
  '--te-ink-700': rgbTriplet('#334155'),
  '--te-ink-800': rgbTriplet('#1e293b'),
  '--te-ink-900': rgbTriplet('#0f172a'),
  '--te-ink-950': rgbTriplet('#020617'),
  '--te-brand-50': rgbTriplet('#e4f6fb'),
  '--te-brand-100': rgbTriplet('#cfeef7'),
  '--te-brand-200': rgbTriplet('#a5dfee'),
  '--te-brand-300': rgbTriplet('#62c2dd'),
  '--te-brand-400': rgbTriplet('#2aa3c4'),
  '--te-brand-500': rgbTriplet('#0f8cad'),
  '--te-brand-600': rgbTriplet('#0b7795'),
  '--te-brand-700': rgbTriplet('#09637d'),
  '--te-brand-800': rgbTriplet('#074e63'),
  '--te-brand-900': rgbTriplet('#053c4d'),
}
for (const fam of ACCENTS) {
  DARK[`--te-${fam}-200`] = rgbTriplet(twColors[fam][200])
  DARK[`--te-${fam}-300`] = rgbTriplet(twColors[fam][300])
  DARK[`--te-${fam}-400`] = rgbTriplet(twColors[fam][400])
  LIGHT[`--te-${fam}-200`] = rgbTriplet(twColors[fam][800])
  LIGHT[`--te-${fam}-300`] = rgbTriplet(twColors[fam][700])
  LIGHT[`--te-${fam}-400`] = rgbTriplet(twColors[fam][600])
}

function varScale(name, shades) {
  return Object.fromEntries(
    shades.map((s) => [s, `rgb(var(--te-${name}-${s}) / <alpha-value>)`]),
  )
}

const accentOverrides = Object.fromEntries(
  ACCENTS.map((fam) => [
    fam,
    { ...twColors[fam], ...varScale(fam, [200, 300, 400]) },
  ]),
)

/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{vue,js,ts}'],
  theme: {
    extend: {
      fontFamily: {
        // Control-room type system (#1822). All three families are
        // self-hosted via @fontsource — this UI is reached over a
        // kubectl port-forward, so no font CDN can be assumed.
        sans: [
          'IBM Plex Sans',
          '-apple-system',
          'BlinkMacSystemFont',
          'Segoe UI',
          'sans-serif',
        ],
        mono: ['IBM Plex Mono', 'Menlo', 'Monaco', 'Consolas', 'monospace'],
        // Display face for the wordmark, page titles and big readouts.
        display: ['Sora', 'IBM Plex Sans', 'sans-serif'],
      },
      colors: {
        ...accentOverrides,
        ink: varScale('ink', [50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950]),
        brand: varScale('brand', [50, 100, 200, 300, 400, 500, 600, 700, 800, 900]),
        panel: 'rgb(var(--te-panel) / <alpha-value>)',
        void: 'rgb(var(--te-void) / <alpha-value>)',
        // Hairline borders/dividers: white-alpha on dark, slate-alpha on
        // light. Always used with an explicit /alpha.
        hairline: 'rgb(var(--te-hairline) / <alpha-value>)',
        severity: {
          blocker: '#f87171',
          major: '#fbbf24',
          minor: '#94a3b8',
          nit: '#64748b',
        },
      },
      animation: {
        'pulse-dot': 'pulse-dot 1.4s ease-in-out infinite',
        'fade-in': 'fade-in 250ms ease-out',
        'sweep-in': 'sweep-in 350ms cubic-bezier(0.22, 1, 0.36, 1)',
      },
      keyframes: {
        'pulse-dot': {
          '0%, 100%': { opacity: '1', transform: 'scale(1)' },
          '50%': { opacity: '0.55', transform: 'scale(0.8)' },
        },
        'fade-in': {
          '0%': { opacity: '0', transform: 'translateY(2px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        'sweep-in': {
          '0%': { opacity: '0', transform: 'translateY(6px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
      },
    },
  },
  plugins: [
    plugin(({ addBase }) => {
      addBase({
        // Dark is the default (no attribute / unknown value).
        ':root': DARK,
        ':root[data-theme="light"]': LIGHT,
      })
    }),
  ],
}
