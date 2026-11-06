/**
 * Device-wise forecasting, below the plant forecast and separate from it.
 *
 * Three things live here:
 *
 *   Inverters   each backtested on its own history, each picking its own model
 *   Combiners   ranked against the other boxes behind the same inverter
 *   Detail      one device's own Actual vs Predicted chart, Day/Week/Month
 *
 * The plant forecast above is the official number. These rows are a breakdown
 * of where the generation comes from and which units are keeping up; they do
 * not replace it and they do not sum to it, for a reason the panel states.
 */
import React, { useState, useEffect, useCallback, useMemo } from 'react'
import { Spinner } from '../Common'
import { GenerationChart, BucketTabs, PartialNote } from '../../pages/Forecast/Forecast'
import {
  fetchDeviceInventory, runDeviceForecasts, fetchDeviceForecasts,
  fetchDeviceHistory, fetchCombinerAnalysis,
} from '../../services/api'

const fmt = (v, d = 0) =>
  v === null || v === undefined || Number.isNaN(v)
    ? '—'
    : Number(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d })

/** A genuine zero is a measurement; a missing value is not. They differ. */
const val = (v, d = 1) => (v === null || v === undefined ? '—' : fmt(v, d))

/**
 * Natural order for device ids, not lexicographic.
 *
 * Every id ends in a bare number — INV01_SCB1 … INV01_SCB21 — and a plain string
 * compare orders those character by character, filing SCB10 between SCB1 and
 * SCB2. `numeric` compares each run of digits as a number, so SCB2 comes
 * before SCB10. Zero-padded names such as INVERTER_01 are unaffected; they
 * already sort the same either way. One Collator is built once and reused,
 * because constructing one per comparison is the slow way to do this.
 */
const COLLATOR = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' })

const STATUS_TONE = {
  'OK': 'bg-emerald-900/40 text-emerald-400',
  'MEASURED': 'bg-emerald-900/40 text-emerald-400',
  'PROVISIONAL': 'bg-amber-900/40 text-amber-400',
  'BELOW PEERS': 'bg-amber-900/40 text-amber-400',
  'ZERO OUTPUT': 'bg-red-900/40 text-red-400',
  'INSUFFICIENT DATA': 'bg-slate-700/50 text-slate-300',
  'NO DATA': 'bg-slate-700/50 text-slate-300',
}

function Tag({ children }) {
  const tone = STATUS_TONE[children] || 'bg-slate-700/50 text-slate-300'
  return (
    <span className={`font-mono text-[9px] px-1.5 py-0.5 rounded whitespace-nowrap ${tone}`}>
      {children}
    </span>
  )
}

/** Sortable header cell. Sorting is client-side over rows already fetched. */
function Th({ id, sort, setSort, children, right }) {
  const active = sort.key === id
  return (
    <th
      onClick={() => setSort(s => ({ key: id, dir: s.key === id && s.dir === 'desc' ? 'asc' : 'desc' }))}
      className={`px-2 py-1.5 font-mono text-[9px] uppercase tracking-wider cursor-pointer
                  select-none whitespace-nowrap ${right ? 'text-right' : 'text-left'}
                  ${active ? 'text-ge-text1' : 'text-ge-text3 hover:text-ge-text2'}`}
    >
      {children}{active ? (sort.dir === 'desc' ? ' ▼' : ' ▲') : ''}
    </th>
  )
}

function useSorted(rows, sort) {
  return useMemo(() => {
    const out = [...rows]
    out.sort((a, b) => {
      const x = a[sort.key], y = b[sort.key]
      // Nulls always sink, whichever way the column is sorted: a device with
      // no number is not "worst", it is unmeasured.
      if (x === null || x === undefined) return 1
      if (y === null || y === undefined) return -1
      if (typeof x === 'string')
        return sort.dir === 'desc' ? COLLATOR.compare(y, x) : COLLATOR.compare(x, y)
      return sort.dir === 'desc' ? y - x : x - y
    })
    return out
  }, [rows, sort])
}

