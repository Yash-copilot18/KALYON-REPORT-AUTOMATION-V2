// src/pages/DGRReports/DGRReports.jsx
import React, { useState, useMemo, useEffect, useCallback, useRef } from 'react'
import {
  BarChart, Bar, XAxis, YAxis, Treemap,
  CartesianGrid, Tooltip, ResponsiveContainer,
} from 'recharts'
import { KpiCard, PageHeader, DataTable, Spinner } from '../../components/Common'
import { useApp } from '../../utils/AppContext'
import { RECHARTS_COLORS as C } from '../../utils/helpers'
import {
  fetchDGRSeries, DGR_TAGS, fetchMGRMonthlyGeneration,
  fetchDGRInverterSeries, fetchDGRInverterPower, fetchReportTags,
  fetchYGRYears, fetchYGRYearlyGeneration, fetchMGRPeriods,
} from '../../services/api'
import { INTERVAL_LABELS } from '../../utils/intervals'
import { captureChartPng } from '../../utils/chartCapture'
import { buildReportPdf } from '../../utils/pdfReport'

// Corporate header shown on every exported PDF (DGR / MGR / YGR).
const PLANT_NAME = 'KALYON NIGDE 130 MW'

// Shared compact chart height across Analytics, DGR, MGR and YGR — 192px (h-48),
// ~33–40% shorter than the previous h-72/h-80 so charts fit on one screen.
const COMPACT_CHART_H = 'h-48'

// ── Constants ───────────────────────────────────────────────────────────────
const MONTHS       = ['January','February','March','April','May','June',
                      'July','August','September','October','November','December']
const MONTHS_SHORT = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']

// Intra-day intervals only — a daily/monthly bucket is meaningless for a single date.
// Labels come from the shared interval module so "1 Minute (Instant Data)" reads the
// same here as on every other screen.
const DGR_INTERVALS = ['1min', '5min', '15min', '30min', 'hourly']

// ── Helpers ─────────────────────────────────────────────────────────────────
const pad = n => String(n).padStart(2, '0')
const fmt3 = v => (v === null || v === undefined || v === '' ? '' : Number(v).toFixed(3))
const fmt2 = v => (v === null || v === undefined || v === '' ? '' : Number(v).toFixed(2))
const todayDMY = () => {
  const d = new Date()
  return `${pad(d.getDate())}-${pad(d.getMonth() + 1)}-${d.getFullYear()}`
}

// Downsample a series for charts so 1-min data stays responsive.
function downsample(arr, max = 160) {
  if (arr.length <= max) return arr
  const k = Math.ceil(arr.length / max)
  return arr.filter((_, i) => i % k === 0)
}

// ── DGR: real PPC plant-meter telemetry → report rows ────────────────────────
// PLANT_DAILY_PRODUCTION is a cumulative MWh counter that resets at midnight, so
// the energy generated inside a bucket is the rise of that counter across it.
// The counter starts the day at 0, hence the first bucket's delta is its own value.
// Energy is reported in kWh (the meter stores MWh) to match the rest of the app.
const num = v => (v === null || v === undefined || v === '' ? null : Number(v))
const MWH_TO_KWH = 1000

function toDailyRows(payload) {
  const raw = Array.isArray(payload?.rows) ? payload.rows : []
  let prev = 0
  return raw.map(r => {
    const ts  = new Date(r.timestamp)
    const cum = num(r[DGR_TAGS.energy])
    let generation = null
    if (cum !== null) {
      generation = Math.max(0, cum - prev) * MWH_TO_KWH   // guard against a counter reset
      prev = cum
    }
    return {
      time:       Number.isNaN(ts.getTime()) ? String(r.timestamp) : `${pad(ts.getHours())}:${pad(ts.getMinutes())}`,
      generation,
      peak:       num(r[DGR_TAGS.power]),
      cumulative: cum === null ? null : cum * MWH_TO_KWH,
    }
  })
}

// ── DGR: inverter-wise generation for the treemap ────────────────────────────
// Sources, read over the SAME date + interval as the report table:
//   [dbo].[INVERTER_DAILY_GEN] — INVERTER_xx_GEN, daily-resetting cumulative kWh
//     counters, so generation is that counter's rise (the identical delta rule the
//     plant meter uses in toDailyRows above).
//   [dbo].[POWER_GRAPH]        — INVERTER_xx_ACTIVE_POWER in kW, for peak power.
const GEN_RE = /^INVERTER_(\d+)_GEN$/i

const PWR_RE = /^INVERTER_(\d+)_ACTIVE_POWER$/i

// 'INVERTER_07_GEN' → 'INV7' (the label each treemap rectangle carries).
const invLabel = col => `INV${parseInt(col.match(GEN_RE)[1], 10)}`

/**
 * Per-inverter totals for the treemap. Both values are read from the database —
 * nothing is derived or estimated.
 *
 *  generation (kWh) — sum of the INVERTER_DAILY_GEN counter's rises across the day.
 *  peak       (MW)  — highest bucket reading of INVERTER_xx_ACTIVE_POWER (kW → MW),
 *                     or null when POWER_GRAPH returned no value for that inverter.
 *                     A null peak is omitted from the tooltip, never filled in.
 */
