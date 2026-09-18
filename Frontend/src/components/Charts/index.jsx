// src/components/Charts/index.jsx
import React from 'react'
import {
  LineChart, Line, BarChart, Bar, AreaChart, Area,
  ScatterChart, Scatter, XAxis, YAxis, CartesianGrid,
  Tooltip, Legend, ResponsiveContainer, ReferenceLine, Cell, LabelList,
} from 'recharts'
import { RECHARTS_COLORS as C } from '../../utils/helpers'
import { columnValueLabel, domainMax, CHART_LABEL_COLOR } from '../../utils/chartValueLabel'

// Bar-chart axis ticks use the same bright, readable label colour as the bar value labels
// so all chart text is clearly legible on the dark background (client request). Line/area/
// scatter charts keep their existing muted ticks.
const BAR_TICK = { fill: CHART_LABEL_COLOR, fontSize: 10 }

const TOOLTIP_STYLE = {
  backgroundColor: '#1a2035',
  border: '1px solid #2a3350',
  borderRadius: 6,
  fontSize: 11,
  fontFamily: 'IBM Plex Mono, monospace',
  color: '#e8ecf4',
}

const CustomTooltip = ({ active, payload, label, unit = '' }) => {
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

// ── Power Trend Line Chart ────────────────────────────────────────────────────
export function PowerTrendChart({ data }) {
  return (
    <ResponsiveContainer width="100%" height="100%">
      <AreaChart data={data} margin={{ top: 4, right: 8, left: -16, bottom: 0 }}>
        <defs>
          <linearGradient id="powerGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="5%"  stopColor={C.blue} stopOpacity={0.25} />
            <stop offset="95%" stopColor={C.blue} stopOpacity={0}    />
          </linearGradient>
        </defs>
        <CartesianGrid strokeDasharray="3 3" stroke={C.grid} />
        <XAxis dataKey="hour" tick={{ fill: C.text3, fontSize: 10 }} axisLine={false} tickLine={false} />
        <YAxis tick={{ fill: C.text3, fontSize: 10 }} axisLine={false} tickLine={false}
               tickFormatter={v => `${v}MW`} />
        <Tooltip content={<CustomTooltip unit=" MW" />} />
        <Area type="monotone" dataKey="power" name="Power" stroke={C.blue}
              fill="url(#powerGrad)" strokeWidth={2} dot={false} />
      </AreaChart>
    </ResponsiveContainer>
  )
}

// ── Daily Energy Bar Chart (MWh/day, last 7 days) ──────────────────────────────
export function DailyEnergyChart({ data }) {
  return (
    <ResponsiveContainer width="100%" height="100%">
      <BarChart data={data} margin={{ top: 10, right: 8, left: -16, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke={C.grid} vertical={false} />
        <XAxis dataKey="label" tick={BAR_TICK} axisLine={false} tickLine={false} />
        <YAxis tick={BAR_TICK} axisLine={false} tickLine={false}
               domain={[0, domainMax(data, 'value', 1.5)]}
               tickFormatter={v => (v >= 1000 ? (v / 1000).toFixed(1) + 'k' : Math.round(v).toLocaleString())} />
        <Tooltip content={<CustomTooltip unit=" MWh" />} />
        {/* Permanent value label on every day's bar (visible without hovering). */}
        <Bar dataKey="value" name="Energy" fill={C.blue} radius={[3,3,0,0]}
             fillOpacity={0.85} isAnimationActive={false}>
          <LabelList dataKey="value" content={columnValueLabel({ unit: 'MWh', barCount: data?.length || 0 })} />
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  )
}

// ── Irradiance vs Power Scatter (one point per inverter, live) ────────────────
const IrrPowerTooltip = ({ active, payload }) => {
  if (!active || !payload?.length) return null
  const p = payload[0]?.payload || {}
  return (
    <div style={TOOLTIP_STYLE} className="px-3 py-2">
      {p.name && <p className="text-ge-text1 text-[11px] mb-0.5"><strong>{p.name}</strong></p>}
      <p className="text-ge-text3 text-[10px]">Irradiance: <strong>{Number(p.irr ?? 0).toLocaleString()} W/m²</strong></p>
      <p className="text-ge-accent text-[10px]">Active Power: <strong>{Number(p.pwr ?? 0).toFixed(3)} MW</strong></p>
    </div>
  )
}

export function IrradiancePowerChart({ data }) {
  return (
    <ResponsiveContainer width="100%" height="100%">
      <ScatterChart margin={{ top: 4, right: 8, left: -16, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke={C.grid} />
        <XAxis type="number" dataKey="irr" name="Irradiance" unit=" W/m²"
               domain={[0, (max) => Math.ceil((max + 60) / 50) * 50]}
               tick={{ fill: C.text3, fontSize: 10 }} axisLine={false} tickLine={false} />
        <YAxis type="number" dataKey="pwr" name="Power" unit=" MW"
               tick={{ fill: C.text3, fontSize: 10 }} axisLine={false} tickLine={false} />
        <Tooltip cursor={{ strokeDasharray: '3 3' }} content={<IrrPowerTooltip />} />
        <Scatter name="Irradiance vs Power" data={data} fill={C.accent} fillOpacity={0.7} />
      </ScatterChart>
    </ResponsiveContainer>
  )
}

// ── PR vs Irradiance Dual Axis ────────────────────────────────────────────────
export function PRIrradianceChart({ data }) {
  return (
    <ResponsiveContainer width="100%" height="100%">
      <LineChart data={data} margin={{ top: 4, right: 40, left: -16, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke={C.grid} />
        <XAxis dataKey="hour" tick={{ fill: C.text3, fontSize: 10 }} axisLine={false} tickLine={false} />
        <YAxis yAxisId="left"  tick={{ fill: C.text3, fontSize: 10 }} axisLine={false} tickLine={false}
               tickFormatter={v => v+'%'} domain={[70, 90]} />
        <YAxis yAxisId="right" orientation="right" tick={{ fill: C.amber, fontSize: 10 }}
               axisLine={false} tickLine={false} />
        <Tooltip content={<CustomTooltip />} />
        <Line yAxisId="left"  type="monotone" dataKey="pr"  name="PR (%)"
              stroke={C.accent} strokeWidth={2} dot={false} />
        <Line yAxisId="right" type="monotone" dataKey="irr" name="Irradiance"
              stroke={C.amber} strokeWidth={1.5} strokeDasharray="4 4" dot={false} />
      </LineChart>
    </ResponsiveContainer>
  )
}

// ── Inverter Comparison Horizontal Bar ────────────────────────────────────────
export function InverterComparisonChart({ data }) {
  return (
    <ResponsiveContainer width="100%" height="100%">
      <BarChart data={data} layout="vertical" margin={{ top: 4, right: 24, left: 20, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke={C.grid} horizontal={false} />
        <XAxis type="number" tick={BAR_TICK} axisLine={false} tickLine={false}
               tickFormatter={v => `${v}MW`} />
        <YAxis type="category" dataKey="id" tick={{ ...BAR_TICK, fontSize: 11 }}
               axisLine={false} tickLine={false} />
        <Tooltip content={<CustomTooltip unit=" MW" />} />
        <Bar dataKey="power" name="Output" fill={C.blue} radius={[0,3,3,0]} fillOpacity={0.85} />
      </BarChart>
    </ResponsiveContainer>
  )
}

// ── Weekly Energy Area ─────────────────────────────────────────────────────────
export function WeeklyEnergyChart({ data }) {
  return (
    <ResponsiveContainer width="100%" height="100%">
      <AreaChart data={data} margin={{ top: 4, right: 8, left: -16, bottom: 0 }}>
        <defs>
          <linearGradient id="weekGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="5%"  stopColor={C.accent} stopOpacity={0.2} />
            <stop offset="95%" stopColor={C.accent} stopOpacity={0}   />
          </linearGradient>
        </defs>
        <CartesianGrid strokeDasharray="3 3" stroke={C.grid} />
        <XAxis dataKey="label" tick={{ fill: C.text3, fontSize: 10 }} axisLine={false} tickLine={false} />
        <YAxis tick={{ fill: C.text3, fontSize: 10 }} axisLine={false} tickLine={false}
               tickFormatter={v => (v/1000).toFixed(1)+'k'} />
        <Tooltip content={<CustomTooltip unit=" MWh" />} />
        <ReferenceLine y={8000} stroke={C.amber} strokeDasharray="4 4" strokeWidth={1} label={false} />
        <Area type="monotone" dataKey="value" name="Energy" stroke={C.accent}
              fill="url(#weekGrad)" strokeWidth={2} dot={{ r: 3, fill: C.accent }} />
      </AreaChart>
    </ResponsiveContainer>
  )
}

// ── Monthly Energy Bar ─────────────────────────────────────────────────────────
export function MonthlyEnergyChart({ data, currentMonth = 0 }) {
  return (
    <ResponsiveContainer width="100%" height="100%">
      <BarChart data={data} margin={{ top: 10, right: 8, left: -16, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke={C.grid} vertical={false} />
        <XAxis dataKey="label" tick={BAR_TICK} axisLine={false} tickLine={false} />
        <YAxis tick={BAR_TICK} axisLine={false} tickLine={false}
               domain={[0, domainMax(data, 'value', 1.5)]}
               tickFormatter={v => (v >= 1000 ? (v / 1000).toFixed(1) + 'k' : Math.round(v).toLocaleString())} />
        <Tooltip content={<CustomTooltip unit=" MWh" />} cursor={{ fill: 'rgba(255,255,255,0.04)' }} />
        <Bar dataKey="value" name="Energy" radius={[4, 4, 0, 0]} isAnimationActive={false}>
          {data.map((d, i) => (
            // Highlight the current production month (brighter blue), others accent.
            <Cell key={i} fill={d.month === currentMonth ? C.blue : C.accent}
                  fillOpacity={d.month === currentMonth ? 1 : 0.7} />
          ))}
          {/* Permanent value label on every month's bar (visible without hovering). */}
          <LabelList dataKey="value" content={columnValueLabel({ unit: 'MWh', barCount: data?.length || 0 })} />
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  )
}
