// src/pages/Dashboard/InverterTempPanel.jsx
//
// Inverter Temperature section for the Operations Dashboard.
// Live IGBT-heatsink temperature per inverter (real data via the dashboard hook),
// colour-coded with min/max/avg + hottest/coolest, and a click-through modal that
// shows the temperature trend (1h / 24h / 7d) fetched on demand. No mock data.
import React, { useState, useEffect, useMemo } from 'react'
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, ReferenceLine,
} from 'recharts'
import { Spinner, Skeleton } from '../../components/Common'
import { useFetch } from '../../hooks/useFetch'
import { fetchInverterTempHistory } from '../../services/api'

// Thresholds: < 60 normal · 60–75 warning · > 75 critical.
function tempClass(t) {
  if (t == null) return { key: 'na', bar: '#3a4363', text: 'text-ge-text3', ring: 'border-ge-border' }
  if (t > 75)   return { key: 'crit', bar: '#e66767', text: 'text-red-300',    ring: 'border-red-500/50' }
  if (t >= 60)  return { key: 'warn', bar: '#e0a838', text: 'text-amber-300',  ring: 'border-amber-500/50' }
  return { key: 'ok', bar: '#199e70', text: 'text-emerald-300', ring: 'border-emerald-500/40' }
}
const fmt1 = v => (v == null || isNaN(v) ? '—' : Number(v).toFixed(1))
const pad = n => String(n).padStart(2, '0')
const fmtFull = ms => { const d = new Date(ms); return `${pad(d.getDate())}/${pad(d.getMonth() + 1)}/${d.getFullYear()} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}` }
const fmtAxis = ms => { const d = new Date(ms); return `${pad(d.getDate())}/${pad(d.getMonth() + 1)} ${pad(d.getHours())}:${pad(d.getMinutes())}` }

// ── Stat tile ────────────────────────────────────────────────────────────────
const Stat = ({ label, value, unit = '°C', sub, accent }) => (
  <div className="bg-ge-elevated border border-ge-border rounded-lg px-3 py-2">
    <div className="text-[10px] text-ge-text3 uppercase tracking-wider">{label}</div>
    <div className={`text-[16px] font-mono font-semibold ${accent || 'text-ge-text1'}`}>{value}<span className="text-[11px] text-ge-text3 ml-0.5">{unit}</span></div>
    {sub && <div className="text-[10px] text-ge-text3 font-mono truncate">{sub}</div>}
  </div>
)

// ── Temperature history modal ────────────────────────────────────────────────
const RANGES = [['1h', '1 Hour'], ['24h', '24 Hours'], ['7d', '7 Days']]

