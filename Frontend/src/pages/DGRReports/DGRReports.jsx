// src/pages/DGRReports/DGRReports.jsx
import React, { useState, useMemo, useEffect, useCallback, useRef } from 'react'
import {
  BarChart, Bar, XAxis, YAxis,
  CartesianGrid, Tooltip, ResponsiveContainer, LabelList,
} from 'recharts'
import { KpiCard, PageHeader, DataTable, Spinner } from '../../components/Common'
import { useApp } from '../../utils/AppContext'
import { RECHARTS_COLORS as C } from '../../utils/helpers'
import {
  fetchDGRSeries, DGR_TAGS, fetchMGRMonthlyGeneration,
  fetchDGRInverterSeries, fetchDGRInverterPower, fetchReportTags,
  fetchYGRYears, fetchYGRYearlyGeneration, fetchMGRPeriods,
  fetchWmsSeriesRange, fetchWmsColumns, fetchMGRInverterDaily,
} from '../../services/api'
import { INTERVAL_LABELS } from '../../utils/intervals'
import { captureChartPng } from '../../utils/chartCapture'
import { buildReportPdf } from '../../utils/pdfReport'
import { columnValueLabel, domainMax, CHART_LABEL_COLOR } from '../../utils/chartValueLabel'
import { makeChartWorkbookBlob } from '../../utils/xlsx'

// Corporate header shown on every exported PDF (DGR / MGR / YGR).
const PLANT_NAME = 'KALYON NIGDE 130 MW'
// Company title band used at the top of the Tracker-style Excel reports (matches the
// backend report_excel.py COMPANY_TITLE) so MGR Excel reads identically.
const COMPANY_TITLE = 'Kalyon Solar Monitoring'

// Shared compact chart height across Analytics, DGR, MGR and YGR — 192px (h-48),
// ~33–40% shorter than the previous h-72/h-80 so charts fit on one screen.
const COMPACT_CHART_H = 'h-48'

// ── Constants ───────────────────────────────────────────────────────────────
const MONTHS       = ['January','February','March','April','May','June',
                      'July','August','September','October','November','December']
const MONTHS_SHORT = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']


// Expand the YGR month rows to the COMPLETE Jan→Dec calendar so the client always sees
// the whole yearly timeline (chart, table and export), in calendar order. Months the
// database has no data for are shown with generation 0 (a zero/empty bar) and BLANK
// PR/CUF/Peak. This is DISPLAY-ONLY padding — it never changes real values or the
// calculations: 0 adds nothing to the annual total, and a null PR is excluded from the
// average. Only called when at least one month has data (an empty year stays empty, so
// a no-data year / load error is never shown as a fabricated all-zero year).
function fillYearMonths(monthRows) {
  const byNo = new Map((monthRows || []).map(r => [r.month_no, r]))
  return MONTHS.map((full, i) => byNo.get(i + 1) || {
    month: full, m: MONTHS_SHORT[i], month_no: i + 1,
    generation: 0, peak: null, pr: null, cuf: null, days: 0,
  })
}

// ── Helpers ─────────────────────────────────────────────────────────────────
const pad = n => String(n).padStart(2, '0')
// Full inverter equipment name (matches the DB column stem INVERTER_0n_GEN): the YGR
// table/exports label each inverter column "INVERTER_01" … "INVERTER_24" (display only).
const invName = n => `INVERTER_${pad(n)}`
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
    const ts    = new Date(r.timestamp)
    const valid = !Number.isNaN(ts.getTime())
    const cum   = num(r[DGR_TAGS.energy])
    let generation = null
    if (cum !== null) {
      generation = Math.max(0, cum - prev) * MWH_TO_KWH   // guard against a counter reset
      prev = cum
    }
    return {
      time:       valid ? `${pad(ts.getHours())}:${pad(ts.getMinutes())}` : String(r.timestamp),
      // Minute-of-day, used ONLY to select the operational window for the summary row.
      mins:       valid ? ts.getHours() * 60 + ts.getMinutes() : null,
      generation,
      peak:       num(r[DGR_TAGS.power]),
      cumulative: cum === null ? null : cum * MWH_TO_KWH,
    }
  })
}

// ── DGR: ONE summary row per day (client requirement) ────────────────────────
// The DGR table/PDF must show a SINGLE row that aggregates the operational window
// 06:00–20:00 (6:00 AM–8:00 PM) — not the individual interval buckets. Only records
// whose timestamp falls in [06:00, 20:00] are used; anything before 06:00 or after
// 20:00 is excluded. Every value is derived ONLY from those in-window records:
//   · Generation (kWh)          — SUM of the in-window interval generation (day total).
//   · Peak Power (MW)           — MAX in-window power reading (the day's peak).
//   · Cumulative Production kWh — the window's closing meter reading (its max).
// No in-window records (or no numeric values) → [] → the table shows "No data
// available" (no fabricated/mock row is ever produced).
const DGR_WINDOW_START_MIN = 6 * 60       // 06:00
const DGR_WINDOW_END_MIN   = 20 * 60      // 20:00
const DGR_WINDOW_LABEL     = '06:00 – 20:00'

function summarizeDaily(rows) {
  const inWindow = (rows || []).filter(
    r => r.mins !== null && r.mins >= DGR_WINDOW_START_MIN && r.mins <= DGR_WINDOW_END_MIN,
  )
  if (!inWindow.length) return []

  let gen = null, peak = null, cum = null
  for (const r of inWindow) {
    if (r.generation !== null && r.generation !== undefined) gen  = (gen ?? 0) + r.generation
    if (r.peak       !== null && r.peak       !== undefined) peak = peak === null ? r.peak       : Math.max(peak, r.peak)
    if (r.cumulative !== null && r.cumulative !== undefined) cum  = cum  === null ? r.cumulative : Math.max(cum,  r.cumulative)
  }
  // Records exist but carry no numeric readings → treat as no data.
  if (gen === null && peak === null && cum === null) return []
  return [{ time: DGR_WINDOW_LABEL, generation: gen, peak, cumulative: cum }]
}

// ── DGR: WMS weather-station rows (ADDED) ────────────────────────────────────
// Rows are passed through UNCHANGED — every WMS value is exactly what the query
// returned. Per-station cards derive their metrics/values from these rows.
function toWmsRows(payload) {
  return Array.isArray(payload?.rows) ? payload.rows : []
}

// WMS date-range builders — one per report level, so the SAME loader serves all three:
//   DGR → the selected day at the DGR interval; MGR → the whole selected month at a daily
//   bucket; YGR → the whole selected year at a monthly bucket. The wmsMatrix memo then
//   pivots those buckets into the Date × WMS-columns table. Local date strings only —
//   no timezone shift.
const wmsDayRange   = ({ date, interval }) => ({ from: `${date}T00:00:00`, to: `${date}T23:59:59`, interval })
const wmsMonthRange = ({ month, year }) => {
  const lastDay = new Date(year, month, 0).getDate()       // days in the selected month
  return {
    from: `${year}-${pad(month)}-01T00:00:00`,
    to:   `${year}-${pad(month)}-${pad(lastDay)}T23:59:59`,
    interval: 'daily',
  }
}
const wmsYearRange  = ({ year }) => ({ from: `${year}-01-01T00:00:00`, to: `${year}-12-31T23:59:59`, interval: 'monthly' })

