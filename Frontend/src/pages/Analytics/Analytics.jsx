// src/pages/Analytics/Analytics.jsx
//
// Production SCADA analytics dashboard — every KPI, chart and table is computed
// from real SQL Server telemetry via /analytics/overview. One date range drives
// the whole page; an equipment filter scopes the per-inverter ranking.

import React, { useState, useEffect, useCallback, useRef, useMemo } from 'react'
import {
  AreaChart, Area, BarChart, Bar, LineChart, Line, XAxis, YAxis,
  CartesianGrid, Tooltip, ResponsiveContainer,
} from 'recharts'
import { KpiCard, PageHeader, DataTable, Skeleton, Spinner } from '../../components/Common'
import { RECHARTS_COLORS as C } from '../../utils/helpers'
import { fetchAnalyticsOverview } from '../../services/api'
import { useApp } from '../../utils/AppContext'
import { captureChartPng } from '../../utils/chartCapture'
import { buildReportPdf } from '../../utils/pdfReport'

const PLANT_NAME = 'Kalyon Solar Power Plant'
// Shared compact chart height across Analytics, DGR, MGR and YGR — 192px (h-48),
// ~33–40% shorter than the previous h-72/h-80 so charts fit on one screen.
const COMPACT_CHART_H = 'h-48'
const AXIS = { fill: C.text3, fontSize: 10 }
const TOOLTIP = { background: '#1a2035', border: '1px solid #2a3350', borderRadius: 6, fontSize: 11 }
const CHART_BG = '#141928'

const KPI_LAYOUT = [
  { key: 'today_energy',  color: 'green'  },
  { key: 'period_energy', color: 'blue'   },
  { key: 'avg_pr',        color: 'amber'  },
  { key: 'avg_cuf',       color: 'purple' },
  { key: 'availability',  color: 'green'  },
  { key: 'peak_power',    color: 'blue'   },
]

