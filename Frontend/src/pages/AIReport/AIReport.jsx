// src/pages/AIReport/AIReport.jsx
//
// AI Solar Plant Performance Assurance — the analysis layer that sits on top of the
// monitoring pages. Where the Dashboard answers "what is happening?", this answers
// "did the plant perform as expected, can the numbers be trusted, and what should be
// investigated?".
//
// Two principles are visible in the UI itself:
//
//  · Nothing is invented. Analyses the archive cannot support are shown as blocked,
//    with the data that would unlock them, rather than omitted or filled with a
//    plausible-looking number.
//  · Every figure is traceable. Each finding can be expanded to its source tables,
//    columns, window, formula and both sides of the evidence.
//
// The run happens in a background job on the server; this page starts it and polls.

import React, { useState, useEffect, useRef, useCallback } from 'react'
import { PageHeader, Spinner } from '../../components/Common'
import { useApp } from '../../utils/AppContext'
import { fetchAIPreflight, startAIReport, fetchAIReportStatus } from '../../services/api'

const POLL_MS = 1500

const SEVERITY_STYLE = {
  CRITICAL: 'pill-red',
  HIGH:     'pill-red',
  MEDIUM:   'pill-amber',
  LOW:      'pill-blue',
  INFO:     'pill-blue',
}

const STATUS_STYLE = {
  AVAILABLE: 'text-ge-accent',
  PARTIAL:   'text-ge-warn',
  BLOCKED:   'text-ge-danger',
}

/** 'YYYY-MM-DDTHH:MM:SS' | 'YYYY-MM-DD HH:MM:SS' → 'DD/MM/YYYY HH:MM'. */
function dmy(value) {
  if (!value) return '—'
  const m = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/.exec(String(value))
  return m ? `${m[3]}/${m[2]}/${m[1]} ${m[4]}:${m[5]}` : String(value)
}

/** ISO date for an <input type="datetime-local">. */
function forInput(value) {
  if (!value) return ''
  const m = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})/.exec(String(value))
  return m ? `${m[1]}T${m[2]}` : ''
}

const num = (v, d = 0) =>
  v === null || v === undefined ? '—' : Number(v).toLocaleString(undefined,
    { minimumFractionDigits: d, maximumFractionDigits: d })

/** A collapsible block — detail stays available without crowding the page. */
function Section({ title, subtitle, badge, children, defaultOpen = false }) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <div className="card mb-3">
      <button type="button" onClick={() => setOpen(o => !o)}
        className="w-full flex items-center gap-3 text-left">
        <span className="card-title mb-0">{title}</span>
        {badge}
        {subtitle && <span className="text-[11px] text-ge-text3">{subtitle}</span>}
        <span className="ml-auto text-ge-text3 text-[11px]">{open ? '▲' : '▼'}</span>
      </button>
      {open && <div className="mt-3">{children}</div>}
    </div>
  )
}

function Stat({ label, value, tone = 'text-ge-text1', sub }) {
  return (
    <div className="card text-center py-3">
      <div className={`text-xl font-mono ${tone}`}>{value}</div>
      <div className="text-[11px] text-ge-text3 mt-1">{label}</div>
      {sub && <div className="text-[10px] text-ge-text3 mt-0.5">{sub}</div>}
    </div>
  )
}

