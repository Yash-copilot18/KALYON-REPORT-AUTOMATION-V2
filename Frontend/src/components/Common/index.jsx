// src/components/Common/index.jsx
import React, { useEffect, useRef, useState } from 'react'
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
// Map a column's `align` to a Tailwind text-align class. Applied to the HEADER and the
// DATA cells identically, so a column's header always lines up with its values (the
// `<th>` used to be left-aligned while numeric `<td>`s were right-aligned → misaligned).
const alignClass = a => (a === 'center' ? 'text-center' : a === 'right' ? 'text-right'
  : a === 'left' ? 'text-left' : '')

// `fixed` → table-layout:fixed, so columns split the FULL container width by their
// declared `width` (or evenly) instead of hugging their content and leaving the table
// looking left-shifted. Off by default, so existing callers are unchanged.
export function DataTable({ columns, rows, emptyMsg = 'No data', fixed = false }) {
  return (
    <div className="overflow-x-auto">
      <table className={`data-table${fixed ? ' table-fixed' : ''}`}>
        <thead>
          <tr>{columns.map(c => (
            // Header uses the SAME alignment + width as its data column (req 1 & 7).
            <th key={c.key} className={alignClass(c.align)}
              style={c.width ? { width: c.width } : undefined}>{c.label}</th>
          ))}</tr>
        </thead>
        <tbody>
          {rows.length === 0
            ? <tr><td colSpan={columns.length} className="text-center text-ge-text3 py-8">{emptyMsg}</td></tr>
            : rows.map((row, i) => (
              <tr key={i}>
                {columns.map(c => (
                  <td key={c.key} className={`${alignClass(c.align)} ${c.className || ''}`.trim()}
                    style={c.width ? { width: c.width } : undefined}>
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
      {toasts.map(t => {
        // showToast has always carried a `type`; it was previously ignored, so a
        // failure was announced with a green tick. An 'error' toast now reads as one.
        const err = t.type === 'error'
        return (
          <div
            key={t.id}
            className={`bg-ge-surface border border-ge-border border-l-2 rounded-lg px-4 py-2.5
                        text-xs text-ge-text1 shadow-lg animate-in flex items-center gap-2
                        min-w-[220px] max-w-[360px] ${
                          err ? 'border-l-ge-danger' : 'border-l-ge-accent'}`}
          >
            <span className={`text-sm ${err ? 'text-ge-danger' : 'text-ge-accent'}`}>
              {err ? '✕' : '✓'}
            </span>
            {t.msg}
          </div>
        )
      })}
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

// ── Dialog shell ──────────────────────────────────────────────────────────────
/**
 * The shared frame for every in-app dialog — overlay, card, header, body, footer.
 * It exists so the confirm and prompt dialogs below (and anything added later) look
 * identical and behave identically without the markup being copied a third time.
 *
 * Escape, the ✕ and a backdrop click all close it, and all three are inert while
 * `busy`, so a dialog cannot be dismissed out from under an in-flight request.
 */
export function Dialog({ open, title, onClose, busy = false, children, footer, titleId = 'dialog-title' }) {
  useEffect(() => {
    if (!open) return
    const onKey = e => { if (e.key === 'Escape' && !busy) { e.stopPropagation(); onClose?.() } }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, busy, onClose])

  if (!open) return null

  return (
    <div
      className="fixed inset-0 bg-black/70 z-50 flex items-center justify-center p-4"
      onClick={e => { if (e.target === e.currentTarget && !busy) onClose?.() }}
      role="dialog" aria-modal="true" aria-labelledby={titleId}
    >
      <div className="bg-ge-card border border-ge-border rounded-xl w-[440px] max-w-full max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between px-4 py-3.5 border-b border-ge-border">
          <span id={titleId} className="text-[14px] font-semibold text-ge-text1">{title}</span>
          <button type="button" onClick={() => !busy && onClose?.()} disabled={busy} aria-label="Close"
            className="text-ge-text3 hover:text-ge-text1 text-lg leading-none disabled:opacity-40">✕</button>
        </div>
        <div className="p-4 text-[12px] text-ge-text2 leading-relaxed">{children}</div>
        <div className="flex flex-wrap justify-end gap-2 px-4 py-3 border-t border-ge-border">{footer}</div>
      </div>
    </div>
  )
}

// ── Confirm dialog ────────────────────────────────────────────────────────────
/**
 * In-app confirmation for an action that cannot be undone — replaces
 * window.confirm(), which shows the browser's "localhost:5173 says" chrome.
 *
 * Focus moves to Cancel on open, so a stray Enter cancels and can never trigger the
 * destructive action. While `busy` every control is disabled, so it cannot fire twice.
 */
export function ConfirmDialog({
  open, title, children,
  confirmLabel = 'Confirm', cancelLabel = 'Cancel', busyLabel = 'Working…',
  // Destructive by default; pass 'btn-primary' for a confirmation that is not a
  // deletion, so the colour still tells the user what kind of action it is.
  confirmClass = 'btn-danger',
  onConfirm, onCancel, busy = false,
}) {
  const cancelRef = useRef(null)
  useEffect(() => { if (open) cancelRef.current?.focus() }, [open])

  return (
    <Dialog open={open} title={title} onClose={onCancel} busy={busy} titleId="confirm-dialog-title"
      footer={<>
        <button ref={cancelRef} type="button" className="btn btn-outline btn-sm"
          onClick={() => onCancel?.()} disabled={busy}>
          {cancelLabel}
        </button>
        <button type="button" className={`btn ${confirmClass} btn-sm`}
          onClick={() => onConfirm?.()} disabled={busy}>
          {busy ? <><Spinner size={12} /> {busyLabel}</> : confirmLabel}
        </button>
      </>}
    >
      {children}
    </Dialog>
  )
}

// ── Prompt dialog ─────────────────────────────────────────────────────────────
/**
 * In-app single-field prompt — replaces window.prompt().
 *
 * The CALLER owns validation: `validate(trimmedValue)` returns an error string to
 * block submission, or null/'' to allow it. That keeps store-specific rules (unique
 * names, length limits) where they belong while the dialog stays generic.
 *
 * Behaviour:
 *  · The input is focused and its text selected on open, so typing replaces it.
 *  · Enter submits, but only when the value passes `validate` — it is a real <form>,
 *    so the browser's own submit handling applies.
 *  · The value is TRIMMED before validation and before it reaches onSubmit.
 *  · Escape / ✕ / backdrop / Cancel all close without submitting.
 *  · While `busy` every control is disabled, so the request cannot be sent twice.
 *
 * @param {string}   initialValue  pre-filled (and re-filled each time it opens)
 * @param {function} validate      (trimmed) => error string | null
 * @param {function} onSubmit      (trimmed) => void
 */
export function PromptDialog({
  open, title, label, description,
  initialValue = '', placeholder = '',
  submitLabel = 'Save', cancelLabel = 'Cancel', busyLabel = 'Saving…',
  validate, onSubmit, onCancel, busy = false, error = '',
}) {
  const [value, setValue] = useState(initialValue)
  const [touched, setTouched] = useState(false)
  const inputRef = useRef(null)

  // Re-seed every time the dialog opens so it always shows the CURRENT name, never
  // whatever was typed the last time it was open.
  useEffect(() => {
    if (!open) return
    setValue(initialValue)
    setTouched(false)
    // The input is already committed to the DOM by the time this effect runs, so it
    // can be focused directly. A 0ms retry covers the case where something else
    // claims focus in the same tick (a still-mounted trigger button, for instance);
    // a rAF would not, because it does not fire while the tab is hidden.
    const focus = () => { inputRef.current?.focus(); inputRef.current?.select() }
    focus()
    const id = setTimeout(focus, 0)
    return () => clearTimeout(id)
  }, [open, initialValue])

  const trimmed  = value.trim()
  const invalid  = validate ? validate(trimmed) : null
  const canSubmit = !busy && !invalid

  const submit = e => {
    e?.preventDefault()
    setTouched(true)
    if (!canSubmit) return
    onSubmit?.(trimmed)
  }

  // Show a validation message only once the user has engaged, so the dialog does not
  // open already complaining that the unchanged name is unchanged.
  const shown = error || (touched && invalid) || ''

  return (
    <Dialog open={open} title={title} onClose={onCancel} busy={busy} titleId="prompt-dialog-title"
      footer={<>
        <button type="button" className="btn btn-outline btn-sm"
          onClick={() => onCancel?.()} disabled={busy}>
          {cancelLabel}
        </button>
        <button type="submit" form="prompt-dialog-form" className="btn btn-primary btn-sm"
          disabled={!canSubmit}>
          {busy ? <><Spinner size={12} /> {busyLabel}</> : submitLabel}
        </button>
      </>}
    >
      <form id="prompt-dialog-form" onSubmit={submit}>
        {description}
        <label className="form-label mt-3 block" htmlFor="prompt-dialog-input">{label}</label>
        <input
          id="prompt-dialog-input" ref={inputRef} type="text" className="form-control w-full"
          value={value} placeholder={placeholder} disabled={busy}
          onChange={e => { setValue(e.target.value); setTouched(true) }}
          aria-invalid={!!shown} aria-describedby={shown ? 'prompt-dialog-error' : undefined}
        />
        {shown && (
          <div id="prompt-dialog-error" role="alert" className="mt-1.5 text-[11px] text-ge-danger">
            {shown}
          </div>
        )}
      </form>
    </Dialog>
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
