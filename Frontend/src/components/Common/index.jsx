// src/components/Common/index.jsx
import React from 'react'
import { useApp } from '../../utils/AppContext'

// ── Spinner ──────────────────────────────────────────────────────────────────
export function Spinner({ size = 14 }) {
  return (
    <span
      style={{ width: size, height: size }}
      className="inline-block border-2 border-white/30 border-t-white rounded-full animate-spin-slow"
    />
  )
}

// ── KPI Card ─────────────────────────────────────────────────────────────────
export function KpiCard({ label, value, unit, change, up, color = 'blue' }) {
  return (
    <div className={`kpi-card kpi-${color}`}>
      <div className="font-mono text-[10px] text-ge-text3 uppercase tracking-widest mb-1.5">{label}</div>
      <div className="font-mono text-[22px] font-semibold text-ge-text1 leading-none mb-1.5">
        {value}
        {unit && <span className="text-[12px] text-ge-text3 ml-1">{unit}</span>}
      </div>
      {change && (
        <div className="flex items-center gap-1.5">
          <span className={`font-mono text-[10px] px-1.5 py-0.5 rounded ${
            up ? 'bg-emerald-900/40 text-emerald-400' : 'bg-red-900/40 text-red-400'
          }`}>
            {up ? '▲' : '▼'} {change}
          </span>
        </div>
      )}
    </div>
  )
}

// ── Page Header ───────────────────────────────────────────────────────────────
export function PageHeader({ title, subtitle, children }) {
  return (
    <div className="flex items-center justify-between mb-4">
      <div>
        <div className="section-title">{title}</div>
        {subtitle && <div className="section-sub mt-0.5">{subtitle}</div>}
      </div>
      {children && <div className="flex items-center gap-2">{children}</div>}
    </div>
  )
}

// ── Async Button ──────────────────────────────────────────────────────────────
export function AsyncButton({ onClick, children, className = 'btn btn-outline btn-sm', successMsg, ...rest }) {
  const [loading, setLoading] = React.useState(false)
  const { showToast } = useApp()

  const handle = async () => {
    setLoading(true)
    await new Promise(r => setTimeout(r, 1200))
    setLoading(false)
    if (successMsg) showToast(successMsg)
    if (onClick) onClick()
  }

  return (
    <button className={className} onClick={handle} disabled={loading} {...rest}>
      {loading ? <><Spinner size={12} /> Loading...</> : children}
    </button>
  )
}

// ── Data Table ────────────────────────────────────────────────────────────────
export function DataTable({ columns, rows, emptyMsg = 'No data' }) {
  return (
    <div className="overflow-x-auto">
      <table className="data-table">
        <thead>
          <tr>{columns.map(c => <th key={c.key}>{c.label}</th>)}</tr>
        </thead>
        <tbody>
          {rows.length === 0
            ? <tr><td colSpan={columns.length} className="text-center text-ge-text3 py-8">{emptyMsg}</td></tr>
            : rows.map((row, i) => (
              <tr key={i}>
                {columns.map(c => (
                  <td key={c.key} className={c.className || ''}>
                    {c.render ? c.render(row[c.key], row) : row[c.key]}
                  </td>
                ))}
              </tr>
            ))
          }
        </tbody>
      </table>
    </div>
  )
}

// ── Pagination ─────────────────────────────────────────────────────────────────
export function Pagination({ page, setPage, total, perPage = 10 }) {
  const pages = Math.ceil(total / perPage)
  return (
    <div className="flex items-center gap-1.5 mt-3">
      {Array.from({ length: Math.min(pages, 5) }, (_, i) => i + 1).map(p => (
        <button
          key={p}
          onClick={() => setPage(p)}
          className={`px-2.5 py-1 text-[11px] font-mono rounded border transition-all ${
            p === page
              ? 'bg-ge-blue text-white border-ge-blue'
              : 'bg-ge-elevated border-ge-border text-ge-text2 hover:text-ge-text1'
          }`}
        >
          {p}
        </button>
      ))}
      <span className="ml-auto text-[11px] font-mono text-ge-text3">
        {((page-1)*perPage)+1}–{Math.min(page*perPage, total)} of {total.toLocaleString()}
      </span>
    </div>
  )
}

