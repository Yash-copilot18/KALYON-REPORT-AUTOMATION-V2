/**
 * The data behind the Actual vs Predicted chart, as a table.
 *
 * The client asked to see the numbers the graph is drawn from and to check
 * that the two agree. They agree by construction: this component is handed the
 * SAME `points` array the chart receives, and reads the same fields off it -
 * actual_mwh, predicted_mwh, lower_mwh, upper_mwh. It fetches nothing and
 * recomputes nothing. There is no second source that could drift.
 *
 * Row i of the table is point i of the chart. That is the whole guarantee.
 */

import React, { useMemo, useState } from 'react'
import { DataTable, Pagination, SearchInput } from '../Common'

const PER_PAGE = 15

const fmt = (v, d = 1) =>
  v === null || v === undefined || Number.isNaN(v)
    ? '—'
    : Number(v).toLocaleString(undefined,
        { minimumFractionDigits: d, maximumFractionDigits: d })

/**
 * A missing reading and a measured zero are different facts and must not look
 * the same. Only null/undefined becomes an em dash; a genuine 0 prints as 0.
 */
const val = (v, d = 1) => (v === null || v === undefined ? '—' : fmt(v, d))

/** ISO timestamp or date -> YYYY-MM-DD, which is also what the filters compare. */
const isoDay = (d) => (d ? String(d).slice(0, 10) : '')

const STATUS_TONE = {
  Complete: 'text-emerald-400',
  Partial: 'text-amber-400',
  Missing: 'text-red-400',
  Future: 'text-ge-text3',
  Unknown: 'text-ge-text3',
}

/* ── CSV ────────────────────────────────────────────────────────────────────
 * Exports the filtered rows, every page of them - not just the page on screen.
 * A client who filters to a month and exports expects the month.
 */
function toCsv(rows, bucket, model) {
  const head = bucket === 'day'
    ? ['Date', 'Actual MWh', 'Predicted MWh', 'Lower MWh', 'Upper MWh',
       'Forecast Type', 'Model', 'Data Status']
    : ['Period', 'Start', 'End', 'Actual MWh', 'Predicted MWh', 'Lower MWh',
       'Upper MWh', 'Days Measured', 'Days In Period', 'Forecast Type',
       'Model', 'Data Status']

  // Empty, not "0" and not "null": a gap in the data stays a gap in the file.
  const cell = (v) => {
    if (v === null || v === undefined) return ''
    const s = String(v)
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
  }

  const body = rows.map(r => (bucket === 'day'
    ? [isoDay(r.date), r.actual_mwh, r.predicted_mwh, r.lower_mwh, r.upper_mwh,
       r.is_backtest ? 'Backtest' : 'Forecast', model, r.data_status]
    : [r.label, isoDay(r.period_start), isoDay(r.period_end), r.actual_mwh,
       r.predicted_mwh, r.lower_mwh, r.upper_mwh, r.days_with_actual,
       r.days_in_period, r.is_backtest ? 'Backtest' : 'Forecast', model,
       r.data_status]
  ).map(cell).join(','))

  return [head.join(','), ...body].join('\r\n')
}

