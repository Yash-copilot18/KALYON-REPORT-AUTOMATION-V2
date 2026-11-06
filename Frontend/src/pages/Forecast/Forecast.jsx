// src/pages/Forecast/Forecast.jsx
//
// Day-ahead generation forecasting and deviation detection.
//
// The page is built around one principle: never show an accuracy figure without
// the window it was measured on, and never show the agreed target as met unless
// the held-out test says so. The score banner is green only when the backend
// reports target_met — the colour is driven by measurement, not by hope.

import React, { useState, useEffect, useCallback } from 'react'
import {
  ComposedChart, Area, Line, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, Legend, ReferenceLine,
} from 'recharts'
import { KpiCard, PageHeader, DataTable, Skeleton, Spinner } from '../../components/Common'
import DeviceSection from '../../components/Devices/DeviceSection'
import {
  fetchForecastStatus, runForecast, fetchForecastLatest, fetchForecastNextDay,
  fetchForecastHistory, fetchForecastDeviations, reconcileForecast,
} from '../../services/api'

const fmt = (v, d = 0) =>
  v === null || v === undefined || Number.isNaN(v)
    ? '—'
    : Number(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d })

const shortDate = (s) => (s ? String(s).slice(0, 10) : '—')

/** Colour a status word consistently wherever it appears. */
const STATUS_STYLE = {
  OK:         'bg-emerald-900/40 text-emerald-400',
  UNDER:      'bg-red-900/40 text-red-400',
  OVER:       'bg-amber-900/40 text-amber-400',
  UNRELIABLE: 'bg-slate-700/50 text-slate-300',
}

/** Chart aggregation periods. Energy is summed per period, never averaged. */
const BUCKETS = [
  { id: 'day',   label: 'Day',   unit: 'MWh / day',   title: 'daily' },
  { id: 'week',  label: 'Week',  unit: 'MWh / week',  title: 'weekly (Mon–Sun)' },
  { id: 'month', label: 'Month', unit: 'MWh / month', title: 'monthly' },
]

/** Day | Week | Month. The same series, grouped differently. */
export function BucketTabs({ value, onChange }) {
  return (
    <div className="flex rounded border border-ge-border overflow-hidden">
      {BUCKETS.map(b => (
        <button
          key={b.id}
          onClick={() => onChange(b.id)}
          aria-pressed={value === b.id}
          className={`px-2.5 py-1 text-[11px] transition-colors ${
            value === b.id ? 'bg-ge-blue/20 text-ge-text1'
                           : 'text-ge-text3 hover:text-ge-text2'}`}
        >
          {b.label}
        </button>
      ))}
    </div>
  )
}

/**
 * Tooltip for the aggregated chart.
 *
 * A bar can be three measured days sitting beside a prediction for all seven,
 * and the shape on screen cannot show that. The coverage counts are therefore
 * spelled out on hover, and a period that is not whole says so instead of
 * offering a comparison that would be read as a shortfall.
 */
export function PeriodTooltip({ active, payload, label, bucket }) {
  if (!active || !payload?.length) return null
  const m = payload[0]?.payload?.meta
  const rows = [
    ['Actual', payload.find(p => p.dataKey === 'actual')?.value, m?.days_with_actual],
    ['Predicted', payload.find(p => p.dataKey === 'predicted')?.value, m?.days_with_predicted],
  ]
  return (
    <div className="bg-[#121a26] border border-[#243043] px-2.5 py-2 text-[11px]">
      <div className="text-ge-text1 font-medium mb-1">{label}</div>
      {rows.map(([name, value, days]) => (
        <div key={name} className="flex gap-2 justify-between">
          <span className="text-ge-text3">{name}</span>
          <span>
            {value == null ? '—' : `${fmt(value, 1)} MWh`}
            {bucket !== 'day' && value != null && m
              ? <span className="text-ge-text3"> ({days}/{m.days_in_period} d)</span>
              : null}
          </span>
        </div>
      ))}
      {m?.lower_mwh != null && (
        <div className="flex gap-2 justify-between">
          <span className="text-ge-text3">Interval</span>
          <span>{fmt(m.lower_mwh, 0)} – {fmt(m.upper_mwh, 0)} MWh</span>
        </div>
      )}
      {bucket !== 'day' && m && !m.comparable && (
        <div className="text-amber-400/90 mt-1 max-w-[220px]">
          {m.days_with_actual === 0
            ? 'Forecast only — this period has not been measured.'
            : `Partial: ${m.days_with_actual} of ${m.days_in_period} days measured, `
              + `so this total is not compared with the prediction.`}
        </div>
      )}
    </div>
  )
}

