// src/pages/Preconfigured/Preconfigured.jsx
//
// Preconfigured Reports — build one report from columns across several equipment
// types at once (Inverter + WMS + PPC), then export it as a single workbook.
//
// Every selected type stays selected: adding a second type never clears the first
// one's chosen columns. Selections are held in ONE ordered list spanning all
// types, so the report's column order is exactly the order the user picked them,
// and the Selected panel simply groups that list by equipment type for reading.
//
// It is independent of the Reports, DGR, Analytics and Scheduled pages: nothing
// here is imported by them and nothing there is modified. Data comes from the
// multi-equipment endpoints, which merge each source on the interval bucket.
//
// Excel is the only export format offered on this page by design.

import React, { useState, useEffect, useMemo, useCallback } from 'react'
import { PageHeader, FormRow, FormGroup, Spinner, Skeleton } from '../../components/Common'
import { useApp } from '../../utils/AppContext'
import {
  INTERVALS, INTERVAL_LABELS, AGG_OPTIONS, DEFAULT_AGG,
  showsAggregation,
} from '../../utils/intervals'
import { getPreset } from '../../utils/reportPresets'
import {
  fetchReportEquipmentList,
  fetchReportTags,
  fetchMultiReportData,
  exportMultiReportExcel,
} from '../../services/api'

// The only report types this page supports.
const EQ_TYPES  = ['Inverter', 'WMS', 'PPC']
const PAGE_SIZES = [50, 100]

function fmtTimestamp(val) {
  if (!val) return '—'
  const s = String(val).replace('T', ' ').slice(0, 19)
  const [date, time] = s.split(' ')
  if (!date) return s
  const [y, mo, d] = date.split('-')
  return `${d}/${mo}/${y} ${time || '00:00:00'}`
}

const fmtCell = v =>
  v === null || v === undefined ? '—' : (typeof v === 'number' ? v.toFixed(3) : String(v))

const todayDMY = () => {
  const d = new Date()
  return `${String(d.getDate()).padStart(2, '0')}-${String(d.getMonth() + 1).padStart(2, '0')}-${d.getFullYear()}`
}

const toISO = dtLocal => (dtLocal ? new Date(dtLocal).toISOString() : '')

function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url; a.download = filename; a.style.display = 'none'
  document.body.appendChild(a); a.click(); document.body.removeChild(a)
  setTimeout(() => URL.revokeObjectURL(url), 1500)
}

// A selection is identified by type + equipment + column, so the same column name
// on two different tables never collides.
const selKey = s => `${s.type}|${s.eqId}|${s.column}`