// Every calendar day of a month → { day, date 'DD/MM/YYYY', key 'YYYY-MM-DD' }. `month` is
// 1-based. Used to pad the MGR tables so EVERY day of the selected month is shown (a day the
// database has no record for still gets a row; its values are left empty — display only, it
// never changes the real data or the totals).
function monthDays(year, month) {
  const last = new Date(year, month, 0).getDate()
  return Array.from({ length: last }, (_, i) => {
    const day = i + 1
    return { day, date: `${pad(day)}/${pad(month)}/${year}`, key: `${year}-${pad(month)}-${pad(day)}` }
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

// Rasterise a report's on-screen chart to a PNG for embedding in BOTH the PDF and the Excel
// exports, so every export carries the same graph the UI shows. MGR/YGR charts are Recharts
// SVGs (captured by the crisp vector rasteriser); DGR's "chart" is the inverter box grid
// (HTML divs, no <svg>) so it falls back to html2canvas. Returns { dataUrl, width, height }
// in CSS px, or null — a null simply omits the image and never fails the export.
async function captureReportChart(node, background = '#141928') {
  if (!node) return null
  const svgShot = await captureChartPng(node, { background })
  if (svgShot) return svgShot
  try {
    const { default: html2canvas } = await import('html2canvas')
    const canvas = await html2canvas(node, { backgroundColor: background, scale: 2 })
    return { dataUrl: canvas.toDataURL('image/png'), width: Math.round(canvas.width / 2), height: Math.round(canvas.height / 2) }
  } catch {
    return null   // capture failed → export continues with the table only
  }
}

// ── Mini chart components (theme-consistent, recharts) ───────────────────────
// Axis tick values use the same bright, readable label colour as the bar value labels so
// every bit of chart text is clearly legible on the dark background (client requirement).
const AXIS = { fill: CHART_LABEL_COLOR, fontSize: 10 }
const TOOLTIP_STYLE = {
  background: '#1a2035', border: '1px solid #2a3350', borderRadius: 6, fontSize: 11,
}
// Axis titles ("Date", "Generation (kWh)") use the same readable label colour as the ticks
// and bar values, so the whole chart reads consistently.
const axisLabel = (value, angle = 0) => ({
  value, angle,
  position: angle ? 'insideLeft' : 'insideBottom',
  offset: angle ? 10 : -4,
  style: { fill: CHART_LABEL_COLOR, fontSize: 10, textAnchor: 'middle' },
})

// `xLabel` / `yLabel` are OPTIONAL axis titles. When passed, the axis renders a muted
// title (X below, Y rotated on the left) and the bottom margin grows to fit the X title.
// They default off, so callers that don't pass them (e.g. YGR) are visually unchanged.
// `maxBarSize` (optional) caps each bar's width in px. Without it Recharts stretches a
// bar to fill its whole category, so a chart with only 2–4 categories shows absurdly
// wide bars; capping keeps bars a clean, consistent width at any category count.
function MiniBar({ data, xKey, yKey, name, color = C.blue, unit = '', xLabel, yLabel, maxBarSize,
                  showValues = false, allTicks = false }) {
  // When `showValues` is set, every bar carries a permanent value label (client
  // requirement) — visible on-screen AND in the PDF (which captures this same SVG).
  // Labels are vertical (one per bar column, never overlapping) and the Y domain is
  // padded above the tallest bar so the top label can't clip. Off by default, so the
  // non-generation callers stay exactly as they were.
  const unitText = (unit || '').trim()
  const barCount = data?.length || 0
  return (
    <ResponsiveContainer width="100%" height="100%">
      {/* Left margin must stay >= 0: a negative margin pulls the Y axis into the
          clipped region and shears the leading digits off large generation values.
          `width` reserves room for the widest tick (7-digit kWh totals), which is
          Recharts' equivalent of ECharts' grid.left + containLabel. */}
      <BarChart data={data} margin={{ top: showValues ? 10 : 4, right: 10, left: yLabel ? 8 : 4, bottom: xLabel ? 16 : 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke={C.grid} vertical={false} />
        {/* Date ticks (e.g. 08/02, 09/02 …); `minTickGap` thins them so they never
            collide. Optional "Date" title sits below the axis. */}
        {/* `allTicks` forces EVERY category (date) to render on the axis, in sequence —
            used by MGR so all calendar days show, including padded 0-generation days. */}
        <XAxis dataKey={xKey} tick={AXIS} axisLine={false} tickLine={false}
          interval={allTicks ? 0 : undefined} minTickGap={allTicks ? 0 : 16}
          label={xLabel ? axisLabel(xLabel) : undefined} />
        {/* kWh scale; `width` keeps full 7-digit values readable. Optional
            "Generation (kWh)" title, rotated on the left. When labelling bars, the
            domain is padded so the tallest value label has headroom (never clipped). */}
        <YAxis tick={AXIS} axisLine={false} tickLine={false} width={76} tickMargin={4}
          domain={showValues ? [0, domainMax(data, yKey)] : undefined}
          label={yLabel ? axisLabel(yLabel, -90) : undefined} />
        <Tooltip cursor={{ fill: 'rgba(255,255,255,0.04)' }}
          contentStyle={{ background: '#1a2035', border: '1px solid #2a3350', borderRadius: 6, fontSize: 11 }}
          labelStyle={{ color: '#6b7a99' }} formatter={v => [`${Number(v).toFixed(2)}${unit}`, name]} />
        <Bar dataKey={yKey} name={name} fill={color} radius={[3, 3, 0, 0]} fillOpacity={0.85}
          maxBarSize={maxBarSize} isAnimationActive={!showValues}>
          {showValues && (
            <LabelList dataKey={yKey} content={columnValueLabel({ unit: unitText, barCount })} />
          )}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  )
}

// ── Inverter-wise generation (DGR) ───────────────────────────────────────────
// Per-inverter kWh totals are shown as a simple grid of equal-sized blue boxes
// (see InverterGenerationGrid). Generation values are unchanged from before.
const fmtKwh = v => Number(v || 0).toLocaleString(undefined, { maximumFractionDigits: 1 })

const INVERTER_COUNT = 24

// Build the INV1…INV24 blocks from the DGR inverter series, in numeric order, filling 0 for
// any inverter the pipeline omitted. This is the SINGLE source of the inverter-wise blocks —
// used by both the on-screen grid and the PDF export, so the two are always identical.
function buildInverterBoxes(data) {
  const byName = new Map((data || []).map(d => [d.name, d.generation || 0]))
  return Array.from({ length: INVERTER_COUNT }, (_, i) => {
    const name = `INV${i + 1}`
    return { name, generation: byName.get(name) || 0 }
  })
}

function InverterGenerationGrid({ data }) {
  // Look up each inverter's generation by name and render INV1…INV24 in order, so all
  // 24 always appear. An inverter the pipeline omitted (0 kWh) still gets a box.
  const boxes = useMemo(() => buildInverterBoxes(data), [data])

  // Seven compact boxes per row on desktop (INV1–INV7, INV8–INV14, …), fewer columns on
  // smaller screens so it stays responsive. `content-center` centres the rows in the card;
  // every box carries identical content + padding (and single-line values via nowrap), so
  // all boxes are exactly equal width (1fr) and equal height. Nothing here changes the data.
  return (
    <div className="grid h-full content-center gap-1.5 mx-auto w-full max-w-5xl
                    grid-cols-4 sm:grid-cols-5 md:grid-cols-7">
      {boxes.map(b => (
        <div
          key={b.name}
          title={`${b.name} — ${fmtKwh(b.generation)} kWh`}
          className="flex flex-col items-center justify-center gap-0.5 rounded-md px-1 py-1.5
                     text-center overflow-hidden select-none"
          style={{
            background: '#1e5fd8',
            border: '1px solid rgba(255, 255, 255, 0.10)',
            boxShadow: '0 1px 3px rgba(0, 0, 0, 0.35)',
          }}
        >
          <span className="font-bold leading-none text-white"
            style={{ fontSize: 'clamp(9px, 0.95vw, 11px)' }}>
            {b.name}
          </span>
          <span className="font-mono leading-none text-white/90 whitespace-nowrap"
            style={{ fontSize: 'clamp(7px, 0.8vw, 10px)' }}>
            {fmtKwh(b.generation)} kWh
          </span>
        </div>
      ))}
    </div>
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

// ── MGR: Date × Inverter generation matrix ───────────────────────────────────
// Real per-day, per-inverter generation from [dbo].[INVERTER_DAILY_GEN] (daily bucket
// MAX = that day's total for the inverter, since the counter resets at midnight). Laid
// out horizontally: one ROW per day, one COLUMN per inverter (Date | Inverter 1 … N).
// Horizontally scrollable; the Date column is sticky so it stays visible while scrolling
// the inverter columns. Values/calculations are the database's own — display only.


// MGR Excel — ONE workbook, ONE sheet, stacked in this exact order (client layout):
//   Kalyon Solar Monitoring                              (company title)
//   <Month> <Year>                                       (selected month + year, e.g. February 2024)
//   Bar Graph — Inverter-wise Generation (kWh)           (native, editable Excel bar chart:
//                                                         one bar per inverter = its monthly
//                                                         total, aggregated from the matrix)
//   Inverter Report   → Date | Inverter 1 … N            (the on-screen inverter matrix)
// A SECOND worksheet, "WMS Report", then carries the on-screen WMS matrix
//   WMS Report        → Date | every WMS column          (its own sheet = its own print page)
// The month/year line is the MGR filter's selected period (monthLabel). The inverter and WMS
// daily values are unchanged (2-dp inverters, 3-dp WMS); the bar chart only VISUALISES
// per-inverter monthly totals — no data/calculation is altered.
// `matrix` = mgrMatrix ({rows:[{date,cells[]}], invNums}); `wms` = wmsMatrix ({columns, rows}).
function exportMgrExcel(matrix, wms, monthLabel) {
  const invNums  = matrix?.invNums || []
  const invRows  = matrix?.rows || []
  const wmsCols  = wms?.columns || []
  const wmsRows  = wms?.rows || []

  // Per-inverter monthly total = sum of that inverter's daily values (null-safe). Visualisation
  // aggregate only — the Inverter Report below still shows every individual daily value.
  const totals = invNums.map((n, i) => {
    let s = 0, any = false
    for (const rr of invRows) {
      const v = rr.cells[i]
      if (v != null && Number.isFinite(Number(v))) { s += Number(v); any = true }
    }
    return any ? s : null
  })

  const sheet = {
    name: 'MGR Report',
    title: COMPANY_TITLE,
    subtitle: monthLabel,                           // selected Month + Year, e.g. "February 2024"
    graphHeading: 'Bar Graph — Inverter-wise Generation (kWh)',
    chartTitle: 'Inverter-wise Generation (kWh)',
    barColor: '1E5FD8',
    graph: {
      catHeader: 'Inverter',
      valHeader: 'Generation (kWh)',
      cats: invNums.map(n => `Inverter ${n}`),
      vals: totals,
      numStyle: 2,
    },
    sections: [
      {
        heading: 'Inverter Report',
        columns: ['Date', ...invNums.map(n => `Inverter ${n}`)],
        rows: invRows.map(r => [r.date, ...r.cells.map(v => (v == null ? null : Number(v)))]),
        numStyle: 2,                                // inverter values → 0.00
      },
    ],
    // WMS moves to its OWN worksheet (sheet 2) so it can never sit beside or below the MGR
    // chart. A separate worksheet is also a separate PRINT page, so printing / exporting to
    // PDF puts the MGR chart + Inverter Report first and starts WMS on the next page. The
    // WMS columns, rows, 3-dp formatting and metadata block are all unchanged — only the
    // worksheet it lives on changed.
    extraSheets: [
      {
        name: 'WMS Report',
        // The WMS worksheet carries NEITHER the company title NOR the period header
        // (client request) — no `title` and no `subtitle`, both optional in the sheet
        // writer, so the sheet opens straight on the "WMS Report" heading and its data.
        // The MGR sheet above is untouched and keeps both. The per-row Date values in the
        // WMS data are NOT affected — only the header line above the table is dropped.
        sections: [{
          heading: 'WMS Report',
          columns: ['Date', ...wmsCols],
          rows: wmsRows.map(r => [r.label, ...r.cells.map(v => (v == null ? null : Number(v)))]),
          numStyle: 3,                              // WMS values → 0.000
        }],
      },
    ],
  }

  downloadBlob(makeChartWorkbookBlob(sheet), `MGR_Report_${todayDMY()}.xlsx`)
}

// YGR Excel — SAME structure as the MGR export: ONE workbook, ONE sheet, stacked in this order:
//   Kalyon Solar Monitoring                              (company title)
//   Year: <year>                                         (selected year)
//   Bar Graph — Inverter-wise Generation (MWh)           (native, editable Excel bar chart:
//                                                         one bar per inverter = its YEARLY
//                                                         total, aggregated from the matrix)
//   Inverter Report   → Month | Generation (MWh) | INVERTER_01 … INVERTER_N
// A SECOND worksheet, "WMS Report", then carries the WMS matrix
//   WMS Report        → Date | every WMS column          (its own sheet = its own print page)
// YGR generation is in MWh, so the chart Y-axis is Generation (MWh) (the actual unit — the
// values are never changed). PR / CUF / Peak Power are NOT exported. Year is dynamic.
// `table` = ygrTable ({ invNums, rows:[{month,generation,cells[]}] }); `wms` = wmsMatrix.
function exportYgrExcel(table, genLabel, wms, year) {
  const invNums = table?.invNums || []
  const rows = table?.rows || []
  const wmsCols = wms?.columns || []
  const wmsRows = wms?.rows || []

  // Per-inverter YEARLY total = sum of that inverter's monthly generation (null-safe). Chart
  // data only — the Inverter Report below keeps every monthly value unchanged.
  const totals = invNums.map((n, i) => {
    let s = 0, any = false
    for (const r of rows) {
      const v = r.cells[i]
      if (v != null && Number.isFinite(Number(v))) { s += Number(v); any = true }
    }
    return any ? s : null
  })

  const sheet = {
    name: 'YGR Report',
    title: COMPANY_TITLE,
    subtitle: `Year: ${year}`,
    graphHeading: 'Bar Graph — Inverter-wise Generation (MWh)',
    chartTitle: 'Inverter-wise Generation (MWh)',
    barColor: '1E5FD8',
    graph: {
      catHeader: 'Inverter',
      valHeader: genLabel,                          // 'Generation (MWh)'
      cats: invNums.map(invName),                   // INVERTER_01 … INVERTER_N
      vals: totals,
      numStyle: 3,
    },
    sections: [
      {
        heading: 'Inverter Report',
        columns: ['Month', genLabel, ...invNums.map(invName)],
        rows: rows.map(r => [
          r.month,
          r.generation == null ? null : Number(r.generation),
          ...r.cells.map(v => (v == null ? null : Number(v))),
        ]),
        numStyle: 3,                                // MWh values → 0.000
      },
    ],
    // WMS on its OWN worksheet (sheet 2) — same reasoning as the MGR export above: it can
    // never overlap the YGR chart, and it starts on a new print page when the workbook is
    // printed or exported to PDF. WMS data/formatting are untouched.
    extraSheets: [
      {
        name: 'WMS Report',
        // No company title and no "Year: <year>" header on the WMS worksheet (client
        // request) — same as the MGR export above. The YGR sheet keeps both, and the WMS
        // rows' own Date values are unchanged.
        sections: [{
          heading: 'WMS Report',
          columns: ['Date', ...wmsCols],
          rows: wmsRows.map(r => [r.label, ...r.cells.map(v => (v == null ? null : Number(v)))]),
          numStyle: 3,                              // WMS values → 0.000
        }],
      },
    ],
  }
  downloadBlob(makeChartWorkbookBlob(sheet), `YGR_Report_${todayDMY()}.xlsx`)
}

// YGR CSV — Month | Generation (MWh) | INV1 … INV24 (matches the on-screen YGR table and the
// Excel export). PR / CUF / Peak Power are NOT exported. Same metadata block as the app's
// other CSVs; BOM + CRLF so Excel opens aligned columns.
function exportYgrCsv(table, genLabel) {
  const invNums = table?.invNums || []
  const rows = table?.rows || []
  const esc = s => (/[",\n]/.test(s) ? `"${String(s).replace(/"/g, '""')}"` : String(s))
  const header = ['Month', genLabel, ...invNums.map(invName)]
  const lines = [
    esc(`Project Name: Kalyon Solar Power Plant`),
    esc(`Report Name: ${REPORTS.YGR.title}`),
    esc(`Generated Date & Time: ${csvGeneratedStamp()}`),
    '',
    header.map(esc).join(','),
    ...rows.map(r => [r.month, r.generation, ...r.cells]
      .map((v, i) => esc(i === 0 ? (v ?? '') : (v == null ? '' : fmt3(v)))).join(',')),
  ]
  downloadBlob(new Blob(['﻿' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8' }), `YGR_Report_${todayDMY()}.csv`)
}

const InverterMonthlyMatrix = React.memo(function InverterMonthlyMatrix({ matrix, monthLabel }) {
  const { rows, invNums, loading, error } = matrix
  return (
    <div className="card">
      <div className="card-title">
        {/* Section-level Excel button removed — MGR exports via the single top Excel button
            (one workbook: Monthly Generation + WMS Data sheets). */}
        <span>Monthly Generation by Inverter — {monthLabel} (kWh)</span>
      </div>
      {loading ? (
        <div className="space-y-1">
          {[...Array(6)].map((_, i) => <div key={i} className="h-8 bg-ge-surface rounded animate-pulse" />)}
        </div>
      ) : error ? (
        <div className="py-8 text-center text-[12px] text-red-400">{error}</div>
      ) : rows.length === 0 ? (
        <div className="py-8 text-center text-[12px] text-ge-text3">
          No inverter generation for the selected month
        </div>
      ) : (
        // Horizontal matrix — scrolls sideways so all inverter columns stay readable on
        // smaller screens; the Date column stays pinned on the left (sticky).
        <div className="overflow-x-auto border border-ge-border rounded">
          <table className="data-table">
            <thead>
              <tr>
                <th className="text-left sticky left-0 z-20 bg-ge-surface whitespace-nowrap">Date</th>
                {invNums.map(n => (
                  <th key={n} className="text-right whitespace-nowrap">Inverter {n}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map(r => (
                <tr key={r.date}>
                  <td className="text-left font-mono text-[11px] whitespace-nowrap sticky left-0 z-10 bg-ge-surface">
                    {r.date}
                  </td>
                  {r.cells.map((v, i) => (
                    <td key={i} className="text-right font-mono text-[11px] text-ge-accent whitespace-nowrap">
                      {fmt2(v)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
})

// ── YGR: Month × Inverter generation table ───────────────────────────────────
// Month | Generation (MWh) | INV1 … INV24. Each inverter cell is that month's generation
// for that inverter (sum of the inverter's daily totals ÷ 1000 → MWh, so the row's Generation
// equals the sum of its INV columns). PR / CUF / Peak Power are no longer shown. Horizontally
// scrollable; the Month column stays pinned (sticky) so all 24 inverter columns stay readable.
const YgrInverterTable = React.memo(function YgrInverterTable({ table, genLabel, loading, error }) {
  const { rows, invNums } = table
  return (
    <div className="card">
      <div className="card-title">
        <span>Yearly Generation by Inverter — {genLabel} · INVERTER_01…{invName(invNums.length)}</span>
      </div>
      {loading ? (
        <div className="space-y-1">
          {[...Array(6)].map((_, i) => <div key={i} className="h-8 bg-ge-surface rounded animate-pulse" />)}
        </div>
      ) : error ? (
        <div className="py-8 text-center text-[12px] text-red-400">{error}</div>
      ) : rows.length === 0 ? (
        <div className="py-8 text-center text-[12px] text-ge-text3">
          No data available for the selected year
        </div>
      ) : (
        <div className="overflow-x-auto border border-ge-border rounded">
          <table className="data-table">
            <thead>
              <tr>
                <th className="text-left sticky left-0 z-20 bg-ge-surface whitespace-nowrap">Month</th>
                <th className="text-right whitespace-nowrap">{genLabel}</th>
                {invNums.map(n => (
                  <th key={n} className="text-right whitespace-nowrap">{invName(n)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map(r => (
                <tr key={r.month_no}>
                  <td className="text-left font-mono text-[11px] whitespace-nowrap sticky left-0 z-10 bg-ge-surface">
                    {r.month}
                  </td>
                  <td className="text-right font-mono text-[11px] text-ge-accent whitespace-nowrap">
                    {fmt3(r.generation)}
                  </td>
                  {r.cells.map((v, i) => (
                    <td key={i} className="text-right font-mono text-[11px] whitespace-nowrap">
                      {fmt3(v)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
})

// ── Table column adapters for DataTable (3-dp formatting) ────────────────────
function tableColumns(type) {
  return REPORTS[type].columns.map(c => ({
    key: c.key,
    label: c.label,
    // Numeric values (Generation, Peak Power, Cumulative …) are CENTER-aligned (req 2);
    // text/date columns stay left. `align` is applied to the header AND the cells, so
    // they line up perfectly — no more left header over a right-aligned number.
    align: c.kind === 'num' ? 'center' : 'left',
    className: 'font-mono text-[11px]',
    render: c.kind === 'num'
      ? v => <span className={c.key === 'generation' ? 'text-ge-accent' : ''}>{fmt3(v)}</span>
      : undefined,
  }))
}

// ── Filter bar ───────────────────────────────────────────────────────────────
function FilterBar({ type, daily, setDaily, monthly, setMonthly, yearly, setYearly,
                     onApply, onDailyDate, onMonthlyChange, loading, years = [], monthOptions = [] }) {

  return (
    <div className="card mb-3">
      <div className="flex flex-wrap items-end gap-3">
        {type === 'DGR' && (
          // Date only — the Interval selector was removed; the report uses a fixed default
          // interval (carried in `daily.interval`) so the API still receives one. Changing
          // the date auto-applies (one refresh) via onDailyDate; the Apply Filter button
          // remains for an explicit re-run.
          <div className="flex flex-col gap-1">
            <label className="form-label">Date</label>
            <input type="date" className="form-control" value={daily.date}
              onChange={e => onDailyDate(e.target.value)} />
          </div>
        )}

        {type === 'MGR' && (
          // Month / Year auto-apply on change (onMonthlyChange) — the graph + inverter table
          // fetch and update immediately, so there is no Apply Filter button for MGR.
          <>
            <div className="flex flex-col gap-1">
              <label className="form-label">Month</label>
              {/* Only months the database actually holds for the selected year. */}
              <select className="form-control" value={monthly.month}
                onChange={e => onMonthlyChange({ month: Number(e.target.value) })}
                disabled={!monthOptions.length}>
                {monthOptions.length === 0
                  ? <option value={monthly.month}>—</option>
                  : monthOptions.map(n => <option key={n} value={n}>{MONTHS[n - 1]}</option>)}
              </select>
            </div>
            <div className="flex flex-col gap-1">
              <label className="form-label">Year</label>
              <select className="form-control" value={monthly.year}
                onChange={e => onMonthlyChange({ year: Number(e.target.value) })}>
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

        {/* MGR auto-applies on Month/Year change, so it has no Apply Filter button. DGR/YGR
            keep the explicit Apply. A compact loading indicator replaces it for MGR so the
            existing loading state stays visible while the new month's data is fetched. */}
        {type === 'MGR' ? (
          loading && (
            <span className="btn btn-sm inline-flex items-center gap-1 opacity-70 cursor-default">
              <Spinner size={12} /> Loading...
            </span>
          )
        ) : (
          <button className="btn btn-primary btn-sm" onClick={onApply} disabled={loading}>
            {loading ? <><Spinner size={12} /> Loading...</> : '⟳ Apply Filter'}
          </button>
        )}
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
  // MGR Date × Inverter matrix: real per-day, per-inverter generation for the month.
  const [mgrMatrix, setMgrMatrix] = useState({ rows: [], invNums: [], loading: false, error: null })
  const mgrMatrixReq = useRef(0)
  // YGR Month × Inverter matrix: per-inverter generation for each month of the year.
  const [ygrMatrix, setYgrMatrix] = useState({ rows: [], invNums: [], loading: false, error: null })
  const ygrMatrixReq = useRef(0)
  // WMS weather-station series for the WMS table — shared by DGR (day), MGR (month) and
  // YGR (year); only the fetched range/interval differ.
  const [wms, setWms] = useState({ rows: [], loading: false, error: null })
  // Exact WMS column list in database order (data-driven from the schema).
  const [wmsColumns, setWmsColumns] = useState([])
  const wmsColsRef = useRef(null)
  const wmsReq = useRef(0)
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

  // WMS weather-station data over an arbitrary range + interval — the ONE shared WMS
  // loader for all three reports (DGR: day, MGR: month, YGR: year). Values are stored
  // as-is; the wmsMatrix memo pivots the returned buckets into the Date × columns table.
  // This is an ADDED read; it never touches the meter/inverter/generation loads.
  const loadWms = useCallback(async ({ from, to, interval }) => {
    const id = ++wmsReq.current
    setWms(s => ({ ...s, loading: true, error: null }))
    try {
      // Column list (exact DB names + order) is fetched once and cached for the session.
      if (!wmsColsRef.current) {
        const res = await fetchWmsColumns()
        wmsColsRef.current = Array.isArray(res?.columns) ? res.columns : []
        setWmsColumns(wmsColsRef.current)
      }
      const payload = await fetchWmsSeriesRange(from, to, interval, wmsColsRef.current)
      if (id !== wmsReq.current) return           // superseded by a newer request
      setWms({ rows: toWmsRows(payload), loading: false, error: null })
    } catch (e) {
      if (id !== wmsReq.current) return
      setWms({ rows: [], loading: false, error: e.message || 'Failed to load WMS data' })
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

  // MGR Date × Inverter matrix — real per-day, per-inverter generation for the selected
  // month from INVERTER_DAILY_GEN (one 'daily' bucket per day; each cell = that day's total
  // for the inverter). The GEN column list is discovered from the schema (cached), sorted
  // by inverter number and capped at 24 → columns Inverter 1…24. Rows are the days that
  // actually have data, in chronological order.
  const loadMgrMatrix = useCallback(async ({ month, year }) => {
    const id = ++mgrMatrixReq.current
    setMgrMatrix(s => ({ ...s, loading: true, error: null }))
    try {
      if (!invTagsRef.current) {
        const tags = await fetchReportTags('Daily Generation', 'INVERTER_DAILY_GEN')
        invTagsRef.current = (Array.isArray(tags) ? tags : [])
          .map(t => t.column_name).filter(c => GEN_RE.test(c))
      }
      const genTags = [...invTagsRef.current]
        .sort((a, b) => Number(a.match(GEN_RE)[1]) - Number(b.match(GEN_RE)[1]))
        .slice(0, INVERTER_COUNT)                   // Inverter 1…24
      const lastDay = new Date(year, month, 0).getDate()
      const from = `${year}-${pad(month)}-01T00:00:00`
      const to   = `${year}-${pad(month)}-${pad(lastDay)}T23:59:59`
      const payload = await fetchMGRInverterDaily(from, to, genTags)
      if (id !== mgrMatrixReq.current) return        // superseded by a newer request
      const dataRows = (Array.isArray(payload?.rows) ? payload.rows : [])
        .map(r => {
          const d = new Date(r.timestamp)
          const date = Number.isNaN(d.getTime())
            ? String(r.timestamp)
            : `${pad(d.getDate())}/${pad(d.getMonth() + 1)}/${d.getFullYear()}`
          return { date, ts: String(r.timestamp), cells: genTags.map(t => num(r[t])) }
        })
      // Pad to EVERY day of the month — a day with no record still gets a row (empty cells).
      const byDate = new Map(dataRows.map(r => [r.date, r]))
      const rows = monthDays(year, month).map(d =>
        byDate.get(d.date) || { date: d.date, ts: d.key, cells: genTags.map(() => null) })
      const invNums = genTags.map(t => Number(t.match(GEN_RE)[1]))
      setMgrMatrix({ rows, invNums, loading: false, error: null })
    } catch (e) {
      if (id !== mgrMatrixReq.current) return
      setMgrMatrix({ rows: [], invNums: [], loading: false, error: e.message || 'Failed to load inverter data' })
    }
  }, [])

  // YGR Month × Inverter matrix — per-inverter generation for each month of the selected year.
  // INVERTER_DAILY_GEN gives each inverter's daily total (daily bucket MAX); summing those
  // across a month is that inverter's monthly generation (kWh). Dividing by 1000 → MWh, so a
  // row's Generation (MWh) equals the sum of its INV columns exactly (the YGR Generation value
  // IS the sum of all inverters). One fetch covers the whole year; a month with no data shows 0
  // (matching the padded Generation column). Reuses the SAME daily-generation source/SQL as MGR.
  const loadYgrMatrix = useCallback(async ({ year }) => {
    const id = ++ygrMatrixReq.current
    setYgrMatrix(s => ({ ...s, loading: true, error: null }))
    try {
      if (!invTagsRef.current) {
        const tags = await fetchReportTags('Daily Generation', 'INVERTER_DAILY_GEN')
        invTagsRef.current = (Array.isArray(tags) ? tags : [])
          .map(t => t.column_name).filter(c => GEN_RE.test(c))
      }
      const genTags = [...invTagsRef.current]
        .sort((a, b) => Number(a.match(GEN_RE)[1]) - Number(b.match(GEN_RE)[1]))
        .slice(0, INVERTER_COUNT)                   // Inverter 1…24
      const payload = await fetchMGRInverterDaily(
        `${year}-01-01T00:00:00`, `${year}-12-31T23:59:59`, genTags)
      if (id !== ygrMatrixReq.current) return        // superseded by a newer request
      // Sum each inverter's daily totals into its month; kWh → MWh to match the Generation col.
      const monthly = Array.from({ length: 12 }, () => genTags.map(() => 0))
      for (const r of (Array.isArray(payload?.rows) ? payload.rows : [])) {
        const d = new Date(r.timestamp)
        if (Number.isNaN(d.getTime())) continue
        const mi = d.getMonth()
        genTags.forEach((t, i) => {
          const v = num(r[t])
          if (v != null && Number.isFinite(v)) monthly[mi][i] += v
        })
      }
      const invNums = genTags.map(t => Number(t.match(GEN_RE)[1]))
      const rows = MONTHS.map((full, mi) => ({
        month: full, month_no: mi + 1, cells: monthly[mi].map(v => v / 1000),
      }))
      setYgrMatrix({ rows, invNums, loading: false, error: null })
    } catch (e) {
      if (id !== ygrMatrixReq.current) return
      setYgrMatrix({ rows: [], invNums: [], loading: false, error: e.message || 'Failed to load inverter data' })
    }
  }, [])

  useEffect(() => {
    if (type !== 'DGR') return                     // no request for the tabs that don't need one
    loadDaily(applied.daily)                       // table (plant meter)
    loadDailyInverters(applied.daily)              // chart (per inverter) — same filters
    loadWms(wmsDayRange(applied.daily))            // WMS table — the selected day
  }, [type, applied.daily, loadDaily, loadDailyInverters, loadWms])

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
    loadMgrMatrix(applied.monthly)                  // Date × Inverter matrix for the month
    loadWms(wmsMonthRange(applied.monthly))        // WMS table — the whole selected month
  }, [type, applied.monthly, loadMonthly, loadMgrMatrix, loadWms])

  useEffect(() => {
    if (type !== 'YGR') return
    loadYearly(applied.yearly)
    loadYgrMatrix(applied.yearly)                   // Month × Inverter matrix for the year
    loadWms(wmsYearRange(applied.yearly))          // WMS table — the whole selected year
  }, [type, applied.yearly, loadYearly, loadYgrMatrix, loadWms])

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
  // MGR daily totals padded to EVERY day of the selected month: a day the plant meter has
  // no record for still appears as a row (generation 0 — display-only padding, exactly like
  // the YGR month padding; 0 adds nothing to any total). Only pads when there is some data,
  // so a genuinely empty month stays empty (no fabricated all-zero month).
  const mgrDailyFull = useMemo(() => {
    if (!mgr.rows.length) return mgr.rows
    const byDate = new Map(mgr.rows.map(r => [r.date, r]))
    return monthDays(applied.monthly.year, applied.monthly.month).map(d =>
      byDate.get(d.date) || { date: d.date, day: String(d.day), generation: 0 })
  }, [mgr.rows, applied.monthly])

  const rows = useMemo(() => {
    // DGR shows ONE summary row per day (06:00–20:00 aggregate), never the raw buckets.
    if (type === 'DGR') return summarizeDaily(dgr.rows)
    if (type === 'MGR') return mgrDailyFull
    // YGR: pad to the full Jan→Dec calendar for display (chart + table + export) when
    // there is any data; an empty year stays empty so no-data / errors aren't masked.
    return ygr.rows.length ? fillYearMonths(ygr.rows) : ygr.rows
  }, [type, dgr.rows, mgrDailyFull, ygr.rows])

  // YGR table data: Month | Generation (MWh) | INV1 … INV24. Merges the existing per-month
  // Generation (from ygr.rows, padded to all 12 months) with the per-inverter monthly cells
  // (ygrMatrix). Used by the on-screen table AND the YGR CSV/Excel/PDF exports.
  const ygrTable = useMemo(() => {
    const invNums = ygrMatrix.invNums
    const cellsByMonth = new Map(ygrMatrix.rows.map(r => [r.month_no, r.cells]))
    const monthRows = ygr.rows.length ? fillYearMonths(ygr.rows) : []
    const rows = monthRows.map(r => ({
      month: r.month, month_no: r.month_no, generation: r.generation,
      cells: cellsByMonth.get(r.month_no) || invNums.map(() => null),
    }))
    return { invNums, rows }
  }, [ygr.rows, ygrMatrix])

  const active  = type === 'DGR' ? dgr : type === 'MGR' ? mgr : ygr
  const loading = active ? active.loading : false
  const error   = active ? active.error : null

  const onApply = () => {
    setApplied({ daily, monthly, yearly })
    showToast(`${REPORTS[type].title} updated`)
  }

  // DGR: changing the date auto-applies (one refresh) — no need to click Apply Filter.
  // Both `daily` (the visible value) and `applied.daily` (which the load effect watches)
  // are set together, so the report refreshes exactly once for the newly picked date.
  const onDailyDate = (date) => {
    const next = { ...daily, date }
    setDaily(next)
    setApplied(a => ({ ...a, daily: next }))
  }

  // MGR: changing the Month or Year auto-applies (one fetch) — no Apply Filter click needed.
  // Both `monthly` (the visible selection) and `applied.monthly` (which the MGR load effect
  // watches) are set together, so the graph and the Inverter matrix refresh exactly once for
  // the newly selected period. `patch` carries only the changed field ({month} or {year}).
  const onMonthlyChange = (patch) => {
    setMonthly(m => ({ ...m, ...patch }))
    setApplied(a => ({ ...a, monthly: { ...a.monthly, ...patch } }))
  }

  const cols = useMemo(() => tableColumns(type), [type])

  // WMS matrix — the SAME horizontal Date × columns layout as the inverter matrix. Rows are
  // the real data buckets returned for the period (DGR: intraday, MGR: each day, YGR: each
  // month), COLUMNS are every WMS data column (exact DB names, DB order). The timestamp
  // (TimeCol) and audit fields (MSecCol/LocalCol/UserCol/ReasonCol) are excluded. Every cell
  // is the real stored value for that column at that bucket — no averaging, no repeated
  // totals. Purely a pivot of `wms.rows`; the WMS fetch/data is unchanged.
  const wmsMatrix = useMemo(() => {
    const AVG_SPECIALS = new Set(['ALL_WMS_AVG_MODULE_TEMP', 'TOTAL_IRRADIANCE'])
    const isDataCol = c => c.startsWith('AVG_') || AVG_SPECIALS.has(c) || /^WMS\d+_/.test(c)
    const columns = wmsColumns.filter(isDataCol)     // exact DB names; TimeCol + audit dropped

    // Row label from the bucket timestamp: date only for day/month buckets, date + time for
    // sub-daily (DGR) buckets, so no two rows carry the same label.
    const label = (ts) => {
      const d = new Date(ts)
      if (Number.isNaN(d.getTime())) return String(ts)
      const date = `${pad(d.getDate())}/${pad(d.getMonth() + 1)}/${d.getFullYear()}`
      return (d.getHours() || d.getMinutes()) ? `${date} ${pad(d.getHours())}:${pad(d.getMinutes())}` : date
    }

    let rows = (wms.rows || [])
      .map(r => ({ label: label(r.timestamp), ts: String(r.timestamp), cells: columns.map(c => num(r[c])) }))
      .sort((a, b) => a.ts.localeCompare(b.ts))       // chronological

    // MGR: pad to EVERY day of the selected month (daily buckets), so a day with no WMS
    // record still gets a row with empty cells — never dropped. DGR (intraday) and YGR
    // (monthly) rows are shown as returned.
    if (type === 'MGR') {
      const byDate = new Map(rows.map(r => [r.label, r]))
      rows = monthDays(applied.monthly.year, applied.monthly.month).map(d =>
        byDate.get(d.date) || { label: d.date, ts: d.key, cells: columns.map(() => null) })
    }

    return { columns, rows }
  }, [wms.rows, wmsColumns, type, applied.monthly])

  // WMS per-station CARDS (DGR): the WMS columns grouped by station — Average WMS (the DB's
  // AVG_* plant-average columns), then WMS 1 … 4 (WMSn_* columns). Each parameter's value is
  // the AVERAGE across the day's data buckets (nulls skipped), so the intraday timestamps
  // collapse into ONE clean value per parameter and NO date/time is shown. Values come
  // straight from the stored DB columns — nothing hardcoded. Display only; the SQL/retrieval
  // is unchanged (this only averages the rows already fetched). Station prefix is stripped
  // from the parameter label for readability; the parameter itself is unchanged.
  const wmsCards = useMemo(() => {
    const groups = [
      { key: 'AVG',  title: 'Average WMS', match: c => /^AVG_/.test(c) },
      { key: 'WMS1', title: 'WMS 1',       match: c => /^WMS1_/.test(c) },
      { key: 'WMS2', title: 'WMS 2',       match: c => /^WMS2_/.test(c) },
      { key: 'WMS3', title: 'WMS 3',       match: c => /^WMS3_/.test(c) },
      { key: 'WMS4', title: 'WMS 4',       match: c => /^WMS4_/.test(c) },
    ]
    const rows = wms.rows || []
    const mean = (col) => {
      let s = 0, n = 0
      for (const r of rows) {
        const v = num(r[col])
        if (v != null && Number.isFinite(v)) { s += v; n++ }
      }
      return n ? s / n : null
    }
    const strip = c => c.replace(/^AVG_/, '').replace(/^WMS\d+_/, '')
    return groups
      .map(g => ({
        key: g.key, title: g.title,
        params: wmsColumns.filter(g.match).map(c => ({ name: strip(c), value: mean(c) })),
      }))
      .filter(g => g.params.length)            // a station with no columns is omitted
  }, [wms.rows, wmsColumns])

  // Export — always the currently selected report only.
  const doExport = (fn, kind) => {
    if (!rows.length) { showToast('Nothing to export — no data for the selected filter'); return }
    fn(type, rows, `${type}_Report_${todayDMY()}`)
    showToast(`${kind} exported`)
  }

  // Excel export. MGR is a clean Tracker-style header + metadata sheet (no graph, no daily
  // table). The Excel button is shown only for MGR and YGR (DGR exports PDF only). Each is
  // ONE workbook with two sheets — the WMS data rides along as its own sheet, so removing the
  // section-level WMS Excel button loses no functionality:
  //   MGR → sheets: Monthly Generation + WMS Data
  //   YGR → sheets: Yearly Generation (Month | Generation (MWh) | INVERTER_01…INVERTER_24) + WMS Data
  const onExcelExport = () => {
    if (!rows.length) { showToast('Nothing to export — no data for the selected filter'); return }
    if (type === 'MGR') exportMgrExcel(mgrMatrix, wmsMatrix, monthLabel)
    // Selected year is dynamic; MGR-style layout (Title → Year → bar chart → Inverter → WMS).
    else if (type === 'YGR') exportYgrExcel(ygrTable, 'Generation (MWh)', wmsMatrix, applied.yearly.year)
    showToast('Excel exported')
  }

  // CSV export. YGR uses the new Month | Generation | INV1 … INV24 layout; the other reports
  // keep the generic column export.
  const onCsvExport = () => {
    if (type === 'YGR') {
      if (!ygrTable.rows.length) { showToast('Nothing to export — no data for the selected filter'); return }
      exportYgrCsv(ygrTable, 'Generation (MWh)')
      showToast('CSV exported')
      return
    }
    doExport(exportCSV, 'CSV')
  }

  // Map a REPORTS column definition to a PDF-table column (label→header, kind→align/format).
  const pdfColumns = (columns, numFmt = fmt3) => columns.map(c => ({
    header:  c.label,
    dataKey: c.key,
    align:   c.kind === 'num' ? 'center' : 'left',   // numeric → centred (req 2); header follows
    format:  c.kind === 'num' ? numFmt : undefined,
  }))

  // Assemble the PDF for the active report: same charts + tables + data on screen.
  const buildPdfConfig = (chart) => {
    const base = { plant: PLANT_NAME, reportTitle: REPORTS[type].title,
                   charts: chart ? [chart] : [] }

    if (type === 'DGR') {
      // DGR PDF: header → meta → Inverter-wise Generation blocks (same INV1…INV24 the
      // dashboard shows) → the Daily Generation table → WMS Data cards. The inverter grid
      // paginates cleanly onto a second page if the blocks don't all fit (WMS cards still
      // auto-size to the space left on their page). No inverter chart (charts:[]).
      return {
        ...base,
        charts: [],
        // Minimal footer: ONLY "Page X of Y" on every page (no rule, no generated time, no
        // centre text). Header stays page-1-only; pagination/content unchanged.
        footer: 'minimal',
        // No band subtitle for DGR — the date is shown once, as the REPORT DATE detail below
        // the title (client layout), so it is not repeated as a "Date: …" line in the header.
        filename: `DGR_Report_${todayDMY()}.pdf`,
        // Header/details show ONLY Report Date (client request) — Time Range, Interval, Total
        // Generation, Peak Power and Generated were removed. The report goes straight from the
        // Report Date into the Inverter-wise Generation blocks and the Daily Generation table.
        meta: [
          { label: 'Report Date', value: applied.daily.date.split('-').reverse().join('/') },
        ],
        // Exact same inverter blocks the dashboard renders (buildInverterBoxes + fmtKwh) —
        // INV1…INV24 in order, real per-inverter kWh for the selected date, never hardcoded.
        inverterBlocks: buildInverterBoxes(dgrInv.rows)
          .map(b => ({ name: b.name, value: fmtKwh(b.generation) })),
        tables: [{ title: 'Daily Generation Report', columns: pdfColumns(REPORTS.DGR.columns), rows }],
        // Same per-station cards shown on screen (Average WMS + WMS 1 … 4). Params are mapped
        // explicitly to {name, value} from the SAME wmsCards memo the dashboard renders — the
        // exact values (mean over wms.rows), never a separate/empty dataset — so every value
        // that shows on screen is passed straight into the PDF renderer.
        wmsCards: wmsCards.map(c => ({
          title: c.title,
          params: c.params.map(p => ({ name: p.name, value: p.value })),
        })),
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
          { label: 'Days', value: `${mgrDailyFull.length}` },
          { label: 'Total Generation', value: `${fmt3(sum(mgrDailyFull, 'generation'))} kWh` },
          { label: 'Generated', value: todayDMY().replace(/-/g, '/') },
        ],
        tables: [
          { title: `Monthly Generation by Inverter — ${monthLabel} (kWh)`,
            // Date × Inverter matrix (same as the on-screen table, padded to every day).
            columns: [
              { header: 'Date', dataKey: 'date', align: 'left' },
              ...mgrMatrix.invNums.map(n => ({
                header: `Inverter ${n}`, dataKey: `inv${n}`, align: 'right', format: fmt2,
              })),
            ],
            rows: mgrMatrix.rows.map(r => ({
              date: r.date,
              ...Object.fromEntries(mgrMatrix.invNums.map((n, i) => [`inv${n}`, r.cells[i]])),
            })) },
          // "Daily Generation Totals — <month>" (Date | Generation kWh) table removed from
          // the MGR PDF per client request. The Date × Inverter matrix above is the only
          // MGR table now; the graph + matrix remain unchanged.
        ],
      }
    }

    // YGR — ONE continuous report: the Yearly Inverter Generation table followed by the WMS
    // Report table, in the same PDF (flows onto further pages naturally, never a separate
    // report). The meta card leads with Year | Inverters (both dynamic). Inverter data and WMS
    // data are unchanged from the on-screen tables (values/columns/ordering identical).
    const wmsCols = wmsMatrix?.columns || []
    return {
      ...base,
      subtitle: `Year: ${applied.yearly.year}  |  Inverters: ${ygrTable.invNums.length}`,
      filename: `YGR_Report_${todayDMY()}.pdf`,
      meta: [
        { label: 'Year', value: `${applied.yearly.year}` },
        { label: 'Inverters', value: `${ygrTable.invNums.length}` },
        { label: 'Annual Generation', value: `${fmt3(sum(rows, 'generation'))} MWh` },
        { label: 'Generated', value: todayDMY().replace(/-/g, '/') },
      ],
      tables: [
        {
          title: 'Yearly Inverter Generation Report',
          columns: [
            { header: 'Month', dataKey: 'month', align: 'left' },
            { header: 'Generation (MWh)', dataKey: 'generation', align: 'right', format: fmt3 },
            ...ygrTable.invNums.map(n => ({ header: invName(n), dataKey: `inv${n}`, align: 'right', format: fmt3 })),
          ],
          rows: ygrTable.rows.map(r => ({
            month: r.month, generation: r.generation,
            ...Object.fromEntries(ygrTable.invNums.map((n, i) => [`inv${n}`, r.cells[i]])),
          })),
        },
        // WMS Report — Date × every WMS column (Average WMS + WMS 1 … N), same wmsMatrix data
        // the Excel "WMS Report" section and the on-screen WMS table use. Drawn as a normal
        // table so it continues after the inverter table (and paginates) within the SAME report.
        {
          title: 'WMS Report',
          columns: [
            { header: 'Date', dataKey: 'date', align: 'left' },
            ...wmsCols.map((c, i) => ({ header: c, dataKey: `w${i}`, align: 'right', format: fmt3 })),
          ],
          rows: (wmsMatrix?.rows || []).map(r => ({
            date: r.label,
            ...Object.fromEntries(r.cells.map((v, i) => [`w${i}`, v])),
          })),
        },
      ],
    }
  }

  const exportPdf = async () => {
    if (!rows.length) { showToast('Nothing to export — no data for the selected filter'); return }
    // DGR embeds the inverter-wise blocks and the WMS cards. Both the inverter series (dgrInv)
    // and the WMS readings (wms.rows — the source of every WMS value) load on SEPARATE requests
    // from the table (rows). Guard against exporting before they arrive — otherwise the inverter
    // blocks would render as 0 kWh and the WMS values would come through blank (only names),
    // while the dashboard already shows the real values.
    if (type === 'DGR' && (dgrInv.loading || !dgrInv.rows.length)) {
      showToast(dgrInv.loading
        ? 'Inverter data is still loading — please retry in a moment'
        : 'Inverter data not available yet — please wait for the dashboard to finish loading')
      return
    }
    if (type === 'DGR' && wms.loading) {
      showToast('WMS data is still loading — please retry in a moment')
      return
    }
    setPdfBusy(true)
    try {
      // Capture the on-screen chart at high resolution (null if not yet rendered). MGR/YGR
      // embed their generation bar chart (a raster of the same SVG, so the PDF bars match the
      // UI). DGR's PDF is a single page (table + WMS cards, no chart), so its chart is skipped.
      const chart = type === 'DGR'
        ? null
        : await captureReportChart(chartRefs[type].current, '#141928')
      if (chart) chart.title = type === 'MGR' ? `Daily Generation During ${monthLabel} (kWh)`
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
      // Same 'green' accent as Annual Generation so the two YGR KPI cards are visually
      // consistent (removes the stray blue top line/glow). Value + '%' unit unchanged.
      { label: 'Avg PR',            value: prRows.length ? fmt3(avg(prRows, 'pr')) : '—', unit: '%', color: 'green' },
    ]
  }, [type, rows])

  const chartRows = useMemo(() => (type === 'DGR' ? downsample(rows) : rows), [type, rows])

  // YGR: the bar chart must ALWAYS run Jan→Dec (all 12 months; missing months show a
  // zero bar via fillYearMonths). `rows` is already the padded, calendar-ordered set;
  // this sort is just a defensive guarantee of order and never reorders by value.
  const ygrChartRows = useMemo(
    () => [...rows].sort((a, b) => (a.month_no ?? 0) - (b.month_no ?? 0)),
    [rows],
  )

  const monthLabel = `${MONTHS[applied.monthly.month - 1]} ${applied.monthly.year}`

  // Period the WMS section covers, phrased for the active report level (used in the
  // "No data available for …" message): DGR → a date, MGR → a month, YGR → a year.
  const wmsPeriodLabel = type === 'DGR'
    ? applied.daily.date.split('-').reverse().join('/')
    : type === 'MGR'
      ? monthLabel
      : `${applied.yearly.year}`

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
        {/* DGR exports PDF only (client requirement); MGR/YGR keep CSV + Excel. */}
        {type !== 'DGR' && (
          <button className="btn btn-outline btn-sm" onClick={onCsvExport} disabled={loading || pdfBusy}>📊 CSV</button>
        )}
        {type !== 'DGR' && (
          <button className="btn btn-outline btn-sm" onClick={onExcelExport} disabled={loading || pdfBusy}>📗 Excel</button>
        )}
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
        onApply={onApply} onDailyDate={onDailyDate} onMonthlyChange={onMonthlyChange} loading={loading}
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
          {/* Inverter-wise generation — a clean grid of 24 equal-sized blue boxes
              (INV1…INV24), each showing that inverter's kWh for the selected date. */}
          <ChartCard title="Inverter-Wise Generation (kWh)" height="h-52">
            <ChartBody
              loading={dgrInv.loading}
              error={dgrInv.error}
              empty={!dgrInv.rows.length}
              emptyMsg="No data available"
            >
              <InverterGenerationGrid data={dgrInv.rows} />
            </ChartBody>
          </ChartCard>
        </div>
      )}
      {type === 'MGR' && (
        <div className="grid grid-cols-1 gap-3 mb-3" ref={chartRefs.MGR}>
          {/* One bar per day of the selected month — same `daily` data the plant total
              is summed from — so the title names the month it belongs to. */}
          {/* Taller than the other compact charts so the vertical per-bar value labels
              (up to 31 days) have headroom and never clip. */}
          <ChartCard title={`Daily Generation During ${monthLabel} (kWh)`} height="h-72">
            <ChartBody loading={loading} error={error} empty={!chartRows.length}>
              {/* Axis titles added for client presentation: X = Date, Y = Generation (kWh).
                  Same data/bars — only the labelling improved. Bar colour is the same blue
                  as the DGR inverter boxes (client request: no green, consistent blue).
                  showValues prints each day's exact generation permanently on the bar
                  (UI + PDF), not just on hover. */}
              <MiniBar data={chartRows} xKey="day" yKey="generation" name="Generation" unit=" kWh"
                color="#1e5fd8" xLabel="Date" yLabel="Generation (kWh)" showValues allTicks />
            </ChartBody>
          </ChartCard>
        </div>
      )}
      {type === 'YGR' && (
        <div className="grid grid-cols-1 gap-3 mb-3" ref={chartRefs.YGR}>
          {/* Taller so each month's permanent value label has headroom (no clipping). */}
          <ChartCard title="Monthly Generation (MWh)" height="h-72">
            {/* Always the full Jan→Dec sequence in calendar order; months without data
                show a zero bar. maxBarSize keeps every bar a clean, consistent width.
                Bar colour matches the DGR inverter boxes (client request: consistent blue).
                showValues prints each month's exact generation permanently on the bar. */}
            <MiniBar data={ygrChartRows} xKey="m" yKey="generation" name="Generation"
              unit=" MWh" color="#1e5fd8" maxBarSize={26} showValues />
          </ChartCard>
        </div>
      )}

      {/* MGR bottom: the Monthly Generation by Inverter table now spans the FULL page
          width. The previous 2-column grid and the Daily Generation Trend chart beside
          it were removed — the table is rendered on its own so no empty space is left. */}
      {type === 'MGR' ? (
        <InverterMonthlyMatrix
          matrix={mgrMatrix}
          monthLabel={monthLabel}
        />
      ) : type === 'YGR' ? (
        <YgrInverterTable
          table={ygrTable}
          genLabel="Generation (MWh)"
          loading={ygr.loading || ygrMatrix.loading}
          error={ygr.error || ygrMatrix.error}
        />
      ) : reportTable}

      {/* WMS Data — SEPARATE cards per group (client's drawing): AVG WMS + WMS1…WMS4, each a
          self-contained, scrollable Parameter | Value list. Data-driven from the WMS schema
          so EVERY column appears and no station's values are mixed. Shown on ALL three
          report levels (DGR: day, MGR: month, YGR: year) via the shared WMS loader —
          identical layout everywhere. Responsive: 1 → 2 → 3 cards per row. */}
      {(type === 'DGR' || type === 'MGR' || type === 'YGR') && (
        <div className="mt-3">
          {/* Section header. DGR shows NO date (client request: no Date/Time in the WMS
              section); MGR/YGR keep the selected period label on the left as before. */}
          <div className="flex items-baseline gap-3 mb-2">
            <div className="card-title mb-0">WMS Data</div>
            {type !== 'DGR' && (
              <span className="text-[11px] font-mono text-ge-text2">Date: {wmsPeriodLabel}</span>
            )}
          </div>
          {wms.loading ? (
            <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
              {[...Array(6)].map((_, i) => <div key={i} className="h-64 bg-ge-surface rounded animate-pulse" />)}
            </div>
          ) : wms.error ? (
            <div className="card py-8 text-center text-[12px] text-red-400">{wms.error}</div>
          ) : !wms.rows.length ? (
            <div className="card py-10 text-center text-[12px] text-ge-text3">
              {type === 'DGR' ? 'No WMS data available' : `No data available for ${wmsPeriodLabel}`}
            </div>
          ) : type === 'DGR' ? (
            // DGR: per-station CARDS (Average WMS + WMS 1 … 4). Each card is a self-contained
            // Parameter | Value list with NO Date/Time — the value is the day's average of the
            // stored readings (see wmsCards). Responsive: 1 → 2 → 3 cards per row. DGR exports
            // via PDF only (client requirement), so there is no Excel button here.
            <div>
              <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
                {wmsCards.map(card => (
                  <div key={card.key} className="card">
                    <div className="card-title mb-2">{card.title}</div>
                    <div className="overflow-y-auto max-h-72 border border-ge-border rounded">
                      <table className="data-table">
                        <thead>
                          <tr>
                            <th className="text-left">Parameter</th>
                            <th className="text-right">Value</th>
                          </tr>
                        </thead>
                        <tbody>
                          {card.params.map(p => (
                            <tr key={p.name}>
                              <td className="text-left font-mono text-[11px] whitespace-nowrap">{p.name}</td>
                              <td className="text-right font-mono text-[11px]">{fmt3(p.value)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          ) : (
            // Horizontal Date × WMS-columns matrix — same layout/format as the inverter
            // matrix. Scrolls sideways; the Date column stays pinned (sticky) so every WMS
            // column is readable. One row per real data bucket; every cell is the stored value.
            <div className="card">
              {/* No section-level WMS Excel button (MGR & YGR both export WMS via the single
                  top Excel button, as the "WMS Data" sheet in the one workbook). */}
              <div className="overflow-x-auto border border-ge-border rounded">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th className="text-left sticky left-0 z-20 bg-ge-surface whitespace-nowrap">Date</th>
                      {wmsMatrix.columns.map(c => (
                        <th key={c} className="text-right whitespace-nowrap">{c}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {wmsMatrix.rows.map(r => (
                      <tr key={r.ts}>
                        <td className="text-left font-mono text-[11px] whitespace-nowrap sticky left-0 z-10 bg-ge-surface">
                          {r.label}
                        </td>
                        {r.cells.map((v, i) => (
                          <td key={i} className="text-right font-mono text-[11px]">{fmt3(v)}</td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