function toInverterTotals(genPayload, powerPayload) {
  const raw  = Array.isArray(genPayload?.rows) ? genPayload.rows : []
  const cols = (Array.isArray(genPayload?.columns) ? genPayload.columns : []).filter(c => GEN_RE.test(c))
  if (!raw.length || !cols.length) return []

  // Cumulative kWh counter → total generated over the day, per inverter.
  const prev = {}
  const total = {}
  raw.forEach(r => {
    cols.forEach(c => {
      const cum = num(r[c])
      if (cum === null) return
      total[c] = (total[c] || 0) + Math.max(0, cum - (prev[c] ?? 0))   // guard a reset
      prev[c] = cum
    })
  })

  // Peak active power per inverter number, from POWER_GRAPH (kW).
  const pRows = Array.isArray(powerPayload?.rows) ? powerPayload.rows : []
  const pCols = (Array.isArray(powerPayload?.columns) ? powerPayload.columns : [])
    .filter(c => PWR_RE.test(c))
  const peakByNo = {}
  pCols.forEach(c => {
    const no = parseInt(c.match(PWR_RE)[1], 10)
    let mx = null
    pRows.forEach(r => {
      const v = num(r[c])
      if (v !== null && (mx === null || v > mx)) mx = v
    })
    if (mx !== null) peakByNo[no] = mx / 1000        // kW → MW
  })

  return cols
    .map(c => {
      const no = parseInt(c.match(GEN_RE)[1], 10)
      const kwh = total[c] || 0
      return {
        name:       invLabel(c),
        generation: kwh,
        peak:       peakByNo[no] ?? null,
      }
    })
    // Drop inverters that generated nothing (e.g. a spare unit) — a zero-area
    // rectangle cannot be rendered and would only clutter the treemap.
    .filter(d => d.generation > 0)
    .sort((a, b) => b.generation - a.generation)
}

// ── Report definitions (columns drive table + every export) ─────────────────
const REPORTS = {
  DGR: {
    title: 'Daily Generation Report',
    // Every column is a real PPC plant-meter value (or its exact delta).
    columns: [
      { key: 'time',       label: 'Time',                        kind: 'text' },
      { key: 'generation', label: 'Generation (kWh)',            kind: 'num'  },
      { key: 'peak',       label: 'Peak Power (MW)',             kind: 'num'  },
      { key: 'cumulative', label: 'Cumulative Production (kWh)', kind: 'num'  },
    ],
  },
  MGR: {
    title: 'Monthly Generation Report',
    // Plant totals per day, summed from the real per-inverter kWh counters.
    columns: [
      { key: 'date',       label: 'Date',             kind: 'date' },
      { key: 'generation', label: 'Generation (kWh)', kind: 'num'  },
    ],
  },
  YGR: {
    title: 'Yearly Generation Report',
    columns: [
      { key: 'month',      label: 'Month',            kind: 'text' },
      { key: 'generation', label: 'Generation (MWh)', kind: 'num'  },
      { key: 'pr',         label: 'PR (%)',           kind: 'num'  },
      { key: 'cuf',        label: 'CUF (%)',          kind: 'num'  },
      { key: 'peak',       label: 'Peak Power (MW)',  kind: 'num'  },
    ],
  },
}

// ── Export utilities (export ONLY the currently selected report) ─────────────
const formatCell = (v, kind) => {
  if (v === null || v === undefined) return ''
  return kind === 'num' ? fmt3(v) : String(v)
}
function buildMatrix(type, rows) {
  const cols   = REPORTS[type].columns
  const header = cols.map(c => c.label)
  const body   = rows.map(r => cols.map(c => formatCell(r[c.key], c.kind)))
  return { cols, header, body }
}
function downloadBlob(blob, filename) {
  // Anchor must be in the DOM; revoking the blob URL synchronously after click()
  // aborts the download in most browsers — defer it.
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.style.display = 'none'
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  setTimeout(() => URL.revokeObjectURL(url), 1500)
}
// Standard app-wide CSV metadata block: "DD/MM/YYYY HH:mm:ss" generated stamp.
function csvGeneratedStamp() {
  const d = new Date(), p = n => String(n).padStart(2, '0')
  return `${p(d.getDate())}/${p(d.getMonth() + 1)}/${d.getFullYear()} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}
function exportCSV(type, rows, filename) {
  const { header, body } = buildMatrix(type, rows)
  const esc = s => (/[",\n]/.test(s) ? `"${String(s).replace(/"/g, '""')}"` : s)
  // Same professional format as the backend CSVs: metadata block, one blank row,
  // header, then data. BOM + CRLF so Excel opens aligned columns. Each metadata
  // line is a SINGLE cell ("Label: Value") so Excel overflows it into the empty
  // cells beside it — fully readable with no manual column resizing.
  const meta = [
    ['Project Name', 'Kalyon Solar Power Plant'],
    ['Report Name', REPORTS[type].title],
    ['Generated Date & Time', csvGeneratedStamp()],
  ]
  const lines = [
    ...meta.map(([k, v]) => esc(`${k}: ${v}`)),
    '',
    header.map(esc).join(','),
    ...body.map(r => r.map(esc).join(',')),
  ]
  const csv = '﻿' + lines.join('\r\n')
  downloadBlob(new Blob([csv], { type: 'text/csv;charset=utf-8' }), `${filename}.csv`)
}
const escHtml = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
// Clean white/black corporate theme: light-gray header, thin black borders,
// alternating white/#FAFAFA rows, numbers right-aligned. Printer-friendly.
function tableHTML(type, rows) {
  const { cols, header, body } = buildMatrix(type, rows)
  const th = header.map(h =>
    `<th style="background:#F2F2F2;color:#000000;border:1px solid #000000;padding:6px 8px;font-family:Calibri,Arial;font-weight:bold;text-align:center">${escHtml(h)}</th>`
  ).join('')
  const trs = body.map((r, ri) =>
    `<tr style="background:${ri % 2 === 0 ? '#FFFFFF' : '#FAFAFA'}">${r.map((cell, ci) =>
      `<td style="border:1px solid #000000;padding:4px 8px;font-family:Calibri,Arial;color:#000000;text-align:${cols[ci].kind === 'num' ? 'right' : 'left'}">${escHtml(cell)}</td>`
    ).join('')}</tr>`
  ).join('')
  return `<table style="border-collapse:collapse;font-size:12px;color:#000000"><thead><tr>${th}</tr></thead><tbody>${trs}</tbody></table>`
}
function exportExcel(type, rows, filename) {
  const html = `<html><head><meta charset="utf-8"></head><body>${tableHTML(type, rows)}</body></html>`
  downloadBlob(new Blob([html], { type: 'application/vnd.ms-excel' }), `${filename}.xls`)
}

