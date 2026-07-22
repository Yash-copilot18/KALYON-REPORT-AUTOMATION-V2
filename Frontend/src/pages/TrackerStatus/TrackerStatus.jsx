// src/pages/TrackerStatus/TrackerStatus.jsx
import React, { useEffect, useMemo, useState, useCallback } from 'react'
import {
  AreaChart, Area, LineChart, Line, XAxis, YAxis,
  CartesianGrid, Tooltip, ResponsiveContainer,
} from 'recharts'
import { PageHeader, KpiCard, Spinner, Skeleton } from '../../components/Common'
import { RECHARTS_COLORS as C } from '../../utils/helpers'
import { fetchTrackerTrend } from '../../services/api'
import { useTrackerStatus } from './useTrackerStatus'

// Configured live telemetry source for this dashboard.
const TABLE = 'T1_IS8'

const TOOLTIP_STYLE = {
  backgroundColor: '#1a2035', border: '1px solid #2a3350', borderRadius: 6,
  fontSize: 11, fontFamily: 'IBM Plex Mono, monospace', color: '#e8ecf4',
}

function fmtClock(ms) {
  if (!ms) return '—'
  const s = Math.max(0, Math.round((Date.now() - ms) / 1000))
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  return `${Math.floor(s / 3600)}h ago`
}

function fmtTs(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  return isNaN(d) ? iso
    : `${String(d.getDate()).padStart(2,'0')}/${String(d.getMonth()+1).padStart(2,'0')} ${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`
}

// ── Connection badge ───────────────────────────────────────────────────────────
function ConnBadge({ transport, connected }) {
  const map = {
    ws:         { dot: 'bg-emerald-400', text: 'Live · WebSocket', cls: 'text-emerald-400' },
    poll:       { dot: 'bg-amber-400 animate-pulse', text: 'Live · Polling', cls: 'text-amber-400' },
    connecting: { dot: 'bg-ge-text3 animate-pulse', text: 'Connecting…', cls: 'text-ge-text3' },
  }
  const s = map[transport] || map.connecting
  return (
    <span className="inline-flex items-center gap-1.5 text-[11px] font-mono">
      <span className={`w-1.5 h-1.5 rounded-full ${connected ? 'bg-emerald-400' : s.dot}`} />
      <span className={s.cls}>{s.text}</span>
    </span>
  )
}

// ── Tracker status grid ────────────────────────────────────────────────────────
const cellClass = (t) => {
  if (t.status === 'alarm')                                   return 'bg-red-500/25 border-red-500/60 text-red-200'
  if (t.battery_level != null && t.battery_level < 20)        return 'bg-amber-500/20 border-amber-500/50 text-amber-200'
  return 'bg-emerald-500/10 border-emerald-500/30 text-emerald-200'
}

function TrackerGrid({ trackers }) {
  const [q, setQ] = useState('')
  const filtered = useMemo(() => {
    const s = q.trim()
    return s ? trackers.filter(t => String(t.id).includes(s) || t.label.toLowerCase().includes(s.toLowerCase())) : trackers
  }, [trackers, q])

  return (
    <div>
      <div className="flex items-center gap-3 mb-2.5 flex-wrap">
        <div className="flex items-center gap-2 text-[11px]">
          <span className="inline-flex items-center gap-1"><span className="w-2.5 h-2.5 rounded-sm bg-emerald-500/50 border border-emerald-500/60" /> OK</span>
          <span className="inline-flex items-center gap-1"><span className="w-2.5 h-2.5 rounded-sm bg-amber-500/50 border border-amber-500/60" /> Low battery</span>
          <span className="inline-flex items-center gap-1"><span className="w-2.5 h-2.5 rounded-sm bg-red-500/50 border border-red-500/60" /> Alarm</span>
        </div>
        <div className="relative ml-auto w-40">
          <span className="absolute left-2.5 top-1/2 -translate-y-1/2 text-ge-text3 text-[11px]">🔍</span>
          <input value={q} onChange={e => setQ(e.target.value)} placeholder="Find tracker #…"
            className="form-control pl-7 text-[12px]" />
        </div>
      </div>
      <div className="grid grid-cols-6 sm:grid-cols-10 lg:grid-cols-12 xl:grid-cols-[repeat(16,minmax(0,1fr))] gap-1.5 max-h-[22rem] overflow-y-auto pr-1">
        {filtered.map(t => (
          <div key={t.id}
            title={`${t.label}\nStatus: ${t.status}\nAlarm: ${t.alarm}\nBattery: ${t.battery_level ?? '—'}%\nPosition: ${t.elevation_position ?? '—'}  Setpoint: ${t.elevation_setpoint ?? '—'}\nTracking error: ${t.tracking_error ?? '—'}\nMotor current: ${t.max_motor_current ?? '—'} A\nMode: ${t.operation_mode ?? '—'}`}
            className={`h-9 rounded border flex flex-col items-center justify-center leading-none cursor-default select-none ${cellClass(t)}`}>
            <span className="text-[10px] font-mono font-semibold">{t.id}</span>
            <span className="text-[8px] font-mono opacity-70">{t.battery_level != null ? `${Math.round(t.battery_level)}%` : '—'}</span>
          </div>
        ))}
        {filtered.length === 0 && (
          <div className="col-span-full text-center text-[12px] text-ge-text3 py-6">No trackers match “{q}”.</div>
        )}
      </div>
    </div>
  )
}