export default function AIReport() {
  const { showToast } = useApp()

  const [pre, setPre] = useState(null)
  const [preErr, setPreErr] = useState('')
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const [job, setJob] = useState(null)        // { pct, message }
  const [report, setReport] = useState(null)
  const runningRef = useRef(false)
  const pollRef = useRef(null)
  useEffect(() => () => clearTimeout(pollRef.current), [])

  // What the archive supports. Seeds the window with the last 30 days the data
  // actually holds — not the last 30 days of wall-clock time, which for a historical
  // archive would be empty.
  useEffect(() => {
    fetchAIPreflight()
      .then(p => {
        setPre(p)
        if (p?.plant_latest) {
          const end = new Date(p.plant_latest)
          const start = new Date(end.getTime() - 30 * 864e5)
          const floor = p.plant_earliest ? new Date(p.plant_earliest) : start
          setTo(forInput(p.plant_latest))
          setFrom(forInput((start > floor ? start : floor).toISOString()))
        }
      })
      .catch(e => setPreErr(e.message))
  }, [])

  const run = useCallback(async () => {
    if (runningRef.current) return            // synchronous guard: no duplicate jobs
    runningRef.current = true
    setReport(null)
    setJob({ pct: 1, message: 'Queued…' })
    try {
      const { job_id } = await startAIReport({
        from_datetime: from ? from.replace('T', ' ') : null,
        to_datetime: to ? to.replace('T', ' ') : null,
      })
      let misses = 0
      const finished = await new Promise(resolve => {
        const tick = async () => {
          let s
          try { s = await fetchAIReportStatus(job_id); misses = 0 }
          catch (e) {
            // A dropped status reply must not abandon a job that is still running.
            if (++misses >= 5) return resolve({ status: 'error', error: e.message })
            pollRef.current = setTimeout(tick, POLL_MS); return
          }
          setJob({ pct: s.progress ?? 0, message: s.message || '' })
          if (s.status === 'done' || s.status === 'error') return resolve(s)
          pollRef.current = setTimeout(tick, POLL_MS)
        }
        tick()
      })
      if (finished.status === 'done') {
        setReport(finished.report)
        showToast('AI report generated.')
      } else {
        showToast(finished.error || finished.message || 'Report failed.', 'error')
      }
    } catch (e) {
      showToast(`Report failed: ${e.message}`, 'error')
    } finally {
      runningRef.current = false
      setJob(null)
    }
  }, [from, to, showToast])

  const win = report?.analysis_window
  const integrity = report?.module_results?.find(m => m.analysis === 'data_integrity')
  const totals = integrity?.metrics?.totals

  return (
    <div>
      <PageHeader title="AI Performance Assurance" />

      {/* ── Configuration ─────────────────────────────────────────────────── */}
      <div className="card mb-3">
        <div className="card-title">AI Report Configuration</div>

        {preErr && <div className="text-[12px] text-ge-danger mb-2">{preErr}</div>}

        {pre && (
          <div className="text-[11px] text-ge-text3 mb-3 leading-relaxed">
            Archive holds <span className="text-ge-text1 font-mono">
              {dmy(pre.plant_earliest)}</span> to <span className="text-ge-text1 font-mono">
              {dmy(pre.plant_latest)}</span>.
            {pre.all_source_overlap_start && (
              <> All sources overlap only from <span className="text-ge-warn font-mono">
                {dmy(pre.all_source_overlap_start)}</span> to <span className="text-ge-warn font-mono">
                {dmy(pre.all_source_overlap_end)}</span> — cross-source analyses are
                limited to that period.</>
            )}
          </div>
        )}

        <div className="grid grid-cols-2 gap-3 mb-3">
          <div className="flex flex-col gap-1">
            <label className="form-label">From</label>
            <input type="datetime-local" className="form-control"
              value={from} onChange={e => setFrom(e.target.value)} />
          </div>
          <div className="flex flex-col gap-1">
            <label className="form-label">To</label>
            <input type="datetime-local" className="form-control"
              value={to} onChange={e => setTo(e.target.value)} />
          </div>
        </div>

        <div className="flex items-center gap-3">
          <button className="btn btn-primary btn-sm" onClick={run} disabled={!!job || !pre}>
            {job ? <><Spinner size={12} /> {job.pct}%</> : '🔬 Run Analysis'}
          </button>
          {job && <span className="text-[11px] text-ge-text2">{job.message}</span>}
        </div>

        {job && (
          <div className="mt-2 h-1 rounded bg-ge-elevated overflow-hidden">
            <div className="h-full bg-ge-accent transition-all duration-300"
              style={{ width: `${Math.max(2, Math.min(100, job.pct))}%` }} />
          </div>
        )}
      </div>

      {/* ── Source coverage (always visible, even before a run) ────────────── */}
      {pre?.groups && (
        <Section title="Data Coverage" defaultOpen
          subtitle="measured from the database, per source group">
          <div className="overflow-x-auto">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Source group</th><th>Present</th><th>Cadence</th>
                  <th>Common window (all tables in the group)</th><th>Rows</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(pre.groups).map(([k, g]) => (
                  <tr key={k}>
                    <td className="font-mono text-[11px]">{k}</td>
                    <td className={`font-mono text-[11px] ${
                      g.present < g.expected ? 'text-ge-warn' : 'text-ge-text2'}`}>
                      {g.present}/{g.expected}
                    </td>
                    <td className="font-mono text-[11px]">
                      {g.cadence_seconds ? `${g.cadence_seconds}s` : '—'}
                    </td>
                    <td className="font-mono text-[11px] text-ge-text3">
                      {dmy(g.common_start)} → {dmy(g.common_end)}
                    </td>
                    <td className="font-mono text-[11px] text-right">{num(g.rows)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="text-[10px] text-ge-text3 mt-2">
            Cadence is detected from the data (the modal gap between consecutive
            records), not configured.
          </p>
        </Section>
      )}

      {/* ── Results ───────────────────────────────────────────────────────── */}
      {report && (
        <>
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 mb-3">
            <Stat label="Findings" value={report.findings?.length ?? 0} />
            <Stat label="Rows examined" value={num(totals?.rows_examined)} />
            <Stat label="Mean coverage"
              value={totals?.mean_coverage_pct != null ? `${totals.mean_coverage_pct}%` : '—'}
              tone={totals?.mean_coverage_pct >= 99 ? 'text-ge-accent' : 'text-ge-warn'}
              sub={totals?.worst_coverage_pct != null
                ? `worst ${totals.worst_coverage_pct}%` : null} />
            <Stat label="Blocked analyses" value={report.data_asks?.length ?? 0}
              tone="text-ge-danger" />
          </div>

          {win && (
            <div className="card mb-3">
              <div className="text-[12px] text-ge-text2">
                Analysed <span className="font-mono text-ge-text1">
                  {dmy(win.effective_start)} → {dmy(win.effective_end)}</span>
                {win.clamped && (
                  <span className="text-ge-warn"> · {win.note}</span>
                )}
              </div>
            </div>
          )}

          {/* Data trust */}
          {integrity && (
            <Section title="Data Trust" defaultOpen
              badge={<span className={`status-pill text-[10px] ${
                integrity.status === 'AVAILABLE' ? 'pill-green' : 'pill-amber'}`}>
                {integrity.status}</span>}
              subtitle={integrity.reason}>
              <div className="grid grid-cols-2 lg:grid-cols-5 gap-3 mb-3">
                <Stat label="Sources audited" value={integrity.metrics?.sources_audited} />
                <Stat label="Missing intervals" value={num(totals?.missing_intervals)} />
                <Stat label="Duplicate timestamps" value={num(totals?.duplicate_timestamps)} />
                <Stat label="Frozen channels" value={num(totals?.frozen_channels_flagged)} />
                <Stat label="Range violations" value={num(totals?.range_violations_flagged)} />
              </div>
              <div className="overflow-x-auto">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Source</th><th>Rows</th><th>Cadence</th><th>Coverage</th>
                      <th>Missing</th><th>Duplicates</th><th>Frozen</th><th>Out of range</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(integrity.metrics?.per_source || []).map(s => (
                      <tr key={s.table}>
                        <td className="font-mono text-[11px]">{s.table}</td>
                        <td className="font-mono text-[11px] text-right">{num(s.rows)}</td>
                        <td className="font-mono text-[11px]">
                          {s.cadence_seconds ? `${s.cadence_seconds}s` : '—'}</td>
                        <td className={`font-mono text-[11px] text-right ${
                          s.continuity?.coverage_pct < 99 ? 'text-ge-warn' : 'text-ge-accent'}`}>
                          {s.continuity?.coverage_pct != null
                            ? `${s.continuity.coverage_pct}%` : '—'}</td>
                        <td className="font-mono text-[11px] text-right">
                          {num(s.continuity?.missing_intervals)}</td>
                        <td className="font-mono text-[11px] text-right">
                          {num(s.duplicate_timestamps)}</td>
                        <td className="font-mono text-[11px] text-right">
                          {s.frozen_channels?.length ?? '—'}</td>
                        <td className="font-mono text-[11px] text-right">
                          {s.invalid_values?.length ?? '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Section>
          )}

          {/* Findings */}
          <Section title="Findings" defaultOpen
            subtitle={`${report.findings?.length ?? 0} observation(s), most severe first`}>
            {(report.findings || []).length === 0
              ? <div className="text-[12px] text-ge-text3 py-6 text-center">
                  No findings were raised for this window.
                </div>
              : (report.findings || []).map(f => (
                <details key={f.finding_id} className="border border-ge-border rounded mb-2">
                  <summary className="px-3 py-2 cursor-pointer flex items-center gap-2">
                    <span className={`status-pill text-[10px] ${
                      SEVERITY_STYLE[f.severity] || 'pill-blue'}`}>{f.severity}</span>
                    <span className="text-[12px] text-ge-text1">{f.title}</span>
                    <span className="ml-auto text-[10px] text-ge-text3 font-mono">
                      {f.classification}
                    </span>
                  </summary>
                  <div className="px-3 pb-3 text-[11px] text-ge-text2 space-y-2">
                    <p>{f.summary}</p>

                    {f.recommended_action && (
                      <p><span className="text-ge-text3">Recommended action: </span>
                        {f.recommended_action}</p>
                    )}

                    {(f.supporting_evidence || []).length > 0 && (
                      <div>
                        <div className="text-ge-text3">Supporting evidence</div>
                        <ul className="list-disc ml-5">
                          {f.supporting_evidence.map((e, i) =>
                            <li key={i}>{e.statement}</li>)}
                        </ul>
                      </div>
                    )}
                    {(f.contradicting_evidence || []).length > 0 && (
                      <div>
                        <div className="text-ge-text3">Contradicting evidence</div>
                        <ul className="list-disc ml-5 text-ge-warn">
                          {f.contradicting_evidence.map((e, i) =>
                            <li key={i}>{e.statement}</li>)}
                        </ul>
                      </div>
                    )}

                    {/* Provenance — what makes the finding auditable. */}
                    <div className="grid grid-cols-2 gap-2 pt-1 text-[10px] font-mono text-ge-text3">
                      <div>tables: {(f.source_tables || []).join(', ') || '—'}</div>
                      <div>columns: {(f.source_columns || []).join(', ') || '—'}</div>
                      <div>window: {dmy(f.window_start)} → {dmy(f.window_end)}</div>
                      <div>verification: {f.verification}</div>
                    </div>
                  </div>
                </details>
              ))}
          </Section>

          {/* Data asks */}
          <Section title="Data Asks"
            subtitle="what additional data would unlock a blocked analysis"
            badge={<span className="status-pill pill-amber text-[10px]">
              {report.data_asks?.length ?? 0}</span>}>
            <div className="overflow-x-auto">
              <table className="data-table">
                <thead>
                  <tr><th>Analysis</th><th>Required data</th><th>Unlocks</th><th>Reason</th></tr>
                </thead>
                <tbody>
                  {(report.data_asks || []).map(a => (
                    <tr key={a.ask_id}>
                      <td className="text-[11px]">{a.analysis}</td>
                      <td className="text-[11px] text-ge-warn">
                        {(a.required_data || []).join(', ')}</td>
                      <td className="text-[11px] text-ge-text3">{a.unlocks}</td>
                      <td className="text-[11px] text-ge-text3">{a.reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Section>

          {/* Methodology / audit trail */}
          <Section title="Methodology & Audit Trail"
            subtitle="how each module ran, and what it could not do">
            <div className="overflow-x-auto mb-3">
              <table className="data-table">
                <thead>
                  <tr><th>Module</th><th>Status</th><th>Window</th><th>Duration</th><th>Note</th></tr>
                </thead>
                <tbody>
                  {(report.module_results || []).map(m => (
                    <tr key={m.analysis}>
                      <td className="font-mono text-[11px]">{m.analysis}</td>
                      <td className={`font-mono text-[11px] ${STATUS_STYLE[m.status] || ''}`}>
                        {m.status}</td>
                      <td className="font-mono text-[10px] text-ge-text3">
                        {dmy(m.window_start)} → {dmy(m.window_end)}</td>
                      <td className="font-mono text-[11px] text-right">
                        {m.duration_ms != null ? `${(m.duration_ms / 1000).toFixed(1)}s` : '—'}</td>
                      <td className="text-[11px] text-ge-text3">{m.reason || '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="text-[11px] text-ge-text3">
              <div className="text-ge-text2 mb-1">Limitations of this archive</div>
              <ul className="list-disc ml-5">
                {(report.limitations || []).map((l, i) => <li key={i}>{l}</li>)}
              </ul>
            </div>
            <p className="text-[10px] text-ge-text3 mt-3">
              Report {report.report_id} · generated {dmy(report.generated_at)} · every
              figure above is computed in SQL/Python; no language model was involved in
              producing these numbers.
            </p>
          </Section>
        </>
      )}
    </div>
  )
}