const fmt = (v, d = 1) =>
  v === null || v === undefined ? '—' : Number(v).toLocaleString(undefined, { maximumFractionDigits: d })
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
  { key: 'rank', label: 'Rank', className: 'font-mono text-[11px]' },
  { key: 'inverter', label: 'Inverter', className: 'font-mono text-[11px]' },
  { key: 'generation_kwh', label: 'Generation (kWh)', className: 'font-mono text-[11px] text-right',
    render: v => <span className="text-ge-accent">{fmt(v, 2)}</span> },
  { key: 'pr', label: 'PR (%)', className: 'font-mono text-[11px] text-right',
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

export default function Analytics() {
  const { showToast } = useApp()
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const [equipment, setEquipment] = useState('all')
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [pdfBusy, setPdfBusy] = useState(false)
  const reqId = useRef(0)

  // Chart wrappers, captured for the PDF export.
  const refs = { daily: useRef(null), monthly: useRef(null), top: useRef(null), pr: useRef(null) }

  const load = useCallback(async (opts = {}) => {
    const id = ++reqId.current
    setLoading(true); setError(null)
    try {
      const d = await fetchAnalyticsOverview({ from: opts.from ?? from, to: opts.to ?? to, equipment: opts.equipment ?? equipment })
      if (id !== reqId.current) return
      setData(d)
      // Seed the date inputs from the resolved range on first load.
      if (!from) setFrom(d.range.from)
      if (!to) setTo(d.range.to)
    } catch (e) {
      if (id !== reqId.current) return
      setError(e.message || 'Failed to load analytics')
    } finally {
      if (id === reqId.current) setLoading(false)
    }
  }, [from, to, equipment])

  useEffect(() => { load() /* initial load with server defaults */ }, [])  // eslint-disable-line

  const onRefresh = () => load()
  const onEquipment = (v) => { setEquipment(v); load({ equipment: v }) }

  const kpis = data?.kpis || {}
  const daily = data?.daily || []
  const monthly = data?.monthly || []
  const prTrend = data?.pr_trend || []
  const inverters = data?.inverters || []
  const equipmentOptions = data?.equipment_options || ['all']

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
    const th = t => `<th style="background:#F2F2F2;color:#000000;border:1px solid #000000;padding:6px 8px;font-family:Calibri;font-weight:bold">${esc(t)}</th>`
    const td = (v, right) => `<td style="border:1px solid #000000;padding:4px 8px;font-family:Calibri;color:#000000;text-align:${right ? 'right' : 'left'}">${esc(v)}</td>`
    const tr = (cells, i) => `<tr style="background:${i % 2 === 0 ? '#FFFFFF' : '#FAFAFA'}">${cells}</tr>`
    const kpiRows = KPI_LAYOUT.map(({ key }, i) =>
      tr(`${td(kpis[key]?.label || key)}${td(fmt(kpis[key]?.value), true)}${td(kpis[key]?.unit || '')}`, i)).join('')
    const perfRows = list => list.map((r, i) =>
      tr(`${td(r.rank)}${td(r.inverter)}${td(fmt(r.generation_kwh, 2), true)}${td(r.pr == null ? '—' : fmt(r.pr, 1), true)}`, i)).join('')
    const perfHead = `<tr>${['Rank', 'Inverter', 'Generation (kWh)', 'PR (%)'].map(th).join('')}</tr>`
    const html = `<html><head><meta charset="utf-8"></head><body style="font-family:Calibri;color:#000000">
      <h2 style="color:#000000;font-size:16pt;text-align:left">${esc(PLANT_NAME)} — Plant Analytics Report</h2>
      <p style="color:#000000;text-align:left">Period: ${esc(rangeLabel)}<br/>Generated: ${esc(stamp())}</p>
      <h3 style="color:#000000;text-align:left">Key Performance Indicators</h3>
      <table style="border-collapse:collapse;color:#000000"><tr>${['Metric', 'Value', 'Unit'].map(th).join('')}</tr>${kpiRows}</table>
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
      <PageHeader title="Analytics & Performance" subtitle="Plant performance analytics from live telemetry">
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
          <div className="flex flex-col gap-1">
            <label className="form-label">Equipment</label>
            <select className="form-control" value={equipment} onChange={e => onEquipment(e.target.value)}>
              {equipmentOptions.map(o => (
                <option key={o} value={o}>{o === 'all' ? 'All Inverters' : o}</option>
              ))}
            </select>
          </div>
          <button className="btn btn-primary btn-sm" onClick={onRefresh} disabled={loading}>
            {loading ? <><Spinner size={12} /> Loading…</> : '⟳ Refresh'}
          </button>
          {error && <span className="text-[12px] text-red-400 ml-1">{error}</span>}
        </div>
      </div>

      {/* KPIs */}
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3 mb-6">
        {KPI_LAYOUT.map(({ key, color }) => {
          const k = kpis[key] || {}
          return loading && !data
            ? <Skeleton key={key} h="h-24" className="rounded-lg" />
            : <KpiCard key={key} label={k.label || key} value={fmt(k.value)} unit={k.unit || ''} color={color} />
        })}
      </div>

      {/* Charts row 1 */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3 mb-3">
        <ChartCard ref={refs.daily} title="Daily Generation Trend (MWh)" loading={loading && !data} error={error} empty={!daily.length}>
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={daily} margin={{ top: 8, right: 14, left: 2, bottom: 4 }}>
              <defs>
                <linearGradient id="ana-gen" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={C.accent} stopOpacity={0.45} />
                  <stop offset="100%" stopColor={C.accent} stopOpacity={0.02} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke={C.grid} vertical={false} />
              <XAxis dataKey="label" tick={AXIS} axisLine={false} tickLine={false} minTickGap={28} />
              <YAxis tick={AXIS} axisLine={false} tickLine={false} width={52} />
              <Tooltip contentStyle={TOOLTIP} labelStyle={{ color: C.text3 }}
                formatter={v => [`${fmt(v)} MWh`, 'Generation']} />
              <Area type="monotone" dataKey="generation_mwh" name="Generation" stroke={C.accent}
                strokeWidth={2} fill="url(#ana-gen)" dot={false} activeDot={{ r: 4 }} />
            </AreaChart>
          </ResponsiveContainer>
        </ChartCard>

        <ChartCard ref={refs.monthly} title="Monthly Generation Trend (MWh)" loading={loading && !data} error={error} empty={!monthly.length}>
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={monthly} margin={{ top: 8, right: 14, left: 2, bottom: 4 }}>
              <CartesianGrid strokeDasharray="3 3" stroke={C.grid} vertical={false} />
              <XAxis dataKey="label" tick={AXIS} axisLine={false} tickLine={false} />
              <YAxis tick={AXIS} axisLine={false} tickLine={false} width={52} />
              <Tooltip cursor={{ fill: 'rgba(255,255,255,0.04)' }} contentStyle={TOOLTIP} labelStyle={{ color: C.text3 }}
                formatter={v => [`${fmt(v)} MWh`, 'Generation']} />
              <Bar dataKey="generation_mwh" name="Generation" fill={C.blue} radius={[3, 3, 0, 0]} fillOpacity={0.9} />
            </BarChart>
          </ResponsiveContainer>
        </ChartCard>
      </div>

      {/* Charts row 2 */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3 mb-3">
        <ChartCard ref={refs.top} title="Top 10 Inverters by Generation (kWh)" loading={loading && !data} error={error} empty={!top10.length}>
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={top10} layout="vertical" margin={{ top: 4, right: 18, left: 8, bottom: 4 }}>
              <CartesianGrid strokeDasharray="3 3" stroke={C.grid} horizontal={false} />
              <XAxis type="number" tick={AXIS} axisLine={false} tickLine={false}
                tickFormatter={v => (v >= 1000 ? `${Math.round(v / 1000)}k` : v)} />
              <YAxis type="category" dataKey="inverter" tick={AXIS} axisLine={false} tickLine={false} width={92} />
              <Tooltip cursor={{ fill: 'rgba(255,255,255,0.04)' }} contentStyle={TOOLTIP} labelStyle={{ color: C.text3 }}
                formatter={v => [`${fmt(v, 0)} kWh`, 'Generation']} />
              <Bar dataKey="generation_kwh" name="Generation" fill={C.accent} radius={[0, 3, 3, 0]} fillOpacity={0.9} />
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
              <Line type="monotone" dataKey="pr" name="PR" stroke={C.amber} strokeWidth={2}
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
    </div>
  )
}

// PDF table columns (align/format for the shared pdf builder).
const pdfPerfCols = [
  { header: 'Rank', dataKey: 'rank', align: 'left' },
  { header: 'Inverter', dataKey: 'inverter', align: 'left' },
  { header: 'Generation (kWh)', dataKey: 'generation_kwh', align: 'right', format: v => fmt(v, 2) },
  { header: 'PR (%)', dataKey: 'pr', align: 'right', format: v => (v == null ? '—' : fmt(v, 1)) },
]
