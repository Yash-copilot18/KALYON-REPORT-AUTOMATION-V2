// src/pages/Analytics/Analytics.jsx
//
// Production SCADA analytics dashboard — every KPI, chart and table is computed
// from real SQL Server telemetry via /analytics/overview. One date range drives
// the whole page; an equipment filter scopes the per-inverter ranking.

import React, { useState, useEffect, useCallback, useRef, useMemo } from 'react'
import {
  AreaChart, Area, BarChart, Bar, LineChart, Line, XAxis, YAxis,
  CartesianGrid, Tooltip, ResponsiveContainer, LabelList, Legend,
} from 'recharts'
import { KpiCard, PageHeader, DataTable, Skeleton, Spinner } from '../../components/Common'
import { RECHARTS_COLORS as C } from '../../utils/helpers'
import { fetchAnalyticsOverview, fetchWmsColumns, fetchWmsSeriesRange, fetchReportDataV2 } from '../../services/api'
import { useApp } from '../../utils/AppContext'
import { captureChartPng } from '../../utils/chartCapture'
import { buildReportPdf } from '../../utils/pdfReport'
import { columnValueLabel, rowValueLabel, domainMax, CHART_LABEL_COLOR } from '../../utils/chartValueLabel'

const PLANT_NAME = 'Kalyon Solar Power Plant'
// How long a From/To edit must settle before Analytics refreshes itself. Short enough
// to feel immediate, long enough to coalesce a From-then-To change into one request.
const DATE_REFRESH_MS = 350
// Equipment-type buttons shown beside the Inverters picker. "Inverter" is represented by
// the checkbox multi-select itself (Select All + INVERTER_01…24), so only WMS and PPC
// remain as buttons here.
const EQ_TYPE_BUTTONS = ['WMS', 'PPC']
// Shared compact chart height across Analytics, DGR, MGR and YGR — 192px (h-48),
// ~33–40% shorter than the previous h-72/h-80 so charts fit on one screen.
const COMPACT_CHART_H = 'h-48'
const AXIS = { fill: C.text3, fontSize: 10 }
// Bar-chart axis ticks use the same bright, readable label colour as the bar value labels
// so every value/axis reading is clearly legible on the dark background (client request).
const BAR_AXIS = { fill: CHART_LABEL_COLOR, fontSize: 10 }
const TOOLTIP = { background: '#1a2035', border: '1px solid #2a3350', borderRadius: 6, fontSize: 11 }
const CHART_BG = '#141928'

// Blue-only theme: every KPI card highlight uses the same blue accent (kpi-blue), so the
// whole Analytics page reads as one consistent blue scheme (no green/amber/purple accents).
const KPI_LAYOUT = [
  { key: 'today_energy',  color: 'blue' },
  { key: 'period_energy', color: 'blue' },
  { key: 'avg_pr',        color: 'blue' },
  { key: 'avg_cuf',       color: 'blue' },
  { key: 'availability',  color: 'blue' },
  { key: 'peak_power',    color: 'blue' },
]

const fmt = (v, d = 1) =>
  v === null || v === undefined ? '—' : Number(v).toLocaleString(undefined, { maximumFractionDigits: d })
const num = v => (v === null || v === undefined || v === '' ? null : Number(v))

// ── WMS comparison (Average WMS vs each station) ─────────────────────────────
// The parameters compared across the stations. Each must exist as AVG_<key> + WMS<n>_<key>
// in the WMS schema; any parameter/station absent is skipped, and stations are DISCOVERED
// dynamically (never hardcoded to 1–4), so extra WMS units appear automatically.
const WMS_PARAMS = [
  { key: 'GHI_IRRADIATION', label: 'GHI Irradiance',   unit: 'W/m²' },
  { key: 'GTI_IRRADIATION', label: 'GTI Irradiance',   unit: 'W/m²' },
  { key: 'AIR_TEMP',        label: 'Air Temperature',  unit: '°C'   },
  { key: 'WIND_SPEED',      label: 'Wind Speed',       unit: 'm/s'  },
]
// Blue-only theme (page is blue-only): the primary/average series is the bold base blue; the
// remaining series each get a distinct BLUE shade so every line is clear in the legend.
const CMP_PRIMARY_COLOR = '#0099ff'
const CMP_SHADES = ['#8fd0ff', '#1a7fd1', '#4db3ff', '#063e78', '#2f6fb0', '#b7e2ff', '#0a5aa8']
// Build the line descriptors for a comparison chart: the first series is the bold primary
// (Average WMS / the measured PPC quantity); the rest use distinct blue shades.
const cmpLines = (series) => series.map((s, i) => ({
  key: s.key, label: s.label,
  color: i === 0 ? CMP_PRIMARY_COLOR : CMP_SHADES[(i - 1) % CMP_SHADES.length],
  width: i === 0 ? 2.5 : 1.6,
}))

