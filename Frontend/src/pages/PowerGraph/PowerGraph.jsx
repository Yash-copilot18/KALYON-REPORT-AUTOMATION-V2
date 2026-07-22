// src/pages/PowerGraph/PowerGraph.jsx
//
// Power Graph — multi-equipment power time-series for the whole plant.
// Source: the real equipment tables (Inverter / PPC / String Combiner / …) via the
// reports data API (server-side interval aggregation). Each plotted line is an
// (equipment × tag) series; per-equipment aggregated series are fetched with bounded
// concurrency and merged by timestamp on the client. Zoom / pan / reset / fullscreen,
// PNG / CSV / Excel / PDF export, legend toggles, live polling that appends only new
// points. No mock data.
import React, { useState, useEffect, useMemo, useRef, useCallback } from 'react'
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
  ReferenceArea, Brush,
} from 'recharts'
import { PageHeader, FormRow, FormGroup, Spinner, Skeleton } from '../../components/Common'
import { useApp } from '../../utils/AppContext'
import {
  fetchReportEquipmentTypes, fetchReportEquipmentList, fetchTagAvailability, fetchTrendData,
} from '../../services/api'

// dataviz-skill validated categorical palette for the dark surface #141928.
const SERIES_COLORS = [
  '#3987e5', '#199e70', '#c98500', '#008300',
  '#9085e9', '#e66767', '#d55181', '#d95926',
]
const PLOT_CAP   = 8          // max simultaneous lines (never cycle colours)
const RENDER_CAP = 3000       // downsample points for smooth rendering (exports are full)
const FETCH_LIMIT = 6         // concurrent per-equipment fetches
const POLL_MS    = 15000

const INTERVALS = [
  ['1min', '1 Minute'], ['5min', '5 Minutes'], ['15min', '15 Minutes'],
  ['30min', '30 Minutes'], ['hourly', 'Hourly'], ['daily', 'Daily'],
]
const AGGS = [['avg', 'Average'], ['min', 'Minimum'], ['max', 'Maximum']]
// Power tags float to the top of the tag list; everything else stays available.
const POWER_HINT = /POWER|MVAR|MVA|MW\b/i