/* ── one device's own chart ─────────────────────────────────────────────── */
function DeviceDetail({ deviceId, onClose }) {
  const [bucket, setBucket] = useState('day')
  const [data, setData] = useState(null)
  const [err, setErr] = useState('')

  useEffect(() => {
    let live = true
    setErr('')
    fetchDeviceHistory(deviceId, bucket)
      .then(d => { if (live) setData(d) })
      .catch(e => live && setErr(e?.response?.data?.detail || e.message))
    return () => { live = false }
  }, [deviceId, bucket])

  const points = data?.points || []
  return (
    <div className="card p-3 border border-ge-border2">
      <div className="flex items-center justify-between gap-3 mb-2">
        <div className="card-title">
          {deviceId}
          {data?.model && <span className="text-ge-text3 font-normal"> · {data.model}</span>}
        </div>
        <div className="flex items-center gap-2">
          <BucketTabs value={bucket} onChange={setBucket} />
          <button onClick={onClose}
                  className="text-[11px] text-ge-text3 hover:text-ge-text1 px-2">
            Close
          </button>
        </div>
      </div>

      {err && <div className="text-[12px] text-red-300 py-4">{err}</div>}

      {!err && data && (
        <>
          <div className="grid grid-cols-2 md:grid-cols-6 gap-2 mb-3">
            {[
              ['Latest actual', `${val(data.latest_actual_mwh, 1)} MWh`],
              ['Score', data.score_pct == null ? '—' : `${fmt(data.score_pct, 1)}%`],
              ['WAPE', data.wape_pct == null ? '—' : `${fmt(data.wape_pct, 1)}%`],
              ['MAE', `${val(data.mae_mwh, 2)} MWh`],
              ['Held out', `${val(data.n_test, 0)} days`],
              ['History', `${val(data.complete_days, 0)} days`],
            ].map(([k, v]) => (
              <div key={k} className="bg-ge-surface rounded px-2 py-1.5">
                <div className="font-mono text-[9px] uppercase tracking-wider text-ge-text3">{k}</div>
                <div className="text-[13px] text-ge-text1 font-mono">{v}</div>
              </div>
            ))}
          </div>

          {points.length === 0 ? (
            <div className="text-[12px] text-ge-text3 py-8 text-center">
              No series for this device.
            </div>
          ) : (
            <div className="h-64"><GenerationChart points={points} bucket={bucket} /></div>
          )}
          <PartialNote points={points} bucket={bucket} />
          <div className="text-[11px] text-ge-text3 mt-1">
            {data.validation && <Tag>{data.validation}</Tag>}{' '}
            {data.note}
            {data.last_seen && <> Last reading {String(data.last_seen).slice(0, 10)}.</>}
          </div>
        </>
      )}
    </div>
  )
}