function download(csv, name) {
  const blob = new Blob(['﻿' + csv], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = name
  a.style.display = 'none'
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  setTimeout(() => URL.revokeObjectURL(url), 1500)
}

/* ── the explainer ──────────────────────────────────────────────────────────
 * Only steps that are actually implemented. Collapsed by default so it answers
 * the question without taking over the page.
 */
export function HowCalculated({ source, model, bucket }) {
  const steps = [
    ['Read', `Daily plant generation from ${source?.table || 'the plant table'}`
      + `${source?.measure ? ` — ${source.measure}` : ''}.`],
    ['Clean', 'Duplicate timestamps are averaged to one reading per minute, so '
      + 'a minute logged twice cannot be counted twice.'],
    ['Integrate', source?.method
      || 'Energy is the sum of the minute readings divided by 60.'],
    ['Validate', `A day is used only if it has at least `
      + `${source?.complete_day_minutes ?? 1380} of 1,440 minutes. A partial `
      + `day would integrate to an understated total and read as a plant fault, `
      + `so it is excluded rather than shown low.`],
    ['Describe', 'Each day is turned into features built only from the days '
      + 'before it — lags, rolling means, and position in the solar year.'],
    ['Select', 'Five candidates compete on identical chronological folds, '
      + 'including two deliberately simple benchmarks. The winner here was '
      + `${model || 'the selected model'}.`],
    ['Predict', 'Held-out days are predicted by a model that never saw them '
      + '(Backtest). Days after the archive are predicted forward (Forecast), '
      + 'each step consuming the previous step’s own prediction.'],
    ['Bound', 'The interval comes from the errors the model actually made on '
      + 'the held-out days, widened by √(days ahead). The lower bound is '
      + 'floored at zero because generation cannot be negative, which is why a '
      + 'far-out interval looks lopsided around its prediction.'],
    ['Serve', `These records are returned by the API and this page draws the `
      + `chart and fills this table from that one array — row for point.`],
  ]
  return (
    <details className="mt-3 border border-ge-border rounded">
      <summary className="cursor-pointer select-none px-3 py-2 text-[12px]
                          text-ge-text2 hover:text-ge-text1">
        How is this graph calculated?
      </summary>
      <div className="px-3 pb-3 pt-1 space-y-1.5">
        {steps.map(([k, text]) => (
          <div key={k} className="flex gap-2 text-[11px] leading-relaxed">
            <span className="font-mono text-ge-accent w-[62px] flex-shrink-0">
              {k}
            </span>
            <span className="text-ge-text2">{text}</span>
          </div>
        ))}
        {bucket !== 'day' && (
          <div className="text-[11px] text-amber-400/90 pt-1">
            In this view the daily records above are summed into
            {bucket === 'week' ? ' Monday-to-Sunday weeks' : ' calendar months'},
            never averaged. A period that is only partly measured is labelled
            Partial and is not compared against a whole-period prediction.
          </div>
        )}
      </div>
    </details>
  )
}

export default function ForecastDataTable({
  points, bucket, model, source, selected, onSelect,
}) {
  const [query, setQuery] = useState('')
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const [page, setPage] = useState(1)

  const rows = useMemo(() => {
    const q = query.trim().toLowerCase()
    return (points || []).filter(p => {
      const day = isoDay(p.date)
      if (from && day < from) return false
      if (to && day > to) return false
      if (!q) return true
      return day.includes(q) || String(p.label || '').toLowerCase().includes(q)
    })
  }, [points, query, from, to])

  // Any filter change moves the window; staying on page 7 of 2 shows nothing.
  const pageCount = Math.max(1, Math.ceil(rows.length / PER_PAGE))
  const current = Math.min(page, pageCount)
  const visible = rows.slice((current - 1) * PER_PAGE, current * PER_PAGE)

  const Status = (r) => (
    <span className={STATUS_TONE[r.data_status] || 'text-ge-text3'}>
      {r.data_status || '—'}
    </span>
  )
  const Type = (r) => (
    <span className={r.is_backtest ? 'text-ge-text1' : 'text-ge-text3'}>
      {r.is_backtest ? 'Backtest' : 'Forecast'}
    </span>
  )

  const dayCols = [
    { key: 'date', label: 'Date', className: '!text-ge-text1 font-mono',
      render: (_, r) => isoDay(r.date) },
    { key: 'actual_mwh', label: 'Actual MWh', align: 'right',
      render: (_, r) => val(r.actual_mwh, 1) },
    { key: 'predicted_mwh', label: 'Predicted MWh', align: 'right',
      render: (_, r) => val(r.predicted_mwh, 1) },
    { key: 'lower_mwh', label: 'Lower MWh', align: 'right',
      render: (_, r) => val(r.lower_mwh, 1) },
    { key: 'upper_mwh', label: 'Upper MWh', align: 'right',
      render: (_, r) => val(r.upper_mwh, 1) },
    { key: 'is_backtest', label: 'Forecast Type', render: (_, r) => Type(r) },
    { key: 'model', label: 'Model', render: () => model || '—' },
    { key: 'data_status', label: 'Data Status', render: (_, r) => Status(r) },
  ]

  const periodCols = [
    { key: 'label', label: 'Period', className: '!text-ge-text1',
      render: (_, r) => r.label },
    { key: 'actual_mwh', label: 'Actual MWh', align: 'right',
      render: (_, r) => val(r.actual_mwh, 1) },
    { key: 'predicted_mwh', label: 'Predicted MWh', align: 'right',
      render: (_, r) => val(r.predicted_mwh, 1) },
    { key: 'lower_mwh', label: 'Lower MWh', align: 'right',
      render: (_, r) => val(r.lower_mwh, 1) },
    { key: 'upper_mwh', label: 'Upper MWh', align: 'right',
      render: (_, r) => val(r.upper_mwh, 1) },
    { key: 'days_with_actual', label: 'Measured', align: 'right',
      render: (_, r) => `${r.days_with_actual}/${r.days_in_period} d` },
    { key: 'is_backtest', label: 'Forecast Type', render: (_, r) => Type(r) },
    { key: 'model', label: 'Model', render: () => model || '—' },
    { key: 'data_status', label: 'Data Status', render: (_, r) => Status(r) },
  ]

  const unit = bucket === 'day' ? 'MWh per day'
    : bucket === 'week' ? 'MWh per week' : 'MWh per month'

  return (
    <div className="mt-4">
      <div className="flex flex-wrap items-center gap-2 mb-2">
        <div className="card-title">
          Data behind the graph
          <span className="ml-2 text-[11px] font-normal text-ge-text3">
            {unit} · {rows.length} of {(points || []).length} rows
          </span>
        </div>
        <div className="flex flex-wrap items-center gap-2 w-full sm:w-auto sm:ml-auto">
          <SearchInput value={query}
            onChange={v => { setQuery(v); setPage(1) }}
            placeholder="Search date…" className="w-[150px]" />
          <label htmlFor="fc-from" className="text-[11px] text-ge-text2">From</label>
          <input id="fc-from" type="date" value={from} max={to || undefined}
            aria-label="Filter from date"
            onChange={e => { setFrom(e.target.value); setPage(1) }}
            className="form-control text-[11px] py-1 px-2 w-[138px]" />
          <label htmlFor="fc-to" className="text-[11px] text-ge-text2">To</label>
          <input id="fc-to" type="date" value={to} min={from || undefined}
            aria-label="Filter to date"
            onChange={e => { setTo(e.target.value); setPage(1) }}
            className="form-control text-[11px] py-1 px-2 w-[138px]" />
          {(from || to || query) && (
            <button className="btn btn-outline btn-sm"
              onClick={() => { setFrom(''); setTo(''); setQuery(''); setPage(1) }}>
              Clear
            </button>
          )}
          <button className="btn btn-outline btn-sm"
            disabled={!rows.length}
            onClick={() => download(toCsv(rows, bucket, model),
              `kalyon_forecast_${bucket}_${isoDay(new Date().toISOString())}.csv`)}>
            Download CSV
          </button>
        </div>
      </div>

      {/* Row click marks the matching point on the chart above. The chart is
          driven by the same array, so "matching" is an identity, not a lookup
          that could go wrong. */}
      <div onClick={(e) => {
        const tr = e.target.closest?.('tbody tr')
        if (!tr || !onSelect) return
        const r = visible[[...tr.parentNode.children].indexOf(tr)]
        if (r) onSelect(selected === isoDay(r.date) ? null : isoDay(r.date))
      }}>
        <DataTable
          columns={bucket === 'day' ? dayCols : periodCols}
          rows={visible}
          emptyMsg={(points || []).length
            ? 'No rows match this filter.'
            : 'No forecast points yet. Use Run forecast to produce them.'} />
      </div>

      {rows.length > PER_PAGE && (
        <Pagination page={current} setPage={setPage}
          total={rows.length} perPage={PER_PAGE} />
      )}

      <div className="text-[11px] text-ge-text3 mt-2 leading-relaxed">
        Every row here is one point on the chart above, read from the same API
        response — the chart does not compute its own values. A dash means no
        reading exists; it is never shown as zero.
        {source?.stored_in && <> Source: <span className="font-mono">
          {source.table}</span>, stored in {source.stored_in}.</>}
      </div>

      <HowCalculated source={source} model={model} bucket={bucket} />
    </div>
  )
}