function TempHistoryModal({ inverter, onClose }) {
  const [range, setRange] = useState('24h')
  const { data, loading, error, refetch } = useFetch(
    () => fetchInverterTempHistory(inverter.id, range), [inverter.id, range],
  )
  useEffect(() => {
    const onKey = e => e.key === 'Escape' && onClose()
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  const series = useMemo(
    () => (data?.data ?? []).map(p => ({ _t: Date.parse(p.timestamp), temp: p.temp })).filter(p => !isNaN(p._t)),
    [data],
  )

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <div className="bg-ge-card border border-ge-border rounded-xl w-full max-w-3xl shadow-2xl" onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between px-4 py-3 border-b border-ge-border">
          <div>
            <div className="text-[14px] font-semibold text-ge-text1">🌡 {inverter.name} — Temperature Trend</div>
            <div className="text-[11px] text-ge-text3 font-mono">IGBT Heatsink Temperature (°C)</div>
          </div>
          <button onClick={onClose} className="text-ge-text3 hover:text-ge-text1 text-lg leading-none">✕</button>
        </div>

        <div className="px-4 py-2 flex items-center gap-1.5 border-b border-ge-border">
          {RANGES.map(([v, l]) => (
            <button key={v} onClick={() => setRange(v)}
              className={`px-2.5 py-1 text-[11px] font-mono rounded border transition-all
                ${range === v ? 'bg-ge-blue text-white border-ge-blue' : 'bg-ge-elevated border-ge-border text-ge-text2 hover:text-ge-text1'}`}>
              {l}
            </button>
          ))}
        </div>

        <div className="p-4 h-72">
          {loading ? (
            <Skeleton h="h-full" />
          ) : error ? (
            <div className="h-full flex flex-col items-center justify-center gap-2">
              <div className="text-[12px] text-ge-danger">Failed to load temperature history</div>
              <button onClick={refetch} className="btn btn-outline btn-sm">Retry</button>
            </div>
          ) : series.length === 0 ? (
            <div className="h-full flex items-center justify-center text-[12px] text-ge-text3">No data available for this range.</div>
          ) : (
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={series} margin={{ top: 6, right: 12, bottom: 4, left: -8 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#2c3350" />
                <XAxis dataKey="_t" type="number" scale="time" domain={['dataMin', 'dataMax']}
                  tickFormatter={fmtAxis} tick={{ fill: '#8a93a8', fontSize: 10 }} stroke="#3a4363" minTickGap={40} />
                <YAxis tick={{ fill: '#8a93a8', fontSize: 10 }} stroke="#3a4363" width={44} unit="°"
                  domain={['auto', 'auto']} />
                <ReferenceLine y={60} stroke="#e0a838" strokeDasharray="4 4" strokeOpacity={0.6} />
                <ReferenceLine y={75} stroke="#e66767" strokeDasharray="4 4" strokeOpacity={0.6} />
                <Tooltip
                  contentStyle={{ background: '#1a2035', border: '1px solid #2a3350', borderRadius: 6, fontSize: 11 }}
                  labelFormatter={ms => fmtFull(ms)}
                  formatter={v => [`${fmt1(v)} °C`, 'Temp']} />
                <Line type="monotone" dataKey="temp" stroke="#3987e5" strokeWidth={2} dot={false} isAnimationActive={false} />
              </LineChart>
            </ResponsiveContainer>
          )}
        </div>
      </div>
    </div>
  )
}

// ── Main panel ───────────────────────────────────────────────────────────────
export default function InverterTempPanel({ invTemps }) {
  const { data, loading, error, refetch } = invTemps
  const [selected, setSelected] = useState(null)

  const inverters = data?.inverters ?? []
  const isEmpty = !loading && !error && inverters.length === 0

  return (
    <div className="card">
      <div className="card-title justify-between">
        <span>🌡 Inverter Temperatures</span>
        <div className="flex items-center gap-3 text-[10px] font-mono text-ge-text3">
          <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full" style={{ background: '#199e70' }} />&lt;60</span>
          <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full" style={{ background: '#e0a838' }} />60–75</span>
          <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full" style={{ background: '#e66767' }} />&gt;75</span>
          {data?.timestamp && <span className="ml-1">Updated {data.timestamp}</span>}
        </div>
      </div>

      {loading ? (
        <div className="space-y-3">
          <div className="grid grid-cols-2 sm:grid-cols-5 gap-2">{[...Array(5)].map((_, i) => <Skeleton key={i} h="h-14" />)}</div>
          <div className="grid grid-cols-3 sm:grid-cols-6 lg:grid-cols-8 gap-2">{[...Array(16)].map((_, i) => <Skeleton key={i} h="h-14" />)}</div>
        </div>
      ) : error ? (
        <div className="py-10 flex flex-col items-center justify-center gap-2">
          <div className="text-[13px] text-ge-danger">⚠ Failed to load inverter temperatures</div>
          <button onClick={refetch} className="btn btn-outline btn-sm">Retry</button>
        </div>
      ) : isEmpty ? (
        <div className="py-10 text-center text-[12px] text-ge-text3">No data available</div>
      ) : (
        <>
          {/* Stats */}
          <div className="grid grid-cols-2 sm:grid-cols-5 gap-2 mb-3">
            <Stat label="Min" value={fmt1(data.min)} accent="text-emerald-300" />
            <Stat label="Average" value={fmt1(data.avg)} />
            <Stat label="Max" value={fmt1(data.max)} accent="text-red-300" />
            <Stat label="Hottest" value={fmt1(data.hottest?.temp)} sub={data.hottest?.name} accent="text-red-300" />
            <Stat label="Coolest" value={fmt1(data.coolest?.temp)} sub={data.coolest?.name} accent="text-emerald-300" />
          </div>

          {/* Per-inverter grid */}
          <div className="grid grid-cols-3 sm:grid-cols-6 lg:grid-cols-8 gap-2">
            {inverters.map(inv => {
              const c = tempClass(inv.temp)
              return (
                <button key={inv.id} onClick={() => setSelected(inv)} title={`${inv.name} — view trend`}
                  className={`bg-ge-elevated border ${c.ring} rounded-lg px-2 py-1.5 text-left hover:bg-ge-surface transition-colors`}>
                  <div className="text-[10px] text-ge-text3 font-mono truncate">{inv.name}</div>
                  <div className="flex items-baseline gap-1">
                    <span className={`text-[15px] font-mono font-semibold ${c.text}`}>{fmt1(inv.temp)}</span>
                    <span className="text-[9px] text-ge-text3">°C</span>
                  </div>
                  <div className="h-1 rounded-full mt-1" style={{ background: c.bar }} />
                </button>
              )
            })}
          </div>
        </>
      )}

      {selected && <TempHistoryModal inverter={selected} onClose={() => setSelected(null)} />}
    </div>
  )
}