// ── Helpers ──────────────────────────────────────────────────────────────────
const pad = n => String(n).padStart(2, '0')
const fmtFull = ms => { const d = new Date(ms); return `${pad(d.getDate())}/${pad(d.getMonth() + 1)}/${d.getFullYear()} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}` }
const fmtAxis = ms => { const d = new Date(ms); return `${pad(d.getDate())}/${pad(d.getMonth() + 1)} ${pad(d.getHours())}:${pad(d.getMinutes())}` }
const fmt3 = v => (v === null || v === undefined || v === '' || isNaN(v) ? '—' : Number(v).toFixed(3))
const toISO = d => (d ? new Date(d).toISOString() : '')
const dtLocal = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
const csvEsc = s => (/[",\n]/.test(String(s)) ? `"${String(s).replace(/"/g, '""')}"` : String(s))

function downsample(rows, cap) {
  if (rows.length <= cap) return rows
  const step = rows.length / cap, out = []
  for (let i = 0; i < cap; i++) out.push(rows[Math.floor(i * step)])
  if (out[out.length - 1] !== rows[rows.length - 1]) out.push(rows[rows.length - 1])
  return out
}
function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url; a.download = filename; a.style.display = 'none'
  document.body.appendChild(a); a.click(); document.body.removeChild(a)
  setTimeout(() => URL.revokeObjectURL(url), 1500)
}
// Bounded-concurrency map — keeps 24-inverter fetches from stampeding the API.
async function mapLimit(items, limit, fn, onEach) {
  const out = new Array(items.length); let i = 0
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (i < items.length) {
      const idx = i++
      out[idx] = await fn(items[idx], idx)
      onEach && onEach()
    }
  }))
  return out
}

// ── Equipment multi-select (search + checkboxes) ─────────────────────────────
function EqPicker({ list, selected, setSelected, loading, disabled }) {
  const [q, setQ] = useState('')
  const filtered = useMemo(() => {
    const s = q.trim().toLowerCase()
    return s ? list.filter(e => (e.display_name || e.equipment_id).toLowerCase().includes(s)) : list
  }, [list, q])
  const allSel = list.length > 0 && selected.size === list.length
  if (loading) return <div className="form-control flex items-center gap-2 text-ge-text3 text-[12px]"><Spinner size={12} /> Loading…</div>
  if (disabled) return <div className="form-control text-ge-text3 text-[12px] italic">— single source —</div>
  return (
    <div>
      <div className="flex gap-1.5 mb-1.5">
        <input value={q} onChange={e => setQ(e.target.value)} placeholder="Search equipment…" className="form-control text-[12px] flex-1" />
        <button className="btn btn-outline btn-sm whitespace-nowrap"
          onClick={() => setSelected(allSel ? new Set() : new Set(list.map(e => e.equipment_id)))}>
          {allSel ? 'Clear' : `All (${list.length})`}
        </button>
      </div>
      <div className="bg-ge-elevated border border-ge-border rounded-md max-h-32 overflow-y-auto">
        {filtered.map(e => {
          const on = selected.has(e.equipment_id)
          return (
            <div key={e.equipment_id} onClick={() => setSelected(prev => { const s = new Set(prev); s.has(e.equipment_id) ? s.delete(e.equipment_id) : s.add(e.equipment_id); return s })}
              className="flex items-center gap-2 px-2.5 py-1 text-[12px] cursor-pointer hover:bg-ge-surface border-b border-ge-border last:border-0">
              <span className={`w-3.5 h-3.5 rounded border flex items-center justify-center text-[9px] ${on ? 'bg-ge-blue border-ge-blue text-white' : 'border-ge-border2'}`}>{on ? '✓' : ''}</span>
              <span className="text-ge-text1 font-mono">{e.display_name || e.equipment_id}</span>
            </div>
          )
        })}
      </div>
    </div>
  )
}

// ── Tag multi-select ─────────────────────────────────────────────────────────
function TagPicker({ tags, selected, setSelected, loading }) {
  const [q, setQ] = useState('')
  const filtered = useMemo(() => {
    const s = q.trim().toLowerCase()
    const l = s ? tags.filter(t => t.tag.toLowerCase().includes(s) || t.column_name.toLowerCase().includes(s)) : tags
    return [...l].sort((a, b) => (POWER_HINT.test(b.column_name) ? 1 : 0) - (POWER_HINT.test(a.column_name) ? 1 : 0))
  }, [tags, q])
  if (loading) return <div className="form-control flex items-center gap-2 text-ge-text3 text-[12px]"><Spinner size={12} /> Loading tags…</div>
  return (
    <div>
      <input value={q} onChange={e => setQ(e.target.value)} placeholder="Search tags (power tags first)…" className="form-control text-[12px] mb-1.5" />
      <div className="bg-ge-elevated border border-ge-border rounded-md max-h-32 overflow-y-auto">
        {filtered.length === 0 ? <div className="px-3 py-3 text-[12px] text-ge-text3 text-center">No tags</div> :
          filtered.map(t => {
            const on = selected.has(t.column_name)
            return (
              <div key={t.column_name} onClick={() => setSelected(prev => { const s = new Set(prev); s.has(t.column_name) ? s.delete(t.column_name) : s.add(t.column_name); return s })}
                className="flex items-center gap-2 px-2.5 py-1 text-[12px] cursor-pointer hover:bg-ge-surface border-b border-ge-border last:border-0">
                <span className={`w-3.5 h-3.5 rounded border flex items-center justify-center text-[9px] ${on ? 'bg-ge-accent border-ge-accent text-white' : 'border-ge-border2'}`}>{on ? '✓' : ''}</span>
                <span className={`flex-1 leading-tight ${POWER_HINT.test(t.column_name) ? 'text-ge-accent' : 'text-ge-text1'}`}>{t.tag}</span>
                {t.unit && <span className="text-[10px] text-ge-text3 font-mono">{t.unit}</span>}
              </div>
            )
          })}
      </div>
    </div>
  )
}

function ChartTooltip({ active, payload, label, seriesMeta }) {
  if (!active || !payload?.length) return null
  return (
    <div className="bg-ge-surface border border-ge-border rounded-md px-3 py-2 shadow-lg text-[11px] max-w-xs">
      <div className="text-ge-text3 font-mono mb-1">{fmtFull(label)}</div>
      {payload.map(p => {
        const m = seriesMeta[p.dataKey] || {}
        return (
          <div key={p.dataKey} className="flex items-center gap-2 leading-tight">
            <span className="w-2.5 h-2.5 rounded-sm flex-shrink-0" style={{ background: p.stroke }} />
            <span className="text-ge-text2 flex-1 truncate">{m.eqLabel} · {m.tagLabel}</span>
            <span className="font-mono text-ge-text1">{fmt3(p.value)}</span>
            {m.unit && <span className="text-ge-text3 font-mono">{m.unit}</span>}
          </div>
        )
      })}
    </div>
  )
}

export default function PowerGraph() {
  const { showToast } = useApp()

  const [eqTypes, setEqTypes] = useState([])
  const [eqType, setEqType]   = useState('Inverter')
  const [eqList, setEqList]   = useState([])
  const [loadingEq, setLoadingEq] = useState(false)
  const [selectedEq, setSelectedEq] = useState(new Set())
  const [tagList, setTagList] = useState([])
  const [loadingTags, setLoadingTags] = useState(false)
  const [selectedTags, setSelectedTags] = useState(new Set())

  const now = useMemo(() => new Date(), [])
  const [from, setFrom] = useState(dtLocal(new Date(now.getTime() - 7 * 864e5)))
  const [to, setTo]     = useState(dtLocal(now))
  const [interval, setIntervalV] = useState('hourly')
  const [agg, setAgg]   = useState('avg')

  const [rows, setRows] = useState([])
  const [loading, setLoading] = useState(false)
  const [progress, setProgress] = useState(null)   // { done, total }
  const [error, setError] = useState(null)
  const [hidden, setHidden] = useState(new Set())
  const [live, setLive] = useState(false)
  const [fs, setFs] = useState(false)

  const colorRef = useRef({})
  const [colorMap, setColorMap] = useState({})
  const [brush, setBrush] = useState(null)
  const [refL, setRefL] = useState(null)
  const [refR, setRefR] = useState(null)
  const cardRef = useRef(null)
  const chartWrapRef = useRef(null)

  const eqMetaByType = useMemo(() => { const m = new Map(); eqTypes.forEach(t => m.set(t.equipment_type, t)); return m }, [eqTypes])
  const isSingleSource = useMemo(() => (eqMetaByType.get(eqType)?.equipment_count ?? 1) <= 1, [eqType, eqMetaByType])
  const tagMeta = useMemo(() => { const m = {}; tagList.forEach(t => { m[t.column_name] = t }); return m }, [tagList])
  const eqLabelById = useMemo(() => { const m = {}; eqList.forEach(e => { m[e.equipment_id] = e.display_name || e.equipment_id }); return m }, [eqList])

  // series = equipment × tag (capped). Colours bound to series identity.
  const allSeries = useMemo(() => {
    const out = []
    for (const eqId of selectedEq)
      for (const tag of selectedTags)
        out.push({ key: `${eqId}::${tag}`, eqId, tag, eqLabel: eqLabelById[eqId] || eqId, tagLabel: tagMeta[tag]?.tag || tag, unit: tagMeta[tag]?.unit || '' })
    return out
  }, [selectedEq, selectedTags, eqLabelById, tagMeta])
  const plottedSeries = useMemo(() => allSeries.slice(0, PLOT_CAP), [allSeries])
  const overCap = allSeries.length > PLOT_CAP
  const seriesMeta = useMemo(() => { const m = {}; plottedSeries.forEach(s => { m[s.key] = s }); return m }, [plottedSeries])
  const plottedKey = plottedSeries.map(s => s.key).join(',')

  useEffect(() => {
    const map = {}, taken = new Set()
    plottedSeries.forEach(s => { if (colorRef.current[s.key]) { map[s.key] = colorRef.current[s.key]; taken.add(map[s.key]) } })
    plottedSeries.forEach(s => { if (!map[s.key]) { const c = SERIES_COLORS.find(x => !taken.has(x)) || SERIES_COLORS[0]; map[s.key] = c; taken.add(c) } })
    colorRef.current = map; setColorMap(map)
  }, [plottedKey]) // eslint-disable-line react-hooks/exhaustive-deps

  // Equipment types once.
  useEffect(() => {
    fetchReportEquipmentTypes()
      .then(d => setEqTypes(Array.isArray(d) ? d : (d?.items || [])))
      .catch(() => setEqTypes([]))
  }, [])

  // Equipment list + tags when type changes.
  useEffect(() => {
    if (!eqType) return
    setSelectedEq(new Set()); setSelectedTags(new Set()); setRows([])
    const meta = eqMetaByType.get(eqType)
    if (meta && (meta.equipment_count ?? 1) <= 1) {
      setEqList([]); setSelectedEq(new Set([meta.table_name || eqType])); setLoadingEq(false)
    } else {
      setLoadingEq(true)
      fetchReportEquipmentList(eqType)
        .then(d => { const l = Array.isArray(d) ? d : (d?.items || []); setEqList(l); setSelectedEq(new Set(l.slice(0, 2).map(e => e.equipment_id))) })
        .catch(() => setEqList([]))
        .finally(() => setLoadingEq(false))
    }
  }, [eqType, eqMetaByType])

  const eqIdsKey = useMemo(() => [...selectedEq].sort().join(','), [selectedEq])
  useEffect(() => {
    if (!eqType || selectedEq.size === 0) { setTagList([]); setSelectedTags(new Set()); return }
    setLoadingTags(true)
    fetchTagAvailability(eqType, [...selectedEq])
      .then(d => {
        const t = Array.isArray(d?.tags) ? d.tags : []
        setTagList(t)
        setSelectedTags(prev => {
          const avail = t.filter(x => x.available !== false).map(x => x.column_name)
          const kept = [...prev].filter(c => avail.includes(c))
          if (kept.length) return new Set(kept)
          const power = avail.filter(c => POWER_HINT.test(c)).slice(0, 2)
          return new Set(power.length ? power : avail.slice(0, 1))
        })
      })
      .catch(() => setTagList([]))
      .finally(() => setLoadingTags(false))
  }, [eqType, eqIdsKey]) // eslint-disable-line react-hooks/exhaustive-deps

  const eqPayload = useCallback((eqId, f = from, t = to) => ({
    equipment_type: eqType, equipment_id: eqId, equipment_ids: [eqId],
    tags: [...selectedTags], from_datetime: toISO(f), to_datetime: toISO(t),
    interval, agg_function: agg, page: 1, page_size: 20000,
  }), [eqType, selectedTags, from, to, interval, agg])

  // Fetch each equipment's aggregated series (bounded concurrency) and merge by time.
  const reqIdRef = useRef(0)
  const fetchAll = useCallback(async () => {
    const eqs = [...selectedEq], tags = [...selectedTags]
    if (eqs.length === 0 || tags.length === 0) { setRows([]); return }
    const id = ++reqIdRef.current
    setLoading(true); setError(null); setProgress({ done: 0, total: eqs.length })
    try {
      let done = 0
      const results = await mapLimit(eqs, FETCH_LIMIT,
        (eqId) => fetchTrendData(eqPayload(eqId)).then(d => ({ eqId, rows: d?.rows || [] })).catch(() => ({ eqId, rows: [] })),
        () => { if (id === reqIdRef.current) setProgress({ done: ++done, total: eqs.length }) })
      if (id !== reqIdRef.current) return
      const byTime = new Map()
      for (const { eqId, rows: rr } of results) {
        for (const r of rr) {
          const ms = Date.parse(r.timestamp); if (isNaN(ms)) continue
          let e = byTime.get(ms); if (!e) { e = { _t: ms }; byTime.set(ms, e) }
          for (const tag of tags) { const v = r[tag]; if (v !== null && v !== undefined) e[`${eqId}::${tag}`] = v }
        }
      }
      setRows([...byTime.values()].sort((a, b) => a._t - b._t))
      setBrush(null); setRefL(null); setRefR(null)
    } catch (e) {
      if (id === reqIdRef.current) { setError(e.message || 'Failed to load power data'); setRows([]) }
    } finally {
      if (id === reqIdRef.current) { setLoading(false); setProgress(null) }
    }
  }, [selectedEq, selectedTags, eqPayload])

  useEffect(() => {
    const h = setTimeout(fetchAll, 300)
    return () => clearTimeout(h)
  }, [eqIdsKey, [...selectedTags].sort().join(','), from, to, interval, agg]) // eslint-disable-line react-hooks/exhaustive-deps

  // Live: append only new points after the last timestamp.
  useEffect(() => {
    if (!live || selectedEq.size === 0 || selectedTags.size === 0) return
    const h = setInterval(async () => {
      const last = rows.length ? rows[rows.length - 1]._t : Date.parse(toISO(from))
      const eqs = [...selectedEq], tags = [...selectedTags]
      try {
        const results = await mapLimit(eqs, FETCH_LIMIT, eqId =>
          fetchTrendData(eqPayload(eqId, new Date(last + 1000), new Date())).then(d => ({ eqId, rows: d?.rows || [] })).catch(() => ({ eqId, rows: [] })))
        const add = new Map()
        for (const { eqId, rows: rr } of results) for (const r of rr) {
          const ms = Date.parse(r.timestamp); if (isNaN(ms) || ms <= last) continue
          let e = add.get(ms); if (!e) { e = { _t: ms }; add.set(ms, e) }
          for (const tag of tags) { const v = r[tag]; if (v != null) e[`${eqId}::${tag}`] = v }
        }
        if (add.size) setRows(prev => [...prev, ...[...add.values()].sort((a, b) => a._t - b._t)])
      } catch { /* retry next tick */ }
    }, POLL_MS)
    return () => clearInterval(h)
  }, [live, eqIdsKey, interval, agg, eqPayload]) // eslint-disable-line react-hooks/exhaustive-deps

  const chartData = useMemo(() => downsample(rows, RENDER_CAP), [rows])
  const truncated = rows.length > RENDER_CAP
  const hasData = chartData.length > 0
  const busy = loading || loadingEq || loadingTags

  const toggleLine = k => setHidden(prev => { const s = new Set(prev); s.has(k) ? s.delete(k) : s.add(k); return s })
  const onDown = e => { if (e?.activeTooltipIndex != null) { setRefL(e.activeTooltipIndex); setRefR(e.activeTooltipIndex) } }
  const onMove = e => { if (refL != null && e?.activeTooltipIndex != null) setRefR(e.activeTooltipIndex) }
  const onUp = () => { if (refL != null && refR != null && refL !== refR) { const [a, b] = [refL, refR].sort((x, y) => x - y); setBrush({ startIndex: a, endIndex: b }) } setRefL(null); setRefR(null) }
  const resetZoom = () => { setBrush(null); setRefL(null); setRefR(null) }

  // Fullscreen.
  useEffect(() => {
    const onFs = () => setFs(!!document.fullscreenElement)
    document.addEventListener('fullscreenchange', onFs)
    return () => document.removeEventListener('fullscreenchange', onFs)
  }, [])
  const toggleFs = () => { if (document.fullscreenElement) document.exitFullscreen(); else cardRef.current?.requestFullscreen?.() }

  // ── Exports (from the displayed merged data — matches the chart exactly) ────
  const exportMatrix = () => {
    const cols = plottedSeries
    const header = ['Timestamp (DD/MM/YYYY HH:MM:SS)', ...cols.map(s => `${s.eqLabel} · ${s.tagLabel}${s.unit ? ` (${s.unit})` : ''}`)]
    const body = rows.map(r => [fmtFull(r._t), ...cols.map(s => (r[s.key] == null ? '' : Number(r[s.key]).toFixed(3)))])
    return { header, body }
  }
  const metaLines = () => ([
    ['Project Name', 'Kalyon Solar Power Plant'], ['Report Name', 'Power Graph'],
    ['Equipment Type', eqType], ['Equipment', [...selectedEq].map(e => eqLabelById[e] || e).join('; ')],
    ['Generated Date & Time', fmtFull(Date.now())],
    ['From Date', fmtFull(Date.parse(toISO(from)))], ['To Date', fmtFull(Date.parse(toISO(to)))],
    ['Time Interval', INTERVALS.find(([v]) => v === interval)?.[1] || interval],
    ['Aggregation', AGGS.find(([v]) => v === agg)?.[1] || agg],
  ])
  const exportCSV = () => {
    if (!hasData) return showToast('No data to export', 'warn')
    const { header, body } = exportMatrix()
    const lines = [
      ...metaLines().map(([k, v]) => csvEsc(`${k}: ${v}`)), '',
      header.map(csvEsc).join(','), ...body.map(r => r.map(csvEsc).join(',')),
    ]
    saveBlob(new Blob(['﻿' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8' }), `Power_Graph_${dtLocal(new Date()).slice(0, 10)}.csv`)
    showToast('CSV exported')
  }
  const exportExcel = () => {
    if (!hasData) return showToast('No data to export', 'warn')
    const { header, body } = exportMatrix()
    const esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    const th = header.map(h => `<th style="background:#0f1524;color:#00d4aa;border:1px solid #2a3350;padding:6px 8px;font-family:Calibri;text-align:center">${esc(h)}</th>`).join('')
    const trs = body.map(r => `<tr>${r.map((c, i) => `<td style="border:1px solid #2a3350;padding:4px 8px;font-family:Calibri;text-align:${i === 0 ? 'left' : 'right'}">${esc(c)}</td>`).join('')}</tr>`).join('')
    const metaHtml = metaLines().map(([k, v]) => `<div style="font-family:Calibri;font-size:12px"><b>${esc(k)}:</b> ${esc(v)}</div>`).join('')
    const html = `<html><head><meta charset="utf-8"></head><body>${metaHtml}<br/><table style="border-collapse:collapse;font-size:12px"><thead><tr>${th}</tr></thead><tbody>${trs}</tbody></table></body></html>`
    saveBlob(new Blob([html], { type: 'application/vnd.ms-excel' }), `Power_Graph_${dtLocal(new Date()).slice(0, 10)}.xls`)
    showToast('Excel exported')
  }
  const chartPNGDataURL = () => new Promise((resolve, reject) => {
    const svg = chartWrapRef.current?.querySelector('svg')
    if (!svg) return reject(new Error('no chart'))
    const clone = svg.cloneNode(true)
    const w = svg.clientWidth || 1000, h = svg.clientHeight || 420
    clone.setAttribute('width', w); clone.setAttribute('height', h)
    const xml = new XMLSerializer().serializeToString(clone)
    const img = new Image()
    img.onload = () => {
      const scale = 2, c = document.createElement('canvas')
      c.width = w * scale; c.height = h * scale
      const ctx = c.getContext('2d'); ctx.scale(scale, scale)
      ctx.fillStyle = '#141928'; ctx.fillRect(0, 0, w, h); ctx.drawImage(img, 0, 0, w, h)
      resolve(c.toDataURL('image/png'))
    }
    img.onerror = () => reject(new Error('render'))
    img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(xml)
  })
  const exportPNG = async () => { try { const url = await chartPNGDataURL(); const a = document.createElement('a'); a.href = url; a.download = `Power_Graph_${dtLocal(new Date()).slice(0, 10)}.png`; a.click(); showToast('PNG exported') } catch { showToast('PNG export failed') } }
  const exportPDF = async () => {
    if (!hasData) return showToast('No data to export', 'warn')
    try {
      const url = await chartPNGDataURL()
      const win = window.open('', '_blank')
      if (!win) return showToast('Allow pop-ups to export PDF', 'warn')
      const metaHtml = metaLines().map(([k, v]) => `<div><b>${k}:</b> ${v}</div>`).join('')
      win.document.write(`<html><head><title>Power Graph</title><style>body{font-family:Calibri,Arial;color:#111;margin:24px}h2{margin:0 0 8px}img{width:100%;max-width:1000px;border:1px solid #ccc}.m{font-size:12px;color:#333;margin:2px 0}</style></head><body><h2>Kalyon Solar — Power Graph</h2>${metaHtml.replace(/<div>/g, '<div class="m">')}<br/><img src="${url}"/><script>window.onload=()=>{window.print()}</script></body></html>`)
      win.document.close()
      showToast('PDF export opened')
    } catch { showToast('PDF export failed') }
  }

  return (
    <div>
      <PageHeader title="Power Graph" subtitle="Multi-equipment power time-series · server-side aggregated">
        <label className="flex items-center gap-1.5 text-[11px] text-ge-text2 font-mono cursor-pointer select-none">
          <input type="checkbox" checked={live} onChange={e => setLive(e.target.checked)} /> Live
          {live && <span className="live-chip">● polling</span>}
        </label>
      </PageHeader>

      <div className="card mb-3">
        <div className="card-title">Filters</div>
        <FormRow>
          <FormGroup label="Equipment Type">
            <select value={eqType} onChange={e => setEqType(e.target.value)} className="form-control text-[12px]">
              {eqTypes.map(t => <option key={t.equipment_type} value={t.equipment_type}>{t.equipment_type}</option>)}
            </select>
          </FormGroup>
          <FormGroup label="From"><input type="datetime-local" value={from} onChange={e => setFrom(e.target.value)} className="form-control text-[12px]" /></FormGroup>
          <FormGroup label="To"><input type="datetime-local" value={to} onChange={e => setTo(e.target.value)} className="form-control text-[12px]" /></FormGroup>
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
        <FormRow className="mt-2.5">
          <FormGroup label={`Equipment${selectedEq.size ? ` (${selectedEq.size})` : ''}`}>
            <EqPicker list={eqList} selected={selectedEq} setSelected={setSelectedEq} loading={loadingEq} disabled={isSingleSource} />
          </FormGroup>
          <FormGroup label="Tags">
            <TagPicker tags={tagList} selected={selectedTags} setSelected={setSelectedTags} loading={loadingTags} />
          </FormGroup>
        </FormRow>
        {overCap && (
          <div className="mt-2 text-[11px] text-ge-warn">
            ⚠ {allSeries.length} series selected (equipment × tags). Showing the first {PLOT_CAP} for readability — reduce equipment or tags.
          </div>
        )}
      </div>

      <div ref={cardRef} className={`card ${fs ? 'fixed inset-0 z-50 m-0 rounded-none overflow-auto' : ''}`}>
        <div className="card-title justify-between">
          <span>⚡ Power Graph {rows.length > 0 && <span className="text-[11px] font-mono text-ge-text3 ml-2">{rows.length.toLocaleString()} points{truncated ? ` · showing ${RENDER_CAP.toLocaleString()}` : ''}</span>}</span>
          <div className="flex items-center gap-1.5 flex-wrap">
            <button onClick={resetZoom} disabled={!brush} className="btn btn-outline btn-sm disabled:opacity-40">↺ Reset</button>
            <button onClick={toggleFs} className="btn btn-outline btn-sm">{fs ? '⤢ Exit' : '⛶ Full Screen'}</button>
            <button onClick={exportPNG} disabled={!hasData} className="btn btn-outline btn-sm disabled:opacity-40">🖼 PNG</button>
            <button onClick={exportCSV} disabled={!hasData} className="btn btn-outline btn-sm disabled:opacity-40">📊 CSV</button>
            <button onClick={exportExcel} disabled={!hasData} className="btn btn-outline btn-sm disabled:opacity-40">📗 Excel</button>
            <button onClick={exportPDF} disabled={!hasData} className="btn btn-outline btn-sm disabled:opacity-40">📄 PDF</button>
          </div>
        </div>

        {plottedSeries.length > 0 && (
          <div className="flex flex-wrap gap-2 mb-2">
            {plottedSeries.map(s => (
              <button key={s.key} onClick={() => toggleLine(s.key)}
                className={`flex items-center gap-1.5 px-2 py-0.5 rounded border text-[11px] font-mono transition-all ${hidden.has(s.key) ? 'opacity-40 border-ge-border' : 'border-ge-border2'}`}>
                <span className="w-3 h-1.5 rounded-sm" style={{ background: colorMap[s.key] || '#888' }} />
                <span className={hidden.has(s.key) ? 'line-through text-ge-text3' : 'text-ge-text1'}>{s.eqLabel} · {s.tagLabel}</span>
              </button>
            ))}
          </div>
        )}

        <div ref={chartWrapRef} className="relative" style={{ userSelect: 'none' }}>
          {busy && (
            <div className="absolute inset-0 z-10 flex items-center justify-center bg-ge-card/60 rounded">
              <span className="flex items-center gap-2 text-[12px] text-ge-text2">
                <Spinner size={14} /> {progress ? `Fetching equipment ${progress.done}/${progress.total}…` : 'Loading…'}
              </span>
            </div>
          )}
          {error ? (
            <div className="py-16 text-center"><div className="text-3xl mb-2">⚠</div><div className="text-[13px] text-ge-danger mb-1">{error}</div><button onClick={fetchAll} className="btn btn-outline btn-sm mt-2">Retry</button></div>
          ) : (selectedEq.size === 0 || selectedTags.size === 0) ? (
            <div className="py-16 text-center text-[12px] text-ge-text3">Select equipment and at least one tag to plot the power graph.</div>
          ) : !hasData && !busy ? (
            <div className="py-16 text-center"><div className="text-3xl mb-2">📭</div><div className="text-[13px] text-ge-text2 mb-1">No data available for the selected range.</div><div className="text-[11px] text-ge-text3">Adjust the date range or interval and try again.</div></div>
          ) : busy && !hasData ? (
            <div className="space-y-2 py-4">{[...Array(6)].map((_, i) => <Skeleton key={i} h="h-6" />)}</div>
          ) : (
            <ResponsiveContainer width="100%" height={fs ? Math.round(window.innerHeight * 0.72) : 440}>
              <LineChart data={chartData} onMouseDown={onDown} onMouseMove={onMove} onMouseUp={onUp} margin={{ top: 8, right: 16, bottom: 4, left: 4 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#2c3350" />
                <XAxis dataKey="_t" type="number" scale="time" domain={['dataMin', 'dataMax']} tickFormatter={fmtAxis} tick={{ fill: '#8a93a8', fontSize: 10 }} stroke="#3a4363" minTickGap={40} />
                <YAxis tick={{ fill: '#8a93a8', fontSize: 10 }} stroke="#3a4363" width={60} tickFormatter={v => (Math.abs(v) >= 1000 ? v.toLocaleString() : fmt3(v))} />
                <Tooltip content={<ChartTooltip seriesMeta={seriesMeta} />} />
                {plottedSeries.filter(s => !hidden.has(s.key)).map(s => (
                  <Line key={s.key} type="monotone" dataKey={s.key} stroke={colorMap[s.key] || '#888'} strokeWidth={2} dot={false} connectNulls isAnimationActive={false} />
                ))}
                {refL != null && refR != null && (
                  <ReferenceArea x1={chartData[Math.min(refL, refR)]?._t} x2={chartData[Math.max(refL, refR)]?._t} strokeOpacity={0.3} fill="#3987e5" fillOpacity={0.12} />
                )}
                <Brush dataKey="_t" height={22} stroke="#3a4363" fill="#141928" travellerWidth={8} tickFormatter={fmtAxis}
                  startIndex={brush?.startIndex} endIndex={brush?.endIndex}
                  onChange={r => (r && r.startIndex != null) && setBrush({ startIndex: r.startIndex, endIndex: r.endIndex })} />
              </LineChart>
            </ResponsiveContainer>
          )}
        </div>
        {hasData && <div className="text-[10px] text-ge-text3 mt-2 font-mono">Drag to zoom · drag the bar to pan · Reset to clear. Exports contain the full aggregated dataset.</div>}
      </div>
    </div>
  )
}