// ── Search Input ──────────────────────────────────────────────────────────────
export function SearchInput({ value, onChange, placeholder = 'Search...', className = '' }) {
  return (
    <div className={`relative ${className}`}>
      <span className="absolute left-2.5 top-1/2 -translate-y-1/2 text-ge-text3 text-sm">🔍</span>
      <input
        type="text"
        value={value}
        onChange={e => onChange(e.target.value)}
        placeholder={placeholder}
        className="form-control pl-7 text-[12px]"
      />
    </div>
  )
}

// ── Loading Skeleton ──────────────────────────────────────────────────────────
export function Skeleton({ h = 'h-4', w = 'w-full', className = '' }) {
  return (
    <div className={`${h} ${w} bg-ge-surface rounded animate-pulse ${className}`} />
  )
}

// ── Toasts ────────────────────────────────────────────────────────────────────
export function ToastContainer() {
  const { toasts } = useApp()
  return (
    <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-2">
      {toasts.map(t => (
        <div
          key={t.id}
          className="bg-ge-surface border border-ge-border border-l-2 border-l-ge-accent
                     rounded-lg px-4 py-2.5 text-xs text-ge-text1 shadow-lg animate-in
                     flex items-center gap-2 min-w-[220px]"
        >
          <span className="text-ge-accent text-sm">✓</span>
          {t.msg}
        </div>
      ))}
    </div>
  )
}

// ── Modal ─────────────────────────────────────────────────────────────────────
export function Modal() {
  const { modalState, closeModal, showToast } = useApp()
  if (!modalState.open) return null

  return (
    <div
      className="fixed inset-0 bg-black/70 z-50 flex items-center justify-center p-4"
      onClick={e => e.target === e.currentTarget && closeModal()}
    >
      <div className="bg-ge-card border border-ge-border rounded-xl w-[480px] max-w-full max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between px-4 py-3.5 border-b border-ge-border">
          <span className="text-[14px] font-semibold text-ge-text1">{modalState.title}</span>
          <button onClick={closeModal} className="text-ge-text3 hover:text-ge-text1 text-lg leading-none">✕</button>
        </div>
        <div className="p-4">{modalState.content}</div>
        <div className="flex justify-end gap-2 px-4 py-3 border-t border-ge-border">
          <button className="btn btn-outline btn-sm" onClick={closeModal}>Cancel</button>
          <button
            className="btn btn-success btn-sm"
            onClick={() => { closeModal(); showToast('Saved successfully') }}
          >
            Save
          </button>
        </div>
      </div>
    </div>
  )
}

// ── Progress Bar ──────────────────────────────────────────────────────────────
export function ProgressBar({ value, max = 100, color = '#00d4aa', label, sublabel }) {
  const pct = Math.min((value / max) * 100, 100)
  return (
    <div className="mb-3">
      <div className="flex justify-between mb-1">
        <span className="text-[11px] text-ge-text2">{label}</span>
        <span className="text-[11px] font-mono text-ge-text1">{sublabel}</span>
      </div>
      <div className="bg-ge-navy rounded h-1.5">
        <div className="h-full rounded transition-all duration-500" style={{ width: `${pct}%`, background: color }} />
      </div>
    </div>
  )
}

// ── Status Dot ────────────────────────────────────────────────────────────────
export function StatusDot({ status }) {
  const colors = { Online:'bg-emerald-400', Normal:'bg-emerald-400', Active:'bg-emerald-400',
                   Warning:'bg-amber-400', Paused:'bg-amber-400',
                   Fault:'bg-red-400', Critical:'bg-red-400', Offline:'bg-red-400' }
  return <span className={`inline-block w-1.5 h-1.5 rounded-full ${colors[status]||'bg-blue-400'}`} />
}

// ── Form Row ──────────────────────────────────────────────────────────────────
export function FormRow({ children, className = '' }) {
  return (
    <div className={`flex gap-2.5 flex-wrap ${className}`}>{children}</div>
  )
}

export function FormGroup({ label, children, className = '' }) {
  return (
    <div className={`flex flex-col gap-1 flex-1 min-w-[140px] ${className}`}>
      {label && <label className="form-label">{label}</label>}
      {children}
    </div>
  )
}
