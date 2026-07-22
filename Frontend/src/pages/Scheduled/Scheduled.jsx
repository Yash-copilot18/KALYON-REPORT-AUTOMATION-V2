// src/pages/Scheduled/Scheduled.jsx
import React, { useState, useMemo, useEffect } from 'react'
import { PageHeader, Spinner } from '../../components/Common'
import { useApp } from '../../utils/AppContext'
import {
  fetchScheduledEmailConfig, sendScheduledEmail, sendTestEmail,
  fetchReportEquipmentList,
} from '../../services/api'
import {
  INTERVALS, INTERVAL_LABELS, AGG_OPTIONS, DEFAULT_AGG,
  showsAggregation, withAggregation,
} from '../../utils/intervals'
import { PRESET_TYPES, getPreset, presetEquipmentIds } from '../../utils/reportPresets'

const FREQ_OPTIONS   = ['Daily', 'Weekly', 'Monthly']
const FORMAT_OPTIONS = ['CSV', 'Excel']

function todayStr() {
  return new Date().toISOString().slice(0, 10)
}

// Only CSV and Excel are supported. Any legacy/unsupported format (e.g. PDF or
// combined formats) is gracefully coerced to Excel so existing schedules keep working.
function normalizeFormat(fmt) {
  return fmt === 'CSV' || fmt === 'Excel' ? fmt : 'Excel'
}

function nextRun(freq) {
  const d = new Date()
  if (freq === 'Daily')   d.setDate(d.getDate() + 1)
  if (freq === 'Weekly')  d.setDate(d.getDate() + 7)
  if (freq === 'Monthly') d.setMonth(d.getMonth() + 1)
  const dd = String(d.getDate()).padStart(2,'0')
  const mm = String(d.getMonth()+1).padStart(2,'0')
  return `${dd}/${mm}/${d.getFullYear()}`
}

// Legacy/seed formats (PDF, combined) are intentionally left here to verify that
// normalizeFormat() coerces them safely when the schedules are loaded.
const INITIAL_SCHEDULES = [
  {
    id:1, eq_type:'Inverter',        eq_id:'INVERTER_01',
    from:'2024-02-08', to:'2024-05-21', interval:'hourly',  agg:'avg',
    format:'Excel', freq:'Daily',   email:'ops@ge.com',         status:'Active',
    last_run:'22/06/2026 06:00:00', created:'08/06/2026',
  },
  {
    id:2, eq_type:'PPC',             eq_id:'PPC',
    from:'2024-02-08', to:'2024-05-21', interval:'daily',   agg:'avg',
    format:'PDF',   freq:'Weekly',  email:'management@ge.com',  status:'Active',
    last_run:'18/06/2026 07:00:00', created:'01/06/2026',
  },
  {
    id:3, eq_type:'Daily Generation', eq_id:'INVERTER_DAILY_GEN',
    from:'2024-02-08', to:'2024-05-21', interval:'monthly', agg:'sum',
    format:'CSV + Excel + PDF', freq:'Monthly', email:'ceo@ge.com', status:'Active',
    last_run:'01/06/2026 08:00:00', created:'01/01/2026',
  },
  {
    id:4, eq_type:'Alarms',           eq_id:'Alarms',
    from:'2024-02-08', to:'2024-05-21', interval:'daily',   agg:'avg',
    format:'CSV',   freq:'Daily',   email:'ops@ge.com',         status:'Paused',
    last_run:'21/06/2026 20:00:00', created:'15/06/2026',
  },
  {
    id:5, eq_type:'WMS',              eq_id:'WMS',
    from:'2024-02-08', to:'2024-05-21', interval:'hourly',  agg:'avg',
    format:'Excel', freq:'Weekly',  email:'eng@ge.com',         status:'Active',
    last_run:'16/06/2026 06:30:00', created:'10/06/2026',
  },
]