/**
 * The Actual vs Predicted chart, shared by the plant and by one device.
 *
 * One component with a configurable period rather than three charts: the
 * colours, the band and the axis conventions then cannot drift apart between
 * the plant view and a device view.
 */
export function GenerationChart({ points, bucket }) {
  const B = BUCKETS.find(b => b.id === bucket) || BUCKETS[0]
  const data = (points || []).map(p => ({
    // Week and month carry their own label ("02 Jun - 08 Jun", "Jun 2025");
    // the day series has none, so it keeps the format it always had.
    date: p.label || shortDate(p.date),
    actual: p.actual_mwh,
    predicted: p.predicted_mwh,
    // Recharts stacks an Area from a base, so the band is [lower, upper].
    band: p.lower_mwh != null && p.upper_mwh != null
      ? [p.lower_mwh, p.upper_mwh] : null,
    meta: p,
  }))
  return (
    <ResponsiveContainer width="100%" height="100%">
      <ComposedChart data={data} margin={{ top: 8, right: 16, bottom: 4, left: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#243043" />
        <XAxis dataKey="date" tick={{ fontSize: 10, fill: '#7d8da3' }} />
        <YAxis tick={{ fontSize: 10, fill: '#7d8da3' }}
               label={{ value: B.unit, angle: -90, position: 'insideLeft',
                        fill: '#7d8da3', fontSize: 10 }} />
        <Tooltip content={<PeriodTooltip bucket={bucket} />} />
        <Legend wrapperStyle={{ fontSize: 11 }} />
        <Area dataKey="band" name="Prediction interval" stroke="none"
              fill="#0b63a8" fillOpacity={0.18} connectNulls={false} />
        <Line dataKey="actual" name="Actual" stroke="#22c55e" strokeWidth={2}
              dot={false} connectNulls={false} />
        <Line dataKey="predicted" name="Predicted" stroke="#f59e0b"
              strokeWidth={2} strokeDasharray="4 3" dot={false} />
      </ComposedChart>
    </ResponsiveContainer>
  )
}

/** Periods that are only partly measured, named so nobody has to infer it. */
export function PartialNote({ points, bucket }) {
  if (bucket === 'day') return null
  const partial = (points || []).filter(
    p => p.days_with_actual > 0 && !p.actual_complete)
  if (!partial.length) return null
  return (
    <div className="text-[11px] text-amber-400/90 mt-1.5">
      Partly measured, so not compared with their predictions:{' '}
      {partial.map(p =>
        `${p.label} (${p.days_with_actual} of ${p.days_in_period} days)`).join(', ')}.
      A period is only scored when both sides cover all of it.
    </div>
  )
}

export default function Forecast() {
  const [status, setStatus]       = useState(null)
  const [latest, setLatest]       = useState(null)
  const [nextDay, setNextDay]     = useState(null)
  const [history, setHistory]     = useState(null)
  const [deviations, setDeviations] = useState(null)
  const [loading, setLoading]     = useState(true)
  const [running, setRunning]     = useState(false)
  const [error, setError]         = useState('')
  // 'day' is the original view and stays the default: switching only changes
  // how the same daily series is grouped, never which numbers it holds.
  const [bucket, setBucket]       = useState('day')

  const loadAll = useCallback(async () => {
    setError('')
    try {
      // The shared axios client unwraps the body in its response interceptor
      // (`res => res.data`), so these are payloads already — never `.data`.
      const [s, l, n, d] = await Promise.all([
        fetchForecastStatus(),
        fetchForecastLatest(),
        fetchForecastNextDay(),
        fetchForecastDeviations(false, 60),
      ])
      setStatus(s); setLatest(l); setNextDay(n); setDeviations(d)
    } catch (e) {
      setError(e?.response?.data?.detail || e.message || 'Could not load forecast data.')
    } finally {
      setLoading(false)
    }
  }, [])

  // The chart series is fetched on its own so that changing Day/Week/Month
  // re-reads one endpoint rather than reloading the whole page.
  const loadHistory = useCallback(async (b) => {
    try {
      setHistory(await fetchForecastHistory(180, b))
    } catch (e) {
      setError(e?.response?.data?.detail || e.message || 'Could not load the chart series.')
    }
  }, [])

  useEffect(() => { loadAll() }, [loadAll])
  useEffect(() => { loadHistory(bucket) }, [bucket, loadHistory])

  // Train, then reconcile any completed days, then refresh everything.
  const onRun = async () => {
    if (running) return
    setRunning(true); setError('')
    try {
      await runForecast({ target: 'energy_net_mwh' })
      await reconcileForecast(400)
      await Promise.all([loadAll(), loadHistory(bucket)])
    } catch (e) {
      setError(e?.response?.data?.detail || e.message || 'Forecast run failed.')
    } finally {
      setRunning(false)
    }
  }

  if (loading) {
    return (
      <div className="space-y-3">
        <PageHeader title="Generation Forecast" subtitle="Loading…" />
        <Skeleton h="h-24" /><Skeleton h="h-64" />
      </div>
    )
  }

  const cov   = status?.coverage || {}
  const wx    = status?.weather || {}
  const run   = latest?.available ? latest : null
  const rel   = nextDay?.reliability || run?.reliability
  const met   = !!run?.target_met
  const points = history?.points || []
  const BUCKET = BUCKETS.find(b => b.id === bucket) || BUCKETS[0]

  return (
    <div className="space-y-3">
      <PageHeader
        title="Generation Forecast & Deviation Detection"
        subtitle="Day-ahead energy prediction from plant history, scored on a held-out period"
      >
        <button className="btn btn-primary btn-sm" onClick={onRun} disabled={running}>
          {running ? <><Spinner size={12} /> Running…</> : 'Run forecast'}
        </button>
      </PageHeader>

      {error && (
        <div className="card border border-red-800/60 bg-red-950/30 text-red-300 text-[12px] p-3">
          {error}
        </div>
      )}

      {/* ── validation status: the honest headline ───────────────────────── */}
      {run && (
        <div className={`card p-3 border ${met ? 'border-emerald-700/60 bg-emerald-950/20'
                                               : 'border-amber-700/60 bg-amber-950/20'}`}>
          <div className="flex items-start gap-3">
            <span className={`font-mono text-[10px] px-2 py-1 rounded ${
              met ? 'bg-emerald-900/50 text-emerald-300' : 'bg-amber-900/50 text-amber-300'}`}>
              {rel?.level || (met ? 'VALIDATED' : 'LIMITED')}
            </span>
            <div className="text-[12px] text-ge-text2 leading-relaxed">
              {rel?.reason}
              {' '}Measured on {shortDate(run.test_start)} – {shortDate(run.test_end)}
              {' '}({run.n_test} days held out of training), model <b>{run.model}</b>.
            </div>
          </div>
        </div>
      )}

      {/* ── headline numbers ─────────────────────────────────────────────── */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
        <KpiCard label="Next day forecast"
                 value={nextDay?.available ? fmt(nextDay.predicted_mwh) : '—'}
                 unit="MWh" color="blue" />
        <KpiCard label={`Forecast score (target ${fmt(run?.target_score_pct)}%)`}
                 value={run ? fmt(run.score_pct, 1) : '—'} unit="%"
                 color={met ? 'green' : 'amber'} />
        <KpiCard label="WAPE" value={run ? fmt(run.wape_pct, 1) : '—'} unit="%" color="blue" />
        <KpiCard label="MAE" value={run ? fmt(run.mae_mwh) : '—'} unit="MWh" color="blue" />
        <KpiCard label="RMSE" value={run ? fmt(run.rmse_mwh) : '—'} unit="MWh" color="blue" />
      </div>

      {/* ── weather input disclosure ─────────────────────────────────────── */}
      <div className="card p-3">
        <div className="card-title mb-2">Weather inputs used by the model</div>
        {wx.configured ? (
          <div className="text-[12px] text-ge-text2">
            Provider <b>{wx.provider}</b> is configured. Forecast irradiance, air
            temperature and cloud cover inform the forward prediction.
          </div>
        ) : (
          <div className="text-[12px] text-ge-text2 leading-relaxed">
            <span className="font-mono text-[10px] px-1.5 py-0.5 rounded bg-slate-700/50 text-slate-300 mr-2">
              NO WEATHER FORECAST
            </span>
            {wx.reason}
          </div>
        )}
        <div className="text-[11px] text-ge-text3 mt-2">
          Measured weather is never used as a model input. Scoring against the
          irradiance that actually occurred would inflate the result by a wide
          margin and could not be reproduced in service.
        </div>
      </div>

      {/* ── actual vs predicted ──────────────────────────────────────────── */}
      <div className="card p-3">
        <div className="flex items-center justify-between gap-3 mb-2">
          <div className="card-title">
            Actual vs predicted energy
            {bucket !== 'day' && (
              <span className="text-ge-text3 font-normal"> · {BUCKET.title}</span>
            )}
          </div>
          <BucketTabs value={bucket} onChange={setBucket} />
        </div>
        {points.length === 0 ? (
          <div className="text-[12px] text-ge-text3 py-8 text-center">
            No forecast points yet. Use <b>Run forecast</b> to produce them.
          </div>
        ) : (
          <div className="h-72">
            <GenerationChart points={points} bucket={bucket} />
          </div>
        )}
        <PartialNote points={points} bucket={bucket} />
        <div className="text-[11px] text-ge-text3 mt-1">
          {history?.note}
        </div>
      </div>

      {/* ── model comparison ─────────────────────────────────────────────── */}
      {run?.detail?.leaderboard?.length > 0 && (
        <div className="card p-3">
          <div className="card-title mb-2">
            Model comparison — day-ahead, on the validation period only
          </div>
          <DataTable
            columns={[
              { key: 'model', label: 'Model' },
              { key: 'score', label: 'Score %' },
              { key: 'wape',  label: 'WAPE %' },
              { key: 'mae',   label: 'MAE MWh' },
              { key: 'rmse',  label: 'RMSE MWh' },
              { key: 'bias',  label: 'Bias MWh' },
              { key: 'kind',  label: 'Type' },
            ]}
            rows={run.detail.leaderboard.map(r => ({
              model: r.model === run.model ? `${r.model}  ← selected` : r.model,
              score: fmt(r.score_pct, 1),
              wape:  fmt(r.wape_pct, 1),
              mae:   fmt(r.mae_mwh),
              rmse:  fmt(r.rmse_mwh),
              bias:  fmt(r.bias_mwh),
              kind:  r.is_baseline ? 'baseline' : 'model',
            }))}
          />
          <div className="text-[11px] text-ge-text3 mt-2">
            Baselines are scored on identical folds. A model that cannot beat
            them has earned nothing, and that outcome is shown rather than hidden.
          </div>
        </div>
      )}

      {/* ── accuracy by condition and horizon ────────────────────────────── */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
        {run?.detail?.by_condition?.length > 0 && (
          <div className="card p-3">
            <div className="card-title mb-2">Accuracy by measured condition</div>
            <DataTable
              columns={[
                { key: 'c', label: 'Condition' },
                { key: 's', label: 'Score %' },
                { key: 'm', label: 'MAE MWh' },
                { key: 'n', label: 'Days' },
              ]}
              rows={run.detail.by_condition.map(c => ({
                c: c.condition, s: fmt(c.score_pct, 1), m: fmt(c.mae_mwh), n: c.n,
              }))}
            />
          </div>
        )}
        {run?.detail?.by_horizon?.length > 0 && (
          <div className="card p-3">
            <div className="card-title mb-2">Accuracy by days ahead</div>
            <DataTable
              columns={[
                { key: 'h', label: 'Days ahead' },
                { key: 's', label: 'Score %' },
                { key: 'm', label: 'MAE MWh' },
                { key: 'n', label: 'Days' },
              ]}
              rows={run.detail.by_horizon.map(h => ({
                h: h.horizon_days, s: fmt(h.score_pct, 1), m: fmt(h.mae_mwh), n: h.n,
              }))}
            />
          </div>
        )}
      </div>

      {/* ── deviations ───────────────────────────────────────────────────── */}
      <div className="card p-3">
        <div className="card-title mb-2">Deviations and alert history</div>
        {(deviations?.deviations || []).length === 0 ? (
          <div className="text-[12px] text-ge-text3 py-6 text-center">
            No reconciled days yet. Deviations appear once a predicted day has
            completed and been compared with the measured total.
          </div>
        ) : (
          <div className="space-y-2">
            {deviations.deviations.map((d, i) => (
              <div key={i} className="border border-ge-border rounded p-2.5">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="font-mono text-[11px] text-ge-text1">{shortDate(d.date)}</span>
                  <span className={`font-mono text-[10px] px-1.5 py-0.5 rounded ${
                    STATUS_STYLE[d.status] || STATUS_STYLE.UNRELIABLE}`}>
                    {d.status}
                  </span>
                  <span className="text-[11px] text-ge-text2">
                    predicted {fmt(d.predicted_mwh)} MWh · actual {fmt(d.actual_mwh)} MWh ·
                    deviation <b>{fmt(d.deviation_mwh)} MWh ({fmt(d.deviation_pct, 1)}%)</b>
                  </span>
                </div>
                {d.hypotheses?.length > 0 && (
                  <ul className="mt-1.5 ml-1 space-y-0.5">
                    {d.hypotheses.map((h, k) => (
                      <li key={k} className="text-[11px] text-ge-text3">• {h}</li>
                    ))}
                  </ul>
                )}
              </div>
            ))}
          </div>
        )}
        <div className="text-[11px] text-ge-text3 mt-2">{deviations?.disclaimer}</div>
      </div>

      {/* ── data freshness and limits ────────────────────────────────────── */}
      <div className="card p-3">
        <div className="card-title mb-2">Data coverage and freshness</div>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-x-6 gap-y-1 text-[11px]">
          <Fact k="Plant record" v={`${shortDate(cov.plant_first)} → ${shortDate(cov.plant_last)}`} />
          <Fact k="Weather record" v={`${shortDate(cov.weather_first)} → ${shortDate(cov.weather_last)}`} />
          <Fact k="Complete days" v={cov.complete_days} />
          <Fact k="Days with weather" v={cov.days_with_weather} />
          <Fact k="Supervised rows" v={cov.supervised_rows} />
          <Fact k="Curtailed days" v={cov.curtailed_days} />
          <Fact k="Heavily curtailed" v={cov.heavily_curtailed_days} />
          <Fact k="Complete-day threshold" v={`${cov.complete_day_threshold_minutes} min`} />
        </div>
        <div className="text-[11px] text-ge-text3 mt-2 leading-relaxed">
          {cov.timezone_note} {status?.data_freshness?.note}
        </div>
      </div>

      {/* ── device-wise forecasting ──────────────────────────────────────── */}
      {/* Below the plant section and independent of it: nothing in here can
          change the plant forecast above, and it loads on its own. */}
      <DeviceSection />
    </div>
  )
}

function Fact({ k, v }) {
  return (
    <div className="flex justify-between gap-2 border-b border-ge-border/50 py-0.5">
      <span className="text-ge-text3">{k}</span>
      <span className="font-mono text-ge-text1">{v ?? '—'}</span>
    </div>
  )
}
