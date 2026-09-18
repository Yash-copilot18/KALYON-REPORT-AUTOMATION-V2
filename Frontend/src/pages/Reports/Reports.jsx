// src/pages/Reports/Reports.jsx
import React, { useState, useEffect, useMemo, useRef, useCallback } from 'react'
import { useNavigate, useLocation } from 'react-router-dom'
import { PageHeader, FormRow, FormGroup, Skeleton, Spinner } from '../../components/Common'
import { useApp } from '../../utils/AppContext'
import {
  INTERVAL_LABELS, AGG_OPTIONS, DEFAULT_AGG,
  intervalsForRange, resolveInterval,
  showsAggregation, withAggregation,
} from '../../utils/intervals'
import { getPreset, hasPreset, presetEquipmentIds } from '../../utils/reportPresets'
import {
  fetchReportEquipmentTypes,
  fetchReportEquipmentList,
  fetchTagAvailability,
  fetchReportDataV2,
  fetchBatchDataResult,
  createMergedFirstJob,
  fetchMergedPage,
  exportReportCSVV2,
  exportReportExcelV2,
  createExcelExportJob,
  downloadExcelExportJob,
  exportProgressUrl,
  prepareExcelStreamJob,
  exportStreamUrl,
  startExcelToDownloads,
  createSavedReport,
} from '../../services/api'

// ── Helpers ────────────────────────────────────────────────────────────────────
function safeArr(v) {
  if (Array.isArray(v)) return v
  if (v && Array.isArray(v.items)) return v.items
  if (v && Array.isArray(v.data))  return v.data
  return []
}

function fmtTimestamp(val) {
  if (!val) return '—'
  const s = String(val).replace('T', ' ').slice(0, 19)
  const [date, time] = s.split(' ')
  if (!date) return s
  const [y, mo, d] = date.split('-')
  return `${d}/${mo}/${y} ${time || '00:00:00'}`
}

function fmtNum(val) {
  if (val === null || val === undefined) return '—'
  if (typeof val === 'number') return val.toFixed(3)
  return String(val)
}

function toISO(dtLocal) {
  return dtLocal ? new Date(dtLocal).toISOString() : ''
}

// ── Virtualized data table ──────────────────────────────────────────────────
// Dependency-free row virtualization: only the rows in (and near) the viewport
// are ever in the DOM, so the preview can show EVERY selected equipment (hundreds
// of rows across 12–24 devices) with a tiny, constant DOM and smooth scrolling —
// nothing is truncated, nothing is hidden.
const V_ROW_H      = 30    // px — fixed row height (kept exact so the math is stable)
const V_VIEWPORT_H = 460   // px — scroll viewport height
const V_OVERSCAN   = 10    // extra rows rendered above/below the viewport
// Column virtualization only engages for genuinely WIDE tables (e.g. 700+ tags);
// narrow/normal reports render every column exactly as before (zero behaviour change).
const V_COL_MIN    = 60    // engage column virtualization only when cols exceed this
const V_COL_OVERSCAN = 3   // extra columns rendered left/right of the viewport
const V_DEFAULT_VIEW_W = 1200  // fallback viewport width before the container is measured

// Presentation-only: map an internal isolation table id (T1_IS3 / T2_IS13) to its
// tracker display name in the client's required format — IS03 / IS13 (always two
// digits, leading zero; three+ digits keep their length). Pure and id-shaped, so any
// non-isolation equipment id — and an already-formatted "IS03" from the backend —
// passes through unchanged. The underlying value/id is never mutated — only what the
// operator sees.
function trackerDisplayName(id) {
  const m = /^T\d+_IS0*(\d+)$/i.exec(String(id ?? ''))
  return m ? `IS${String(m[1]).padStart(2, '0')}` : id
}

function renderCell(col, val) {
  if (col.key === '_equipment')
    return <span className="font-mono text-[11px] text-ge-blue">{trackerDisplayName(val)}</span>
  if (col.isTs)
    return <span className="font-mono text-[11px] text-ge-text3">{fmtTimestamp(val)}</span>
  if (val === null || val === undefined)
    return <span className="text-ge-text3 text-[11px]">—</span>
  if (typeof val === 'number')
    return <span className="font-mono text-ge-accent text-[11px]">{fmtNum(val)}</span>
  return <span className="font-mono text-ge-text2 text-[11px]">{String(val)}</span>
}

// Per-column width (px), sized so the HEADER is fully readable in AT MOST two wrapped
// lines (never truncated with "…"), and data like "31/12/2024 23:59:59" never clips.
// ~6.3px per char at the 10px header font: half the label fits on each of two lines,
// and the longest single word is guaranteed to fit one line. Clamped so the table
// stays horizontally scrollable rather than gigantic. Header + data share this width
// (via <colgroup>), so columns stay perfectly aligned.
const colWidth = (c) => {
  if (c.isTs) return 200
  if (c.key === '_equipment') return 150
  const label = String(c.label || '')
  const CH = 6.3, PAD = 22
  const longestWord = label.split(/\s+/).reduce((m, w) => Math.max(m, w.length), 0)
  const twoLine = Math.ceil(label.length / 2)          // chars/line for a 2-line wrap
  const chars = Math.max(longestWord, twoLine)
  return Math.min(210, Math.max(120, Math.round(chars * CH + PAD)))
}

const _cellStyle = {
  height: V_ROW_H, paddingTop: 0, paddingBottom: 0, boxSizing: 'border-box',
  whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
}

// One data row. Memoized so vertical scrolling only mounts the newly-revealed rows
// instead of re-rendering every visible row on each frame. `visCols` is a stable
// slice (see useMemo below), so a row that stays on screen skips re-render entirely.
const VirtualRow = React.memo(function VirtualRow({ row, visCols, leftPad, rightPad }) {
  return (
    <tr style={{ height: V_ROW_H }}>
      {leftPad > 0 && <td aria-hidden style={{ padding: 0, border: 0, width: leftPad }} />}
      {visCols.map(col => (
        <td key={col.key} style={_cellStyle}>{renderCell(col, row[col.key])}</td>
      ))}
      {rightPad > 0 && <td aria-hidden style={{ padding: 0, border: 0, width: rightPad }} />}
    </tr>
  )
})