// ── Trend charts ────────────────────────────────────────────────────────────────
function TrendTooltip({ active, payload, label, unit = '' }) {
  if (!active || !payload?.length) return null
  return (
    <div style={TOOLTIP_STYLE} className="px-3 py-2">
      <p className="text-ge-text3 text-[10px] mb-1">{label}</p>
      {payload.map((p, i) => (
        <p key={i} style={{ color: p.color }} className="text-xs">
          {p.name}: <strong>{typeof p.value === 'number' ? p.value.toLocaleString() : p.value}{unit}</strong>
        </p>
      ))}
    </div>
  )
}

// ── Page ─────────────────────────────────────────────────────────────────────
export default function TrackerStatus() {
  const { snapshot, transport, connected, error, lastUpdated } = useTrackerStatus(TABLE)
  const [trend, setTrend] = useState(null)
  const [trendErr, setTrendErr] = useState(null)

  const loadTrend = useCallback(async () => {
    try {
      const d = await fetchTrackerTrend(TABLE, { interval: 'hourly' })
      setTrend((d.points || []).map(p => ({ ...p, label: fmtTs(p.timestamp) })))
      setTrendErr(null)
    } catch (e) { setTrendErr(e.message || 'Failed to load trend') }
  }, [])

  useEffect(() => { loadTrend() }, [loadTrend])

  const kpis = snapshot?.kpis
  const trackers = snapshot?.trackers || []
  const loading = !snapshot

  return (
    <div>
      <PageHeader title="Live Tracker Status" subtitle={`dbo.${TABLE} · real-time telemetry`}>
        <ConnBadge transport={transport} connected={connected} />
        <span className="text-[11px] font-mono text-ge-text3 ml-2">Updated {fmtClock(lastUpdated)}</span>
      </PageHeader>

      {error && (
        <div className="card mb-3 bg-amber-500/5 border-amber-500/40">
          <p className="text-[12px] text-amber-300">⚠ Live connection issue: {error} — retrying automatically.</p>
        </div>
      )}

      {/* KPI cards */}
      {loading ? (
        <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-3 mb-3">
          {[...Array(6)].map((_, i) => <Skeleton key={i} h="h-20" />)}
        </div>
      ) : (
        <>
          <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-3 mb-3">
            <KpiCard label="Total Trackers"   value={kpis.total}                          color="blue" />
            <KpiCard label="Healthy"          value={kpis.healthy}                        color="green" />
            <KpiCard label="Active Alarms"    value={kpis.active_alarms}                  color={kpis.active_alarms ? 'red' : 'green'} />
            <KpiCard label="Avg Battery"      value={kpis.avg_battery ?? '—'}  unit="%"   color="blue" />
            <KpiCard label="Avg Track Error"  value={kpis.avg_tracking_error ?? '—'}      color="purple" />
            <KpiCard label="Max Motor Curr."  value={kpis.max_motor_current ?? '—'} unit="A" color="amber" />
          </div>

          {/* Secondary stats */}
          <div className="card mb-3">
            <div className="flex flex-wrap items-center gap-x-6 gap-y-2 text-[12px]">
              <span className="text-ge-text3">Snapshot: <span className="font-mono text-ge-text1">{fmtTs(snapshot.timestamp)}</span></span>
              <span className="text-ge-text3">Min battery: <span className="font-mono text-ge-text1">{kpis.min_battery ?? '—'}%</span></span>
              <span className="text-ge-text3">Low-battery trackers: <span className="font-mono text-amber-300">{kpis.low_battery}</span></span>
              <span className="text-ge-text3">Max tracking error: <span className="font-mono text-ge-text1">{kpis.max_tracking_error ?? '—'}</span></span>
              <span className="text-ge-text3 flex items-center gap-1.5">Modes:
                {Object.entries(kpis.mode_distribution || {}).map(([m, n]) => (
                  <span key={m} className="font-mono text-ge-text1 bg-ge-elevated border border-ge-border rounded px-1.5 py-0.5">
                    {m}: {n}
                  </span>
                ))}
              </span>
            </div>
          </div>
        </>
      )}

      {/* Tracker grid */}
      <div className="card mb-3">
        <div className="card-title">🛰 Tracker Fleet — {trackers.length} units</div>
        {loading
          ? <div className="grid grid-cols-12 gap-1.5">{[...Array(48)].map((_, i) => <Skeleton key={i} h="h-9" />)}</div>
          : <TrackerGrid trackers={trackers} />}
      </div>

      {/* Trend charts */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
        <div className="card">
          <div className="card-title justify-between">
            <span>📈 Avg Battery Level (last 3 days)</span>
            {!trend && !trendErr && <Spinner size={12} />}
          </div>
          <div style={{ height: 220 }}>
            {trendErr ? <div className="text-[12px] text-ge-danger py-8 text-center">⚠ {trendErr}</div>
              : !trend ? <div className="space-y-1 pt-2">{[...Array(5)].map((_, i) => <Skeleton key={i} h="h-6" />)}</div>
              : (
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={trend} margin={{ top: 8, right: 8, left: -18, bottom: 0 }}>
                    <defs>
                      <linearGradient id="battGrad" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="5%" stopColor={C.accent} stopOpacity={0.3} />
                        <stop offset="95%" stopColor={C.accent} stopOpacity={0} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid strokeDasharray="3 3" stroke={C.grid} />
                    <XAxis dataKey="label" tick={{ fill: C.text3, fontSize: 9 }} axisLine={false} tickLine={false} minTickGap={24} />
                    <YAxis domain={[0, 100]} tick={{ fill: C.text3, fontSize: 10 }} axisLine={false} tickLine={false} tickFormatter={v => `${v}%`} />
                    <Tooltip content={<TrendTooltip unit="%" />} />
                    <Area type="monotone" dataKey="avg_battery" name="Avg Battery" stroke={C.accent} fill="url(#battGrad)" strokeWidth={2} dot={false} />
                  </AreaChart>
                </ResponsiveContainer>
              )}
          </div>
        </div>

        <div className="card">
          <div className="card-title"><span>🚨 Active Alarms &amp; Tracking Error (last 3 days)</span></div>
          <div style={{ height: 220 }}>
            {trendErr ? <div className="text-[12px] text-ge-danger py-8 text-center">⚠ {trendErr}</div>
              : !trend ? <div className="space-y-1 pt-2">{[...Array(5)].map((_, i) => <Skeleton key={i} h="h-6" />)}</div>
              : (
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={trend} margin={{ top: 8, right: 8, left: -18, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke={C.grid} />
                    <XAxis dataKey="label" tick={{ fill: C.text3, fontSize: 9 }} axisLine={false} tickLine={false} minTickGap={24} />
                    <YAxis yAxisId="a" tick={{ fill: C.text3, fontSize: 10 }} axisLine={false} tickLine={false} />
                    <YAxis yAxisId="b" orientation="right" tick={{ fill: C.text3, fontSize: 10 }} axisLine={false} tickLine={false} />
                    <Tooltip content={<TrendTooltip />} />
                    <Line yAxisId="a" type="monotone" dataKey="active_alarms" name="Active Alarms" stroke={C.danger} strokeWidth={2} dot={false} />
                    <Line yAxisId="b" type="monotone" dataKey="avg_tracking_error" name="Avg Tracking Error" stroke={C.amber} strokeWidth={2} dot={false} />
                  </LineChart>
                </ResponsiveContainer>
              )}
          </div>
        </div>
      </div>
    </div>
  )
}
