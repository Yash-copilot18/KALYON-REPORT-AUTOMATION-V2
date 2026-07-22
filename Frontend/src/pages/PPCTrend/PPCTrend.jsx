// src/pages/PPCTrend/PPCTrend.jsx
//
// PPC Trend — real-time + historical time-series for the PPC plant controller.
// Source: dbo.PPC via the reports data API (server-side interval aggregation).
// Multi-tag chart, colour-per-tag legend toggles, drag-zoom + brush-pan + reset,
// rich tooltip, CSV/Excel/PNG export, live polling that only appends new points.
import React, { useState, useEffect, useMemo, useRef, useCallback } from 'react'
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
  ReferenceArea, Brush,
} from 'recharts'
import { PageHeader, FormRow, FormGroup, Spinner, Skeleton } from '../../components/Common'
import { useApp } from '../../utils/AppContext'
import {
  fetchTagAvailability, fetchTrendData, exportReportCSVV2, exportReportExcelV2,
} from '../../services/api'

// Categorical palette validated (dataviz skill) against the app's dark surface
// #141928 — colours are bound to a tag's identity, never its position, so toggling
// one line never repaints the others.
const SERIES_COLORS = [
  '#3987e5', '#199e70', '#c98500', '#008300',
  '#9085e9', '#e66767', '#d55181', '#d95926',
]
const PLOT_CAP    = 8       // max simultaneous lines (never cycle colours)
const RENDER_CAP  = 3000    // downsample point count for smooth rendering (export is full)
const POLL_MS     = 15000   // live-append interval

const INTERVALS = [
  ['1min', '1 Minute'], ['5min', '5 Minutes'], ['15min', '15 Minutes'],
  ['30min', '30 Minutes'], ['hourly', 'Hourly'], ['daily', 'Daily'], ['monthly', 'Monthly'],
]
const AGGS = [['avg', 'Average'], ['min', 'Minimum'], ['max', 'Maximum'], ['sum', 'Sum']]

const EQUIP = 'PPC'

// ── Helpers ──────────────────────────────────────────────────────────────────
const pad = n => String(n).padStart(2, '0')
function fmtFull(ms) {
  if (ms == null) return ''
  const d = new Date(ms)
  return `${pad(d.getDate())}/${pad(d.getMonth() + 1)}/${d.getFullYear()} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}
function fmtAxis(ms) {
  const d = new Date(ms)
  return `${pad(d.getDate())}/${pad(d.getMonth() + 1)} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}
const fmt3 = v => (v === null || v === undefined || v === '' || isNaN(v) ? '—' : Number(v).toFixed(3))
const toISO = d => (d ? new Date(d).toISOString() : '')
// datetime-local default value (YYYY-MM-DDTHH:mm) from a Date.
function dtLocal(d) {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}
// Even downsample keeping first + last (rendering only — exports re-query full data).
function downsample(rows, cap) {
  if (rows.length <= cap) return rows
  const step = rows.length / cap
  const out = []
  for (let i = 0; i < cap; i++) out.push(rows[Math.floor(i * step)])
  const last = rows[rows.length - 1]
  if (out[out.length - 1] !== last) out.push(last)
  return out
}
function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url; a.download = filename; a.style.display = 'none'
  document.body.appendChild(a); a.click(); document.body.removeChild(a)
  setTimeout(() => URL.revokeObjectURL(url), 1500)
}