// ── Mini chart components (theme-consistent, recharts) ───────────────────────
const AXIS = { fill: C.text3, fontSize: 10 }
const TOOLTIP_STYLE = {
  background: '#1a2035', border: '1px solid #2a3350', borderRadius: 6, fontSize: 11,
}
// Axis titles sit in muted ink, never the series colour.
const axisLabel = (value, angle = 0) => ({
  value, angle,
  position: angle ? 'insideLeft' : 'insideBottom',
  offset: angle ? 10 : -4,
  style: { fill: C.text3, fontSize: 10, textAnchor: 'middle' },
})

function MiniBar({ data, xKey, yKey, name, color = C.blue, unit = '' }) {
  return (
    <ResponsiveContainer width="100%" height="100%">
      {/* Left margin must stay >= 0: a negative margin pulls the Y axis into the
          clipped region and shears the leading digits off large generation values.
          `width` reserves room for the widest tick (7-digit kWh totals), which is
          Recharts' equivalent of ECharts' grid.left + containLabel. */}
      <BarChart data={data} margin={{ top: 4, right: 10, left: 4, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke={C.grid} vertical={false} />
        <XAxis dataKey={xKey} tick={AXIS} axisLine={false} tickLine={false} minTickGap={16} />
        <YAxis tick={AXIS} axisLine={false} tickLine={false} width={76} tickMargin={4} />
        <Tooltip cursor={{ fill: 'rgba(255,255,255,0.04)' }}
          contentStyle={{ background: '#1a2035', border: '1px solid #2a3350', borderRadius: 6, fontSize: 11 }}
          labelStyle={{ color: '#6b7a99' }} formatter={v => [`${Number(v).toFixed(2)}${unit}`, name]} />
        <Bar dataKey={yKey} name={name} fill={color} radius={[3, 3, 0, 0]} fillOpacity={0.85} />
      </BarChart>
    </ResponsiveContainer>
  )
}
// ── Inverter-wise generation treemap (DGR) ───────────────────────────────────
// One rectangle per inverter, area proportional to the kWh it generated on the
// selected date. Colour intensity tracks the same value, so the biggest producers
// read as the brightest blocks.
const fmtKwh = v => Number(v || 0).toLocaleString(undefined, { maximumFractionDigits: 1 })
const fmtMw  = v => (v === null || v === undefined ? '—' : `${Number(v).toFixed(2)} MW`)

// Accent hue, lightness ramped 24% → 58% with the inverter's share of the best
// performer, so "brighter = more generation" is readable at a glance.
const treemapFill = (value, maxValue) => {
  const t = maxValue > 0 ? Math.min(Math.max(value / maxValue, 0), 1) : 0
  return `hsl(168, 72%, ${Math.round(24 + t * 34)}%)`
}

// Only fields the database actually returned are shown. Peak Power comes from
// [dbo].[POWER_GRAPH]; when that query returns nothing for an inverter the row is
// omitted entirely rather than rendered as a placeholder or a derived estimate.
function TreemapTooltip({ active, payload }) {
  if (!active || !payload?.length) return null
  const d = payload[0]?.payload
  if (!d?.name) return null
  return (
    <div style={{ ...TOOLTIP_STYLE, padding: '7px 10px', lineHeight: 1.6 }}>
      <div style={{ color: C.accent, fontWeight: 600, marginBottom: 2 }}>{d.name}</div>
      <div style={{ color: C.text3 }}>
        Total Generation: <span style={{ color: '#dbe3f0' }}>{fmtKwh(d.generation)} kWh</span>
      </div>
      {d.peak !== null && d.peak !== undefined && (
        <div style={{ color: C.text3 }}>
          Peak Power: <span style={{ color: '#dbe3f0' }}>{fmtMw(d.peak)}</span>
        </div>
      )}
    </div>
  )
}

// Rectangle renderer: fill by intensity, and print the inverter name + kWh when
// the block is big enough for the text to actually fit.
function TreemapCell(props) {
  const { x, y, width, height, name, generation, maxValue } = props
  if (!name || width <= 0 || height <= 0) return null
  const showName  = width > 46 && height > 24
  const showValue = width > 62 && height > 38
  return (
    <g>
      <rect
        x={x} y={y} width={width} height={height}
        style={{
          fill: treemapFill(generation, maxValue),
          stroke: '#0d1220', strokeWidth: 2, cursor: 'pointer',
        }}
      />
      {showName && (
        <text x={x + width / 2} y={y + height / 2 + (showValue ? -4 : 4)}
          textAnchor="middle" fill="#0d1220" fontSize={11} fontWeight={700}>
          {name}
        </text>
      )}
      {showValue && (
        <text x={x + width / 2} y={y + height / 2 + 12}
          textAnchor="middle" fill="#0d1220" fontSize={10} fontFamily="monospace">
          {fmtKwh(generation)} kWh
        </text>
      )}
    </g>
  )
}

function InverterTreemap({ data }) {
  const maxValue = useMemo(
    () => data.reduce((a, d) => Math.max(a, d.generation || 0), 0), [data])
  // maxValue rides along on each node so the cell renderer can scale its colour.
  const nodes = useMemo(
    () => data.map(d => ({ ...d, maxValue })), [data, maxValue])

  return (
    <ResponsiveContainer width="100%" height="100%">
      <Treemap
        data={nodes} dataKey="generation" nameKey="name"
        stroke="#0d1220" isAnimationActive={false}
        content={<TreemapCell />}
      >
        <Tooltip content={<TreemapTooltip />} />
      </Treemap>
    </ResponsiveContainer>
  )
}

const ChartCard = ({ title, height = 'h-56', children }) => (
  <div className="card">
    <div className="card-title">{title}</div>
    <div className={height}>{children}</div>
  </div>
)
// Shared loading / error / empty handling for every chart on the page.
const ChartBody = ({ loading, error, empty, emptyMsg = 'No Data Available', children }) => {
  if (loading) return (
    <div className="h-full flex items-center justify-center text-[12px] text-ge-text3">
      <Spinner size={14} /> <span className="ml-2">Loading plant data…</span>
    </div>
  )
  if (error) return (
    <div className="h-full flex items-center justify-center text-[12px] text-red-400">{error}</div>
  )
  if (empty) return (
    <div className="h-full flex items-center justify-center text-[12px] text-ge-text3">{emptyMsg}</div>
  )
  return children
}

// ── MGR: per-inverter monthly generation summary ─────────────────────────────
// Rows come straight from [dbo].[INVERTER_DAILY_GEN], aggregated in SQL.
const INVERTER_SUMMARY_COLS = [
  { key: 'inverter', label: 'Inverter', className: 'font-mono text-[11px]' },
  {
    key: 'generation', label: 'Monthly Generation (kWh)',
    className: 'font-mono text-[11px] text-right',
    render: v => <span className="text-ge-accent">{fmt2(v)}</span>,
  },
]

const InverterGenerationTable = React.memo(function InverterGenerationTable({
  rows, loading, error, monthLabel,
}) {
  return (
    <div className="card">
      <div className="card-title justify-between">
        <span>Monthly Generation by Inverter — {monthLabel}</span>
        {!loading && !error && rows.length > 0 && (
          <span className="text-[11px] font-mono text-ge-text3">{rows.length} inverters</span>
        )}
      </div>
      {loading ? (
        <div className="space-y-1">
          {[...Array(6)].map((_, i) => <div key={i} className="h-8 bg-ge-surface rounded animate-pulse" />)}
        </div>
      ) : error ? (
        <div className="py-8 text-center text-[12px] text-red-400">{error}</div>
      ) : (
        <DataTable
          columns={INVERTER_SUMMARY_COLS}
          rows={rows}
          emptyMsg="No inverter generation for the selected month"
        />
      )}
    </div>
  )
})

// ── Table column adapters for DataTable (3-dp formatting) ────────────────────
function tableColumns(type) {
  return REPORTS[type].columns.map(c => ({
    key: c.key,
    label: c.label,
    className: c.kind === 'num' ? 'font-mono text-[11px] text-right' : 'font-mono text-[11px]',
    render: c.kind === 'num'
      ? v => <span className={c.key === 'generation' ? 'text-ge-accent' : ''}>{fmt3(v)}</span>
      : undefined,
  }))
}

// ── Filter bar ───────────────────────────────────────────────────────────────
function FilterBar({ type, daily, setDaily, monthly, setMonthly, yearly, setYearly,
                     onApply, loading, years = [], monthOptions = [] }) {

  return (
    <div className="card mb-3">
      <div className="flex flex-wrap items-end gap-3">
        {type === 'DGR' && (
          <>
            <div className="flex flex-col gap-1">
              <label className="form-label">Date</label>
              <input type="date" className="form-control" value={daily.date}
                onChange={e => setDaily(d => ({ ...d, date: e.target.value }))} />
            </div>
            <div className="flex flex-col gap-1">
              <label className="form-label">Interval</label>
              <select className="form-control" value={daily.interval}
                onChange={e => setDaily(d => ({ ...d, interval: e.target.value }))}>
                {DGR_INTERVALS.map(v => (
                  <option key={v} value={v}>{INTERVAL_LABELS[v]}</option>
                ))}
              </select>
            </div>
          </>
        )}

        {type === 'MGR' && (
          <>
            <div className="flex flex-col gap-1">
              <label className="form-label">Month</label>
              {/* Only months the database actually holds for the selected year. */}
              <select className="form-control" value={monthly.month}
                onChange={e => setMonthly(m => ({ ...m, month: Number(e.target.value) }))}
                disabled={!monthOptions.length}>
                {monthOptions.length === 0
                  ? <option value={monthly.month}>—</option>
                  : monthOptions.map(n => <option key={n} value={n}>{MONTHS[n - 1]}</option>)}
              </select>
            </div>
            <div className="flex flex-col gap-1">
              <label className="form-label">Year</label>
              <select className="form-control" value={monthly.year}
                onChange={e => setMonthly(m => ({ ...m, year: Number(e.target.value) }))}>
                {years.map(y => <option key={y} value={y}>{y}</option>)}
              </select>
            </div>
          </>
        )}

        {type === 'YGR' && (
          <div className="flex flex-col gap-1">
            <label className="form-label">Year</label>
            <select className="form-control" value={yearly.year}
              onChange={e => setYearly({ year: Number(e.target.value) })}>
              {years.map(y => <option key={y} value={y}>{y}</option>)}
            </select>
          </div>
        )}

        <button className="btn btn-primary btn-sm" onClick={onApply} disabled={loading}>
          {loading ? <><Spinner size={12} /> Loading...</> : '⟳ Apply Filter'}
        </button>
      </div>
    </div>
  )
}

// ── Report tabs ───────────────────────────────────────────────────────────────
const TABS = [
  { id: 'DGR', label: 'DGR', sub: 'Daily' },
  { id: 'MGR', label: 'MGR', sub: 'Monthly' },
  { id: 'YGR', label: 'YGR', sub: 'Yearly' },
]
function ReportTabs({ active, onChange }) {
  return (
    <div className="flex flex-wrap gap-2 mb-4">
      {TABS.map(t => {
        const on = active === t.id
        return (
          <button key={t.id} onClick={() => onChange(t.id)}
            className={`px-4 py-2 rounded-md text-[13px] font-semibold border transition-all duration-200
                        active:scale-95
                        ${on
                          ? 'bg-ge-blue border-ge-blue text-white shadow-md'
                          : 'bg-ge-surface border-ge-border text-ge-text2 hover:bg-ge-elevated hover:text-ge-text1'}`}>
            {t.label}
            <span className={`ml-2 text-[10px] font-normal ${on ? 'text-white/70' : 'text-ge-text3'}`}>
              {t.sub}
            </span>
          </button>
        )
      })}
    </div>
  )
}

// ── KPI helpers ───────────────────────────────────────────────────────────────
const sum = (rows, k) => rows.reduce((a, r) => a + (r[k] || 0), 0)
const avg = (rows, k, only = () => true) => {
  const f = rows.filter(only)
  return f.length ? sum(f, k) / f.length : 0
}
const max = (rows, k) => rows.reduce((a, r) => Math.max(a, r[k] || 0), 0)

// ── Main Page ──────────────────────────────────────────────────────────────────
export default function DGRReports() {
  const { showToast } = useApp()

  // Selected report type — preserved in component state until page refresh.
  const [type, setType] = useState('DGR')

  // Filter inputs (editable) — applied values drive the data load on Apply.
  const [daily,   setDaily]   = useState({ date: '2024-02-08', interval: '30min' })
  const [monthly, setMonthly] = useState({ month: 2, year: 2024 })
  const [yearly,  setYearly]  = useState({ year: 2024 })

  const [applied, setApplied] = useState({
    daily:   { date: '2024-02-08', interval: '30min' },
    monthly: { month: 2, year: 2024 },
    yearly:  { year: 2024 },
  })

  // All three reports are served by the backend from real plant telemetry.
  const [dgr, setDgr] = useState({ rows: [], loading: false, error: null })
  const [mgr, setMgr] = useState({ rows: [], inverters: [], loading: false, error: null })
  const [ygr, setYgr] = useState({ rows: [], loading: false, error: null })
  // Years that actually carry data — the dropdown never offers an empty year.
  const [years,  setYears]  = useState([])
  // MGR: { "2024": [2,3,4,5] } — which months each year actually holds.
  const [monthsByYear, setMonthsByYear] = useState({})
  const ygrReq = useRef(0)
  // Inverter-wise series behind the DGR grouped bar chart (same date + interval).
  const [dgrInv, setDgrInv] = useState({ rows: [], loading: false, error: null })
  const invTagsRef = useRef(null)      // GEN column list, discovered once from the DB
  const pwrTagsRef = useRef(null)      // ACTIVE_POWER column list, likewise
  const invReq     = useRef(0)
  const [pdfBusy, setPdfBusy] = useState(false)
  // The chart of the active report is captured from these wrappers for the PDF.
  const chartRefs = { DGR: useRef(null), MGR: useRef(null), YGR: useRef(null) }
  const dgrReq = useRef(0)
  const mgrReq = useRef(0)

  const loadDaily = useCallback(async ({ date, interval }) => {
    const id = ++dgrReq.current
    setDgr(s => ({ ...s, loading: true, error: null }))
    try {
      const payload = await fetchDGRSeries(date, interval)
      if (id !== dgrReq.current) return            // a newer request has superseded this one
      setDgr({ rows: toDailyRows(payload), loading: false, error: null })
    } catch (e) {
      if (id !== dgrReq.current) return
      setDgr({ rows: [], loading: false, error: e.message || 'Failed to load report data' })
    }
  }, [])

  // Inverter-wise generation for the chart. The GEN column list comes from the
  // database schema (cached for the session) so no inverter is ever hardcoded.
  const loadDailyInverters = useCallback(async ({ date, interval }) => {
    const id = ++invReq.current
    setDgrInv(s => ({ ...s, loading: true, error: null }))
    try {
      if (!invTagsRef.current) {
        const tags = await fetchReportTags('Daily Generation', 'INVERTER_DAILY_GEN')
        invTagsRef.current = (Array.isArray(tags) ? tags : [])
          .map(t => t.column_name)
          .filter(c => GEN_RE.test(c))
      }
      if (!invTagsRef.current.length) throw new Error('No inverter generation columns found')

      if (!pwrTagsRef.current) {
        const tags = await fetchReportTags('Power Graph', 'POWER_GRAPH')
        pwrTagsRef.current = (Array.isArray(tags) ? tags : [])
          .map(t => t.column_name)
          .filter(c => PWR_RE.test(c))
      }

      // Energy (treemap area) and active power (tooltip peak) over the same window.
      // Power is optional — if POWER_GRAPH is unavailable the treemap still renders
      // with the metered generation and the peak simply reads as "—".
      const [genPayload, powerPayload] = await Promise.all([
        fetchDGRInverterSeries(date, interval, invTagsRef.current),
        pwrTagsRef.current.length
          ? fetchDGRInverterPower(date, interval, pwrTagsRef.current).catch(() => null)
          : Promise.resolve(null),
      ])
      if (id !== invReq.current) return           // superseded by a newer request
      setDgrInv({
        rows: toInverterTotals(genPayload, powerPayload),
        loading: false, error: null,
      })
    } catch (e) {
      if (id !== invReq.current) return
      setDgrInv({ rows: [], loading: false, error: e.message || 'Failed to load inverter data' })
    }
  }, [])

  const loadMonthly = useCallback(async ({ month, year }) => {
    const id = ++mgrReq.current
    setMgr(s => ({ ...s, loading: true, error: null }))
    try {
      const payload = await fetchMGRMonthlyGeneration(month, year)
      if (id !== mgrReq.current) return             // superseded by a newer request
      setMgr({
        rows:      Array.isArray(payload?.daily)     ? payload.daily     : [],
        inverters: Array.isArray(payload?.inverters) ? payload.inverters : [],
        loading:   false,
        error:     null,
      })
    } catch (e) {
      if (id !== mgrReq.current) return
      setMgr({ rows: [], inverters: [], loading: false, error: e.message || 'Failed to load report data' })
    }
  }, [])

  useEffect(() => {
    if (type !== 'DGR') return                     // no request for the tabs that don't need one
    loadDaily(applied.daily)                       // table (plant meter)
    loadDailyInverters(applied.daily)              // chart (per inverter) — same filters
  }, [type, applied.daily, loadDaily, loadDailyInverters])

  const loadYearly = useCallback(async ({ year }) => {
    const id = ++ygrReq.current
    setYgr(s => ({ ...s, loading: true, error: null }))
    try {
      const payload = await fetchYGRYearlyGeneration(year)
      if (id !== ygrReq.current) return             // superseded by a newer request
      setYgr({
        rows: Array.isArray(payload?.months) ? payload.months : [],
        loading: false, error: null,
      })
    } catch (e) {
      if (id !== ygrReq.current) return
      setYgr({ rows: [], loading: false, error: e.message || 'Failed to load report data' })
    }
  }, [])

  useEffect(() => {
    if (type !== 'MGR') return
    loadMonthly(applied.monthly)
  }, [type, applied.monthly, loadMonthly])

  useEffect(() => {
    if (type !== 'YGR') return
    loadYearly(applied.yearly)
  }, [type, applied.yearly, loadYearly])

  // Year list comes from the database — loaded once, then the current selection is
  // snapped onto a year that actually has data.
  useEffect(() => {
    fetchYGRYears()
      .then(d => {
        const list = Array.isArray(d?.years) ? d.years : []
        setYears(list)
        if (list.length && !list.includes(yearly.year)) {
          const latest = list[list.length - 1]
          setYearly({ year: latest })
          setApplied(a => ({ ...a, yearly: { year: latest } }))
        }
      })
      .catch(() => setYears([]))
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // MGR month options come from the database, so a month with no telemetry is
  // never offered and a newly-logged month appears with no code change.
  useEffect(() => {
    fetchMGRPeriods()
      .then(d => setMonthsByYear(d?.months_by_year || {}))
      .catch(() => setMonthsByYear({}))
  }, [])

  const monthOptions = useMemo(
    () => monthsByYear[String(monthly.year)] || [], [monthsByYear, monthly.year])

  // Keep the chosen month on a month that exists for the selected year.
  useEffect(() => {
    if (!monthOptions.length || monthOptions.includes(monthly.month)) return
    const next = monthOptions[0]
    setMonthly(m => ({ ...m, month: next }))
    setApplied(a => ({ ...a, monthly: { ...a.monthly, month: next } }))
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [monthOptions])

  // Active report rows — only the selected report is materialised.
  const rows = useMemo(() => {
    if (type === 'DGR') return dgr.rows
    if (type === 'MGR') return mgr.rows
    return ygr.rows
  }, [type, dgr.rows, mgr.rows, ygr.rows])

  const active  = type === 'DGR' ? dgr : type === 'MGR' ? mgr : ygr
  const loading = active ? active.loading : false
  const error   = active ? active.error : null

  const onApply = () => {
    setApplied({ daily, monthly, yearly })
    showToast(`${REPORTS[type].title} updated`)
  }

  const cols = useMemo(() => tableColumns(type), [type])

  // Export — always the currently selected report only.
  const doExport = (fn, kind) => {
    if (!rows.length) { showToast('Nothing to export — no data for the selected filter'); return }
    fn(type, rows, `${type}_Report_${todayDMY()}`)
    showToast(`${kind} exported`)
  }

  // Map a REPORTS column definition to a PDF-table column (label→header, kind→align/format).
  const pdfColumns = (columns, numFmt = fmt3) => columns.map(c => ({
    header:  c.label,
    dataKey: c.key,
    align:   c.kind === 'num' ? 'right' : 'left',
    format:  c.kind === 'num' ? numFmt : undefined,
  }))

  // Assemble the PDF for the active report: same charts + tables + data on screen.
  const buildPdfConfig = (chart) => {
    const base = { plant: PLANT_NAME, reportTitle: REPORTS[type].title,
                   charts: chart ? [chart] : [] }

    if (type === 'DGR') {
      return {
        ...base,
        subtitle: `Date: ${applied.daily.date.split('-').reverse().join('/')}`,
        filename: `DGR_Report_${todayDMY()}.pdf`,
        meta: [
          { label: 'Report Date', value: applied.daily.date.split('-').reverse().join('/') },
          { label: 'Interval', value: INTERVAL_LABELS[applied.daily.interval] || applied.daily.interval },
          { label: 'Intervals', value: `${rows.length}` },
          { label: 'Total Generation', value: `${fmt3(sum(rows, 'generation'))} kWh` },
          { label: 'Peak Power', value: `${fmt3(max(rows, 'peak'))} MW` },
          { label: 'Generated', value: todayDMY().replace(/-/g, '/') },
        ],
        tables: [{ title: 'Daily Generation Report', columns: pdfColumns(REPORTS.DGR.columns), rows }],
      }
    }

    if (type === 'MGR') {
      return {
        ...base,
        subtitle: `Month: ${monthLabel}`,
        filename: `MGR_Report_${todayDMY()}.pdf`,
        meta: [
          { label: 'Month', value: monthLabel },
          { label: 'Inverters', value: `${mgr.inverters.length}` },
          { label: 'Days', value: `${mgr.rows.length}` },
          { label: 'Total Generation', value: `${fmt3(sum(mgr.rows, 'generation'))} kWh` },
          { label: 'Generated', value: todayDMY().replace(/-/g, '/') },
        ],
        tables: [
          { title: `Monthly Generation by Inverter — ${monthLabel}`,
            columns: [
              { header: 'Inverter', dataKey: 'inverter', align: 'left' },
              { header: 'Monthly Generation (kWh)', dataKey: 'generation', align: 'right', format: fmt2 },
            ],
            rows: mgr.inverters },
          { title: `Daily Generation Totals — ${monthLabel}`,
            columns: pdfColumns(REPORTS.MGR.columns), rows: mgr.rows },
        ],
      }
    }

    // YGR
    return {
      ...base,
      subtitle: `Year: ${applied.yearly.year}`,
      filename: `YGR_Report_${todayDMY()}.pdf`,
      meta: [
        { label: 'Year', value: `${applied.yearly.year}` },
        { label: 'Annual Generation', value: `${fmt3(sum(rows, 'generation'))} MWh` },
        { label: 'Generated', value: todayDMY().replace(/-/g, '/') },
      ],
      tables: [{ title: 'Yearly Generation Report', columns: pdfColumns(REPORTS.YGR.columns), rows }],
    }
  }

  const exportPdf = async () => {
    if (!rows.length) { showToast('Nothing to export — no data for the selected filter'); return }
    setPdfBusy(true)
    try {
      // Capture the on-screen chart at high resolution (null if not yet rendered).
      const chart = await captureChartPng(chartRefs[type].current, { background: '#141928' })
      if (chart) chart.title = type === 'DGR' ? 'Inverter-wise Generation Treemap'
        : type === 'MGR' ? `Daily Generation During ${monthLabel} (kWh)`
        : `Monthly Generation ${applied.yearly.year} (MWh)`
      buildReportPdf(buildPdfConfig(chart))
      showToast('PDF exported')
    } catch (e) {
      showToast(`PDF export failed: ${e.message || 'unknown error'}`)
    } finally {
      setPdfBusy(false)
    }
  }

  // KPI cards — only YGR defines them; DGR and MGR intentionally have none.
  const kpis = useMemo(() => {
    if (type !== 'YGR') return []
    const prRows = rows.filter(r => r.pr !== null && r.pr !== undefined)
    return [
      { label: 'Annual Generation', value: fmt3(sum(rows, 'generation')), unit: 'MWh', color: 'green' },
      { label: 'Avg PR',            value: prRows.length ? fmt3(avg(prRows, 'pr')) : '—', unit: '%', color: 'blue' },
    ]
  }, [type, rows])

  const chartRows = useMemo(() => (type === 'DGR' ? downsample(rows) : rows), [type, rows])

  const monthLabel = `${MONTHS[applied.monthly.month - 1]} ${applied.monthly.year}`

  // On MGR the report table is the day-by-day breakdown of the selected month, so
  // its header names the month; the exported "Report Name" stays REPORTS[type].title.
  const reportHeading = type === 'MGR'
    ? `Daily Generation Totals — ${monthLabel}`
    : REPORTS[type].title

  const reportTable = (
    <div className="card">
      <div className="card-title justify-between">
        <span>{reportHeading} — {rows.length} rows</span>
      </div>
      {loading ? (
        <div className="space-y-1">
          {[...Array(8)].map((_, i) => <div key={i} className="h-8 bg-ge-surface rounded animate-pulse" />)}
        </div>
      ) : error ? (
        <div className="py-8 text-center text-[12px] text-red-400">{error}</div>
      ) : (
        <DataTable columns={cols} rows={rows} emptyMsg="No data available for the selected filter" />
      )}
    </div>
  )

  return (
    <div>
      <PageHeader
        title="Generation Reports"
      >
        <button className="btn btn-outline btn-sm" onClick={() => doExport(exportCSV, 'CSV')} disabled={loading || pdfBusy}>📊 CSV</button>
        <button className="btn btn-outline btn-sm" onClick={() => doExport(exportExcel, 'Excel')} disabled={loading || pdfBusy}>📗 Excel</button>
        <button className="btn btn-primary btn-sm" onClick={exportPdf} disabled={loading || pdfBusy}>
          {pdfBusy ? <><Spinner size={12} /> PDF…</> : '📄 PDF'}
        </button>
      </PageHeader>

      {/* Report selector */}
      <ReportTabs active={type} onChange={setType} />

      {/* Filters */}
      <FilterBar
        type={type}
        daily={daily}     setDaily={setDaily}
        monthly={monthly} setMonthly={setMonthly}
        yearly={yearly}   setYearly={setYearly}
        onApply={onApply} loading={loading}
        years={years} monthOptions={monthOptions}
      />

      {/* KPIs — rendered only when the active report defines them */}
      {kpis.length > 0 && (
        <div className="grid grid-cols-2 lg:grid-cols-3 gap-3 mb-6">
          {kpis.map(k => <KpiCard key={k.label} {...k} />)}
        </div>
      )}

      {/* Charts — one full-width chart per report */}
      {type === 'DGR' && (
        <div className="grid grid-cols-1 gap-3 mb-3" ref={chartRefs.DGR}>
          {/* Inverter-wise generation treemap — rectangle area and colour both
              track the kWh each inverter produced on the selected date. */}
          <ChartCard title="Inverter-wise Generation Treemap (kWh)" height="h-72">
            <ChartBody
              loading={dgrInv.loading}
              error={dgrInv.error}
              empty={!dgrInv.rows.length}
              emptyMsg="No data available"
            >
              <InverterTreemap data={dgrInv.rows} />
            </ChartBody>
          </ChartCard>
        </div>
      )}
      {type === 'MGR' && (
        <div className="grid grid-cols-1 gap-3 mb-3" ref={chartRefs.MGR}>
          {/* One bar per day of the selected month — same `daily` data the plant total
              is summed from — so the title names the month it belongs to. */}
          <ChartCard title={`Daily Generation During ${monthLabel} (kWh)`} height={COMPACT_CHART_H}>
            <ChartBody loading={loading} error={error} empty={!chartRows.length}>
              <MiniBar data={chartRows} xKey="day" yKey="generation" name="Generation" unit=" kWh" color={C.accent} />
            </ChartBody>
          </ChartCard>
        </div>
      )}
      {type === 'YGR' && (
        <div className="grid grid-cols-1 gap-3 mb-3" ref={chartRefs.YGR}>
          <ChartCard title="Monthly Generation (MWh)" height={COMPACT_CHART_H}>
            <MiniBar data={chartRows} xKey="m" yKey="generation" name="Generation" unit=" MWh" color={C.accent} />
          </ChartCard>
        </div>
      )}

      {/* Data table — paired with the per-inverter summary on MGR */}
      {type === 'MGR' ? (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
          <InverterGenerationTable
            rows={mgr.inverters}
            loading={loading}
            error={error}
            monthLabel={monthLabel}
          />
          {reportTable}
        </div>
      ) : reportTable}
    </div>
  )
}
