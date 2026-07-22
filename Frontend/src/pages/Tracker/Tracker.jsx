// src/pages/Tracker/Tracker.jsx
import React, { useState, useEffect, useMemo, useRef, useCallback } from 'react'
import { PageHeader, Spinner, Skeleton } from '../../components/Common'
import { useApp } from '../../utils/AppContext'
import {
  fetchTrackerIds, fetchTrackerData, createTrackerExportJob,
  downloadExcelExportJob, exportProgressUrl,
} from '../../services/api'

// ── Helpers ──────────────────────────────────────────────────────────────────
const RANGE_SIZE = 20
const pad2  = (n) => String(n).padStart(2, '0')
const toISO = (s) => (s && s.length === 16 ? `${s}:00` : s)

const PARAM_LABELS = {
  ALARM: 'Alarm', BATTERY_LEVEL: 'Battery Level',
  ELEVATION_POSITION: 'Elev. Position', ELEVATION_SETPOINT: 'Elev. Setpoint',
  MAX_MOTOR_CURRENT: 'Max Motor Current', OPERATION_MODE: 'Operation Mode',
}

function columnLabel(col) {
  if (col === 'timestamp') return 'Timestamp'
  const m = /^(.+)_ID(\d+)$/.exec(col)
  if (!m) return col
  return `T${m[2]} · ${PARAM_LABELS[m[1]] || m[1]}`
}

function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url; a.download = filename
  document.body.appendChild(a); a.click(); a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1500)
}

// ── Tracker selector ─────────────────────────────────────────────────────────
function TrackerSelector({ ids, selected, setSelected }) {
  const [search, setSearch] = useState('')

  const ranges = useMemo(() => {
    const out = []
    for (let start = 1; start <= ids.length; start += RANGE_SIZE) {
      const end = Math.min(start + RANGE_SIZE - 1, ids.length)
      out.push([ids[start - 1], ids[end - 1]])
    }
    return out
  }, [ids])

  const filtered = useMemo(() => {
    const q = search.trim()
    if (!q) return ids
    return ids.filter(id => String(id).includes(q))
  }, [ids, search])

  const allSelected = ids.length > 0 && selected.size === ids.length

  const toggle = (id) => setSelected(prev => {
    const s = new Set(prev); s.has(id) ? s.delete(id) : s.add(id); return s
  })
  const selectAll = () => setSelected(allSelected ? new Set() : new Set(ids))
  const clearAll  = () => setSelected(new Set())

  const toggleRange = ([a, b]) => setSelected(prev => {
    const inRange = ids.filter(id => id >= a && id <= b)
    const allIn = inRange.every(id => prev.has(id))
    const s = new Set(prev)
    inRange.forEach(id => (allIn ? s.delete(id) : s.add(id)))
    return s
  })

  return (
    <div>
      <div className="flex items-center gap-2 mb-2.5 flex-wrap">
        <button className="btn btn-outline btn-sm" onClick={selectAll}>
          {allSelected ? '✕ Deselect All' : `✓ Select All (${ids.length})`}
        </button>
        <button className="btn btn-outline btn-sm" onClick={clearAll}>Clear</button>
        <div className="relative flex-1 min-w-[160px]">
          <span className="absolute left-2.5 top-1/2 -translate-y-1/2 text-ge-text3 text-sm">🔍</span>
          <input type="text" value={search} onChange={e => setSearch(e.target.value)}
            placeholder="Search tracker #…" className="form-control pl-7 text-[12px]" />
        </div>
        <span className="text-[11px] font-mono text-ge-accent">{selected.size} selected</span>
      </div>

      {/* Range quick-selects */}
      <div className="flex items-center gap-1.5 mb-2.5 flex-wrap">
        <span className="text-[10px] text-ge-text3 uppercase tracking-widest mr-1">Ranges</span>
        {ranges.map(([a, b]) => {
          const inRange = ids.filter(id => id >= a && id <= b)
          const allIn = inRange.length > 0 && inRange.every(id => selected.has(id))
          return (
            <button key={`${a}-${b}`} onClick={() => toggleRange([a, b])}
              className={`px-2 py-1 text-[11px] font-mono rounded border transition-all ${
                allIn ? 'bg-ge-blue text-white border-ge-blue'
                      : 'bg-ge-elevated border-ge-border text-ge-text2 hover:text-ge-text1'}`}>
              {a}-{b}
            </button>
          )
        })}
      </div>

      {/* Tracker grid */}
      <div className="bg-ge-elevated border border-ge-border rounded-md p-2 max-h-56 overflow-y-auto">
        {filtered.length === 0 ? (
          <div className="text-[12px] text-ge-text3 text-center py-4">No trackers match “{search}”</div>
        ) : (
          <div className="grid grid-cols-8 sm:grid-cols-10 lg:grid-cols-12 gap-1.5">
            {filtered.map(id => {
              const on = selected.has(id)
              return (
                <button key={id} onClick={() => toggle(id)}
                  title={`Tracker ${id}`}
                  className={`h-7 rounded text-[11px] font-mono border transition-all ${
                    on ? 'bg-ge-blue text-white border-ge-blue'
                       : 'bg-ge-surface border-ge-border text-ge-text3 hover:text-ge-text1 hover:border-ge-border2'}`}>
                  {pad2(id)}
                </button>
              )
            })}
          </div>
        )}
      </div>
    </div>
  )
}