// ── Tag picker (search + checkbox list, capped at PLOT_CAP) ───────────────────
function TagPicker({ tags, selected, setSelected, loading }) {
  const [q, setQ] = useState('')
  const filtered = useMemo(() => {
    const s = q.trim().toLowerCase()
    return s ? tags.filter(t => t.tag.toLowerCase().includes(s) || t.column_name.toLowerCase().includes(s)) : tags
  }, [tags, q])
  const toggle = (col) => setSelected(prev => {
    const s = new Set(prev)
    if (s.has(col)) s.delete(col)
    else if (s.size < PLOT_CAP) s.add(col)
    return s
  })
  if (loading) return <div className="form-control flex items-center gap-2 text-ge-text3 text-[12px]"><Spinner size={12} /> Loading tags…</div>
  return (
    <div>
      <input value={q} onChange={e => setQ(e.target.value)} placeholder="Search tags…"
        className="form-control text-[12px] mb-1.5" />
      <div className="bg-ge-elevated border border-ge-border rounded-md max-h-40 overflow-y-auto">
        {filtered.length === 0
          ? <div className="px-3 py-3 text-[12px] text-ge-text3 text-center">No tags</div>
          : filtered.map(t => {
            const on = selected.has(t.column_name)
            const disabled = !on && selected.size >= PLOT_CAP
            return (
              <div key={t.column_name} onClick={() => !disabled && toggle(t.column_name)}
                className={`flex items-center gap-2 px-2.5 py-1.5 text-[12px] border-b border-ge-border last:border-0
                  ${disabled ? 'opacity-40 cursor-not-allowed' : 'cursor-pointer hover:bg-ge-surface'}`}>
                <span className={`w-3.5 h-3.5 rounded border flex items-center justify-center text-[9px]
                  ${on ? 'bg-ge-blue border-ge-blue text-white' : 'border-ge-border2'}`}>{on ? '✓' : ''}</span>
                <span className="text-ge-text1 flex-1 leading-tight">{t.tag}</span>
                {t.unit && <span className="text-[10px] text-ge-text3 font-mono">{t.unit}</span>}
              </div>
            )
          })}
      </div>
      <div className="text-[10px] text-ge-text3 mt-1 font-mono">
        {selected.size}/{PLOT_CAP} tags{selected.size >= PLOT_CAP ? ' (max)' : ''}
      </div>
    </div>
  )
}

// ── Custom tooltip ───────────────────────────────────────────────────────────
function ChartTooltip({ active, payload, label, tagMeta }) {
  if (!active || !payload?.length) return null
  return (
    <div className="bg-ge-surface border border-ge-border rounded-md px-3 py-2 shadow-lg text-[11px]">
      <div className="text-ge-text3 font-mono mb-1">{fmtFull(label)}</div>
      {payload.map(p => (
        <div key={p.dataKey} className="flex items-center gap-2 leading-tight">
          <span className="w-2.5 h-2.5 rounded-sm" style={{ background: p.stroke }} />
          <span className="text-ge-text2 flex-1">{tagMeta[p.dataKey]?.tag || p.dataKey}</span>
          <span className="font-mono text-ge-text1">{fmt3(p.value)}</span>
          {tagMeta[p.dataKey]?.unit && <span className="text-ge-text3 font-mono">{tagMeta[p.dataKey].unit}</span>}
        </div>
      ))}
    </div>
  )
}