// ── Schedule Form ──────────────────────────────────────────────────────────────
function ScheduleForm({ initial, onSave, onCancel, recipient }) {
  const [form, setForm] = useState(
    initial
      ? { ...initial, format: normalizeFormat(initial.format) }
      : {
          eq_type:'', eq_id:'', from:todayStr(), to:todayStr(),
          interval:'hourly', agg:DEFAULT_AGG, format:'Excel', freq:'Daily',
        }
  )

  // Equipment identifiers for the selected report type — read from the database
  // through the same endpoint the Reports page uses (never a hardcoded list).
  const [eqList,    setEqList]    = useState([])
  const [loadingEq, setLoadingEq] = useState(false)

  const set = (k, v) => setForm(f => ({ ...f, [k]: v }))

  // Changing interval restores the default aggregation, so returning from an
  // instant interval shows the dropdown back on "Average".
  const changeInterval = next =>
    setForm(f => ({ ...f, interval: next, agg: DEFAULT_AGG }))

  // ── Preconfigured report ────────────────────────────────────────────────────
  // Picking a report type loads its preset: default Time Interval, Aggregation and
  // Export Format immediately, and the default Equipment Identifier as soon as the
  // identifier list arrives. All of them remain editable before saving.
  const changeType = type => {
    const preset = getPreset(type)
    setForm(f => ({
      ...f,
      eq_type:  type,
      eq_id:    '',
      interval: preset.interval,
      agg:      preset.agg,
      format:   preset.format,
    }))
  }

  useEffect(() => {
    const type = form.eq_type
    if (!type) { setEqList([]); return }
    let cancelled = false
    setLoadingEq(true)
    fetchReportEquipmentList(type)
      .then(data => {
        if (cancelled) return
        const list = Array.isArray(data) ? data : (data?.items || data?.data || [])
        setEqList(list)
        // Keep an already-valid identifier (editing an existing schedule); otherwise
        // fall back to the preset's default identifier for this report type.
        setForm(f => (f.eq_id && list.some(e => e.equipment_id === f.eq_id))
          ? f
          : { ...f, eq_id: presetEquipmentIds(type, list)[0] || '' })
      })
      .catch(() => { if (!cancelled) setEqList([]) })
      .finally(() => { if (!cancelled) setLoadingEq(false) })
    return () => { cancelled = true }
  }, [form.eq_type])

  // A schedule saved against a report type that is no longer offered stays
  // selectable so editing it never silently rewrites the type.
  const typeOptions = useMemo(
    () => (form.eq_type && !PRESET_TYPES.includes(form.eq_type)
      ? [...PRESET_TYPES, form.eq_type]
      : PRESET_TYPES),
    [form.eq_type])

  const preset = getPreset(form.eq_type)

  const handleSubmit = e => {
    e.preventDefault()
    if (!form.eq_type.trim())  return alert('Equipment type required')
    onSave(form)
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-3">
      <div className="grid grid-cols-2 gap-3">
        <div className="flex flex-col gap-1">
          <label className="form-label">Report Type *</label>
          <select className="form-control" value={form.eq_type}
            onChange={e => changeType(e.target.value)}>
            <option value="">— Select —</option>
            {typeOptions.map(t => <option key={t}>{t}</option>)}
          </select>
        </div>
        <div className="flex flex-col gap-1">
          <label className="form-label">Equipment Identifier</label>
          {loadingEq ? (
            <div className="form-control flex items-center gap-2 text-ge-text3 text-[12px]">
              <Spinner size={12} /> Loading identifiers...
            </div>
          ) : (
            <select className="form-control" value={form.eq_id}
              onChange={e => set('eq_id', e.target.value)}
              disabled={!form.eq_type}>
              {!form.eq_type && <option value="">— Select a report type first —</option>}
              {form.eq_id && !eqList.some(e => e.equipment_id === form.eq_id) && (
                <option value={form.eq_id}>{form.eq_id}</option>
              )}
              {eqList.map(e => (
                <option key={e.equipment_id} value={e.equipment_id}>
                  {e.display_name || e.equipment_id}
                </option>
              ))}
            </select>
          )}
        </div>
      </div>

      {/* Preconfigured defaults applied for this report type — all editable below. */}
      {form.eq_type && (
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 rounded-md
                        border border-ge-accent/30 bg-ge-accent/5 px-2.5 py-1.5 text-[11px]">
          <span className="text-ge-accent">⚡</span>
          <span className="text-ge-text2">Preconfigured defaults loaded</span>
          {preset.note && <span className="font-mono text-ge-text3">{preset.note}</span>}
          <span className="ml-auto text-[10px] uppercase tracking-widest text-ge-text3">
            Editable
          </span>
        </div>
      )}

      <div className="grid grid-cols-2 gap-3">
        <div className="flex flex-col gap-1">
          <label className="form-label">From Date</label>
          <input type="date" className="form-control" value={form.from}
            onChange={e => set('from', e.target.value)} />
        </div>
        <div className="flex flex-col gap-1">
          <label className="form-label">To Date</label>
          <input type="date" className="form-control" value={form.to}
            onChange={e => set('to', e.target.value)} />
        </div>
      </div>

      {/* Instant telemetry is never aggregated: the Aggregation field is not rendered,
          and the row collapses to a single column so no empty slot is left behind. */}
      <div className={`grid gap-3 ${showsAggregation(form.interval) ? 'grid-cols-2' : 'grid-cols-1'}`}>
        <div className="flex flex-col gap-1">
          <label className="form-label">Time Interval</label>
          <select className="form-control" value={form.interval}
            onChange={e => changeInterval(e.target.value)}>
            {INTERVALS.map(v => (
              <option key={v} value={v}>{INTERVAL_LABELS[v]}</option>
            ))}
          </select>
        </div>
        {showsAggregation(form.interval) && (
          <div className="flex flex-col gap-1">
            <label className="form-label">Aggregation</label>
            <select className="form-control" value={form.agg}
              onChange={e => set('agg', e.target.value)}>
              {AGG_OPTIONS.map(o => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
          </div>
        )}
      </div>

      <div className="grid grid-cols-2 gap-3">
        <div className="flex flex-col gap-1">
          <label className="form-label">Report Format *</label>
          <select className="form-control" value={form.format}
            onChange={e => set('format', e.target.value)}>
            {FORMAT_OPTIONS.map(f => <option key={f}>{f}</option>)}
          </select>
        </div>
        <div className="flex flex-col gap-1">
          <label className="form-label">Schedule Frequency *</label>
          <select className="form-control" value={form.freq}
            onChange={e => set('freq', e.target.value)}>
            {FREQ_OPTIONS.map(f => <option key={f}>{f}</option>)}
          </select>
        </div>
      </div>

      {/* Recipient dropdown removed for the testing phase — reports are e-mailed to
          a single recipient configured in the backend (.env). Multi-recipient
          support with a searchable dropdown comes in a later phase. */}
      <div className="flex flex-col gap-1">
        <label className="form-label">Recipient</label>
        <div className="form-control flex items-center gap-2 text-ge-text2 text-[12px] bg-ge-elevated">
          <span>📧</span>
          <span className="font-mono">{recipient || 'Configured in backend (.env)'}</span>
          <span className="ml-auto text-[10px] text-ge-text3 uppercase tracking-wider">
            Fixed (testing)
          </span>
        </div>
      </div>

      <div className="flex gap-2 pt-2 justify-end">
        <button type="button" className="btn btn-outline btn-sm" onClick={onCancel}>
          Cancel
        </button>
        <button type="submit" className="btn btn-primary btn-sm">
          {initial?.id ? '💾 Save Changes' : '+ Create Schedule'}
        </button>
      </div>
    </form>
  )
}

// ── Main Page ──────────────────────────────────────────────────────────────────
export default function Scheduled() {
  const { showToast } = useApp()

  // Coerce any legacy/unsupported formats (e.g. PDF) to a supported one on load.
  const [schedules, setSchedules] = useState(
    () => INITIAL_SCHEDULES.map(s => ({ ...s, format: normalizeFormat(s.format) }))
  )
  const [showForm,  setShowForm]  = useState(false)
  const [editItem,  setEditItem]  = useState(null)
  const [running,   setRunning]   = useState(new Set())
  const [filter,    setFilter]    = useState('All')
  const [search,    setSearch]    = useState('')
  const [histItem,  setHistItem]  = useState(null)

  // Backend e-mail config (fixed recipient for the testing phase)
  const [recipient,   setRecipient]   = useState('')
  const [smtpOk,      setSmtpOk]      = useState(true)
  const [missingVars, setMissingVars] = useState([])
  const [testing,     setTesting]     = useState(false)

  useEffect(() => {
    fetchScheduledEmailConfig()
      .then(cfg => {
        setRecipient(cfg?.recipient || '')
        setSmtpOk(!!cfg?.smtp_configured)
        setMissingVars(Array.isArray(cfg?.missing_vars) ? cfg.missing_vars : [])
      })
      .catch(() => { /* backend offline — keep defaults */ })
  }, [])

  const MOCK_HISTORY = [
    { time:'22/06/2026 06:00:00', status:'Success', size:'245 KB', duration:'12s' },
    { time:'21/06/2026 06:00:00', status:'Success', size:'241 KB', duration:'11s' },
    { time:'20/06/2026 06:00:00', status:'Failed',  size:'—',      duration:'30s' },
    { time:'19/06/2026 06:00:00', status:'Success', size:'238 KB', duration:'13s' },
    { time:'18/06/2026 06:00:00', status:'Success', size:'250 KB', duration:'12s' },
  ]

  const rows = useMemo(() =>
    schedules.filter(s => {
      const matchF = filter === 'All' || s.freq === filter
      const matchS = !search || [s.eq_type, s.eq_id].some(
        v => v && v.toLowerCase().includes(search.toLowerCase())
      )
      return matchF && matchS
    }),
    [schedules, filter, search]
  )

  const handleSave = form => {
    if (form.id) {
      setSchedules(prev => prev.map(s => s.id === form.id ? { ...s, ...form } : s))
      showToast('Schedule updated')
    } else {
      const newItem = {
        ...form,
        id:       Date.now(),
        status:   'Active',
        last_run: '—',
        created:  new Date().toLocaleDateString('en-GB'),
      }
      setSchedules(prev => [...prev, newItem])
      showToast('Schedule created')
    }
    setShowForm(false)
    setEditItem(null)
  }

  const handleDelete = id => {
    setSchedules(prev => prev.filter(s => s.id !== id))
    showToast('Schedule deleted')
  }

  const handleToggle = id => {
    setSchedules(prev => prev.map(s =>
      s.id === id
        ? { ...s, status: s.status === 'Active' ? 'Paused' : 'Active' }
        : s
    ))
  }

  // Run a schedule now: backend generates the report, attaches it, and e-mails it.
  const handleRun = async id => {
    const sch = schedules.find(s => s.id === id)
    if (!sch) return
    setRunning(prev => new Set([...prev, id]))
    try {
      // The schedule's interval/aggregation must reach the backend; on an instant
      // interval withAggregation() drops `agg_function` so the report stays raw.
      const res = await sendScheduledEmail(withAggregation({
        equipment_type: sch.eq_type,
        equipment_id:   sch.eq_id,
        format:         sch.format,
      }, sch.interval, sch.agg))
      if (res?.status === 'Success') {
        setSchedules(prev => prev.map(s =>
          s.id === id ? { ...s, last_run: res.execution_time } : s
        ))
        showToast(`Report e-mailed to ${res.recipient}`)
      } else {
        showToast(`⚠ Email failed: ${res?.error || 'unknown error'}`)
      }
    } catch (e) {
      showToast(`⚠ Run failed: ${e.message}`)
    } finally {
      setRunning(prev => { const s = new Set(prev); s.delete(id); return s })
    }
  }

  // Test Email: generate a sample report and e-mail it to the configured recipient.
  const handleTestEmail = async () => {
    setTesting(true)
    try {
      const res = await sendTestEmail({ format: 'Excel' })
      if (res?.status === 'Success') {
        showToast(`Test email sent to ${res.recipient}`)
      } else {
        showToast(`⚠ Test email failed: ${res?.error || 'unknown error'}`)
      }
    } catch (e) {
      showToast(`⚠ Test email failed: ${e.message}`)
    } finally {
      setTesting(false)
    }
  }

  const counts = {
    total:  schedules.length,
    active: schedules.filter(s => s.status === 'Active').length,
    paused: schedules.filter(s => s.status === 'Paused').length,
  }

  return (
    <div>
      <PageHeader
        title="Scheduled Reports"
      >
        <button className="btn btn-outline btn-sm"
          onClick={handleTestEmail}
          disabled={testing}
          title={recipient ? `Send a sample report to ${recipient}` : 'Send a sample report'}>
          {testing
            ? <><Spinner size={12} /> Sending...</>
            : <>📧 Test Email</>
          }
        </button>
        <button className="btn btn-success btn-sm"
          onClick={() => { setEditItem(null); setShowForm(true) }}>
          + Create Schedule
        </button>
      </PageHeader>

      {/* Configure SMTP — shown until e-mail delivery is enabled */}
      {!smtpOk && (
        <div className="card mb-4 bg-ge-warn/5 border-ge-warn/40">
          <div className="card-title text-ge-warn">⚠ Configure SMTP to enable e-mail delivery</div>
          <p className="text-[12px] text-ge-text2 mb-2">
            E-mail sending is currently disabled. Set the following in the backend{' '}
            <span className="font-mono text-ge-text1">Backend/.env</span> file
            (see <span className="font-mono text-ge-text1">.env.example</span>), then restart the backend.
          </p>

          {missingVars.length > 0 && (
            <div className="mb-2">
              <div className="text-[10px] font-semibold text-ge-text3 uppercase tracking-widest mb-1.5">
                Missing variables
              </div>
              <div className="flex flex-wrap gap-1.5">
                {missingVars.map(v => (
                  <span key={v}
                    className="inline-flex items-center px-2 py-0.5 rounded font-mono text-[11px]
                               bg-ge-danger/15 border border-ge-danger/40 text-ge-danger">
                    {v}
                  </span>
                ))}
              </div>
            </div>
          )}

          <div className="text-[10px] font-semibold text-ge-text3 uppercase tracking-widest mb-1.5">
            Example (Gmail)
          </div>
          <pre className="text-[11px] font-mono text-ge-text2 bg-ge-elevated border border-ge-border
                          rounded-md p-2.5 overflow-x-auto leading-relaxed">{`SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your_gmail_address@gmail.com
SMTP_PASSWORD=your_16_char_app_password
SMTP_FROM_EMAIL=your_gmail_address@gmail.com`}</pre>
          <p className="text-[11px] text-ge-text3 mt-2">
            Gmail requires an <span className="text-ge-text2">App Password</span> (2-Step Verification on →
            Google Account → Security → App passwords). Use the 16-character App Password, not your
            login password.
          </p>
        </div>
      )}

      {/* Stats */}
      <div className="grid grid-cols-3 gap-3 mb-4">
        <div className="card text-center py-3">
          <div className="text-2xl font-mono text-ge-blue">{counts.total}</div>
          <div className="text-[11px] text-ge-text3 mt-1">Total Schedules</div>
        </div>
        <div className="card text-center py-3">
          <div className="text-2xl font-mono text-ge-accent">{counts.active}</div>
          <div className="text-[11px] text-ge-text3 mt-1">Active</div>
        </div>
        <div className="card text-center py-3">
          <div className="text-2xl font-mono text-ge-warn">{counts.paused}</div>
          <div className="text-[11px] text-ge-text3 mt-1">Paused</div>
        </div>
      </div>

      {/* Create / Edit Form */}
      {showForm && (
        <div className="card mb-4 border-ge-blue/30">
          <div className="card-title">
            {editItem
              ? `Edit Schedule — ${editItem.eq_type}${editItem.eq_id ? ` / ${editItem.eq_id}` : ''}`
              : 'Create New Schedule'}
          </div>
          <ScheduleForm
            initial={editItem}
            onSave={handleSave}
            onCancel={() => { setShowForm(false); setEditItem(null) }}
            recipient={recipient}
          />
        </div>
      )}

      {/* Filters */}
      <div className="card mb-3">
        <div className="flex flex-wrap gap-2 items-center">
          {['All','Daily','Weekly','Monthly'].map(f => (
            <button key={f} onClick={() => setFilter(f)}
              className={`btn btn-sm ${filter === f ? 'btn-primary' : 'btn-outline'}`}>
              {f}
            </button>
          ))}
          <div className="ml-auto relative w-48">
            <span className="absolute left-2.5 top-1/2 -translate-y-1/2 text-ge-text3 text-sm">
              🔍
            </span>
            <input type="text" value={search} onChange={e => setSearch(e.target.value)}
              placeholder="Search..." className="form-control pl-7 text-[12px]" />
          </div>
        </div>
      </div>

      {/* Schedule Table */}
      <div className="card mb-4">
        <div className="card-title">Report Schedules</div>
        <div className="overflow-x-auto">
          <table className="data-table">
            <thead>
              <tr>
                <th>Equipment</th>
                <th className="whitespace-nowrap w-px">Format</th>
                <th className="whitespace-nowrap w-px">Frequency</th>
                <th className="whitespace-nowrap w-px">Next Run</th>
                <th className="whitespace-nowrap w-px">Last Run</th>
                <th>Recipients</th>
                <th className="whitespace-nowrap w-px">Actions</th>
              </tr>
            </thead>
            <tbody>
              {rows.length === 0 ? (
                <tr>
                  <td colSpan={7} className="text-center text-ge-text3 py-10 text-[12px]">
                    No schedules found — click "Create Schedule" to add one
                  </td>
                </tr>
              ) : rows.map(s => (
                <tr key={s.id}>
                  <td>
                    <div className="text-[12px] text-ge-text1">{s.eq_type}</div>
                    <div className="text-[10px] font-mono text-ge-text3">
                      {s.eq_id} · {s.interval} · {s.agg?.toUpperCase()}
                    </div>
                  </td>
                  <td className="whitespace-nowrap w-px">
                    <span className="status-pill pill-blue text-[10px]">{s.format}</span>
                  </td>
                  <td className="font-mono text-[11px] whitespace-nowrap w-px">{s.freq}</td>
                  <td className="font-mono text-[11px] text-ge-accent whitespace-nowrap w-px">{nextRun(s.freq)}</td>
                  <td className="font-mono text-[11px] text-ge-text3 whitespace-nowrap w-px">{s.last_run || '—'}</td>
                  <td className="text-[11px] text-ge-blue max-w-[220px] truncate"
                    title={recipient}>
                    {recipient || '—'}
                  </td>
                  <td className="whitespace-nowrap w-px">
                    <div className="flex items-center gap-1.5">
                      {/* Run now */}
                      <button
                        className="btn btn-outline btn-sm"
                        onClick={() => handleRun(s.id)}
                        disabled={running.has(s.id) || s.status === 'Paused'}
                        title="Run now"
                      >
                        {running.has(s.id)
                          ? <Spinner size={10} />
                          : '▶'
                        }
                      </button>

                      {/* Toggle */}
                      <button
                        className={`btn btn-sm ${s.status === 'Active' ? 'btn-outline' : 'btn-success'}`}
                        onClick={() => handleToggle(s.id)}
                        title={s.status === 'Active' ? 'Pause' : 'Enable'}
                      >
                        {s.status === 'Active' ? '⏸' : '▶'}
                      </button>

                      {/* Edit */}
                      <button
                        className="btn btn-outline btn-sm"
                        onClick={() => { setEditItem(s); setShowForm(true) }}
                        title="Edit"
                      >
                        ✎
                      </button>

                      {/* History */}
                      <button
                        className="btn btn-outline btn-sm"
                        onClick={() => setHistItem(histItem?.id === s.id ? null : s)}
                        title="History"
                      >
                        📋
                      </button>

                      {/* Delete */}
                      <button
                        className="btn btn-danger btn-sm"
                        onClick={() => handleDelete(s.id)}
                        title="Delete"
                      >
                        ✕
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* Execution History Panel */}
      {histItem && (
        <div className="card border-ge-blue/30">
          <div className="card-title justify-between">
            <span>📋 Execution History — {histItem.eq_type}{histItem.eq_id ? ` / ${histItem.eq_id}` : ''}</span>
            <button className="btn btn-outline btn-sm" onClick={() => setHistItem(null)}>✕ Close</button>
          </div>
          <table className="data-table">
            <thead>
              <tr>
                <th>Execution Time</th>
                <th>Status</th>
                <th>File Size</th>
                <th>Duration</th>
              </tr>
            </thead>
            <tbody>
              {MOCK_HISTORY.map((h, i) => (
                <tr key={i}>
                  <td className="font-mono text-[11px]">{h.time}</td>
                  <td>
                    <span className={`status-pill ${h.status === 'Success' ? 'pill-green' : 'pill-red'}`}>
                      {h.status}
                    </span>
                  </td>
                  <td className="font-mono text-[11px]">{h.size}</td>
                  <td className="font-mono text-[11px]">{h.duration}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}