// PPC comparison groups — each chart compares related PPC controller measurements over the
// selected period. Series columns are the real dbo.PPC tag names; any absent tag is dropped.
const PPC_GROUPS = [
  { key: 'active', title: 'Active Power', unit: 'kW', series: [
    { key: 'GRID_ACTIVE_POWER_MEASURED',  label: 'Grid Active Power' },
    { key: 'INVERTER_TOTAL_ACTIVE_POWER', label: 'Total Active Power' },
    { key: 'ACTIVE_POWER_SET_POINT',      label: 'Active Power Setpoint' },
  ] },
  { key: 'reactive', title: 'Reactive Power', unit: 'kVAR', series: [
    { key: 'GRID_REACTIVE_POWER_MEASURED',  label: 'Grid Reactive Power' },
    { key: 'INVERTER_TOTAL_REACTIVE_POWER', label: 'Total Reactive Power' },
    { key: 'VAR_SET_POINT',                 label: 'VAR Setpoint' },
  ] },
  { key: 'frequency', title: 'Grid Frequency', unit: 'Hz', series: [
    { key: 'GRID_FREQUENCY_MEASURED', label: 'Grid Frequency' },
  ] },
  { key: 'voltage', title: 'Grid Voltage (Phases)', unit: 'V', series: [
    { key: 'VOLTAGE_U_A', label: 'Voltage U-A' },
    { key: 'VOLTAGE_U_B', label: 'Voltage U-B' },
    { key: 'VOLTAGE_U_C', label: 'Voltage U-C' },
  ] },
]
const pad = n => String(n).padStart(2, '0')
const todayName = () => { const d = new Date(); return `${pad(d.getDate())}-${pad(d.getMonth() + 1)}-${d.getFullYear()}` }
const stamp = () => { const d = new Date(); return `${pad(d.getDate())}/${pad(d.getMonth() + 1)}/${d.getFullYear()} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}` }

// ── Reusable chart card with loading / error / empty handling ────────────────
const ChartCard = React.forwardRef(function ChartCard(
  { title, loading, error, empty, height = COMPACT_CHART_H, children }, ref,
) {
  return (
    <div className="card">
      <div className="card-title">{title}</div>
      <div className={height} ref={ref}>
        {loading ? <Skeleton h="h-full" />
          : error ? <div className="h-full flex items-center justify-center text-[12px] text-red-400">Failed to load</div>
          : empty ? <div className="h-full flex items-center justify-center text-[12px] text-ge-text3">No Data Available</div>
          : children}
      </div>
    </div>
  )
})

// ── Performer table (Top / Lowest) ───────────────────────────────────────────
const PERF_COLS = [
  { key: 'rank', label: 'Rank', align: 'left', className: 'font-mono text-[11px]' },
  { key: 'inverter', label: 'Inverter', align: 'left', className: 'font-mono text-[11px]' },
  // Numeric → centred, header follows (req 1 & 2).
  { key: 'generation_kwh', label: 'Generation (kWh)', align: 'center', className: 'font-mono text-[11px]',
    render: v => <span className="text-ge-blue">{fmt(v, 2)}</span> },
  { key: 'pr', label: 'PR (%)', align: 'center', className: 'font-mono text-[11px]',
    render: v => (v == null ? '—' : fmt(v, 1)) },
]

function PerformerTable({ title, rows, loading, error }) {
  return (
    <div className="card">
      <div className="card-title">{title}</div>
      {loading ? (
        <div className="space-y-1">{[...Array(5)].map((_, i) => <div key={i} className="h-8 bg-ge-surface rounded animate-pulse" />)}</div>
      ) : error ? (
        <div className="py-8 text-center text-[12px] text-red-400">Failed to load</div>
      ) : (
        <DataTable columns={PERF_COLS} rows={rows} emptyMsg="No inverter data" />
      )}
    </div>
  )
}