export default function PPCTrend() {
  const { showToast } = useApp()

  const [tagList, setTagList]   = useState([])
  const [loadingTags, setLoadingTags] = useState(true)
  const [selected, setSelected] = useState(new Set())
  const now = useMemo(() => new Date(), [])
  const [from, setFrom] = useState(dtLocal(new Date(now.getTime() - 7 * 864e5)))
  const [to, setTo]     = useState(dtLocal(now))
  const [interval, setIntervalV] = useState('hourly')
  const [agg, setAgg]   = useState('avg')

  const [rows, setRows]   = useState([])          // full aggregated series (numeric _t + tag values)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [hidden, setHidden] = useState(new Set()) // legend-toggled-off tags
  const [live, setLive]   = useState(false)
  const [exporting, setExporting] = useState('')

  const colorRef  = useRef({})                    // stable tag → colour
  const [colorMap, setColorMap] = useState({})
  const [brush, setBrush] = useState(null)        // { startIndex, endIndex }
  const [refL, setRefL] = useState(null)          // drag-zoom anchors (indices)
  const [refR, setRefR] = useState(null)
  const chartWrapRef = useRef(null)

  const tagMeta = useMemo(() => {
    const m = {}; tagList.forEach(t => { m[t.column_name] = t }); return m
  }, [tagList])
  const plotted = useMemo(() => [...selected], [selected])
  const plottedKey = plotted.join(',')

  // Stable colour assignment — survivors keep their colour when others toggle.
  useEffect(() => {
    const map = {}, taken = new Set()
    plotted.forEach(t => { if (colorRef.current[t]) { map[t] = colorRef.current[t]; taken.add(map[t]) } })
    plotted.forEach(t => { if (!map[t]) { const c = SERIES_COLORS.find(x => !taken.has(x)) || SERIES_COLORS[0]; map[t] = c; taken.add(c) } })
    colorRef.current = map
    setColorMap(map)
  }, [plottedKey]) // eslint-disable-line react-hooks/exhaustive-deps

  // Load PPC tags once.
  useEffect(() => {
    setLoadingTags(true)
    fetchTagAvailability(EQUIP, [EQUIP])
      .then(d => {
        const t = Array.isArray(d?.tags) ? d.tags : []
        setTagList(t)
        setSelected(new Set(t.filter(x => x.available !== false).slice(0, 3).map(x => x.column_name)))
      })
      .catch(() => { setTagList([]); setError('Failed to load PPC tags') })
      .finally(() => setLoadingTags(false))
  }, [])

  const buildPayload = useCallback((f = from, t = to) => ({
    equipment_type: EQUIP, equipment_id: EQUIP, equipment_ids: [EQUIP],
    tags: plotted, from_datetime: toISO(f), to_datetime: toISO(t),
    interval, agg_function: agg, page: 1, page_size: 20000,
  }), [plotted, from, to, interval, agg])

  const toRow = (r) => ({ ...r, _t: Date.parse(r.timestamp) })

  // Fetch trend whenever filters change (auto-update).
  const reqIdRef = useRef(0)
  const fetchTrend = useCallback(async () => {
    if (plotted.length === 0) { setRows([]); return }
    const id = ++reqIdRef.current
    setLoading(true); setError(null)
    try {
      const data = await fetchTrendData(buildPayload())
      if (id !== reqIdRef.current) return         // a newer request superseded this
      const out = (data?.rows || []).map(toRow).filter(r => !isNaN(r._t))
      setRows(out)
      setBrush(null); setRefL(null); setRefR(null)
    } catch (e) {
      if (id === reqIdRef.current) { setError(e.message || 'Failed to load trend'); setRows([]) }
    } finally {
      if (id === reqIdRef.current) setLoading(false)
    }
  }, [plotted, buildPayload])

  useEffect(() => {
    const h = setTimeout(fetchTrend, 250)         // debounce filter changes
    return () => clearTimeout(h)
  }, [plottedKey, from, to, interval, agg]) // eslint-disable-line react-hooks/exhaustive-deps

  // Live polling — fetch only points AFTER the last timestamp and append (req 16).
  useEffect(() => {
    if (!live || plotted.length === 0) return
    const tick = async () => {
      const last = rows.length ? rows[rows.length - 1]._t : null
      const fromT = last ? new Date(last + 1000) : new Date(Date.parse(toISO(from)))
      try {
        const data = await fetchTrendData(buildPayload(fromT, new Date()))
        const fresh = (data?.rows || []).map(toRow).filter(r => !isNaN(r._t) && (!last || r._t > last))
        if (fresh.length) setRows(prev => [...prev, ...fresh])
      } catch { /* transient — next tick retries */ }
    }
    const h = setInterval(tick, POLL_MS)
    return () => clearInterval(h)
  }, [live, plottedKey, interval, agg, buildPayload]) // eslint-disable-line react-hooks/exhaustive-deps

  // Render data (downsampled for smoothness; exports use full data via the API).
  const chartData = useMemo(() => downsample(rows, RENDER_CAP), [rows])
  const truncated = rows.length > RENDER_CAP

  const toggleLine = (tag) => setHidden(prev => {
    const s = new Set(prev); s.has(tag) ? s.delete(tag) : s.add(tag); return s
  })

  // Drag-zoom (select an X range on the plot → window the brush to those indices).
  const onDown = (e) => { if (e && e.activeTooltipIndex != null) { setRefL(e.activeTooltipIndex); setRefR(e.activeTooltipIndex) } }
  const onMove = (e) => { if (refL != null && e && e.activeTooltipIndex != null) setRefR(e.activeTooltipIndex) }
  const onUp = () => {
    if (refL != null && refR != null && refL !== refR) {
      const [a, b] = [refL, refR].sort((x, y) => x - y)
      setBrush({ startIndex: a, endIndex: b })
    }
    setRefL(null); setRefR(null)
  }
  const resetZoom = () => { setBrush(null); setRefL(null); setRefR(null) }

  // ── Exports ──────────────────────────────────────────────────────────────
  const doExport = async (kind) => {
    if (plotted.length === 0) return showToast('Select at least one tag', 'warn')
    setExporting(kind)
    try {
      const fn = kind === 'CSV' ? exportReportCSVV2 : exportReportExcelV2
      const blob = await fn({ ...buildPayload(), page_size: 100000 })
      if (!(blob instanceof Blob) || blob.size === 0) throw new Error('Empty file')
      const ext = kind === 'CSV' ? 'csv' : 'xlsx'
      saveBlob(blob, `PPC_Trend_${dtLocal(new Date()).slice(0, 10)}.${ext}`)
      showToast(`${kind} exported`)
    } catch (e) {
      showToast(`${kind} export failed: ${e.message}`)
    } finally { setExporting('') }
  }

  const exportPNG = () => {
    const svg = chartWrapRef.current?.querySelector('svg')
    if (!svg) return showToast('Nothing to export', 'warn')
    try {
      const clone = svg.cloneNode(true)
      const w = svg.clientWidth || 1000, h = svg.clientHeight || 420
      clone.setAttribute('width', w); clone.setAttribute('height', h)
      const xml = new XMLSerializer().serializeToString(clone)
      const img = new Image()
      img.onload = () => {
        const scale = 2, canvas = document.createElement('canvas')
        canvas.width = w * scale; canvas.height = h * scale
        const ctx = canvas.getContext('2d')
        ctx.scale(scale, scale)
        ctx.fillStyle = '#141928'; ctx.fillRect(0, 0, w, h)   // dark surface bg
        ctx.drawImage(img, 0, 0, w, h)
        canvas.toBlob(b => b && saveBlob(b, `PPC_Trend_${dtLocal(new Date()).slice(0, 10)}.png`), 'image/png')
      }
      img.onerror = () => showToast('PNG export failed')
      img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(xml)
    } catch { showToast('PNG export failed') }
  }

  const hasData = chartData.length > 0
  const busy = loading || loadingTags

  return (
    <div>
      <PageHeader title="PPC Trend" subtitle="dbo.PPC · server-side aggregated time-series">
        <label className="flex items-center gap-1.5 text-[11px] text-ge-text2 font-mono cursor-pointer select-none">
          <input type="checkbox" checked={live} onChange={e => setLive(e.target.checked)} />
          Live
          {live && <span className="live-chip">● polling</span>}
        </label>
      </PageHeader>

      {/* Filters */}
      <div className="card mb-3">
        <div className="card-title">Filters</div>
        <FormRow>
          <FormGroup label="Equipment">
            <div className="form-control flex items-center gap-2 text-[12px] text-ge-text2">
              <span className="text-ge-accent">🎯</span><span className="font-mono">PPC</span>
              <span className="ml-auto text-[10px] text-ge-text3 uppercase tracking-widest">Fixed source</span>
            </div>
          </FormGroup>
          <FormGroup label="From">
            <input type="datetime-local" value={from} onChange={e => setFrom(e.target.value)} className="form-control text-[12px]" />
          </FormGroup>
          <FormGroup label="To">
            <input type="datetime-local" value={to} onChange={e => setTo(e.target.value)} className="form-control text-[12px]" />
          </FormGroup>
          <FormGroup label="Time Interval">
            <select value={interval} onChange={e => setIntervalV(e.target.value)} className="form-control text-[12px]">
              {INTERVALS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
          </FormGroup>
          <FormGroup label="Aggregation">
            <select value={agg} onChange={e => setAgg(e.target.value)} className="form-control text-[12px]">
              {AGGS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
          </FormGroup>
        </FormRow>
        <div className="mt-2.5">
          <label className="form-label">Tags (up to {PLOT_CAP})</label>
          <TagPicker tags={tagList} selected={selected} setSelected={setSelected} loading={loadingTags} />
        </div>
      </div>

      {/* Chart */}
      <div className="card">
        <div className="card-title justify-between">
          <span>📈 Trend {rows.length > 0 && <span className="text-[11px] font-mono text-ge-text3 ml-2">{rows.length.toLocaleString()} points{truncated ? ` · showing ${RENDER_CAP.toLocaleString()}` : ''}</span>}</span>
          <div className="flex items-center gap-1.5">
            <button onClick={resetZoom} disabled={!brush} className="btn btn-outline btn-sm disabled:opacity-40">↺ Reset Zoom</button>
            <button onClick={exportPNG} disabled={!hasData} className="btn btn-outline btn-sm disabled:opacity-40">🖼 PNG</button>
            <button onClick={() => doExport('CSV')} disabled={!!exporting || plotted.length === 0} className="btn btn-outline btn-sm disabled:opacity-40">
              {exporting === 'CSV' ? <><Spinner size={12} /> …</> : '📊 CSV'}</button>
            <button onClick={() => doExport('Excel')} disabled={!!exporting || plotted.length === 0} className="btn btn-outline btn-sm disabled:opacity-40">
              {exporting === 'Excel' ? <><Spinner size={12} /> …</> : '📗 Excel'}</button>
          </div>
        </div>

        {/* Legend — click to toggle lines */}
        {plotted.length > 0 && (
          <div className="flex flex-wrap gap-2 mb-2">
            {plotted.map(tag => (
              <button key={tag} onClick={() => toggleLine(tag)}
                className={`flex items-center gap-1.5 px-2 py-0.5 rounded border text-[11px] font-mono transition-all
                  ${hidden.has(tag) ? 'opacity-40 border-ge-border' : 'border-ge-border2'}`}>
                <span className="w-3 h-1.5 rounded-sm" style={{ background: colorMap[tag] || '#888' }} />
                <span className={hidden.has(tag) ? 'line-through text-ge-text3' : 'text-ge-text1'}>{tagMeta[tag]?.tag || tag}</span>
              </button>
            ))}
          </div>
        )}

        <div ref={chartWrapRef} className="relative" style={{ userSelect: 'none' }}>
          {busy && (
            <div className="absolute inset-0 z-10 flex items-center justify-center bg-ge-card/60 rounded">
              <span className="flex items-center gap-2 text-[12px] text-ge-text2"><Spinner size={14} /> Loading trend…</span>
            </div>
          )}

          {error ? (
            <div className="py-16 text-center">
              <div className="text-3xl mb-2">⚠</div>
              <div className="text-[13px] text-ge-danger mb-1">{error}</div>
              <button onClick={fetchTrend} className="btn btn-outline btn-sm mt-2">Retry</button>
            </div>
          ) : plotted.length === 0 ? (
            <div className="py-16 text-center text-[12px] text-ge-text3">Select one or more tags to plot a trend.</div>
          ) : !hasData && !busy ? (
            <div className="py-16 text-center">
              <div className="text-3xl mb-2">📭</div>
              <div className="text-[13px] text-ge-text2 mb-1">No data available for the selected range.</div>
              <div className="text-[11px] text-ge-text3">Adjust the date range or interval and try again.</div>
            </div>
          ) : busy && !hasData ? (
            <div className="space-y-2 py-4">{[...Array(6)].map((_, i) => <Skeleton key={i} h="h-6" />)}</div>
          ) : (
            <ResponsiveContainer width="100%" height={420}>
              <LineChart data={chartData} onMouseDown={onDown} onMouseMove={onMove} onMouseUp={onUp}
                margin={{ top: 8, right: 16, bottom: 4, left: 4 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#2c3350" />
                <XAxis dataKey="_t" type="number" scale="time" domain={['dataMin', 'dataMax']}
                  tickFormatter={fmtAxis} tick={{ fill: '#8a93a8', fontSize: 10 }} stroke="#3a4363"
                  minTickGap={40} />
                <YAxis tick={{ fill: '#8a93a8', fontSize: 10 }} stroke="#3a4363" width={56}
                  tickFormatter={v => (Math.abs(v) >= 1000 ? v.toLocaleString() : fmt3(v))} />
                <Tooltip content={<ChartTooltip tagMeta={tagMeta} />} />
                {plotted.filter(t => !hidden.has(t)).map(tag => (
                  <Line key={tag} type="monotone" dataKey={tag} stroke={colorMap[tag] || '#888'}
                    strokeWidth={2} dot={false} connectNulls isAnimationActive={false} />
                ))}
                {refL != null && refR != null && (
                  <ReferenceArea x1={chartData[Math.min(refL, refR)]?._t} x2={chartData[Math.max(refL, refR)]?._t}
                    strokeOpacity={0.3} fill="#3987e5" fillOpacity={0.12} />
                )}
                <Brush dataKey="_t" height={22} stroke="#3a4363" fill="#141928"
                  travellerWidth={8} tickFormatter={fmtAxis}
                  startIndex={brush?.startIndex} endIndex={brush?.endIndex}
                  onChange={r => (r && r.startIndex != null) && setBrush({ startIndex: r.startIndex, endIndex: r.endIndex })} />
              </LineChart>
            </ResponsiveContainer>
          )}
        </div>
        {hasData && (
          <div className="text-[10px] text-ge-text3 mt-2 font-mono">
            Drag on the chart to zoom · drag the bar below to pan · Reset Zoom to clear. Exports contain the full aggregated dataset.
          </div>
        )}
      </div>
    </div>
  )
}
