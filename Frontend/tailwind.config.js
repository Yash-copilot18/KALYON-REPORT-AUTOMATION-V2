/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      fontFamily: {
        sans: ['IBM Plex Sans', 'system-ui', 'sans-serif'],
        mono: ['IBM Plex Mono', 'monospace'],
      },
      colors: {
        ge: {
          navy:    'rgb(var(--ge-navy) / <alpha-value>)',
          dark:    'rgb(var(--ge-dark) / <alpha-value>)',
          card:    'rgb(var(--ge-card) / <alpha-value>)',
          surface: 'rgb(var(--ge-surface) / <alpha-value>)',
          elevated:'rgb(var(--ge-elevated) / <alpha-value>)',
          border:  'rgb(var(--ge-border) / <alpha-value>)',
          border2: 'rgb(var(--ge-border2) / <alpha-value>)',
          accent:  'rgb(var(--ge-accent) / <alpha-value>)',
          blue:    'rgb(var(--ge-blue) / <alpha-value>)',
          purple:  'rgb(var(--ge-purple) / <alpha-value>)',
          warn:    'rgb(var(--ge-warn) / <alpha-value>)',
          danger:  'rgb(var(--ge-danger) / <alpha-value>)',
          success: 'rgb(var(--ge-success) / <alpha-value>)',
          text1:   'rgb(var(--ge-text1) / <alpha-value>)',
          text2:   'rgb(var(--ge-text2) / <alpha-value>)',
          text3:   'rgb(var(--ge-text3) / <alpha-value>)',
        }
      },
      animation: {
        'pulse-slow': 'pulse 2s cubic-bezier(0.4,0,0.6,1) infinite',
        'spin-slow':  'spin 0.7s linear infinite',
      }
    }
  },
  plugins: []
}