function VirtualDataTable({ cols = [], rows = [] }) {
  const [scrollTop, setScrollTop] = useState(0)
  const [scrollLeft, setScrollLeft] = useState(0)
  const [viewW, setViewW] = useState(V_DEFAULT_VIEW_W)
  const rafRef = useRef(0)
  const scrollRef = useRef(null)

  // Reset to the top-left whenever the page's data changes (new page / new dataset),
  // and (re)measure the viewport width so column virtualization has the real width.
  useEffect(() => {
    const el = scrollRef.current
    if (el) { el.scrollTop = 0; el.scrollLeft = 0; setViewW(el.clientWidth || V_DEFAULT_VIEW_W) }
    setScrollTop(0); setScrollLeft(0)
  }, [rows, cols])

  // Keep the measured width current on container resize; clean up the observer.
  useEffect(() => {
    const el = scrollRef.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(() => setViewW(el.clientWidth || V_DEFAULT_VIEW_W))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  // Cancel any pending scroll frame on unmount.
  useEffect(() => () => { if (rafRef.current) cancelAnimationFrame(rafRef.current) }, [])

  // One state update per animation frame for BOTH axes (coalesced) — no layout
  // thrash while scrolling.
  const onScroll = (e) => {
    const top = e.currentTarget.scrollTop
    const left = e.currentTarget.scrollLeft
    if (rafRef.current) return
    rafRef.current = requestAnimationFrame(() => {
      rafRef.current = 0
      setScrollTop(top); setScrollLeft(left)
    })
  }

  // ── Row window (vertical virtualization) ─────────────────────────────────
  const total = rows.length
  const start = Math.max(0, Math.floor(scrollTop / V_ROW_H) - V_OVERSCAN)
  const visibleCount = Math.ceil(V_VIEWPORT_H / V_ROW_H) + V_OVERSCAN * 2
  const end = Math.min(total, start + visibleCount)
  const padTop = start * V_ROW_H
  const padBottom = Math.max(0, (total - end) * V_ROW_H)
  const visible = rows.slice(start, end)

  // ── Column window (horizontal virtualization) ────────────────────────────
  // Prefix-sum of column widths (recomputed only when the column set changes) lets us
  // map scrollLeft → the exact visible column range, with left/right spacer cells whose
  // widths equal the skipped columns — so alignment is pixel-exact and column widths,
  // sticky header, sorting/filtering and data are all unchanged.
  const { totalW, offsets } = useMemo(() => {
    const offs = new Array(cols.length + 1); offs[0] = 0
    for (let i = 0; i < cols.length; i++) offs[i + 1] = offs[i] + colWidth(cols[i])
    return { totalW: offs[cols.length], offsets: offs }
  }, [cols])

  const virtualizeCols = cols.length > V_COL_MIN
  let colStart = 0, colEnd = cols.length, leftPad = 0, rightPad = 0
  if (virtualizeCols) {
    // Binary-ish linear scan is fine (cols ≤ ~800); find first/last visible column.
    while (colStart < cols.length && offsets[colStart + 1] <= scrollLeft) colStart++
    colStart = Math.max(0, colStart - V_COL_OVERSCAN)
    const right = scrollLeft + viewW
    colEnd = colStart
    while (colEnd < cols.length && offsets[colEnd] < right) colEnd++
    colEnd = Math.min(cols.length, colEnd + V_COL_OVERSCAN)
    leftPad = offsets[colStart]
    rightPad = Math.max(0, totalW - offsets[colEnd])
  }
  const visCols = useMemo(
    () => (virtualizeCols ? cols.slice(colStart, colEnd) : cols),
    [cols, virtualizeCols, colStart, colEnd])

  return (
    <div ref={scrollRef} onScroll={onScroll}
         className="overflow-auto border border-ge-border rounded"
         style={{ maxHeight: V_VIEWPORT_H }}>
      <table className="data-table" style={{ tableLayout: 'fixed', width: totalW }}>
        <colgroup>
          {leftPad > 0 && <col style={{ width: leftPad }} />}
          {visCols.map(c => <col key={c.key} style={{ width: colWidth(c) }} />)}
          {rightPad > 0 && <col style={{ width: rightPad }} />}
        </colgroup>
        <thead>
          <tr>
            {leftPad > 0 && <th aria-hidden className="sticky top-0 z-10" style={{ padding: 0, border: 0 }} />}
            {visCols.map(c => (
              // Header fully visible: wraps to at most 2 lines (never "…"), centred
              // horizontally + vertically, and the row height auto-adjusts to the
              // wrapped text. Inline styles override the .data-table th nowrap/left.
              <th key={c.key} className="text-[10px] sticky top-0 z-10"
                  style={{ textAlign: 'center', verticalAlign: 'middle',
                           whiteSpace: 'normal', padding: '5px 6px' }}>
                <div style={{ display: '-webkit-box', WebkitLineClamp: 2,
                              WebkitBoxOrient: 'vertical', overflow: 'hidden',
                              whiteSpace: 'normal', wordBreak: 'break-word',
                              lineHeight: 1.2, textAlign: 'center' }}>
                  {c.label}
                </div>
              </th>
            ))}
            {rightPad > 0 && <th aria-hidden className="sticky top-0 z-10" style={{ padding: 0, border: 0 }} />}
          </tr>
        </thead>
        <tbody>
          {padTop > 0 && (
            <tr aria-hidden style={{ height: padTop }}>
              <td colSpan={visCols.length + (leftPad > 0 ? 1 : 0) + (rightPad > 0 ? 1 : 0)}
                  style={{ padding: 0, border: 0, height: padTop }} />
            </tr>
          )}
          {visible.map((row, i) => (
            <VirtualRow key={start + i} row={row} visCols={visCols}
                        leftPad={leftPad} rightPad={rightPad} />
          ))}
          {padBottom > 0 && (
            <tr aria-hidden style={{ height: padBottom }}>
              <td colSpan={visCols.length + (leftPad > 0 ? 1 : 0) + (rightPad > 0 ? 1 : 0)}
                  style={{ padding: 0, border: 0, height: padBottom }} />
            </tr>
          )}
        </tbody>
      </table>
    </div>
  )
}

function todayDMY() {
  const d = new Date()
  return `${String(d.getDate()).padStart(2,'0')}-${String(d.getMonth()+1).padStart(2,'0')}-${d.getFullYear()}`
}

// Report types hidden from the Equipment Type dropdown (backend mapping is left
// intact — these are only removed from the picker, order of the rest unchanged).
// 'Tracker' is now the visible, operator-facing merge of the isolation devices, so it
// is NOT hidden. The raw isolation types and the retired MBOX report are hidden here as
// a belt-and-suspenders guard (the backend already omits them from /equipment-types).
const HIDDEN_EQ_TYPES = new Set([
  // Kept out of the picker per client request (Daily Generation / Alarms). Their backend
  // registry mappings are left intact — this only removes them from the dropdown.
  'Alarms', 'Daily Generation',
  // Backend-internal isolation/MBOX types, surfaced only through the merged 'Tracker'
  // type — belt-and-suspenders guard (the backend already omits them from /equipment-types).
  'T1 Isolation', 'T2 Isolation', 'Tracker MBOX Status',
])

// Display label for the Equipment Type dropdown. The stored VALUE stays 'Tracker'
// (backend routing and presets key off it); only the visible text
// reads 'Trackers' per the client's reference. Every other type shows its own name.
const eqTypeLabel = (t) => (t === 'Tracker' ? 'Trackers' : t)

// Multi-equipment table uses classic server-side pagination: the first page loads
// with progress; Prev/Next fetch one page at a time (rows ordered by timestamp then
// equipment, so every device appears on every page).
// How long the From/To edits must settle before the range is shared with Analytics.
// Long enough to coalesce a From-then-To change into one publish, short enough that
// navigating straight to Analytics always finds the latest range.
const RANGE_PUBLISH_MS = 400

const DEFAULT_PAGE_SIZE = 100
const PAGE_SIZE_OPTIONS = [50, 100, 250, 500]


// ── Equipment Multi-Select ─────────────────────────────────────────────────────
function EquipmentMultiSelect({ eqList, selectedIds, setSelectedIds, disabled, loading }) {
  const [rangeStr,  setRangeStr]  = useState('')
  const [rangeErr,  setRangeErr]  = useState('')
  const [eqSearch,  setEqSearch]  = useState('')

  const isAllSelected  = eqList.length > 0 && selectedIds.size === eqList.length
  const isIndeterminate = selectedIds.size > 0 && !isAllSelected

  // O(1) lookup: equipment_id → 1-based index in eqList (for range input numbering)
  const eqIndex = useMemo(() => {
    const m = new Map()
    eqList.forEach((eq, i) => m.set(eq.equipment_id, i + 1))
    return m
  }, [eqList])

  const filtered = useMemo(() => {
    if (!eqSearch.trim()) return eqList
    const q = eqSearch.toLowerCase()
    return eqList.filter(eq =>
      eq.display_name.toLowerCase().includes(q) ||
      eq.equipment_id.toLowerCase().includes(q) ||
      // Also match what the operator actually sees (e.g. "T03"), not just the raw id.
      trackerDisplayName(eq.display_name).toLowerCase().includes(q)
    )
  }, [eqList, eqSearch])

  const toggle = (id) => {
    if (disabled) return
    setSelectedIds(prev => {
      const s = new Set(prev)
      s.has(id) ? s.delete(id) : s.add(id)
      return s
    })
  }

  const toggleAll = () => {
    if (disabled) return
    setSelectedIds(isAllSelected ? new Set() : new Set(eqList.map(eq => eq.equipment_id)))
  }

  const applyRange = () => {
    if (!rangeStr.trim()) { setRangeErr('Enter a range first'); return }
    const result = new Set()
    try {
      const parts = rangeStr.split(',').map(s => s.trim()).filter(Boolean)
      for (const part of parts) {
        if (part.includes('-')) {
          const [a, b] = part.split('-').map(Number)
          if (isNaN(a) || isNaN(b)) throw new Error()
          const lo = Math.min(a, b), hi = Math.max(a, b)
          for (let i = lo; i <= hi; i++) {
            if (i >= 1 && i <= eqList.length) result.add(eqList[i - 1].equipment_id)
          }
        } else {
          const n = parseInt(part, 10)
          if (isNaN(n)) throw new Error()
          if (n >= 1 && n <= eqList.length) result.add(eqList[n - 1].equipment_id)
        }
      }
      if (result.size === 0) { setRangeErr(`No valid equipment in range (1–${eqList.length})`); return }
      setSelectedIds(result)
      setRangeErr('')
      setRangeStr('')
    } catch {
      setRangeErr('Invalid format — examples: 1-5 · 2,4 · 1-3,8-10')
    }
  }

  if (loading) {
    return (
      <div className="form-control flex items-center gap-2 text-ge-text3 text-[12px]">
        <Spinner size={12} /> Loading...
      </div>
    )
  }

  if (!eqList.length) {
    return (
      <div className="form-control text-ge-text3 text-[12px] italic">
        — Select Equipment Type first —
      </div>
    )
  }

  return (
    <div>
      {/* Search */}
      <div className="relative mb-1.5">
        <span className="absolute left-2.5 top-1/2 -translate-y-1/2 text-ge-text3 text-[11px]">🔍</span>
        <input
          type="text"
          value={eqSearch}
          onChange={e => setEqSearch(e.target.value)}
          placeholder="Search equipment..."
          className="form-control pl-7 text-[12px]"
          disabled={disabled}
        />
      </div>

      {/* Scrollable checkbox list */}
      <div className="bg-ge-elevated border border-ge-border rounded-md overflow-y-auto max-h-40">
        {/* Select All — sticky at top of scroll container */}
        <div
          onClick={toggleAll}
          className={`flex items-center gap-2 px-2.5 py-1.5 cursor-pointer
                      border-b-2 border-ge-border transition-colors select-none
                      sticky top-0 z-10 bg-ge-elevated
                      hover:bg-ge-surface
                      ${disabled ? 'opacity-50 cursor-not-allowed' : ''}`}
        >
          <span className="text-[9px] font-mono text-ge-text3 w-5 text-right flex-shrink-0" />
          <div className={`w-3.5 h-3.5 rounded flex items-center justify-center
                          text-[9px] flex-shrink-0 border
                          ${isAllSelected
                            ? 'bg-ge-blue border-ge-blue text-white'
                            : isIndeterminate
                              ? 'bg-ge-blue/50 border-ge-blue text-white'
                              : 'border-ge-border2'}`}>
            {isAllSelected ? '✓' : isIndeterminate ? '−' : ''}
          </div>
          <span className="text-[12px] font-semibold text-ge-text1 flex-1 leading-tight">
            Select All ({eqList.length})
          </span>
          {selectedIds.size > 0 && (
            <span className="text-[10px] font-mono text-ge-blue flex-shrink-0">
              {selectedIds.size} selected
            </span>
          )}
        </div>

        {/* Equipment rows */}
        {filtered.map(eq => (
          <div
            key={eq.equipment_id}
            onClick={() => toggle(eq.equipment_id)}
            className={`flex items-center gap-2 px-2.5 py-1.5 cursor-pointer
                        border-b border-ge-border last:border-b-0 transition-colors
                        hover:bg-ge-surface select-none
                        ${selectedIds.has(eq.equipment_id) ? 'bg-ge-blue/10' : ''}
                        ${disabled ? 'opacity-50 cursor-not-allowed' : ''}`}
          >
            <span className="text-[9px] font-mono text-ge-text3 w-5 text-right flex-shrink-0">
              {eqIndex.get(eq.equipment_id)}
            </span>
            <div className={`w-3.5 h-3.5 rounded flex items-center justify-center
                            text-[9px] flex-shrink-0 border
                            ${selectedIds.has(eq.equipment_id)
                              ? 'bg-ge-blue border-ge-blue text-white'
                              : 'border-ge-border2'}`}>
              {selectedIds.has(eq.equipment_id) && '✓'}
            </div>
            <span className="text-[12px] text-ge-text1 flex-1 leading-tight truncate">
              {trackerDisplayName(eq.display_name)}
            </span>
          </div>
        ))}

        {filtered.length === 0 && (
          <div className="px-3 py-4 text-[12px] text-ge-text3 text-center italic">
            No equipment matching "{eqSearch}"
          </div>
        )}
      </div>

      {/* Range input */}
      <div className="mt-2 p-2.5 bg-ge-elevated border border-ge-border rounded-md">
        <div className="text-[10px] font-semibold text-ge-text3 uppercase tracking-widest mb-1.5">
          Range Selection
        </div>
        <div className="flex items-center gap-2">
          <input
            type="text"
            value={rangeStr}
            onChange={e => { setRangeStr(e.target.value); setRangeErr('') }}
            onKeyDown={e => e.key === 'Enter' && applyRange()}
            placeholder="e.g. 1-5 · 2,4 · 1-3,8-10"
            className="form-control text-[12px] flex-1"
            disabled={disabled}
          />
          <button
            className="btn btn-primary btn-sm"
            onClick={applyRange}
            disabled={disabled}
          >
            Apply
          </button>
        </div>
        {rangeErr
          ? <p className="text-[10px] text-ge-danger mt-1">{rangeErr}</p>
          : <p className="text-[10px] text-ge-text3 mt-1 font-mono">
              Numbers match the # shown in the list above
            </p>
        }
      </div>
    </div>
  )
}

// ── Tag Selector ─────────────────────────────────────────────────────────────
// Interactive tag picker: Select All / Clear All above the list, a working search
// filter, A→Z sort, and per-tag toggling. Every available tag is auto-selected
// upstream when equipment changes, so the panel opens fully selected; the user can
// then refine it. Only fully-available tags are selectable (partial ones are shown
// greyed + locked). `selected` / `setSelected` are owned by the parent.
function DynamicTagSelector({ tags, selected, setSelected }) {
  const [search,  setSearch]  = useState('')
  const [sortAsc, setSortAsc] = useState(false)

  const tagIndex = useMemo(() => {
    const map = {}
    tags.forEach((t, i) => { map[t.column_name] = i + 1 })
    return map
  }, [tags])

  const isAvail = t => t.available !== false
  const availableCount = useMemo(() => tags.filter(isAvail).length, [tags])
  const partialCount   = tags.length - availableCount

  // Search filters ONLY what's DISPLAYED — it never mutates the full `tags` list, so
  // clearing the search restores every tag (req 8). A→Z is an optional display sort.
  const displayed = useMemo(() => {
    const q = search.trim().toLowerCase()
    let list = q
      ? tags.filter(t =>
          t.tag.toLowerCase().includes(q) || t.column_name.toLowerCase().includes(q))
      : tags
    if (sortAsc) list = [...list].sort((a, b) => a.tag.localeCompare(b.tag))
    return list
  }, [tags, search, sortAsc])

  // Toggle one tag (available tags only). Select All / Clear All mirror this exact
  // state, so a bulk select yields a Selected panel identical to picking each by hand.
  const toggle = col => {
    const tag = tags.find(t => t.column_name === col)
    if (tag && !isAvail(tag)) return
    setSelected(prev => {
      const next = new Set(prev)
      next.has(col) ? next.delete(col) : next.add(col)
      return next
    })
  }

  // Select All → EVERY available tag in the FULL list (not just the filtered/visible
  // ones — req 3 & 9). Clear All → none (req 4).
  const selectAll = () => setSelected(new Set(tags.filter(isAvail).map(t => t.column_name)))
  const clearAll  = () => setSelected(new Set())

  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <span className="text-[10px] font-semibold text-ge-text3 uppercase tracking-widest">
          Tag Selection
        </span>
        <span className="text-[10px] font-mono text-ge-accent">{selected.size} selected</span>
      </div>

      {/* Select All / Clear All — above the Available list (req 1 & 2). */}
      <div className="flex items-center gap-2 mb-2 flex-wrap">
        <button type="button" className="btn btn-outline btn-sm" onClick={selectAll}
          disabled={availableCount === 0} title="Select every available tag">
          ✓ Select All
        </button>
        <button type="button" className="btn btn-outline btn-sm" onClick={clearAll}
          disabled={selected.size === 0} title="Deselect all tags">
          ✕ Clear All
        </button>
        <span className="ml-auto text-[11px] text-ge-text3 font-mono">
          {selected.size} of {availableCount} selected
        </span>
      </div>

      {/* Search + Sort — filters the displayed list only (full tag list is preserved). */}
      <div className="flex items-center gap-2 mb-2.5">
        <div className="relative flex-1">
          <span className="absolute left-2.5 top-1/2 -translate-y-1/2 text-ge-text3 text-sm">🔍</span>
          <input type="text" value={search} onChange={e => setSearch(e.target.value)}
            placeholder="Search tags..." className="form-control pl-7 text-[12px]" />
        </div>
        <button type="button" className={`btn btn-sm ${sortAsc ? 'btn-primary' : 'btn-outline'}`}
          onClick={() => setSortAsc(v => !v)} title="Sort A→Z">
          A→Z
        </button>
      </div>

      {partialCount > 0 && (
        <div className="mb-2 text-[11px] text-amber-300 bg-amber-500/5 border border-amber-500/30
                        rounded px-2.5 py-1.5">
          ⚠ {partialCount} tag{partialCount > 1 ? 's are' : ' is'} not available in every selected
          equipment (shown greyed with an <span className="font-mono">N/M</span> badge and locked).
        </div>
      )}

      {/* Tag lists — display only; no click/keyboard interaction. */}
      <div className="grid grid-cols-2 gap-2.5">
        {/* Available */}
        <div>
          <div className="flex items-center justify-between px-2.5 py-1.5
                          bg-ge-elevated border border-ge-border rounded-t-md border-b-0">
            <span className="text-[10px] font-semibold text-ge-text3 uppercase">Available</span>
            <span className="text-[10px] font-mono text-ge-text3">{displayed.length}</span>
          </div>
          <div className="bg-ge-elevated border border-ge-border rounded-b-md overflow-y-auto max-h-52">
            {displayed.length === 0
              ? <div className="px-3 py-4 text-[12px] text-ge-text3 text-center">No tags found</div>
              : displayed.map(tag => {
                const avail = isAvail(tag)
                const isSel = selected.has(tag.column_name)
                return (
                <div key={tag.column_name}
                  onClick={() => avail && toggle(tag.column_name)}
                  title={avail ? '' : `Available in ${tag.available_in} of ${tag.total} selected equipment`}
                  className={`flex items-center gap-2 px-2 py-1.5 select-none
                             border-b border-ge-border last:border-b-0
                             ${avail ? 'cursor-pointer hover:bg-ge-surface' : 'opacity-50 cursor-not-allowed'}
                             ${isSel ? 'bg-ge-blue/10' : ''}`}>
                  <span className="text-[9px] font-mono text-ge-text3 w-5 text-right flex-shrink-0">
                    {tagIndex[tag.column_name]}
                  </span>
                  <div className={`w-3.5 h-3.5 rounded flex items-center justify-center
                                  text-[9px] flex-shrink-0 border
                                  ${isSel
                                    ? 'bg-ge-blue border-ge-blue text-white'
                                    : 'border-ge-border2'}`}>
                    {isSel && '✓'}
                  </div>
                  <span className="text-[12px] text-ge-text1 flex-1 leading-tight">{tag.tag}</span>
                  {!avail && (
                    <span className="text-[9px] font-mono text-amber-400 bg-amber-500/10 px-1 rounded flex-shrink-0">
                      {tag.available_in}/{tag.total}
                    </span>
                  )}
                  {tag.unit && (
                    <span className="text-[10px] font-mono text-ge-text3">{tag.unit}</span>
                  )}
                </div>
              )})
            }
          </div>
        </div>

        {/* Selected */}
        <div>
          <div className="flex items-center justify-between px-2.5 py-1.5
                          bg-ge-elevated border border-ge-border rounded-t-md border-b-0">
            <span className="text-[10px] font-semibold text-ge-text3 uppercase">Selected</span>
            <span className="text-[10px] font-mono text-ge-accent">{selected.size} tags</span>
          </div>
          <div className="bg-ge-elevated border border-ge-border rounded-b-md overflow-y-auto max-h-52">
            {selected.size === 0
              ? <div className="px-3 py-4 text-[12px] text-ge-text3 text-center">No tags selected</div>
              : [...selected].map(col => {
                  const tag = tags.find(t => t.column_name === col) || { tag: col, unit: '' }
                  return (
                    <div key={col}
                      onClick={() => toggle(col)}
                      title="Remove from selection"
                      className="flex items-center gap-2 px-2.5 py-1.5 cursor-pointer select-none
                                 border-b border-ge-border last:border-b-0 bg-ge-blue/10 hover:bg-ge-blue/20">
                      <div className="w-3.5 h-3.5 rounded flex items-center justify-center
                                      text-[9px] flex-shrink-0 bg-ge-blue border border-ge-blue text-white">
                        ✓
                      </div>
                      <span className="text-[12px] text-ge-text1 flex-1 leading-tight">{tag.tag}</span>
                      {tag.unit && (
                        <span className="text-[10px] font-mono text-ge-text3">{tag.unit}</span>
                      )}
                      <span className="text-[11px] text-ge-text3 hover:text-ge-danger flex-shrink-0">✕</span>
                    </div>
                  )
                })
            }
          </div>
        </div>
      </div>
    </div>
  )
}

// ── Main Page ──────────────────────────────────────────────────────────────────
export default function Reports() {
  const navigate           = useNavigate()
  const location           = useLocation()
  const { showToast, registerRefresh, setReportDateRange } = useApp()

  const [eqTypes,       setEqTypes]       = useState([])
  const [eqList,        setEqList]        = useState([])
  const [tagList,       setTagList]       = useState([])
  const [eqType,        setEqType]        = useState('')
  const [selectedEqIds, setSelectedEqIds] = useState(new Set())
  const [selected,      setSelected]      = useState(new Set())
  const [fromDate,      setFromDate]      = useState('2024-02-08T00:00')
  const [toDate,        setToDate]        = useState('2024-05-21T23:59')
  const [interval,      setInterval]      = useState('hourly')
  const [agg,           setAgg]           = useState(DEFAULT_AGG)
  const [page,          setPage]          = useState(1)
  const [search,        setSearch]        = useState('')

  const [loadingTypes,  setLoadingTypes]  = useState(true)
  const [loadingEqList, setLoadingEqList] = useState(false)
  const [loadingTags,   setLoadingTags]   = useState(false)
  const [loadingData,   setLoadingData]   = useState(false)
  const [loadingExport, setLoadingExport] = useState(false)
  const [exportLabel,   setExportLabel]   = useState('')
  const [exportJob,     setExportJob]     = useState(null)  // { pct, message, status }
  const [savedLocation, setSavedLocation] = useState(null)  // absolute path of last saved export
  const [loadJob,       setLoadJob]       = useState(null)  // { pct, message, status }
  const esRef     = useRef(null)
  const loadEsRef = useRef(null)

  const [result,  setResult]  = useState(null)
  const [error,   setError]   = useState(null)
  const [warning, setWarning] = useState(null)   // { count, groups:[{id,tags}] }
  const [warnExpanded, setWarnExpanded] = useState(false)
  const [pageLoading, setPageLoading] = useState(false)   // Prev/Next page fetch
  const [pageSize,    setPageSize]    = useState(DEFAULT_PAGE_SIZE)

  // ── Save / restore a Preconfigured report ──────────────────────────────────
  // `savingReport` drives the Save button spinner. Restoring a saved report opens
  // the Reports page with a config to re-apply: `restoreRef` carries it through the
  // async equipment→tags load cascade so the auto-select effects don't clobber the
  // saved selection; `autoLoadRef` remembers a Run/Load request; `restoreToken`
  // fires once the cascade finishes so an auto-load can run with the state settled.
  const [savingReport, setSavingReport] = useState(false)
  const [restoreToken, setRestoreToken] = useState(0)
  const restoreRef   = useRef(null)
  const autoLoadRef  = useRef(false)
  const restoredRef  = useRef(false)   // guards the one-time mount restore
  // Multi-equipment classic pagination: { page, totalPages, total, pageSize }.
  const [mergedNav,   setMergedNav]   = useState(null)
  // Holds the request payload so page navigation can refetch (server-side paging).
  const mergedRef = useRef({ active: false, payload: null })

  // Report-type metadata (table_name + equipment_count) keyed by type name.
  const eqTypeMeta = useMemo(() => {
    const m = new Map()
    eqTypes.forEach(t => m.set(t.equipment_type, t))
    return m
  }, [eqTypes])

  // A "single-source" report type maps to exactly one fixed table (Temperature
  // Report, Alarms, Tracker, WMS, PPC …). Equipment selection is meaningless for
  // these — we auto-select the one table and hide the Equipment Identifier UI.
  // Multi-equipment types (Inverter, String Combiner, T1/T2 Isolation) are
  // unaffected and keep the full picker.
  const isSingleSource = useMemo(() => {
    if (!eqType) return false
    const meta = eqTypeMeta.get(eqType)
    return !!meta && (meta.equipment_count ?? 1) <= 1
  }, [eqType, eqTypeMeta])

  // First selected equipment — drives single-export
  const primaryEqId = useMemo(() => [...selectedEqIds][0] || '', [selectedEqIds])
  // Stable key for the selected set — drives availability-aware tag loading.
  const eqIdsKey = useMemo(
    () => [...selectedEqIds].sort().join(','), [selectedEqIds])

  // ── Data loading ─────────────────────────────────────────────────────────────
  useEffect(() => {
    setLoadingTypes(true)
    fetchReportEquipmentTypes()
      // Hide a few report types from the picker without touching backend mapping —
      // they remain fully functional if requested directly, just not listed here.
      .then(data => setEqTypes(safeArr(data).filter(t => !HIDDEN_EQ_TYPES.has(t.equipment_type))))
      .catch(() => setEqTypes([]))
      .finally(() => setLoadingTypes(false))
  }, [])

  useEffect(() => {
    if (!eqType) { setEqList([]); setSelectedEqIds(new Set()); return }
    setTagList([]); setSelected(new Set()); setResult(null)

    // Single-source report type → fixed data source. Auto-select its one table
    // and skip the equipment-list API call entirely (nothing to choose from).
    const meta = eqTypeMeta.get(eqType)
    if (meta && (meta.equipment_count ?? 1) <= 1) {
      setEqList([])
      setSelectedEqIds(new Set(meta.table_name ? [meta.table_name] : []))
      setLoadingEqList(false)
      // Single-source equipment is fixed (the one table), so a restore just advances
      // to its tag stage — the tags effect below applies the saved tag selection.
      if (restoreRef.current) restoreRef.current.stage = 'tags'
      return
    }

    // Multi-equipment type → load the selectable equipment list, then preselect the
    // identifiers this report type's preset asks for (all devices, or just the first
    // for very wide sources). Selecting equipment cascades into the tag effect below,
    // which auto-selects every tag those devices expose in the database.
    setLoadingEqList(true)
    setSelectedEqIds(new Set())
    fetchReportEquipmentList(eqType)
      .then(data => {
        const list = safeArr(data)
        setEqList(list)
        // Restoring a saved report → reselect exactly the saved identifiers that
        // still exist; otherwise fall back to the type's preset selection.
        if (restoreRef.current && restoreRef.current.stage === 'eq') {
          const wanted = (restoreRef.current.equipmentIds || [])
            .filter(id => list.some(e => e.equipment_id === id))
          setSelectedEqIds(new Set(wanted.length ? wanted : presetEquipmentIds(eqType, list)))
          restoreRef.current.stage = 'tags'
        } else {
          setSelectedEqIds(new Set(presetEquipmentIds(eqType, list)))
        }
      })
      .catch(() => setEqList([]))
      .finally(() => setLoadingEqList(false))
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [eqType, eqTypeMeta])

  // Load tags with availability across ALL selected equipment. Tags present in
  // every selection are selectable; partially-available ones are shown locked.
  useEffect(() => {
    if (!eqType || selectedEqIds.size === 0) { setTagList([]); setSelected(new Set()); return }
    setLoadingTags(true)
    fetchTagAvailability(eqType, [...selectedEqIds])
      .then(data => {
        const tags = Array.isArray(data?.tags) ? data.tags : []
        setTagList(tags)
        const available = tags.filter(t => t.available !== false).map(t => t.column_name)
        // Restoring a saved report → reselect exactly the saved tags that are still
        // available; otherwise auto-select EVERY available tag (the default behaviour:
        // the union of all fully-available columns across the selected equipment,
        // straight from the database schema — no hardcoded subset, no cap).
        if (restoreRef.current && restoreRef.current.stage === 'tags') {
          const savedTags = restoreRef.current.tags || []
          const availSet  = new Set(available)
          const wanted    = savedTags.filter(t => availSet.has(t))
          setSelected(new Set(wanted.length ? wanted : available))
          restoreRef.current = null
          setRestoreToken(x => x + 1)   // cascade done → an auto-load may now run
        } else {
          setSelected(new Set(available))
        }
      })
      .catch(() => setTagList([]))
      .finally(() => setLoadingTags(false))
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [eqType, eqIdsKey])

  // Switching interval always restores the default aggregation, so leaving an
  // instant interval shows the dropdown back on "Average" rather than a stale pick.
  const changeInterval = useCallback((next) => {
    setInterval(next)
    setAgg(DEFAULT_AGG)
  }, [])

  // ── Publish the chosen range so Analytics opens on the same dates ───────────
  // Only the RANGE is shared, and only once the user actually changes it — the very
  // first run is skipped so the page's built-in defaults never overwrite a range the
  // user picked earlier (and never pre-empt the Analytics default when nothing has
  // been chosen yet). Restoring a saved report also lands here, since that is equally
  // a range the user selected. Nothing about the Reports UI or its own state changes.
  //
  // The publish is DEBOUNCED and the cleanup cancels any pending one, so a burst of
  // edits — the date picker firing per completed segment, or changing From and then
  // To — settles into a SINGLE publish carrying the final pair. Intermediate values
  // are never shared, so Analytics can never load a half-finished range. The context
  // setter additionally rejects anything incomplete or backwards (From > To), which
  // is exactly the state that exists between updating one end and the other.
  const publishedOnce = useRef(false)
  useEffect(() => {
    if (!publishedOnce.current) { publishedOnce.current = true; return }
    const id = setTimeout(() => setReportDateRange(fromDate, toDate), RANGE_PUBLISH_MS)
    return () => clearTimeout(id)
  }, [fromDate, toDate, setReportDateRange])

  // ── Time Interval options follow the selected date range ────────────────────
  // One shared rule (utils/intervals.intervalsForRange) drives the dropdown for
  // EVERY report/equipment type: a range of one month or less offers 1m/5m/15m/
  // 30m/Hourly, a longer range offers only 30m/Hourly. Recomputed on every From/To
  // change, so the list is always in step with the dates.
  const availableIntervals = useMemo(
    () => intervalsForRange(fromDate, toDate), [fromDate, toDate])

  // If the range change made the current interval unavailable, move to a valid one
  // (Hourly by preference) so the selector never keeps a hidden value. Aggregation is
  // reset alongside it, exactly as a manual interval change does.
  useEffect(() => {
    setInterval(prev => {
      const next = resolveInterval(prev, fromDate, toDate)
      if (next !== prev) setAgg(DEFAULT_AGG)
      return next
    })
  }, [fromDate, toDate])

  // ── Preconfigured report ──────────────────────────────────────────────────────
  // Picking a report type loads its preset: default Time Interval and Aggregation
  // here, default Equipment Identifier(s) in the equipment effect above, and every
  // tag of those identifiers in the tag effect. Nothing is locked — each field is a
  // normal control the user can change before loading or exporting.
  const selectReportType = useCallback((type) => {
    setEqType(type)
    if (!type) return
    const preset = getPreset(type)
    setInterval(preset.interval)
    setAgg(preset.agg)
  }, [])

  // ── Restore a saved (Preconfigured) report into the page ────────────────────
  // Re-applies a stored configuration exactly. Interval/aggregation/dates are set
  // directly (so they are NOT overwritten by the type preset), and the equipment +
  // tag selections are handed to `restoreRef` so the equipment→tags load cascade
  // reselects them instead of auto-selecting everything. Setting eqType last kicks
  // the cascade off. `autoLoad` remembers a Run/Load request for after it settles.
  const applyRestore = useCallback((cfg, autoLoad = false) => {
    if (!cfg || !cfg.equipment_type) return
    restoreRef.current = {
      equipmentIds: Array.isArray(cfg.equipment_ids) ? cfg.equipment_ids : [],
      tags:         Array.isArray(cfg.tags) ? cfg.tags : [],
      stage:        'eq',
    }
    autoLoadRef.current = !!autoLoad
    if (cfg.from_date)    setFromDate(cfg.from_date)
    if (cfg.to_date)      setToDate(cfg.to_date)
    if (cfg.interval)     setInterval(cfg.interval)
    if (cfg.agg_function) setAgg(cfg.agg_function)
    if (cfg.page_size)    setPageSize(cfg.page_size)
    setResult(null); setError(null); setWarning(null)
    setEqType(cfg.equipment_type)      // triggers the equipment→tags cascade
  }, [])

  // One-time restore when arriving from Preconfigured Reports (Open/Edit or Run).
  // The navigation state is cleared afterwards so a refresh doesn't re-restore.
  useEffect(() => {
    if (restoredRef.current) return
    const st = location.state
    if (st && st.restore) {
      restoredRef.current = true
      applyRestore(st.restore, st.autoLoad)
      window.history.replaceState({}, '')
    }
  }, [location.state, applyRestore])

  // Preset for the current type — drives the summary banner and the highlighted
  // (recommended) export button.
  const activePreset = useMemo(
    () => (eqType && hasPreset(eqType) ? getPreset(eqType) : null), [eqType])

  // ── Payload builder ───────────────────────────────────────────────────────────
  // withAggregation() omits `agg_function` entirely on an instant interval, so raw
  // telemetry is never sent an aggregation parameter. Every consumer of this payload
  // — preview, pagination, CSV and Excel export — inherits the rule from one place.
  const buildPayload = useCallback((pageNum = 1, pageSize = 100, equipmentId = primaryEqId) =>
    withAggregation({
      equipment_type: eqType,
      equipment_id:   equipmentId,
      // All selected inverters — used by the hierarchical String Combiner Excel export
      // to place one worksheet per inverter in a single workbook. The /data endpoint
      // ignores this and keeps using equipment_id, so single-equipment flows are unchanged.
      equipment_ids:  [...selectedEqIds],
      tags:           [...selected],
      from_datetime:  toISO(fromDate),
      to_datetime:    toISO(toDate),
      page:           pageNum,
      page_size:      pageSize,
    }, interval, agg),
  [eqType, primaryEqId, selectedEqIds, selected, fromDate, toDate, interval, agg])

  // ── Load data (single or multi-equipment) ─────────────────────────────────────
  const handleLoadData = useCallback(async () => {
    if (!eqType)                  return showToast('Select Equipment Type')
    if (selectedEqIds.size === 0) return showToast('Select Equipment Identifier')
    if (selected.size < 1)        return showToast('Select at least one tag')
    // Guard against blank identifiers slipping into the payload (backend rejects
    // an empty/space equipment_id with a 400).
    const ids = [...selectedEqIds].filter(id => id && String(id).trim())
    if (ids.length === 0)         return showToast('Select Equipment Identifier')

    // Multi-equipment → server-side paginated infinite-scroll load with progress.
    if (ids.length > 1) return runAsyncBatchLoad()

    // Single equipment → its own server-side pagination (Prev/Next below).
    mergedRef.current = { active: false, payload: null }; setMergedNav(null)
    setLoadingData(true); setError(null); setResult(null)
    setWarning(null); setWarnExpanded(false); setPage(1)
    try {
      const data = await fetchReportDataV2(buildPayload(1, 100, ids[0]))
      setResult(data)
      const sk = data.skipped_tags || []
      const warn = sk.length ? { count: sk.length, groups: [{ id: ids[0], tags: sk }] } : null
      setWarning(warn)
      showToast(warn
        ? `Loaded — ${warn.count} unavailable tag${warn.count > 1 ? 's' : ''} skipped`
        : 'Loaded successfully')
    } catch (e) {
      setError(e.message || 'Failed to load data')
    } finally {
      setLoadingData(false)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [eqType, selectedEqIds, selected, buildPayload, showToast])

  // ── Save the current configuration as a Preconfigured report ─────────────────
  // A default name for the Save prompt: report type + timestamp, so a user can
  // accept it or type their own. This never affects report data or exports.
  const defaultReportName = useCallback(() => {
    const d = new Date()
    const p = n => String(n).padStart(2, '0')
    const stamp = `${p(d.getDate())}/${p(d.getMonth() + 1)}/${d.getFullYear()} ` +
                  `${p(d.getHours())}:${p(d.getMinutes())}`
    return `${eqType || 'Report'} · ${stamp}`
  }, [eqType])

  // Persist the whole configuration to the backend so it appears in Preconfigured
  // Reports and survives a refresh/restart. Validates that a complete config AND
  // loaded data exist first (client req 9) — the Save button is also disabled until
  // then. Does NOT touch the report data, queries, calculations, or exports.
  const handleSave = useCallback(async () => {
    if (!eqType)                  return showToast('Select Equipment Type')
    if (selectedEqIds.size === 0) return showToast('Select Equipment Identifier')
    if (selected.size === 0)      return showToast('Select at least one tag')
    if (!result)                  return showToast('Click Load Data and verify the report before saving')

    const name = window.prompt('Save report as:', defaultReportName())
    if (name === null) return                       // user cancelled
    if (!name.trim())  return showToast('Report name is required')

    setSavingReport(true)
    try {
      await createSavedReport({
        name:           name.trim(),
        equipment_type: eqType,
        equipment_ids:  [...selectedEqIds].filter(id => id && String(id).trim()),
        tags:           [...selected],
        from_date:      fromDate,
        to_date:        toDate,
        interval,
        agg_function:   agg,
        page_size:      pageSize,
      })
      showToast('Report saved successfully.')
    } catch (e) {
      showToast(`Save failed: ${e.message}`)
    } finally {
      setSavingReport(false)
    }
  }, [eqType, selectedEqIds, selected, result, fromDate, toDate, interval, agg,
      pageSize, defaultReportName, showToast])

  // When a Preconfigured report was opened with Run/Load, auto-trigger Load Data
  // once the equipment→tags restore cascade has finished (restoreToken bumps) and
  // the selection is settled — exactly as if the user clicked Load Data themselves.
  useEffect(() => {
    if (restoreToken === 0) return
    if (autoLoadRef.current && selected.size > 0 && selectedEqIds.size > 0) {
      autoLoadRef.current = false
      handleLoadData()
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [restoreToken])

  // ── Multi-equipment load — classic server-side pagination ────────────────────
  // The first page runs as a background job so per-equipment count preparation can
  // report progress ("Preparing 5/12 equipment…"). Rows are ordered by timestamp
  // then equipment, so every device appears on every page.
  const runAsyncBatchLoad = useCallback(async (size = pageSize) => {
    setLoadingData(true); setError(null); setResult(null); setMergedNav(null)
    setWarning(null); setWarnExpanded(false); setPage(1)
    mergedRef.current = { active: false, payload: null }
    setLoadJob({ pct: 0, message: 'Starting…', status: 'running' })
    const payload = buildPayload(1, size)
    try {
      const { job_id } = await createMergedFirstJob(payload)
      const es = new EventSource(exportProgressUrl(job_id))
      loadEsRef.current = es

      es.onmessage = async (evt) => {
        let d; try { d = JSON.parse(evt.data) } catch { return }
        setLoadJob({ pct: d.progress ?? 0, message: d.message || '', status: d.status })

        if (d.status === 'done') {
          es.close(); loadEsRef.current = null
          try {
            const res = await fetchBatchDataResult(job_id)
            setResult(res)
            mergedRef.current = { active: true, payload }
            setMergedNav({
              page: res.page || 1, totalPages: res.total_pages || 1,
              total: res.total_records || 0, pageSize: size,
            })
            showToast(`Loaded ${(res.total_records || 0).toLocaleString()} rows`)
          } catch (e) {
            setError(e.message || 'Failed to fetch data')
          } finally {
            setLoadJob(null); setLoadingData(false)
          }
        } else if (d.status === 'error') {
          es.close(); loadEsRef.current = null
          setError(d.message || 'Load failed')
          setLoadJob(null); setLoadingData(false)
        }
      }

      es.onerror = () => {
        es.close(); loadEsRef.current = null
        setError('Progress connection lost')
        setLoadJob(null); setLoadingData(false)
      }
    } catch (e) {
      setError(e.message || 'Failed to start load')
      setLoadJob(null); setLoadingData(false)
    }
  }, [buildPayload, showToast, pageSize])

  // Navigate to a specific merged page (Prev/Next/page-size). Server-side: only
  // this page's rows are fetched and the table content is replaced.
  const goToMergedPage = useCallback(async (pageNum, size) => {
    const m = mergedRef.current
    if (!m.active || !m.payload || pageLoading) return
    const ps = size || mergedNav?.pageSize || DEFAULT_PAGE_SIZE
    // On a page-size change totalPages is unknown until the response returns.
    const cap = size ? Number.MAX_SAFE_INTEGER : (mergedNav?.totalPages || 1)
    const target = Math.max(1, Math.min(pageNum, cap))
    setPageLoading(true)
    try {
      // The axios response interceptor already unwraps to `response.data`, so this
      // IS the page body ({columns, rows, total_records, page, total_pages}).
      const data = await fetchMergedPage({ ...m.payload, page: target, page_size: ps })
      if (!data || !Array.isArray(data.columns)) {
        throw new Error('Malformed page response')
      }
      setResult(prev => ({
        ...(prev || {}),
        columns: data.columns,
        rows: Array.isArray(data.rows) ? data.rows : [],
        total_records: data.total_records ?? 0,
      }))
      setMergedNav({
        page: data.page || target, totalPages: data.total_pages || 1,
        total: data.total_records || 0, pageSize: ps,
      })
    } catch {
      showToast('Failed to load page')
    } finally {
      setPageLoading(false)
    }
  }, [mergedNav, pageLoading, showToast])

  // Page-size selector → reload from page 1 with the new size.
  const changePageSize = useCallback((size) => {
    setPageSize(size)
    goToMergedPage(1, size)
  }, [goToMergedPage])

  // ── Refresh registration (ref to avoid stale closure) ────────────────────────
  const handleLoadDataRef = useRef(null)
  handleLoadDataRef.current = handleLoadData

  useEffect(() => {
    registerRefresh(() => {
      if (!handleLoadDataRef.current) return
      return handleLoadDataRef.current()
    })
    return () => registerRefresh(null)
  }, [registerRefresh])

  // ── Pagination (single-equipment only) ───────────────────────────────────────
  const handlePageChange = async (newPage) => {
    if (!eqType || selectedEqIds.size === 0 || selected.size < 1 || !primaryEqId) return
    setLoadingData(true)
    try {
      const data = await fetchReportDataV2(buildPayload(newPage, 100))
      setResult(data); setPage(newPage)
    } catch { showToast('Failed to load page') }
    finally { setLoadingData(false) }
  }

  // ── Export ────────────────────────────────────────────────────────────────────
  const handleDownload = async (exportFn, ext, maxRows, label) => {
    // Export is independent of the loaded preview — it re-queries the backend — so
    // only a valid selection is required (no need to render a huge table first).
    if (!eqType || selectedEqIds.size === 0) return showToast('Select equipment first')
    if (selected.size === 0) return showToast('Select at least one tag')
    setLoadingExport(true); setExportLabel(label)
    const t0 = performance.now()
    console.log(`[Export] ${label} started — ${selectedEqIds.size} equipment, ${selected.size} tags`)
    try {
      const blob = await exportFn(buildPayload(1, maxRows))
      console.log(`[Export] ${label} response received in ${((performance.now() - t0) / 1000).toFixed(1)}s`)
      // Guard against an empty / invalid response instead of downloading a broken file.
      if (!(blob instanceof Blob) || blob.size === 0) {
        throw new Error('Empty file received from server')
      }
      const filePrefix = (selectedEqIds.size === eqList.length && eqList.length > 0)
        ? `All_${eqType}_Equipment`
        : selectedEqIds.size > 1
          ? `${selectedEqIds.size}_Equipment`
          : trackerDisplayName(primaryEqId)   // IS{nn} for the merged type; unchanged otherwise
      const filename = `${filePrefix}_Report_${todayDMY()}.${ext}`
      // Anchor MUST be in the DOM and the blob URL must NOT be revoked synchronously —
      // revoking too early aborts the download in most browsers.
      const url = URL.createObjectURL(blob)
      const a   = document.createElement('a')
      a.href = url
      a.download = filename
      a.style.display = 'none'
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      setTimeout(() => URL.revokeObjectURL(url), 1500)
      console.log(`[Export] ${label} downloaded — ${(blob.size / 1024).toFixed(1)} KB in ${((performance.now() - t0) / 1000).toFixed(1)}s total`)
      showToast(`${label} downloaded`)
    } catch (e) {
      console.error(`[Export] ${label} failed:`, e)
      showToast(`${label} failed: ${e.message}`)
    } finally {
      setLoadingExport(false); setExportLabel('')
    }
  }

  // Trigger a browser download from a Blob (anchor in DOM, deferred revoke).
  const saveBlob = (blob, filename) => {
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url; a.download = filename; a.style.display = 'none'
    document.body.appendChild(a); a.click(); document.body.removeChild(a)
    setTimeout(() => URL.revokeObjectURL(url), 1500)
  }

  // Large batch (multi-inverter) export — async job + SSE progress + auto-download.
  const runAsyncExport = async () => {
    setLoadingExport(true); setExportLabel('Excel')
    setExportJob({ pct: 0, message: 'Creating export job…', status: 'running' })
    try {
      const payload = buildPayload(1, 10000)
      // Diagnostic: prove how many equipment the browser actually sends. If this
      // shows 1 while the picker shows "All selected", the tab is running a stale
      // bundle — hard-refresh (Ctrl+Shift+R).
      console.log(`[Export] async — sending ${payload.equipment_ids.length} equipment:`, payload.equipment_ids)
      const { job_id } = await createExcelExportJob(payload)
      console.log(`[Export] async job ${job_id} created`)

      const es = new EventSource(exportProgressUrl(job_id))
      esRef.current = es

      es.onmessage = async (evt) => {
        let d; try { d = JSON.parse(evt.data) } catch { return }
        setExportJob({ pct: d.progress ?? 0, message: d.message || '', status: d.status })

        if (d.status === 'done') {
          es.close(); esRef.current = null
          try {
            const blob = await downloadExcelExportJob(job_id)
            if (!(blob instanceof Blob) || blob.size === 0) throw new Error('Empty file received')
            // Prefer the server-provided filename (ISO1_Report.xlsx, *.zip, SMB_Report…).
            const fallback = `${selectedEqIds.size}_Equipment_Report_${todayDMY()}.xlsx`
            saveBlob(blob, d.filename || fallback)
            console.log(`[Export] async downloaded ${d.filename || fallback} — ${(blob.size / 1024).toFixed(1)} KB`)
            showToast('Excel exported successfully.')
          } catch (e) {
            console.error('[Export] async download failed:', e)
            showToast(`Export failed: ${e.message}`)
          } finally {
            setExportJob(null); setLoadingExport(false); setExportLabel('')
          }
        } else if (d.status === 'error') {
          es.close(); esRef.current = null
          showToast(`Export failed: ${d.message || 'unknown error'}`)
          setExportJob(null); setLoadingExport(false); setExportLabel('')
        }
      }

      es.onerror = () => {
        es.close(); esRef.current = null
        console.error('[Export] SSE connection error')
        showToast('Export progress connection lost')
        setExportJob(null); setLoadingExport(false); setExportLabel('')
      }
    } catch (e) {
      console.error('[Export] failed to start async job:', e)
      showToast(`Export failed: ${e.message}`)
      setExportJob(null); setLoadingExport(false); setExportLabel('')
    }
  }

  // Trigger a NATIVE browser download of a URL via a hidden iframe. The response
  // (Content-Disposition: attachment) streams straight to disk — the file is
  // never held in JS/browser memory and the main thread stays free.
  const downloadViaIframe = (url) => {
    const iframe = document.createElement('iframe')
    iframe.style.display = 'none'
    iframe.src = url
    document.body.appendChild(iframe)
    // Keep it alive long enough for the download to be handed to the browser,
    // then remove it (the download continues independently of the element).
    setTimeout(() => { try { document.body.removeChild(iframe) } catch { /* noop */ } }, 60000)
  }

  // Single-request STREAMING export (T1 Isolation): one prepare call, then the
  // backend builds the workbook/ZIP sequentially and streams it to disk while the
  // existing SSE progress bar shows the current device. Nothing is buffered in the
  // browser, so the page never freezes even for ISO1–ISO12 with all tags.
  const runStreamingExport = async () => {
    setLoadingExport(true); setExportLabel('Excel')
    setExportJob({ pct: 0, message: 'Preparing export…', status: 'running' })
    try {
      const payload = buildPayload(1, 10000)
      console.log(`[Export] streaming — ${payload.equipment_ids.length} equipment:`, payload.equipment_ids)
      const { job_id } = await prepareExcelStreamJob(payload)

      // Live progress via the same SSE stream the async export uses (UI unchanged).
      const es = new EventSource(exportProgressUrl(job_id))
      esRef.current = es
      es.onmessage = (evt) => {
        let d; try { d = JSON.parse(evt.data) } catch { return }
        setExportJob({ pct: d.progress ?? 0, message: d.message || '', status: d.status })
        if (d.status === 'done') {
          es.close(); esRef.current = null
          setExportJob(null); setLoadingExport(false); setExportLabel('')
          showToast('Export downloaded')
        } else if (d.status === 'error') {
          es.close(); esRef.current = null
          setExportJob(null); setLoadingExport(false); setExportLabel('')
          showToast(`Export failed: ${d.message || 'unknown error'}`)
        }
      }
      es.onerror = () => {
        // Normal end-of-stream also lands here; the download itself is driven by
        // the iframe, so just close and let the browser finish writing to disk.
        es.close(); esRef.current = null
        setExportJob(null); setLoadingExport(false); setExportLabel('')
      }

      // Kick off the native, memory-free streaming download.
      downloadViaIframe(exportStreamUrl(job_id))
    } catch (e) {
      console.error('[Export] streaming failed to start:', e)
      showToast(`Export failed: ${e.message}`)
      setExportJob(null); setLoadingExport(false); setExportLabel('')
    }
  }

  // Direct-to-Downloads export (T1/T2 Isolation): the backend builds each device
  // report in parallel and saves each .xlsx straight into the Downloads folder as it
  // finishes — no ZIP, no browser download. We only start the job and show live
  // progress ("Saved ISO13_Report.xlsx …") over the existing SSE stream.
  const runToDownloadsExport = async () => {
    setLoadingExport(true); setExportLabel('Excel'); setSavedLocation(null)
    setExportJob({ pct: 0, message: 'Preparing export…', status: 'running' })
    try {
      const payload = buildPayload(1, 10000)
      console.log(`[Export] to-Downloads — ${payload.equipment_ids.length} equipment:`, payload.equipment_ids)
      const { job_id } = await startExcelToDownloads(payload)

      const es = new EventSource(exportProgressUrl(job_id))
      esRef.current = es
      es.onmessage = (evt) => {
        let d; try { d = JSON.parse(evt.data) } catch { return }
        setExportJob({ pct: d.progress ?? 0, message: d.message || '', status: d.status })
        if (d.status === 'done') {
          es.close(); esRef.current = null
          setExportJob(null); setLoadingExport(false); setExportLabel('')
          // The backend message ends with the absolute save path — surface it and
          // keep it visible so the user knows exactly where the files landed.
          const savedPath = (d.json_result && (d.json_result.path || d.json_result.directory))
            || (d.message || '').replace(/^Saved .*? to /, '') || null
          setSavedLocation(savedPath)
          showToast(d.message || 'Reports saved to Downloads')
        } else if (d.status === 'error') {
          es.close(); esRef.current = null
          setExportJob(null); setLoadingExport(false); setExportLabel('')
          showToast(`Export failed: ${d.message || 'unknown error'}`)
        }
      }
      es.onerror = () => {
        es.close(); esRef.current = null
        setExportJob(null); setLoadingExport(false); setExportLabel('')
      }
    } catch (e) {
      console.error('[Export] to-Downloads failed to start:', e)
      showToast(`Export failed: ${e.message}`)
      setExportJob(null); setLoadingExport(false); setExportLabel('')
    }
  }

  // Clean up any open SSE connections on unmount.
  useEffect(() => () => {
    if (esRef.current) esRef.current.close()
    if (loadEsRef.current) loadEsRef.current.close()
  }, [])

  const handleExportCSV   = () => handleDownload(exportReportCSVV2,   'csv',  10000, 'CSV')
  const handleExportExcel = () => {
    // EVERY equipment type is saved on disk under the single Reports root, in its own
    // per-type subfolder (Downloads\Reports\<Equipment Type>\…), via the to-Downloads
    // flow (background job + SSE progress + saved-location toast):
    //  · Tracker (T1/T2 Isolation) → Reports\Tracker\IS01.xlsx … (one file per device)
    //  · String Combiner           → Reports\String Combiner\INV{n}.xlsx (one per inverter)
    //  · every other type          → Reports\<type>\<name>_Report_<date>.xlsx (one workbook,
    //                                one worksheet per equipment) — same bytes the browser
    //                                export produced; only the destination changed.
    if (selectedEqIds.size > 0) return runToDownloadsExport()
    return showToast('Select equipment first')
  }

  // ── Column metadata from tag registry ────────────────────────────────────────
  const tagLookup = useMemo(() => {
    const map = {}
    tagList.forEach(t => { map[t.column_name] = t })
    return map
  }, [tagList])

  // The results table shows the FULL preview dataset the backend returns — every
  // selected equipment, every returned column — rendered through a virtualized
  // table, so nothing is truncated on the frontend and the DOM stays tiny. The
  // backend bounds the preview per-equipment (all devices present) so the export
  // still holds the complete data.
  // Column ORDER is decided by the backend and rendered here verbatim — this table
  // never re-sorts it. For Tracker the API orders columns through
  // `isolator_columns.order_columns_by_id`, the SAME function the Excel export uses,
  // so the on-screen sequence (Timestamp → every tag of Id1 → every tag of Id2 → …)
  // is identical to the generated worksheet. The frontend used to re-sort Tracker
  // columns metric-first here (all Alarm, then all Battery Level, …), which both
  // contradicted the workbook and duplicated ordering logic; that sort is gone.
  const tableCols = useMemo(() => {
    if (!result?.columns) return []
    return result.columns.map(col => {
      if (col === '_equipment') return { key: col, label: 'Equipment', isTs: false }
      if (col === 'timestamp')  return { key: col, label: 'Timestamp (DD/MM/YYYY HH:MM:SS)', isTs: true }
      const meta = tagLookup[col]
      if (meta) {
        const label = meta.unit ? `${meta.tag} (${meta.unit})` : meta.tag
        return { key: col, label, isTs: false }
      }
      const label = col.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())
      return { key: col, label, isTs: false }
    })
  }, [result?.columns, tagLookup, eqType])

  const tableRows = useMemo(() => {
    const rows = result?.rows || []
    if (!search) return rows
    return rows.filter(r =>
      Object.values(r).some(v => String(v).toLowerCase().includes(search.toLowerCase()))
    )
  }, [result?.rows, search])

  const totalPages = Math.min(Math.ceil((result?.total_records || 0) / 100), 50)
  const isMultiEq  = selectedEqIds.size > 1

  // Exports stream the FULL data straight from the backend and never depend on the
  // loaded preview, so a valid selection is enough to export — the user does not
  // have to load (and render) a huge preview first. This is the core fix for the
  // "page isn't responding" freeze: exporting no longer forces a giant load.
  const canExport = !!eqType && selectedEqIds.size > 0 && selected.size > 0

  // The report type's preset names a default export format — that button is styled
  // as the primary action. Both formats stay available and fully functional.
  const ExportBtn = ({ onClick, icon, label }) => {
    const isDefault = activePreset?.format === label
    return (
      <button className={`btn btn-sm ${isDefault ? 'btn-primary' : 'btn-outline'}`}
        onClick={onClick} disabled={loadingExport || !canExport}
        title={isDefault ? `Default export format for ${eqType}` : `Export as ${label}`}>
        {loadingExport && exportLabel === label
          ? <><Spinner size={12} /> Exporting...</>
          : <>{icon} Export {label}</>
        }
      </button>
    )
  }

  return (
    <div>
      <PageHeader title="Report Automation" />

      {/* Equipment Selection */}
      <div className="card mb-3">
        <div className="card-title">Equipment Selection</div>

        <FormRow>
          <FormGroup label="Equipment Type">
            {loadingTypes ? (
              <div className="form-control flex items-center gap-2 text-ge-text3 text-[12px]">
                <Spinner size={12} /> Loading...
              </div>
            ) : (
              <select className="form-control" value={eqType}
                onChange={e => selectReportType(e.target.value)}>
                <option value="">— Select Type —</option>
                {eqTypes.map(t => (
                  <option key={t.equipment_type} value={t.equipment_type}>
                    {eqTypeLabel(t.equipment_type)}
                  </option>
                ))}
              </select>
            )}
          </FormGroup>

          {/* Equipment Identifier — only for multi-equipment report types.
              Single-source types (Temperature Report, Alarms, Tracker, …) use a
              fixed table, so the picker is hidden and a data-source note shown. */}
          {isSingleSource ? (
            <FormGroup label="Data Source">
              <div className="form-control flex items-center gap-2 text-[12px] text-ge-text2">
                <span className="text-ge-accent">🎯</span>
                <span className="font-mono">{eqTypeMeta.get(eqType)?.table_name || eqType}</span>
                <span className="ml-auto text-[10px] text-ge-text3 uppercase tracking-widest">
                  Fixed source
                </span>
              </div>
            </FormGroup>
          ) : (
            <FormGroup
              label={`Equipment Identifier${selectedEqIds.size > 0 ? ` (${selectedEqIds.size} selected)` : ''}`}
            >
              <EquipmentMultiSelect
                eqList={eqList}
                selectedIds={selectedEqIds}
                setSelectedIds={setSelectedEqIds}
                disabled={!eqType}
                loading={loadingEqList}
              />
            </FormGroup>
          )}
        </FormRow>

        {/* Selected equipment chips — multi-equipment types only */}
        {!isSingleSource && selectedEqIds.size > 0 && (
          <div className="flex flex-wrap gap-1.5 mt-2.5 pt-2.5 border-t border-ge-border">
            {selectedEqIds.size === eqList.length && eqList.length > 0 ? (
              /* All selected — single summary chip */
              <span className="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded
                               bg-ge-blue/20 border border-ge-blue/40 text-ge-blue text-[11px] font-mono">
                ✓ All Equipment Selected ({eqList.length})
                <button
                  onClick={() => setSelectedEqIds(new Set())}
                  className="hover:text-ge-danger ml-0.5 leading-none"
                >×</button>
              </span>
            ) : (
              /* Individual chips */
              <>
                {[...selectedEqIds].map(id => (
                  <span key={id}
                    className="inline-flex items-center gap-1 px-2 py-0.5 rounded
                               bg-ge-blue/20 border border-ge-blue/40 text-ge-blue text-[11px] font-mono">
                    {trackerDisplayName(id)}
                    <button
                      onClick={() => setSelectedEqIds(prev => {
                        const s = new Set(prev); s.delete(id); return s
                      })}
                      className="hover:text-ge-danger ml-0.5 leading-none"
                    >×</button>
                  </span>
                ))}
                {selectedEqIds.size > 1 && (
                  <button
                    onClick={() => setSelectedEqIds(new Set())}
                    className="text-[11px] text-ge-danger hover:underline font-mono ml-1"
                  >
                    Clear All
                  </button>
                )}
              </>
            )}
          </div>
        )}
      </div>

      {/* Tag Selection — interactive. Every available tag is auto-selected when the
          equipment changes, so the panel opens fully selected; Select All / Clear All,
          search and per-tag toggling let the user refine it. */}
      <div className="card mb-3">
        {loadingTags ? (
          <div className="flex items-center gap-2 py-6 text-ge-text3 text-[12px]">
            <Spinner size={14} /> Loading tags from database...
          </div>
        ) : tagList.length > 0 ? (
          <DynamicTagSelector tags={tagList} selected={selected} setSelected={setSelected} />
        ) : eqType ? (
          <div className="text-[12px] text-ge-text3 py-4 text-center">
            {selectedEqIds.size > 0 ? 'No tags found' : 'Select Equipment Identifier to load tags'}
          </div>
        ) : (
          <div className="text-[12px] text-ge-text3 py-4 text-center">
            Select Equipment Type to see available tags
          </div>
        )}
      </div>

      {/* Date & Time */}
      <div className="card mb-3">
        <div className="card-title">Date &amp; Time Configuration</div>
        <div className="mb-2 px-1 text-[11px] text-ge-accent font-mono">
          📅 Data available: 08/02/2024 → 21/05/2024
        </div>
        <FormRow>
          <FormGroup label="From Date">
            <input type="datetime-local" className="form-control"
              value={fromDate} onChange={e => setFromDate(e.target.value)} />
          </FormGroup>
          <FormGroup label="To Date">
            <input type="datetime-local" className="form-control"
              value={toDate} onChange={e => setToDate(e.target.value)} />
          </FormGroup>
          <FormGroup label="Time Interval">
            <select className="form-control" value={interval}
              onChange={e => changeInterval(e.target.value)}>
              {/* Options come from the shared From→To rule, so every report/equipment
                  type on this page offers exactly the same set for a given range. */}
              {availableIntervals.map(v => (
                <option key={v} value={v}>{INTERVAL_LABELS[v]}</option>
              ))}
            </select>
          </FormGroup>
          {/* Instant telemetry is never aggregated — the field is not rendered at all,
              so the row reflows and leaves no empty slot. */}
          {showsAggregation(interval) && (
            <FormGroup label="Aggregation">
              <select className="form-control" value={agg}
                onChange={e => setAgg(e.target.value)}>
                {AGG_OPTIONS.map(o => (
                  <option key={o.value} value={o.value}>{o.label}</option>
                ))}
              </select>
            </FormGroup>
          )}
        </FormRow>
      </div>

      {/* Actions */}
      <div className="card mb-3">
        <div className="card-title">Actions</div>
        <div className="flex flex-wrap gap-2 items-center">
          <button className="btn btn-primary btn-sm" onClick={handleLoadData}
            disabled={loadingData || !eqType || selectedEqIds.size === 0 || selected.size === 0}>
            {loadingData
              ? <><Spinner size={12} /> Loading data...</>
              : <>📥 Load Data{isMultiEq ? ` (${selectedEqIds.size} equipment)` : ''} · {selected.size} tags</>
            }
          </button>

          <button className="btn btn-outline btn-sm"
            onClick={() => navigate('/scheduled')}>
            ⏱ Scheduled Tasks
          </button>

          <ExportBtn onClick={handleExportCSV}   icon="📊" label="CSV"   />

          {/* Save the current configuration as a Preconfigured report. Enabled only
              after Load Data succeeds (a config + verified data must exist), so it
              validates before saving. Independent of Export Excel, which is untouched. */}
          <button className="btn btn-outline btn-sm" onClick={handleSave}
            disabled={savingReport || !canExport || !result}
            title={result
              ? 'Save this report configuration to Preconfigured Reports'
              : 'Load Data first, then Save'}>
            {savingReport
              ? <><Spinner size={12} /> Saving...</>
              : <>💾 Save</>}
          </button>

          <ExportBtn onClick={handleExportExcel} icon="📗" label="Excel" />

          {result && (
            <div className="ml-auto flex items-center gap-3">
              <span className="text-[11px] font-mono text-ge-text3">
                {trackerDisplayName(result.table_name)} · {INTERVAL_LABELS[result.interval] || result.interval}
              </span>
            </div>
          )}
        </div>

        {/* Async batch-export progress */}
        {exportJob && (
          <div className="mt-3 pt-3 border-t border-ge-border">
            <div className="flex items-center justify-between mb-1.5">
              <span className="text-[11px] text-ge-text2 flex items-center gap-2">
                <Spinner size={12} /> {exportJob.message}
              </span>
              <span className="text-[11px] font-mono text-ge-accent">{exportJob.pct}%</span>
            </div>
            <div className="h-2 bg-ge-elevated rounded overflow-hidden border border-ge-border">
              <div className="h-full bg-ge-accent transition-all duration-300"
                style={{ width: `${exportJob.pct}%` }} />
            </div>
          </div>
        )}

        {/* Persistent saved-location line — shows the REAL absolute path on disk. */}
        {!exportJob && savedLocation && (
          <div className="mt-3 pt-3 border-t border-ge-border flex items-start gap-2">
            <span className="text-ge-accent text-[13px] leading-none mt-0.5">✓</span>
            <div className="flex-1 min-w-0">
              <div className="text-[11px] text-ge-text2">Saved to:</div>
              <div className="text-[12px] font-mono text-ge-text1 break-all">{savedLocation}</div>
            </div>
            <button
              onClick={() => setSavedLocation(null)}
              className="text-[11px] text-ge-text3 hover:text-ge-danger leading-none"
              title="Dismiss"
            >×</button>
          </div>
        )}
      </div>

      {/* Error */}
      {error && (
        <div className="card mb-3 bg-ge-danger/5 border-ge-danger/40">
          <p className="text-[12px] text-ge-danger">⚠ {error}</p>
        </div>
      )}

      {/* Skipped-tags warning — report still loads with all available tags */}
      {warning && (
        <div className="card mb-3 bg-amber-500/5 border-amber-500/40">
          <div className="flex items-start gap-2">
            <span className="text-amber-400 text-[13px] leading-none mt-0.5">⚠</span>
            <div className="flex-1 min-w-0">
              <p className="text-[12px] text-amber-300 font-medium">
                Report loaded successfully. {warning.count} unavailable tag
                {warning.count > 1 ? 's were' : ' was'} skipped.
              </p>
              <button
                onClick={() => setWarnExpanded(v => !v)}
                className="text-[11px] text-ge-blue font-mono hover:underline mt-1">
                {warnExpanded ? 'Hide details ▲' : 'View Details ▼'}
              </button>
              {warnExpanded && (
                <div className="mt-2 space-y-1.5 border-t border-amber-500/20 pt-2">
                  {warning.groups.map(g => (
                    <div key={g.id} className="text-[11px] break-words">
                      <span className="font-mono text-ge-text1">{g.id}</span>
                      <span className="text-ge-text3"> — skipped: </span>
                      <span className="text-amber-300 font-mono">{g.tags.join(', ')}</span>
                    </div>
                  ))}
                  <p className="text-[10px] text-ge-text3 pt-1">
                    Exports include only valid columns; skipped tags are listed in the export summary.
                  </p>
                </div>
              )}
            </div>
            <button
              onClick={() => setWarning(null)}
              className="text-ge-text3 hover:text-ge-text1 text-sm leading-none flex-shrink-0">✕</button>
          </div>
        </div>
      )}

      {/* Data Table */}
      <div className="card">
        <div className="card-title">
          <span>📋 Report Data</span>
        </div>

        {loadingData && loadJob ? (
          <div className="py-10 px-4">
            <div className="flex items-center gap-2 mb-2">
              <Spinner size={13} />
              <span className="text-[12px] text-ge-text1">{loadJob.message || 'Loading…'}</span>
              <span className="ml-auto text-[11px] font-mono text-ge-accent">{loadJob.pct}%</span>
            </div>
            <div className="h-1.5 bg-ge-navy rounded overflow-hidden">
              <div className="h-full bg-ge-accent rounded transition-all duration-300"
                   style={{ width: `${loadJob.pct}%` }} />
            </div>
            <div className="text-[10px] text-ge-text3 mt-2 text-center">
              Processing equipment in parallel — the page stays responsive.
            </div>
          </div>
        ) : loadingData ? (
          <div className="space-y-1">
            {[...Array(8)].map((_, i) => <Skeleton key={i} h="h-8" />)}
          </div>
        ) : !result ? (
          <div className="py-16 text-center">
            <div className="text-4xl mb-3">📊</div>
            <div className="text-[13px] text-ge-text3 mb-1 font-medium">No data loaded yet</div>
            <div className="text-[11px] text-ge-text3">
              Select equipment → tags → date range → click Load Data
            </div>
          </div>
        ) : tableRows.length === 0 ? (
          <div className="py-12 text-center text-[12px] text-ge-text3">
            No records found for selected filters
          </div>
        ) : (
          <>
            <VirtualDataTable cols={tableCols} rows={tableRows} />

            {/* Server-side pagination — multi-equipment (rows span all devices) */}
            {isMultiEq && mergedNav && (
              <div className="flex items-center gap-2 mt-3 flex-wrap">
                <label className="text-[11px] font-mono text-ge-text3 flex items-center gap-1.5">
                  Rows/page
                  <select
                    value={pageSize}
                    onChange={e => changePageSize(Number(e.target.value))}
                    disabled={pageLoading}
                    className="bg-ge-elevated border border-ge-border rounded px-1.5 py-1
                               text-[11px] text-ge-text1 disabled:opacity-40">
                    {PAGE_SIZE_OPTIONS.map(s => <option key={s} value={s}>{s}</option>)}
                  </select>
                </label>

                <button onClick={() => goToMergedPage(mergedNav.page - 1)}
                  disabled={mergedNav.page <= 1 || pageLoading}
                  className="px-2.5 py-1 text-[11px] font-mono rounded border
                             bg-ge-elevated border-ge-border text-ge-text2
                             hover:text-ge-text1 disabled:opacity-40">
                  ◀ Prev
                </button>

                {(() => {
                  const cur = mergedNav.page, tp = mergedNav.totalPages
                  const from = Math.max(1, Math.min(cur - 2, tp - 4))
                  const nums = []
                  for (let p = from; p <= Math.min(tp, from + 4); p++) nums.push(p)
                  return nums.map(p => (
                    <button key={p} onClick={() => goToMergedPage(p)} disabled={pageLoading}
                      className={`px-2.5 py-1 text-[11px] font-mono rounded border transition-all disabled:opacity-40
                        ${p === cur
                          ? 'bg-ge-blue text-white border-ge-blue'
                          : 'bg-ge-elevated border-ge-border text-ge-text2 hover:text-ge-text1'}`}>
                      {p}
                    </button>
                  ))
                })()}

                <button onClick={() => goToMergedPage(mergedNav.page + 1)}
                  disabled={mergedNav.page >= mergedNav.totalPages || pageLoading}
                  className="px-2.5 py-1 text-[11px] font-mono rounded border
                             bg-ge-elevated border-ge-border text-ge-text2
                             hover:text-ge-text1 disabled:opacity-40">
                  Next ▶
                </button>

                {pageLoading && <Spinner size={12} />}

                <span className="ml-auto text-[11px] font-mono text-ge-text3">
                  Page {mergedNav.page.toLocaleString()} of {mergedNav.totalPages.toLocaleString()}
                  {'  ·  '}Total Rows: {mergedNav.total.toLocaleString()}
                </span>
              </div>
            )}

            {/* Pagination — single-equipment */}
            {!isMultiEq && (
              <div className="flex items-center gap-1.5 mt-3 flex-wrap">
                <button onClick={() => handlePageChange(Math.max(1, page - 1))}
                  disabled={page <= 1 || loadingData}
                  className="px-2.5 py-1 text-[11px] font-mono rounded border
                             bg-ge-elevated border-ge-border text-ge-text2
                             hover:text-ge-text1 disabled:opacity-40">
                  ◀ Prev
                </button>

                {Array.from({ length: Math.min(totalPages, 10) }, (_, i) => i + 1).map(p => (
                  <button key={p} onClick={() => handlePageChange(p)}
                    className={`px-2.5 py-1 text-[11px] font-mono rounded border transition-all
                      ${p === page
                        ? 'bg-ge-blue text-white border-ge-blue'
                        : 'bg-ge-elevated border-ge-border text-ge-text2 hover:text-ge-text1'}`}>
                    {p}
                  </button>
                ))}

                {totalPages > 10 && (
                  <span className="text-[11px] text-ge-text3 font-mono">
                    ... {totalPages} pages
                  </span>
                )}

                <button onClick={() => handlePageChange(Math.min(totalPages, page + 1))}
                  disabled={page >= totalPages || loadingData}
                  className="px-2.5 py-1 text-[11px] font-mono rounded border
                             bg-ge-elevated border-ge-border text-ge-text2
                             hover:text-ge-text1 disabled:opacity-40">
                  Next ▶
                </button>

                <span className="ml-auto text-[11px] font-mono text-ge-text3">
                  Page {page} of {totalPages}
                </span>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}