// ── Page ─────────────────────────────────────────────────────────────────────
export default function Tracker() {
  const { showToast } = useApp()

  const [meta, setMeta]         = useState({ tracker_ids: [], params: [], count: 0 })
  const [loadingMeta, setLoadingMeta] = useState(true)
  const [selected, setSelected] = useState(new Set())

  const [fromDate, setFromDate] = useState('2024-05-20T00:00')
  const [toDate,   setToDate]   = useState('2024-05-21T23:59')
  const [interval, setInterval] = useState('hourly')
  const [agg,      setAgg]       = useState('avg')

  const [result, setResult]   = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError]     = useState(null)

  const [exportJob, setExportJob] = useState(null)  // { pct, message, status }
  const esRef = useRef(null)

  useEffect(() => {
    setLoadingMeta(true)
    fetchTrackerIds()
      .then(d => setMeta(d))
      .catch(() => setError('Failed to load tracker list'))
      .finally(() => setLoadingMeta(false))
  }, [])

  useEffect(() => () => { if (esRef.current) esRef.current.close() }, [])

  const buildPayload = useCallback((extra = {}) => ({
    tracker_ids:   [...selected].sort((a, b) => a - b),
    from_datetime: toISO(fromDate),
    to_datetime:   toISO(toDate),
    interval,
    agg_function:  agg,
    ...extra,
  }), [selected, fromDate, toDate, interval, agg])

  const handleLoad = useCallback(async () => {
    if (selected.size === 0) return showToast('Select at least one tracker')
    setLoading(true); setError(null); setResult(null)
    try {
      const data = await fetchTrackerData(buildPayload({ page: 1, page_size: 100 }))
      setResult(data)
      showToast('Loaded successfully')
    } catch (e) {
      setError(e.message || 'Failed to load tracker data')
    } finally {
      setLoading(false)
    }
  }, [selected, buildPayload, showToast])

  const handleExport = useCallback(async () => {
    if (selected.size === 0) return showToast('Select at least one tracker')
    setExportJob({ pct: 0, message: 'Creating export job…', status: 'running' })
    try {
      const { job_id } = await createTrackerExportJob(buildPayload())
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
            saveBlob(blob, d.filename || `Trackers_${selected.size}_Report.xlsx`)
            showToast('Export downloaded')
          } catch (e) {
            showToast(`Export failed: ${e.message}`)
          } finally { setExportJob(null) }
        } else if (d.status === 'error') {
          es.close(); esRef.current = null
          showToast(`Export failed: ${d.message || 'unknown error'}`)
          setExportJob(null)
        }
      }
      es.onerror = () => {
        es.close(); esRef.current = null
        showToast('Export progress connection lost'); setExportJob(null)
      }
    } catch (e) {
      showToast(`Export failed: ${e.message}`); setExportJob(null)
    }
  }, [selected, buildPayload, showToast])

  const columns = result?.columns || []
  const rows    = result?.rows || []

  return (
    <div>
      <PageHeader title="Tracker Reports"
        subtitle={`dbo.T1_IS2 · ${meta.count} trackers · 6 parameters each`}>
        <div className="live-chip">
          <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse-slow" />
          {meta.count} TRACKERS
        </div>
      </PageHeader>

      {/* Selection + filters */}
      <div className="card mb-3">
        <div className="card-title">🛰 Select Trackers</div>
        {loadingMeta ? (
          <Skeleton h="h-40" />
        ) : (
          <TrackerSelector ids={meta.tracker_ids} selected={selected} setSelected={setSelected} />
        )}

        <div className="flex gap-2.5 flex-wrap items-end mt-3 pt-3 border-t border-ge-border">
          <div className="flex flex-col gap-1">
            <label className="form-label">From</label>
            <input type="datetime-local" value={fromDate} onChange={e => setFromDate(e.target.value)}
              className="form-control text-[12px]" />
          </div>
          <div className="flex flex-col gap-1">
            <label className="form-label">To</label>
            <input type="datetime-local" value={toDate} onChange={e => setToDate(e.target.value)}
              className="form-control text-[12px]" />
          </div>
          <div className="flex flex-col gap-1">
            <label className="form-label">Interval</label>
            <select value={interval} onChange={e => setInterval(e.target.value)}
              className="form-control text-[12px]">
              <option value="raw">Raw</option>
              <option value="15min">15 Minutes</option>
              <option value="30min">30 Minutes</option>
              <option value="hourly">Hourly</option>
              <option value="daily">Daily</option>
            </select>
          </div>
          <div className="flex flex-col gap-1">
            <label className="form-label">Aggregation</label>
            <select value={agg} onChange={e => setAgg(e.target.value)}
              className="form-control text-[12px]" disabled={interval === 'raw'}>
              <option value="avg">Average</option>
              <option value="min">Minimum</option>
              <option value="max">Maximum</option>
              <option value="sum">Sum</option>
            </select>
          </div>

          <div className="flex gap-2 ml-auto">
            <button className="btn btn-primary btn-sm" onClick={handleLoad}
              disabled={loading || selected.size === 0}>
              {loading ? <><Spinner size={12} /> Loading…</> : `📥 Load Data (${selected.size})`}
            </button>
            <button className="btn btn-success btn-sm" onClick={handleExport}
              disabled={!!exportJob || selected.size === 0}>
              📊 Export Excel
            </button>
          </div>
        </div>

        {/* Export progress */}
        {exportJob && (
          <div className="mt-3 pt-3 border-t border-ge-border">
            <div className="flex items-center gap-2 mb-1.5">
              <Spinner size={12} />
              <span className="text-[12px] text-ge-text1">{exportJob.message}</span>
              <span className="ml-auto text-[11px] font-mono text-ge-accent">{exportJob.pct}%</span>
            </div>
            <div className="h-1.5 bg-ge-navy rounded overflow-hidden">
              <div className="h-full bg-ge-accent rounded transition-all duration-300"
                style={{ width: `${exportJob.pct}%` }} />
            </div>
            <div className="text-[10px] text-ge-text3 mt-1.5 text-center">
              One worksheet per tracker — the UI stays responsive while generating.
            </div>
          </div>
        )}
      </div>

      {/* Preview */}
      <div className="card">
        <div className="card-title justify-between">
          <span>📋 Tracker Data Preview</span>
          {result && <span className="text-[11px] font-mono text-ge-text3">
            {rows.length} rows · {result.tracker_ids?.length || 0} trackers
          </span>}
        </div>

        {error && (
          <div className="mb-3 bg-ge-danger/5 border border-ge-danger/40 rounded px-3 py-2">
            <p className="text-[12px] text-ge-danger">⚠ {error}</p>
          </div>
        )}

        {loading ? (
          <div className="space-y-1">{[...Array(6)].map((_, i) => <Skeleton key={i} h="h-8" />)}</div>
        ) : !result ? (
          <div className="py-16 text-center">
            <div className="text-4xl mb-3">🛰</div>
            <div className="text-[13px] text-ge-text3 mb-1 font-medium">No data loaded yet</div>
            <div className="text-[11px] text-ge-text3">
              Select trackers → date range → Load Data, or Export Excel for a full workbook.
            </div>
          </div>
        ) : rows.length === 0 ? (
          <div className="py-12 text-center text-[12px] text-ge-text3">
            No records found for the selected trackers and date range.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="data-table">
              <thead>
                <tr>{columns.map(c => <th key={c} className="whitespace-nowrap">{columnLabel(c)}</th>)}</tr>
              </thead>
              <tbody>
                {rows.map((row, i) => (
                  <tr key={i}>
                    {columns.map(c => {
                      const v = row[c]
                      return (
                        <td key={c} className="whitespace-nowrap font-mono text-[11px]">
                          {v === null || v === undefined ? '—'
                            : typeof v === 'number' ? v.toLocaleString(undefined, { maximumFractionDigits: 3 })
                            : String(v)}
                        </td>
                      )
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