// ── Main page ────────────────────────────────────────────────────────────────
export default function Preconfigured() {
  const { showToast } = useApp()

  // Selected equipment types, in the order they were added. Adding one never
  // touches the others' state or their already-chosen columns.
  const [types,      setTypes]      = useState([])
  const [activeType, setActiveType] = useState('')

  // Per-type source state: { eqList, eqId, tags, loadingEq, loadingTags }
  const [srcs, setSrcs] = useState({})

  // ONE ordered list across every type — this order is the report's column order.
  const [selected, setSelected] = useState([])

  const [query,    setQuery]    = useState('')
  const [fromDate, setFromDate] = useState('2024-02-08T00:00')
  const [toDate,   setToDate]   = useState('2024-05-21T23:59')
  const [interval, setInterval] = useState('hourly')
  const [agg,      setAgg]      = useState(DEFAULT_AGG)

  const [result,     setResult]     = useState(null)
  const [error,      setError]      = useState(null)
  const [generating, setGenerating] = useState(false)
  const [exporting,  setExporting]  = useState(false)
  const [page,       setPage]       = useState(1)
  const [pageSize,   setPageSize]   = useState(PAGE_SIZES[0])

  const patch = useCallback((type, changes) => {
    setSrcs(prev => ({ ...prev, [type]: { ...(prev[type] || {}), ...changes } }))
  }, [])

  // ── Load a type's columns for a given equipment ───────────────────────────
  const loadTags = useCallback((type, eqId) => {
    if (!eqId) { patch(type, { tags: [], loadingTags: false }); return }
    patch(type, { loadingTags: true })
    fetchReportTags(type, eqId)
      .then(d => patch(type, { tags: Array.isArray(d) ? d : (d?.tags || []), loadingTags: false }))
      .catch(() => patch(type, { tags: [], loadingTags: false }))
  }, [patch])

  // ── Add / remove an equipment type ────────────────────────────────────────
  const toggleType = useCallback((type) => {
    setTypes(prev => {
      if (prev.includes(type)) {
        // Removing a type also drops the columns that belonged to it.
        setSelected(sel => sel.filter(s => s.type !== type))
        setSrcs(s => { const next = { ...s }; delete next[type]; return next })
        setActiveType(a => (a === type ? prev.filter(t => t !== type)[0] || '' : a))
        return prev.filter(t => t !== type)
      }

      // Adding a type leaves every existing selection untouched.
      setActiveType(type)
      patch(type, { loadingEq: true, eqList: [], eqId: '', tags: [] })
      fetchReportEquipmentList(type)
        .then(d => {
          const list = Array.isArray(d) ? d : (d?.items || d?.data || [])
          const eqId = list[0]?.equipment_id || ''
          patch(type, { eqList: list, eqId, loadingEq: false })
          loadTags(type, eqId)
        })
        .catch(() => patch(type, { eqList: [], eqId: '', loadingEq: false }))
      return [...prev, type]
    })
    setResult(null)
  }, [patch, loadTags])

  // Switching a type's equipment invalidates that type's picks (they belong to
  // the previous device); every other type's selection is preserved.
  const changeEquipment = useCallback((type, eqId) => {
    patch(type, { eqId })
    setSelected(sel => sel.filter(s => s.type !== type))
    setResult(null)
    loadTags(type, eqId)
  }, [patch, loadTags])

  // Seed interval/aggregation from the first type added (same presets the Reports
  // and Scheduled screens use). Both remain editable.
  useEffect(() => {
    if (types.length !== 1) return
    const preset = getPreset(types[0])
    setInterval(preset.interval)
    setAgg(preset.agg)
  }, [types])

  // ── Column selection ──────────────────────────────────────────────────────
  const toggleColumn = useCallback((type, eqId, tag) => {
    const entry = { type, eqId, column: tag.column_name, label: tag.tag, unit: tag.unit }
    setSelected(prev => prev.some(s => selKey(s) === selKey(entry))
      ? prev.filter(s => selKey(s) !== selKey(entry))
      : [...prev, entry])          // appended → selection order is the column order
    setResult(null)
  }, [])

  const selectedKeys = useMemo(() => new Set(selected.map(selKey)), [selected])

  // Selected columns grouped by equipment type for display, each group keeping
  // its slice of the global pick order.
  const grouped = useMemo(() => {
    const g = new Map()
    selected.forEach(s => {
      if (!g.has(s.type)) g.set(s.type, [])
      g.get(s.type).push(s)
    })
    return [...g.entries()]
  }, [selected])

  const active     = srcs[activeType] || {}
  const activeTags = active.tags || []

  const shownTags = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return activeTags
    return activeTags.filter(t =>
      t.tag.toLowerCase().includes(q) || t.column_name.toLowerCase().includes(q))
  }, [activeTags, query])

  // ── Request payload ───────────────────────────────────────────────────────
  // Selections collapse into one source per equipment, tags in pick order.
  const buildPayload = useCallback((pageNum, size) => {
    const byEquipment = new Map()
    selected.forEach(s => {
      const k = `${s.type}|${s.eqId}`
      if (!byEquipment.has(k)) {
        byEquipment.set(k, { equipment_type: s.type, equipment_id: s.eqId, tags: [] })
      }
      byEquipment.get(k).tags.push(s.column)
    })
    return {
      sources:       [...byEquipment.values()],
      from_datetime: toISO(fromDate),
      to_datetime:   toISO(toDate),
      interval,
      agg_function:  agg,
      page:          pageNum,
      page_size:     size,
    }
  }, [selected, fromDate, toDate, interval, agg])

  const ready = selected.length > 0

  const runReport = useCallback(async (pageNum, size) => {
    setGenerating(true); setError(null)
    try {
      const data = await fetchMultiReportData(buildPayload(pageNum, size))
      setResult(data); setPage(pageNum); setPageSize(size)
    } catch (e) {
      setError(e.message || 'Failed to generate report'); setResult(null)
    } finally {
      setGenerating(false)
    }
  }, [buildPayload])

  const handleGenerate = () => {
    if (!ready) return showToast('Select at least one column')
    runReport(1, pageSize)
  }

  const handleExportExcel = async () => {
    if (!ready) return showToast('Select at least one column')
    setExporting(true)
    try {
      // page_size is ignored by the export — the workbook holds the whole range.
      const blob = await exportMultiReportExcel(buildPayload(1, 100))
      if (!(blob instanceof Blob) || blob.size === 0) throw new Error('Empty file received from server')
      saveBlob(blob, `Multi_Equipment_Report_${todayDMY()}.xlsx`)
      showToast('Excel exported')
    } catch (e) {
      showToast(`Export failed: ${e.message}`)
    } finally {
      setExporting(false)
    }
  }

  // ── Preview table ─────────────────────────────────────────────────────────
  const tableCols = useMemo(() => {
    const cols   = result?.columns || []
    const labels = result?.labels  || {}
    return [
      ...cols.filter(c => c === 'timestamp'),
      ...cols.filter(c => c !== 'timestamp'),
    ].map(c => ({
      key: c,
      isTs: c === 'timestamp',
      label: c === 'timestamp' ? 'Timestamp (DD/MM/YYYY HH:MM:SS)' : (labels[c] || c),
    }))
  }, [result?.columns, result?.labels])

  const rows       = result?.rows || []
  const totalRows  = result?.total_records || 0
  const totalPages = Math.max(1, Math.ceil(totalRows / pageSize))

  return (
    <div>
      <PageHeader title="Preconfigured Reports" />

      {/* Equipment types — several may be active at once */}
      <div className="card mb-3">
        <div className="card-title">Equipment Types</div>
        <div className="flex flex-wrap gap-2">
          {EQ_TYPES.map(t => {
            const on = types.includes(t)
            const n  = selected.filter(s => s.type === t).length
            return (
              <button key={t} onClick={() => toggleType(t)}
                className={`btn btn-sm ${on ? 'btn-primary' : 'btn-outline'}`}>
                {on ? '✓ ' : '+ '}{t}{n > 0 ? ` (${n})` : ''}
              </button>
            )
          })}
          {types.length > 0 && (
            <span className="ml-auto text-[11px] font-mono text-ge-text3 self-center">
              {types.length} type{types.length > 1 ? 's' : ''} · {selected.length} column
              {selected.length === 1 ? '' : 's'} selected
            </span>
          )}
        </div>

        {/* One equipment identifier per selected type */}
        {types.length > 0 && (
          <div className="grid gap-3 mt-3 md:grid-cols-3">
            {types.map(t => {
              const s = srcs[t] || {}
              return (
                <FormGroup key={t} label={`${t} — Equipment Identifier`}>
                  {s.loadingEq ? (
                    <div className="form-control flex items-center gap-2 text-ge-text3 text-[12px]">
                      <Spinner size={12} /> Loading...
                    </div>
                  ) : (
                    <select className="form-control" value={s.eqId || ''}
                      onChange={e => changeEquipment(t, e.target.value)}
                      disabled={!(s.eqList || []).length}>
                      {(s.eqList || []).map(e => (
                        <option key={e.equipment_id} value={e.equipment_id}>
                          {e.display_name || e.equipment_id}
                        </option>
                      ))}
                    </select>
                  )}
                </FormGroup>
              )
            })}
          </div>
        )}
      </div>

      {/* Filters */}
      <div className="card mb-3">
        <div className="card-title">Report Configuration</div>
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
              onChange={e => { setInterval(e.target.value); setAgg(DEFAULT_AGG) }}>
              {INTERVALS.map(v => <option key={v} value={v}>{INTERVAL_LABELS[v]}</option>)}
            </select>
          </FormGroup>
          {showsAggregation(interval) && (
            <FormGroup label="Aggregation">
              <select className="form-control" value={agg} onChange={e => setAgg(e.target.value)}>
                {AGG_OPTIONS.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
              </select>
            </FormGroup>
          )}
        </FormRow>
      </div>

      {/* Columns */}
      <div className="card mb-3">
        <div className="card-title">Report Columns</div>

        {types.length === 0 ? (
          <div className="py-6 text-center text-[12px] text-ge-text3">
            Select one or more equipment types above to load their columns
          </div>
        ) : (
          <div className="grid gap-3 md:grid-cols-2">
            {/* Available — browsing one type at a time; selections persist across tabs */}
            <div>
              <div className="flex flex-wrap items-center gap-1.5 mb-2">
                {types.map(t => (
                  <button key={t} onClick={() => { setActiveType(t); setQuery('') }}
                    className={`px-2.5 py-1 text-[11px] rounded border transition-colors
                      ${activeType === t
                        ? 'bg-ge-blue text-white border-ge-blue'
                        : 'bg-ge-elevated border-ge-border text-ge-text2 hover:text-ge-text1'}`}>
                    {t}
                  </button>
                ))}
                {/* Bulk actions sit with the tabs and act ONLY on the active type —
                    every other type's selected columns are left untouched. */}
                <div className="ml-auto flex items-center gap-1.5">
                  <span className="text-[10px] font-mono text-ge-text3">
                    {shownTags.length} / {activeTags.length}
                  </span>
                  <button type="button" className="btn btn-outline btn-sm"
                    disabled={!activeTags.length}
                    title={`Select every ${activeType} column`}
                    onClick={() => setSelected(prev => {
                      const have = new Set(prev.map(selKey))
                      const add  = activeTags
                        .map(t => ({ type: activeType, eqId: active.eqId, column: t.column_name,
                                     label: t.tag, unit: t.unit }))
                        .filter(e => !have.has(selKey(e)))
                      return [...prev, ...add]
                    })}>
                    ✓ Select All
                  </button>
                  <button type="button" className="btn btn-outline btn-sm"
                    disabled={!selected.some(s => s.type === activeType)}
                    title={`Clear the selected ${activeType} columns`}
                    onClick={() => setSelected(prev => prev.filter(s => s.type !== activeType))}>
                    ✕ Clear
                  </button>
                </div>
              </div>

              <div className="relative mb-2">
                <span className="absolute left-2.5 top-1/2 -translate-y-1/2 text-ge-text3 text-[11px]">🔍</span>
                <input type="text" value={query} onChange={e => setQuery(e.target.value)}
                  placeholder={`Search ${activeType} columns...`}
                  className="form-control pl-7 text-[12px]" />
              </div>

              <div className="bg-ge-elevated border border-ge-border rounded-md overflow-y-auto max-h-72">
                {active.loadingTags ? (
                  <div className="flex items-center gap-2 px-3 py-6 text-[12px] text-ge-text3">
                    <Spinner size={13} /> Loading columns from database...
                  </div>
                ) : shownTags.length === 0 ? (
                  <div className="px-3 py-6 text-center text-[12px] text-ge-text3 italic">
                    {activeTags.length ? `No columns matching "${query}"` : 'No columns found'}
                  </div>
                ) : shownTags.map(t => {
                  const isSel = selectedKeys.has(`${activeType}|${active.eqId}|${t.column_name}`)
                  return (
                    <div key={t.column_name}
                      onClick={() => toggleColumn(activeType, active.eqId, t)}
                      className={`flex items-center gap-2 px-2.5 py-1.5 cursor-pointer select-none
                                  border-b border-ge-border last:border-b-0 transition-colors
                                  hover:bg-ge-surface ${isSel ? 'bg-ge-blue/10' : ''}`}>
                      <div className={`w-3.5 h-3.5 rounded border flex items-center justify-center
                                      text-[9px] flex-shrink-0
                                      ${isSel ? 'bg-ge-blue border-ge-blue text-white' : 'border-ge-border2'}`}>
                        {isSel && '✓'}
                      </div>
                      <span className="text-[12px] text-ge-text1 flex-1 leading-tight truncate">{t.tag}</span>
                      {t.unit && <span className="text-[10px] font-mono text-ge-text3">{t.unit}</span>}
                    </div>
                  )
                })}
              </div>

            </div>

            {/* Selected — grouped by equipment type, in pick order */}
            <div>
              <div className="flex items-center justify-between mb-2">
                <span className="text-[10px] font-semibold uppercase tracking-widest text-ge-text3">
                  Selected Report Columns
                </span>
                <span className="text-[10px] font-mono text-ge-accent">{selected.length} selected</span>
              </div>

              <div className="bg-ge-elevated border border-ge-border rounded-md overflow-y-auto max-h-[352px]">
                {selected.length === 0 ? (
                  <div className="px-3 py-8 text-center text-[12px] text-ge-text3">
                    No columns selected — pick them on the left
                  </div>
                ) : grouped.map(([type, items]) => (
                  <div key={type}>
                    <div className="sticky top-0 z-10 flex items-center gap-2 px-2.5 py-1
                                    bg-ge-surface border-b border-ge-border">
                      <span className="text-[10px] font-semibold uppercase tracking-widest text-ge-text2">
                        {type}
                      </span>
                      <span className="text-[10px] font-mono text-ge-text3">
                        {items[0]?.eqId}
                      </span>
                      <span className="ml-auto text-[10px] font-mono text-ge-accent">
                        {items.length}
                      </span>
                    </div>
                    {items.map(s => (
                      <div key={selKey(s)}
                        className="flex items-center gap-2 px-2.5 py-1.5
                                   border-b border-ge-border last:border-b-0 bg-ge-blue/5">
                        <span className="text-[12px] text-ge-text1 flex-1 leading-tight truncate"
                          title={s.column}>
                          {s.label || s.column}
                        </span>
                        {s.unit && <span className="text-[10px] font-mono text-ge-text3">{s.unit}</span>}
                        <button type="button" title="Remove"
                          onClick={() => setSelected(prev => prev.filter(x => selKey(x) !== selKey(s)))}
                          className="px-1 text-[11px] text-ge-text3 hover:text-ge-danger">✕</button>
                      </div>
                    ))}
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Actions */}
      <div className="card mb-3">
        <div className="card-title">Actions</div>
        <div className="flex flex-wrap items-center gap-2">
          <button className="btn btn-primary btn-sm" onClick={handleGenerate}
            disabled={generating || !ready}>
            {generating
              ? <><Spinner size={12} /> Generating...</>
              : <>📥 Generate Report · {selected.length} columns</>}
          </button>
          <button className="btn btn-success btn-sm" onClick={handleExportExcel}
            disabled={exporting || !ready}>
            {exporting
              ? <><Spinner size={12} /> Exporting...</>
              : <>📗 Export Excel</>}
          </button>
        </div>
      </div>

      {error && (
        <div className="card mb-3 bg-ge-danger/5 border-ge-danger/40">
          <p className="text-[12px] text-ge-danger">⚠ {error}</p>
        </div>
      )}

      {/* Report Data */}
      <div className="card">
        <div className="card-title justify-between">
          <span>📋 Report Data</span>
          {result && (
            <span className="text-[11px] font-mono text-ge-text3">
              {(result.sources || []).length} equipment ·{' '}
              {INTERVAL_LABELS[result.interval] || result.interval} ·{' '}
              {totalRows.toLocaleString()} rows
            </span>
          )}
        </div>

        {generating ? (
          <div className="space-y-1">
            {[...Array(6)].map((_, i) => <Skeleton key={i} h="h-8" />)}
          </div>
        ) : !result ? (
          <div className="py-14 text-center">
            <div className="text-4xl mb-3">📊</div>
            <div className="text-[13px] text-ge-text3 mb-1 font-medium">No report generated yet</div>
            <div className="text-[11px] text-ge-text3">
              Select equipment types → columns → click Generate Report
            </div>
          </div>
        ) : rows.length === 0 ? (
          <div className="py-12 text-center text-[12px] text-ge-text3">
            No data available for the selected criteria
          </div>
        ) : (
          <>
            <div className="overflow-auto border border-ge-border rounded" style={{ maxHeight: 520 }}>
              <table className="data-table">
                <thead>
                  <tr>
                    {tableCols.map(c => (
                      <th key={c.key} className="text-[10px] sticky top-0 z-10 whitespace-nowrap">
                        {c.label}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row, i) => (
                    <tr key={i}>
                      {tableCols.map(c => (
                        <td key={c.key} className="font-mono text-[11px] whitespace-nowrap">
                          {c.isTs
                            ? <span className="text-ge-text3">{fmtTimestamp(row[c.key])}</span>
                            : <span className="text-ge-accent">{fmtCell(row[c.key])}</span>}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="flex flex-wrap items-center gap-2 mt-3">
              <label className="text-[11px] font-mono text-ge-text3 flex items-center gap-1.5">
                Rows/page
                <select value={pageSize} disabled={generating}
                  onChange={e => runReport(1, Number(e.target.value))}
                  className="bg-ge-elevated border border-ge-border rounded px-1.5 py-1
                             text-[11px] text-ge-text1 disabled:opacity-40">
                  {PAGE_SIZES.map(s => <option key={s} value={s}>{s}</option>)}
                </select>
              </label>
              <button onClick={() => runReport(page - 1, pageSize)}
                disabled={page <= 1 || generating}
                className="px-2.5 py-1 text-[11px] font-mono rounded border
                           bg-ge-elevated border-ge-border text-ge-text2
                           hover:text-ge-text1 disabled:opacity-40">◀ Prev</button>
              <button onClick={() => runReport(page + 1, pageSize)}
                disabled={page >= totalPages || generating}
                className="px-2.5 py-1 text-[11px] font-mono rounded border
                           bg-ge-elevated border-ge-border text-ge-text2
                           hover:text-ge-text1 disabled:opacity-40">Next ▶</button>
              <span className="ml-auto text-[11px] font-mono text-ge-text3">
                Page {page.toLocaleString()} of {totalPages.toLocaleString()}
              </span>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
