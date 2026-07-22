// src/utils/helpers.js

export const fmt = {
  number: (n, decimals = 0) => Number(n).toLocaleString('en-US', { minimumFractionDigits: decimals, maximumFractionDigits: decimals }),
  pct:    (n)               => Number(n).toFixed(1) + '%',
  fixed:  (n, d = 1)        => Number(n).toFixed(d),
}

export function initials(name) {
  return name.split(' ').map(w => w[0]).join('').toUpperCase().slice(0, 2)
}

export function statusPillClass(status) {
  const map = { Normal:'pill-green', Online:'pill-green', Active:'pill-green',
                Warning:'pill-amber', Paused:'pill-amber', Pending:'pill-amber', Acked:'pill-amber',
                Fault:'pill-red',    Critical:'pill-red',  Offline:'pill-red',
                Info:'pill-blue' }
  return 'status-pill ' + (map[status] || 'pill-blue')
}

export function rolePillClass(role) {
  const map = { Admin:'pill-red', Manager:'pill-amber', Engineer:'pill-blue', Analyst:'pill-green' }
  return 'status-pill ' + (map[role] || 'pill-blue')
}

export function severityPillClass(sev) {
  const map = { Critical:'pill-red', Warning:'pill-amber', Info:'pill-blue' }
  return 'status-pill ' + (map[sev] || 'pill-blue')
}

export const RECHARTS_COLORS = {
  accent:  '#00d4aa',
  blue:    '#0099ff',
  purple:  '#7c3aed',
  amber:   '#f59e0b',
  danger:  '#ef4444',
  success: '#10b981',
  text3:   '#6b7a99',
  grid:    'rgba(255,255,255,0.06)',
}