// ── Multi-line comparison chart (shared by WMS and PPC) ──────────────────────
// X = selected date/time period, Y = the group's unit. `lines` is [{key,label,color,width}]
// — the first is the bold primary (Average WMS / measured PPC value), the rest distinct blue
// shades, all listed in the legend. Only the selected equipment's data is used (no mixing).
const CompareChart = React.forwardRef(function CompareChart(
  { title, unit, data, lines, loading, error }, ref,
) {
  return (
    <ChartCard title={title} loading={loading} error={error} empty={!data?.length} ref={ref}>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 8, right: 14, left: 2, bottom: 4 }}>
          <CartesianGrid strokeDasharray="3 3" stroke={C.grid} vertical={false} />
          <XAxis dataKey="t" tick={AXIS} axisLine={false} tickLine={false} minTickGap={28} />
          <YAxis tick={AXIS} axisLine={false} tickLine={false} width={52} />
          <Tooltip contentStyle={TOOLTIP} labelStyle={{ color: C.text3 }}
            formatter={(v, name) => [v == null ? '—' : `${fmt(v)} ${unit}`, name]} />
          <Legend wrapperStyle={{ fontSize: 11 }} />
          {lines.map((l, i) => (
            <Line key={l.key} type="monotone" dataKey={l.key} name={l.label} stroke={l.color}
              strokeWidth={l.width} dot={false} connectNulls
              activeDot={i === 0 ? { r: 4 } : false} />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </ChartCard>
  )
})

// ── Inverter checkbox multi-select ───────────────────────────────────────────
// Replaces the former single-select "Inverters" dropdown (All Inverters / INVERTER_01 …
// INVERTER_24). A compact trigger shows the live selected count and opens a panel with a
// "Select All" checkbox above the per-inverter list, so any combination of inverters can
// be active at once.
//
// The ids come straight from the API's `equipment_options` (the real INVERTER_xx column
// stems) — nothing is hardcoded and no id is renamed, so the database structure and the
// inverter identifiers are untouched. `selected` is a Set owned by the parent; every
// change is reported through `onChange` as a plain array, which is exactly what
// fetchAnalyticsOverview sends as `equipment_ids`.
function InverterMultiSelect({ options, selected, onChange, onOpen, active, disabled }) {
  const [open, setOpen] = useState(false)
  const boxRef = useRef(null)
  const allRef = useRef(null)

  const total = options.length
  const count = selected ? selected.size : total          // not seeded yet → all inverters
  const allSelected = total > 0 && count === total
  const someSelected = count > 0 && count < total

  // Native tri-state checkbox: `indeterminate` is a DOM property, not an attribute, so it
  // has to be set on the element itself whenever the partial state changes (req 6).
  useEffect(() => {
    if (allRef.current) allRef.current.indeterminate = someSelected
  }, [someSelected])  

  // Close on outside click / Escape so the panel behaves like the dropdown it replaces.
  useEffect(() => {
    if (!open) return
    const onDown = e => { if (boxRef.current && !boxRef.current.contains(e.target)) setOpen(false) }
    const onKey = e => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const toggleOne = id => {
    const next = new Set(selected ?? options)
    next.has(id) ? next.delete(id) : next.add(id)
    onChange([...next])
  }
  // Select All mirrors the individual toggles exactly: all → none, anything else → all.
  const toggleAll = () => onChange(allSelected ? [] : [...options])

  const label = count === 0 ? 'None selected'
    : allSelected ? `All Inverters (${total})`
      : `${count} selected`

  return (
    <div className="relative" ref={boxRef}>
      <button type="button" disabled={disabled}
        onClick={() => { onOpen?.(); setOpen(o => !o) }}
        className={`btn btn-sm justify-between gap-2 min-w-[168px] ${active ? 'btn-primary' : 'btn-outline'}`}>
        <span>Inverters · {label}</span>
        <span className="text-[9px] opacity-70">▼</span>
      </button>

      {open && (
        <div className="absolute z-30 mt-1 w-[220px] bg-ge-card border border-ge-border rounded-md shadow-lg overflow-hidden">
          {/* Select All — tri-state (checked / indeterminate / unchecked). */}
          <label className="flex items-center gap-2 px-2.5 py-1.5 text-[12px] cursor-pointer
                            bg-ge-surface border-b border-ge-border hover:bg-ge-elevated">
            <input ref={allRef} type="checkbox" className="accent-ge-blue w-3.5 h-3.5 cursor-pointer"
              checked={allSelected} onChange={toggleAll} />
            <span className="text-ge-text1 font-semibold flex-1">Select All</span>
            <span className="text-[10px] text-ge-text3 font-mono">{count}/{total}</span>
          </label>

          <div className="max-h-56 overflow-y-auto">
            {total === 0 ? (
              <div className="px-3 py-3 text-[12px] text-ge-text3 text-center">No inverters</div>
            ) : options.map(id => (
              <label key={id}
                className="flex items-center gap-2 px-2.5 py-1 text-[12px] cursor-pointer
                           hover:bg-ge-surface border-b border-ge-border last:border-0">
                <input type="checkbox" className="accent-ge-blue w-3.5 h-3.5 cursor-pointer"
                  checked={selected ? selected.has(id) : true}
                  onChange={() => toggleOne(id)} />
                <span className="text-ge-text1 font-mono">{id}</span>
              </label>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

export default function Analytics() {
  const { showToast, reportDateRange } = useApp()
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  // Checkbox multi-select for inverters. `null` = not seeded yet (the very first request,
  // before the inverter list is known) → no `equipment_ids` param → the server's all-
  // inverters view, exactly as before. Once the first response arrives it is seeded with
  // every inverter, after which it is always a Set of the checked ids.
  const [invIds, setInvIds] = useState(null)
  const seeded = useRef(false)
  // Equipment-type selector (moved here alongside the existing filters). Same three
  // types as the report pages. Reuses the shared button styling.
  const [equipmentType, setEquipmentType] = useState('Inverter')
  // The From|To range that has already been requested — see the auto-refresh effect.
  const loadedRangeRef = useRef(null)
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [pdfBusy, setPdfBusy] = useState(false)
  const reqId = useRef(0)

  // WMS comparison state (Average WMS vs stations) — loaded ONLY when Equipment Type = WMS,
  // from the same From/To range. Kept separate from the inverter `data` so neither mixes.
  const [wms, setWms] = useState({ series: {}, stations: [], params: [], loading: false, error: null })
  const wmsColsRef = useRef(null)   // WMS column list (schema), fetched once and cached
  const wmsReq = useRef(0)

  // PPC comparison state — loaded ONLY when Equipment Type = PPC, from the same From/To range.
  const [ppc, setPpc] = useState({ series: {}, groups: [], loading: false, error: null })
  const ppcReq = useRef(0)

  // Chart wrappers, captured for the PDF export.
  const refs = { daily: useRef(null), monthly: useRef(null), top: useRef(null), pr: useRef(null) }
  const wmsRefs = [useRef(null), useRef(null), useRef(null), useRef(null)]
  const ppcRefs = [useRef(null), useRef(null), useRef(null), useRef(null)]

  const load = useCallback(async (opts = {}) => {
    const id = ++reqId.current
    setLoading(true); setError(null)
    try {
      // The checked inverters, as the array fetchAnalyticsOverview turns into
      // `equipment_ids`. `undefined` (only before seeding) omits the param → all
      // inverters; an EMPTY array is sent as an empty value → nothing selected, which the
      // backend answers with empty charts rather than plant totals.
      const sel = opts.equipmentIds ?? invIds
      const reqFrom = opts.from ?? from
      const reqTo   = opts.to ?? to
      // Mark this range as loaded BEFORE awaiting, so the auto-refresh effect below
      // recognises a load that is already in flight and never issues a second one.
      if (reqFrom && reqTo) loadedRangeRef.current = `${reqFrom}|${reqTo}`
      const d = await fetchAnalyticsOverview({
        from: reqFrom,
        to: reqTo,
        equipmentIds: sel ? [...sel] : undefined,
        equipmentType: opts.equipmentType ?? equipmentType,
      })
      if (id !== reqId.current) return
      setData(d)
      // Seed the date inputs from the resolved range on first load. That seeding is what
      // gives `from`/`to` their initial values, so the marker is updated to the resolved
      // range too — otherwise the auto-refresh effect would treat the seeded dates as a
      // user edit and re-fetch the range that was just loaded.
      if (d.range?.from && d.range?.to) loadedRangeRef.current = `${d.range.from}|${d.range.to}`
      if (!from) setFrom(d.range.from)
      if (!to) setTo(d.range.to)
      // Seed the checkbox list from the real inverter ids the API reports, all checked —
      // so the page opens on the same all-inverters view it always has.
      if (!seeded.current) {
        const ids = (d.equipment_options || []).filter(o => o !== 'all')
        if (ids.length) { seeded.current = true; setInvIds(new Set(ids)) }
      }
    } catch (e) {
      if (id !== reqId.current) return
      setError(e.message || 'Failed to load analytics')
    } finally {
      if (id === reqId.current) setLoading(false)
    }
  }, [from, to, invIds, equipmentType])

  // ── Initial load ────────────────────────────────────────────────────────────
  // Skipped when a Reports range is already available: the sync effect below owns the
  // first request in that case, so exactly ONE overview call is made either way.
  useEffect(() => {
    if (reportDateRange?.from && reportDateRange?.to) return
    load() /* initial load with server defaults */
  }, [])  // eslint-disable-line

  // ── Adopt the date range chosen on the Reports page ─────────────────────────
  // Reports publishes its From/To to the shared app context; this mirrors it into the
  // page's own date inputs and refreshes automatically — no Refresh click needed. Only
  // the DATES are taken: equipment type, the inverter checkbox selection and every
  // other filter are left exactly as the user had them.
  //
  // `appliedRangeRef` records the range already applied, so the effect is idempotent:
  // re-renders and a changing `load` identity cannot re-fire it (no loop, no duplicate
  // request), and after syncing the user is free to edit the dates by hand — their edit
  // is not reverted because the ref still holds the synced key. Opening Analytics again
  // remounts the page, so a newer Reports range is picked up on the next visit.
  const appliedRangeRef = useRef(null)
  useEffect(() => {
    const r = reportDateRange
    if (!r?.from || !r?.to) return
    const f = String(r.from).slice(0, 10)      // 'YYYY-MM-DDTHH:mm' → 'YYYY-MM-DD'
    const t = String(r.to).slice(0, 10)        // the format these date inputs use
    if (!f || !t) return
    const key = `${f}|${t}`
    if (appliedRangeRef.current === key) return
    appliedRangeRef.current = key
    setFrom(f); setTo(t)
    load({ from: f, to: t })                   // the existing loader, called once
  }, [reportDateRange, load])

  // WMS comparison loader — Average WMS + each station over the SAME From/To range. Reuses
  // the existing WMS schema + series API (no backend change); the interval is chosen by range
  // length so the chart stays readable. Stations are discovered from the schema (dynamic).
  const loadWmsCompare = useCallback(async ({ from: f, to: t }) => {
    const id = ++wmsReq.current
    setWms(s => ({ ...s, loading: true, error: null }))
    try {
      if (!wmsColsRef.current) {
        const res = await fetchWmsColumns()
        wmsColsRef.current = Array.isArray(res?.columns) ? res.columns : []
      }
      const allCols = wmsColsRef.current
      const stations = [...new Set(allCols
        .map(c => { const m = c.match(/^WMS(\d+)_/); return m ? Number(m[1]) : null })
        .filter(v => v != null))].sort((a, b) => a - b)
      const params = WMS_PARAMS.filter(p => allCols.includes(`AVG_${p.key}`))
      const needed = []
      for (const p of params) {
        needed.push(`AVG_${p.key}`)
        for (const st of stations) {
          const c = `WMS${st}_${p.key}`
          if (allCols.includes(c)) needed.push(c)
        }
      }
      const days = Math.max(1, Math.round((new Date(t) - new Date(f)) / 86400000) + 1)
      const interval = days > 14 ? 'daily' : 'hourly'
      const payload = await fetchWmsSeriesRange(`${f}T00:00:00`, `${t}T23:59:59`, interval, needed)
      if (id !== wmsReq.current) return
      const rows = Array.isArray(payload?.rows) ? payload.rows : []
      const p2 = n => String(n).padStart(2, '0')
      const fmtT = (ts) => {
        const d = new Date(ts)
        if (Number.isNaN(d.getTime())) return String(ts)
        return interval === 'daily'
          ? `${p2(d.getDate())}/${p2(d.getMonth() + 1)}`
          : `${p2(d.getDate())}/${p2(d.getMonth() + 1)} ${p2(d.getHours())}:${p2(d.getMinutes())}`
      }
      const series = {}
      for (const p of params) {
        series[p.key] = rows.map(r => {
          const o = { t: fmtT(r.timestamp), avg: num(r[`AVG_${p.key}`]) }
          for (const st of stations) o[`s${st}`] = num(r[`WMS${st}_${p.key}`])
          return o
        })
      }
      setWms({ series, stations, params, loading: false, error: null })
    } catch (e) {
      if (id !== wmsReq.current) return
      setWms({ series: {}, stations: [], params: [], loading: false, error: e.message || 'Failed to load WMS data' })
    }
  }, [])

  // PPC comparison loader — the PPC controller measurements grouped into comparison charts,
  // over the SAME From/To range. Requests the group columns from dbo.PPC via the reports data
  // endpoint (no backend change); any absent tag is simply dropped from its chart.
  const loadPpcCompare = useCallback(async ({ from: f, to: t }) => {
    const id = ++ppcReq.current
    setPpc(s => ({ ...s, loading: true, error: null }))
    try {
      const needed = [...new Set(PPC_GROUPS.flatMap(g => g.series.map(s => s.key)))]
      const days = Math.max(1, Math.round((new Date(t) - new Date(f)) / 86400000) + 1)
      const interval = days > 14 ? 'daily' : 'hourly'
      const payload = await fetchReportDataV2({
        equipment_type: 'PPC', equipment_id: 'PPC', tags: needed,
        from_datetime: `${f}T00:00:00`, to_datetime: `${t}T23:59:59`,
        interval, agg_function: 'avg', page: 1, page_size: 20000,
      })
      if (id !== ppcReq.current) return
      const rows = Array.isArray(payload?.rows) ? payload.rows : []
      const present = new Set(Array.isArray(payload?.columns) ? payload.columns : needed)
      const p2 = n => String(n).padStart(2, '0')
      const fmtT = (ts) => {
        const d = new Date(ts)
        if (Number.isNaN(d.getTime())) return String(ts)
        return interval === 'daily'
          ? `${p2(d.getDate())}/${p2(d.getMonth() + 1)}`
          : `${p2(d.getDate())}/${p2(d.getMonth() + 1)} ${p2(d.getHours())}:${p2(d.getMinutes())}`
      }
      // Keep only groups/series whose columns are actually present.
      const groups = PPC_GROUPS
        .map(g => ({ ...g, series: g.series.filter(s => present.has(s.key)) }))
        .filter(g => g.series.length)
      const series = {}
      for (const g of groups) {
        series[g.key] = rows.map(r => {
          const o = { t: fmtT(r.timestamp) }
          for (const s of g.series) o[s.key] = num(r[s.key])
          return o
        })
      }
      setPpc({ series, groups, loading: false, error: null })
    } catch (e) {
      if (id !== ppcReq.current) return
      setPpc({ series: {}, groups: [], loading: false, error: e.message || 'Failed to load PPC data' })
    }
  }, [])

  // Load the WMS/PPC comparison whenever that type is selected and a range is set (initial
  // switch + any From/To change). Inverter mode never triggers either.
  useEffect(() => {
    if (equipmentType === 'WMS' && from && to) loadWmsCompare({ from, to })
    if (equipmentType === 'PPC' && from && to) loadPpcCompare({ from, to })
  }, [equipmentType, from, to, loadWmsCompare, loadPpcCompare])

  // The one refresh routine: the Refresh button and the automatic date-change refresh
  // below both call THIS — there is no second implementation. Memoised so the effect can
  // depend on it without rescheduling on every render.
  const onRefresh = useCallback(() => {
    load()
    if (equipmentType === 'WMS' && from && to) loadWmsCompare({ from, to })
    if (equipmentType === 'PPC' && from && to) loadPpcCompare({ from, to })
  }, [load, equipmentType, from, to, loadWmsCompare, loadPpcCompare])

  // ── Automatic refresh when the user edits From Date / To Date ───────────────
  // The date inputs only set state; nothing used to react to that, so the charts kept
  // showing the previous range until Refresh was clicked. This effect closes that gap by
  // calling the SAME `onRefresh`. It reads `from`/`to` from the current render, so the
  // request always carries the value the user just picked — never a stale one.
  //
  // Three guards keep it to exactly one request and no loops:
  //   · `loadedRangeRef` holds the range already loaded (set by `load` itself), so the
  //     initial load, the Reports-range sync and this effect can never double-fetch;
  //   · an incomplete or backwards range (From > To — the state that exists between
  //     editing one end and the other) is ignored until it becomes valid again;
  //   · the short debounce lets a From-then-To edit settle, so changing both fires once
  //     with the final pair rather than once per field.
  useEffect(() => {
    if (!from || !to || from > to) return
    const key = `${from}|${to}`
    if (loadedRangeRef.current === key) return
    const timer = setTimeout(() => onRefresh(), DATE_REFRESH_MS)
    return () => clearTimeout(timer)
  }, [from, to, onRefresh])
  // A checkbox change re-runs the overview immediately for exactly the checked inverters,
  // over the CURRENT From/To range. `load` stamps each request with an id and discards
  // stale responses, so rapid ticking can never render an out-of-order result.
  const onInvIds = (ids) => { setInvIds(new Set(ids)); load({ equipmentIds: ids }) }
  const onEquipmentType = (t) => { setEquipmentType(t); load({ equipmentType: t }) }
  // Opening the inverter picker also switches the page back to the Inverter view (the
  // dropdown it replaces did the same), without a redundant reload if already there.
  const onInvOpen = () => { if (equipmentType !== 'Inverter') onEquipmentType('Inverter') }

  const kpis = data?.kpis || {}
  const daily = data?.daily || []
  const monthly = data?.monthly || []
  const prTrend = data?.pr_trend || []
  const inverters = data?.inverters || []
  // The real inverter ids for the checkbox list ('all' is the legacy sentinel, not an id).
  const inverterOptions = useMemo(
    () => (data?.equipment_options || []).filter(o => o !== 'all'), [data])

  const top10 = useMemo(() => inverters.slice(0, 10).map(i => ({ ...i })), [inverters])
  const topTable = useMemo(() => inverters.slice(0, 5), [inverters])
  const lowTable = useMemo(() => inverters.slice(-5).reverse(), [inverters])

  const rangeLabel = data ? `${data.range.from.split('-').reverse().join('/')} → ${data.range.to.split('-').reverse().join('/')}` : ''

  // ── Export: PDF (KPIs + charts + tables) ───────────────────────────────────
  const exportPdf = async () => {
    if (!data) return
    setPdfBusy(true)
    try {
      const shots = await Promise.all([
        captureChartPng(refs.daily.current, { background: CHART_BG }),
        captureChartPng(refs.monthly.current, { background: CHART_BG }),
        captureChartPng(refs.top.current, { background: CHART_BG }),
        captureChartPng(refs.pr.current, { background: CHART_BG }),
      ])
      const titles = ['Daily Generation Trend (MWh)', 'Monthly Generation Trend (MWh)',
                      'Top 10 Inverters by Generation (kWh)', 'Plant Performance Ratio Trend (%)']
      const charts = shots.map((s, i) => (s ? { ...s, title: titles[i] } : null)).filter(Boolean)
      buildReportPdf({
        filename: `Analytics_Report_${todayName()}.pdf`,
        plant: PLANT_NAME,
        reportTitle: 'Plant Analytics Report',
        subtitle: rangeLabel,
        meta: KPI_LAYOUT.map(({ key }) => ({
          label: kpis[key]?.label || key,
          value: `${fmt(kpis[key]?.value)} ${kpis[key]?.unit || ''}`.trim(),
        })),
        charts,
        tables: [
          { title: 'Top Performing Inverters', columns: pdfPerfCols, rows: topTable },
          { title: 'Lowest Performing Inverters', columns: pdfPerfCols, rows: lowTable },
        ],
      })
      showToast('PDF exported')
    } catch (e) {
      showToast(`PDF export failed: ${e.message || 'unknown error'}`)
    } finally {
      setPdfBusy(false)
    }
  }

  // ── Export: Excel (KPIs + both tables) ─────────────────────────────────────
  const exportExcel = () => {
    if (!data) return
    // Clean white/black corporate theme: light-gray headers, thin black borders,
    // alternating white/#FAFAFA rows, numbers right-aligned. Printer-friendly.
    const esc = s => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    // `center` flag → numeric columns are CENTER-aligned; header takes the same
    // alignment as its column so they line up (req 1 & 2).
    const th = (t, center) => `<th style="background:#F2F2F2;color:#000000;border:1px solid #000000;padding:6px 8px;font-family:Calibri;font-weight:bold;text-align:${center ? 'center' : 'left'}">${esc(t)}</th>`
    const td = (v, center) => `<td style="border:1px solid #000000;padding:4px 8px;font-family:Calibri;color:#000000;text-align:${center ? 'center' : 'left'}">${esc(v)}</td>`
    const tr = (cells, i) => `<tr style="background:${i % 2 === 0 ? '#FFFFFF' : '#FAFAFA'}">${cells}</tr>`
    const kpiRows = KPI_LAYOUT.map(({ key }, i) =>
      tr(`${td(kpis[key]?.label || key)}${td(fmt(kpis[key]?.value), true)}${td(kpis[key]?.unit || '')}`, i)).join('')
    const perfRows = list => list.map((r, i) =>
      tr(`${td(r.rank)}${td(r.inverter)}${td(fmt(r.generation_kwh, 2), true)}${td(r.pr == null ? '—' : fmt(r.pr, 1), true)}`, i)).join('')
    const perfHead = `<tr>${th('Rank')}${th('Inverter')}${th('Generation (kWh)', true)}${th('PR (%)', true)}</tr>`
    const html = `<html><head><meta charset="utf-8"></head><body style="font-family:Calibri;color:#000000">
      <h2 style="color:#000000;font-size:16pt;text-align:left">${esc(PLANT_NAME)} — Plant Analytics Report</h2>
      <p style="color:#000000;text-align:left">Period: ${esc(rangeLabel)}<br/>Generated: ${esc(stamp())}</p>
      <h3 style="color:#000000;text-align:left">Key Performance Indicators</h3>
      <table style="border-collapse:collapse;color:#000000"><tr>${th('Metric')}${th('Value', true)}${th('Unit')}</tr>${kpiRows}</table>
      <h3 style="color:#000000;text-align:left">Top Performing Inverters</h3>
      <table style="border-collapse:collapse;color:#000000">${perfHead}${perfRows(topTable)}</table>
      <h3 style="color:#000000;text-align:left">Lowest Performing Inverters</h3>
      <table style="border-collapse:collapse;color:#000000">${perfHead}${perfRows(lowTable)}</table>
    </body></html>`
    const blob = new Blob([html], { type: 'application/vnd.ms-excel' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url; a.download = `Analytics_Report_${todayName()}.xls`; a.style.display = 'none'
    document.body.appendChild(a); a.click(); document.body.removeChild(a)
    setTimeout(() => URL.revokeObjectURL(url), 1500)
    showToast('Excel exported')
  }

  const busy = loading || pdfBusy

  return (
    <div>
      <PageHeader title="Analytics & Performance">
        <button className="btn btn-outline btn-sm" onClick={exportExcel} disabled={busy || !data}>📗 Excel</button>
        <button className="btn btn-primary btn-sm" onClick={exportPdf} disabled={busy || !data}>
          {pdfBusy ? <><Spinner size={12} /> PDF…</> : '📄 PDF'}
        </button>
      </PageHeader>

      {/* Filters */}
      <div className="card mb-3">
        <div className="flex flex-wrap items-end gap-3">
          <div className="flex flex-col gap-1">
            <label className="form-label">From Date</label>
            <input type="date" className="form-control" value={from} onChange={e => setFrom(e.target.value)} />
          </div>
          <div className="flex flex-col gap-1">
            <label className="form-label">To Date</label>
            <input type="date" className="form-control" value={to} onChange={e => setTo(e.target.value)} />
          </div>
          {/* Equipment Type — the Inverters CHECKBOX multi-select (Select All + INVERTER_01…
              24) sits beside the WMS and PPC buttons under a single "Equipment Type" label.
              It replaces the former single-select dropdown; WMS and PPC are unchanged. */}
          <div className="flex flex-col gap-1">
            <label className="form-label">Equipment Type</label>
            <div className="flex items-center gap-1.5">
              <InverterMultiSelect
                options={inverterOptions} selected={invIds} onChange={onInvIds}
                onOpen={onInvOpen} active={equipmentType === 'Inverter'} />
              {EQ_TYPE_BUTTONS.map(t => (
                <button key={t} type="button" onClick={() => onEquipmentType(t)} disabled={loading}
                  className={`btn btn-sm ${equipmentType === t ? 'btn-primary' : 'btn-outline'}`}>
                  {t}
                </button>
              ))}
            </div>
          </div>
          <button className="btn btn-primary btn-sm" onClick={onRefresh} disabled={loading}>
            {loading ? <><Spinner size={12} /> Loading…</> : '⟳ Refresh'}
          </button>
          {error && <span className="text-[12px] text-red-400 ml-1">{error}</span>}
        </div>
      </div>

      {/* KPI cards removed from the Analytics UI per request. KPI_LAYOUT / kpis are kept —
          the PDF and Excel exports still include the KPI metrics. */}

      {equipmentType === 'WMS' ? (
        /* WMS COMPARISON — Average WMS vs each station over the selected From/To period.
           Replaces the inverter charts & tables; no inverter data is shown in WMS mode. */
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-3 mb-3">
          {(wms.params || []).map((p, i) => (
            <CompareChart key={p.key} ref={wmsRefs[i]}
              title={`WMS ${p.label} — Average vs Stations (${p.unit})`} unit={p.unit}
              data={wms.series?.[p.key] || []}
              lines={cmpLines([{ key: 'avg', label: 'Average WMS' },
                ...wms.stations.map(st => ({ key: `s${st}`, label: `WMS${st}` }))])}
              loading={wms.loading} error={wms.error} />
          ))}
          {!wms.loading && !wms.error && !(wms.params || []).length && (
            <div className="card py-8 text-center text-[12px] text-ge-text3">No WMS data available</div>
          )}
        </div>
      ) : equipmentType === 'PPC' ? (
        /* PPC COMPARISON — the PPC controller's key measurements over the selected From/To
           period. Replaces the inverter charts & tables; no inverter data is shown here. */
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-3 mb-3">
          {(ppc.groups || []).map((g, i) => (
            <CompareChart key={g.key} ref={ppcRefs[i]}
              title={`PPC ${g.title} (${g.unit})`} unit={g.unit}
              data={ppc.series?.[g.key] || []} lines={cmpLines(g.series)}
              loading={ppc.loading} error={ppc.error} />
          ))}
          {!ppc.loading && !ppc.error && !(ppc.groups || []).length && (
            <div className="card py-8 text-center text-[12px] text-ge-text3">No PPC data available</div>
          )}
        </div>
      ) : (
      <>
      {/* Charts row 1 — asymmetric 12-col split so the Monthly card is narrower than the
          Daily card (7/5) while both stay flush with only the normal gap (no empty space). */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-3 mb-3">
        <div className="lg:col-span-7">
        <ChartCard ref={refs.daily} title="Daily Generation Trend (MWh)" loading={loading && !data} error={error} empty={!daily.length}>
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={daily} margin={{ top: 8, right: 14, left: 2, bottom: 4 }}>
              {/* Blue tone (C.blue) — matches the other blue dashboard charts (e.g. Monthly
                  Generation Trend bars). Only the colour changed; data/axes/tooltip unchanged. */}
              <defs>
                <linearGradient id="ana-gen" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={C.blue} stopOpacity={0.45} />
                  <stop offset="100%" stopColor={C.blue} stopOpacity={0.02} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke={C.grid} vertical={false} />
              <XAxis dataKey="label" tick={AXIS} axisLine={false} tickLine={false} minTickGap={28} />
              <YAxis tick={AXIS} axisLine={false} tickLine={false} width={52} />
              <Tooltip contentStyle={TOOLTIP} labelStyle={{ color: C.text3 }}
                formatter={v => [`${fmt(v)} MWh`, 'Generation']} />
              <Area type="monotone" dataKey="generation_mwh" name="Generation" stroke={C.blue}
                strokeWidth={2} fill="url(#ana-gen)" dot={false} activeDot={{ r: 4 }} />
            </AreaChart>
          </ResponsiveContainer>
        </ChartCard>
        </div>

        {/* Compact Monthly card (client demo): narrower 5/12 column + shorter chart height
            + `self-start` so it doesn't stretch to the Daily card's height. Full width on
            mobile (single column) → stays responsive. Data/labels/colours unchanged. */}
        <div className="lg:col-span-5 self-start">
        <ChartCard ref={refs.monthly} title="Monthly Generation Trend (MWh)" height="h-40" loading={loading && !data} error={error} empty={!monthly.length}>
          <ResponsiveContainer width="100%" height="100%">
            {/* Padded top margin + Y domain give the permanent per-bar value labels
                headroom so they never clip. */}
            <BarChart data={monthly} margin={{ top: 10, right: 14, left: 2, bottom: 4 }}>
              <CartesianGrid strokeDasharray="3 3" stroke={C.grid} vertical={false} />
              <XAxis dataKey="label" tick={BAR_AXIS} axisLine={false} tickLine={false} />
              <YAxis tick={BAR_AXIS} axisLine={false} tickLine={false} width={52}
                domain={[0, domainMax(monthly, 'generation_mwh', 1.6)]} />
              <Tooltip cursor={{ fill: 'rgba(255,255,255,0.04)' }} contentStyle={TOOLTIP} labelStyle={{ color: C.text3 }}
                formatter={v => [`${fmt(v)} MWh`, 'Generation']} />
              {/* Permanent value label on every month's bar (UI + captured PDF). */}
              <Bar dataKey="generation_mwh" name="Generation" fill={C.blue} radius={[3, 3, 0, 0]} fillOpacity={0.9}
                isAnimationActive={false}>
                <LabelList dataKey="generation_mwh"
                  content={columnValueLabel({ unit: 'MWh', barCount: monthly.length })} />
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </ChartCard>
        </div>
      </div>

      {/* Charts row 2 */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3 mb-3">
        <ChartCard ref={refs.top} title="Top 10 Inverters by Generation (kWh)" loading={loading && !data} error={error} empty={!top10.length}>
          <ResponsiveContainer width="100%" height="100%">
            {/* Extra right margin + padded value-axis domain leave room for the value
                label printed just past each bar's end (never clipped by the edge). */}
            <BarChart data={top10} layout="vertical" margin={{ top: 4, right: 78, left: 8, bottom: 4 }}>
              <CartesianGrid strokeDasharray="3 3" stroke={C.grid} horizontal={false} />
              <XAxis type="number" tick={BAR_AXIS} axisLine={false} tickLine={false}
                domain={[0, domainMax(top10, 'generation_kwh', 1.28)]}
                tickFormatter={v => (v >= 1000 ? `${Math.round(v / 1000)}k` : v)} />
              <YAxis type="category" dataKey="inverter" tick={BAR_AXIS} axisLine={false} tickLine={false} width={92} />
              <Tooltip cursor={{ fill: 'rgba(255,255,255,0.04)' }} contentStyle={TOOLTIP} labelStyle={{ color: C.text3 }}
                formatter={v => [`${fmt(v, 0)} kWh`, 'Generation']} />
              {/* Permanent value label past every inverter's bar (UI + captured PDF). */}
              {/* Blue-only theme: same blue (C.blue) as the other Analytics charts. */}
              {/* Thinner bars: maxBarSize caps each bar's thickness so they read cleaner; the
                  freed category space becomes the gap between bars. Container size, height,
                  colours, labels, tooltip and data are all unchanged; stays responsive. */}
              <Bar dataKey="generation_kwh" name="Generation" fill={C.blue} radius={[0, 3, 3, 0]} fillOpacity={0.9}
                maxBarSize={12} isAnimationActive={false}>
                <LabelList dataKey="generation_kwh" content={rowValueLabel({ unit: 'kWh' })} />
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </ChartCard>

        <ChartCard ref={refs.pr} title="Plant Performance Ratio Trend (%)" loading={loading && !data} error={error} empty={!prTrend.length}>
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={prTrend} margin={{ top: 8, right: 14, left: 2, bottom: 4 }}>
              <CartesianGrid strokeDasharray="3 3" stroke={C.grid} vertical={false} />
              <XAxis dataKey="label" tick={AXIS} axisLine={false} tickLine={false} minTickGap={28} />
              <YAxis tick={AXIS} axisLine={false} tickLine={false} width={44} domain={[0, 100]} />
              <Tooltip contentStyle={TOOLTIP} labelStyle={{ color: C.text3 }}
                formatter={v => [v == null ? '—' : `${fmt(v)} %`, 'PR']} />
              {/* Blue-only theme: PR line uses the same blue (C.blue) as the other charts. */}
              <Line type="monotone" dataKey="pr" name="PR" stroke={C.blue} strokeWidth={2}
                dot={false} activeDot={{ r: 4 }} connectNulls={false} />
            </LineChart>
          </ResponsiveContainer>
        </ChartCard>
      </div>

      {/* Tables */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
        <PerformerTable title="Top Performing Inverters" rows={topTable} loading={loading && !data} error={error} />
        <PerformerTable title="Lowest Performing Inverters" rows={lowTable} loading={loading && !data} error={error} />
      </div>
      </>
      )}
    </div>
  )
}

// PDF table columns (align/format for the shared pdf builder).
const pdfPerfCols = [
  { header: 'Rank', dataKey: 'rank', align: 'left' },
  { header: 'Inverter', dataKey: 'inverter', align: 'left' },
  { header: 'Generation (kWh)', dataKey: 'generation_kwh', align: 'center', format: v => fmt(v, 2) },
  { header: 'PR (%)', dataKey: 'pr', align: 'center', format: v => (v == null ? '—' : fmt(v, 1)) },
]