/* ── the section ────────────────────────────────────────────────────────── */
export default function DeviceSection() {
  const [tab, setTab] = useState('inverters')
  const [inventory, setInventory] = useState(null)
  const [forecasts, setForecasts] = useState(null)
  const [combiners, setCombiners] = useState(null)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState('')
  const [query, setQuery] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [selected, setSelected] = useState(null)
  const [sort, setSort] = useState({ key: 'device', dir: 'asc' })

  const load = useCallback(async () => {
    try {
      const [inv, fc] = await Promise.all([fetchDeviceInventory(), fetchDeviceForecasts()])
      setInventory(inv); setForecasts(fc)
    } catch (e) {
      setError(e?.response?.data?.detail || e.message || 'Could not load devices.')
    }
  }, [])

  useEffect(() => { load() }, [load])

  // The server owns "is a run in progress", so the button stays disabled
  // across a page reload rather than re-enabling and inviting a second run.
  const serverRunning = !!forecasts?.running
  const busy = running || serverRunning

  useEffect(() => {
    if (!busy) return
    const t = setInterval(() => fetchDeviceForecasts().then(setForecasts).catch(() => {}), 5000)
    return () => clearInterval(t)
  }, [busy])

  const onRun = async () => {
    if (busy) return
    setRunning(true); setError('')
    try {
      await runDeviceForecasts()
      await load()
    } catch (e) {
      const msg = e?.response?.data?.detail || e.message || 'Device run failed.'
      // A 409 means a run is already going: that is confirmation, not an error.
      if (/already in progress/i.test(msg)) {
        setForecasts(await fetchDeviceForecasts())
      } else {
        setError(msg)
      }
    } finally {
      setRunning(false)
    }
  }

  const onCombiners = async () => {
    if (combiners) return
    try { setCombiners(await fetchCombinerAnalysis()) }
    catch (e) { setError(e?.response?.data?.detail || e.message) }
  }

  const invRows = forecasts?.devices || []
  const cmbRows = combiners?.devices || []
  const rows = tab === 'inverters' ? invRows : cmbRows

  const filtered = rows.filter(r =>
    (!query || r.device.toLowerCase().includes(query.toLowerCase())) &&
    (!statusFilter || r.status === statusFilter))
  const sorted = useSorted(filtered, sort)
  const statuses = [...new Set(rows.map(r => r.status).filter(Boolean))].sort()

  const counts = inventory?.counts || {}
  const scored = invRows.filter(r => r.status === 'OK').length

  return (
    <div className="space-y-3">
      <div className="card p-3">
        <div className="flex items-center justify-between gap-3 flex-wrap mb-3">
          <div className="card-title">Device-wise forecasting and performance</div>
          <button className="btn btn-primary btn-sm" onClick={onRun} disabled={busy}>
            {busy
              ? <><Spinner size={12} /> {forecasts?.total
                  ? `${invRows.length} of ${forecasts.total} done` : 'Running…'}</>
              : 'Run device forecasts'}
          </button>
        </div>

        {error && (
          <div className="text-[12px] text-red-300 mb-2">{error}</div>
        )}

        <div className="flex items-center gap-2 flex-wrap mb-3">
          {[['inverters', `Inverters ${counts.inverters ?? ''}`],
            ['combiners', `Combiners ${counts.combiners ?? ''}`]].map(([id, label]) => (
            <button key={id}
                    onClick={() => { setTab(id); setSelected(null); if (id === 'combiners') onCombiners() }}
                    className={`px-2.5 py-1 text-[11px] rounded border transition-colors ${
                      tab === id ? 'bg-ge-blue text-white border-ge-blue'
                                 : 'border-ge-border text-ge-text3 hover:text-ge-text1'}`}>
              {label}
            </button>
          ))}
          <input value={query} onChange={e => setQuery(e.target.value)}
                 placeholder="Search device…"
                 className="ml-auto bg-ge-surface border border-ge-border rounded px-2 py-1
                            text-[11px] text-ge-text1 placeholder:text-ge-text3 w-44" />
          <select value={statusFilter} onChange={e => setStatusFilter(e.target.value)}
                  className="bg-ge-surface border border-ge-border rounded px-2 py-1
                             text-[11px] text-ge-text1">
            <option value="">All statuses</option>
            {statuses.map(s => <option key={s} value={s}>{s}</option>)}
          </select>
        </div>

        {tab === 'inverters' && invRows.length > 0 && (
          <div className="border border-amber-700/50 bg-amber-950/20 rounded p-2.5 mb-3
                          text-[11px] text-ge-text2 leading-relaxed">
            <b className="text-amber-300">This is a breakdown, not the plant forecast.</b>{' '}
            The official plant forecast is the one above, measured at the grid
            connection. This table covers <b>{scored} of {invRows.length} inverters</b>,
            each evaluated on its own history. The rows measure AC output at the
            inverters, before the transformer and the auxiliary load, so they sum
            to slightly <i>more</i> than the plant exports — about 101% on a
            measured day. Nothing here has been scaled to make the two agree.
          </div>
        )}

        {tab === 'combiners' && combiners && (
          <div className="text-[11px] text-ge-text3 leading-relaxed mb-3">
            {combiners.note}
            {combiners.n_flagged > 0 &&
              <> <b className="text-amber-400">{combiners.n_flagged}</b> of {combiners.n_devices} flagged.</>}
          </div>
        )}

        <div className="overflow-x-auto">
          <table className="w-full text-[11px]">
            <thead className="border-b border-ge-border">
              <tr>
                <Th id="device" sort={sort} setSort={setSort}>Device</Th>
                {tab === 'inverters' ? (
                  <>
                    <Th id="model" sort={sort} setSort={setSort}>Model</Th>
                    <Th id="latest_actual_mwh" sort={sort} setSort={setSort} right>Latest actual</Th>
                    <Th id="predicted_mwh" sort={sort} setSort={setSort} right>Next day</Th>
                    <Th id="forecast_date" sort={sort} setSort={setSort}>For</Th>
                    <Th id="score_pct" sort={sort} setSort={setSort} right>Score %</Th>
                    <Th id="wape_pct" sort={sort} setSort={setSort} right>WAPE %</Th>
                    <Th id="mae_mwh" sort={sort} setSort={setSort} right>MAE</Th>
                    <Th id="r2" sort={sort} setSort={setSort} right>R²</Th>
                    <Th id="n_test" sort={sort} setSort={setSort} right>Held out</Th>
                    <Th id="complete_days" sort={sort} setSort={setSort} right>Coverage</Th>
                  </>
                ) : (
                  <>
                    <Th id="group" sort={sort} setSort={setSort}>Group</Th>
                    <Th id="mean_kwh" sort={sort} setSort={setSort} right>Mean kWh/day</Th>
                    <Th id="group_median_kwh" sort={sort} setSort={setSort} right>Group median</Th>
                    <Th id="deviation_pct" sort={sort} setSort={setSort} right>vs median</Th>
                    <Th id="rank_in_group" sort={sort} setSort={setSort} right>Rank</Th>
                    <Th id="complete_days" sort={sort} setSort={setSort} right>Coverage</Th>
                  </>
                )}
                <Th id="status" sort={sort} setSort={setSort}>Status</Th>
              </tr>
            </thead>
            <tbody>
              {sorted.length === 0 && (
                <tr><td colSpan={12} className="py-6 text-center text-ge-text3">
                  {tab === 'inverters'
                    ? 'No device forecasts yet. Use Run device forecasts.'
                    : 'Loading combiner analysis…'}
                </td></tr>
              )}
              {sorted.map(r => (
                <tr key={r.device}
                    onClick={() => tab === 'inverters' && r.status === 'OK' && setSelected(r.device)}
                    className={`border-b border-ge-border/50 ${
                      tab === 'inverters' && r.status === 'OK'
                        ? 'cursor-pointer hover:bg-ge-surface' : ''} ${
                      selected === r.device ? 'bg-ge-surface' : ''}`}>
                  <td className="px-2 py-1.5 font-mono text-ge-text1">{r.device}</td>
                  {tab === 'inverters' ? (
                    <>
                      <td className="px-2 py-1.5 text-ge-text2">{r.model || '—'}</td>
                      <td className="px-2 py-1.5 text-right font-mono">{val(r.latest_actual_mwh)}</td>
                      <td className="px-2 py-1.5 text-right font-mono">{val(r.predicted_mwh)}</td>
                      <td className="px-2 py-1.5 text-ge-text3">
                        {r.forecast_date ? String(r.forecast_date).slice(0, 10) : '—'}</td>
                      <td className="px-2 py-1.5 text-right font-mono">{val(r.score_pct)}</td>
                      <td className="px-2 py-1.5 text-right font-mono">{val(r.wape_pct)}</td>
                      <td className="px-2 py-1.5 text-right font-mono">{val(r.mae_mwh, 2)}</td>
                      <td className="px-2 py-1.5 text-right font-mono">{val(r.r2, 2)}</td>
                      <td className="px-2 py-1.5 text-right font-mono">{val(r.n_test, 0)}</td>
                      <td className="px-2 py-1.5 text-right font-mono">{val(r.complete_days, 0)}</td>
                    </>
                  ) : (
                    <>
                      <td className="px-2 py-1.5 text-ge-text2">{r.group}</td>
                      <td className="px-2 py-1.5 text-right font-mono">{val(r.mean_kwh)}</td>
                      <td className="px-2 py-1.5 text-right font-mono">{val(r.group_median_kwh)}</td>
                      <td className="px-2 py-1.5 text-right font-mono">
                        {r.deviation_pct == null ? '—' : `${fmt(r.deviation_pct, 1)}%`}</td>
                      <td className="px-2 py-1.5 text-right font-mono">
                        {r.rank_in_group ? `${r.rank_in_group}/${r.group_size}` : '—'}</td>
                      <td className="px-2 py-1.5 text-right font-mono">{val(r.complete_days, 0)}</td>
                    </>
                  )}
                  <td className="px-2 py-1.5"><Tag>{r.status}</Tag></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {tab === 'inverters' && invRows.length > 0 && (
          <div className="text-[11px] text-ge-text3 mt-2 leading-relaxed">
            Each inverter is backtested on its own history and picks its own model
            from the same candidates on identical chronological folds — the plant's
            winner is not inherited. Select a row to see that device's own chart.
          </div>
        )}
      </div>

      {selected && <DeviceDetail deviceId={selected} onClose={() => setSelected(null)} />}
    </div>
  )
}
