// src/pages/Scheduled/Scheduled.jsx
import React, { useState, useMemo, useEffect } from 'react'
import { PageHeader, Spinner } from '../../components/Common'
import { useApp } from '../../utils/AppContext'
import {
  fetchScheduledEmailConfig, sendTestEmail, fetchReportEquipmentList,
  fetchReportEquipmentTypes,
  fetchSchedules, createSchedule, updateSchedule, deleteSchedule,
  runSchedule, pauseSchedule, resumeSchedule, fetchScheduleRuns,
} from '../../services/api'
import {
  INTERVALS, INTERVAL_LABELS, AGG_OPTIONS, DEFAULT_AGG,
  showsAggregation,
} from '../../utils/intervals'
import { getPreset, presetEquipmentIds } from '../../utils/reportPresets'

const FREQ_OPTIONS   = ['Daily', 'Weekly', 'Monthly']
const FORMAT_OPTIONS = ['Excel', 'PDF']

function todayStr() {
  return new Date().toISOString().slice(0, 10)
}

// Scheduled Reports supports ONLY Excel and PDF (client rule — CSV was removed).
// Any legacy/unsupported value (e.g. an old "CSV" schedule) is coerced to Excel so
// existing schedules keep working and never show a format that is no longer offered.
function normalizeFormat(fmt) {
  return fmt === 'PDF' || fmt === 'Excel' ? fmt : 'Excel'
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

// ── Schedule Form ──────────────────────────────────────────────────────────────
// The Report Type dropdown shows every equipment/report type the application supports
// (loaded dynamically from the same /equipment-types API the Reports page uses) AND
// the three generation reports (DGR / MGR / YGR), which are appended at the end. All
// remain fully schedulable — the backend generates each through its own logic.
//
// The three generation report types, in the order they appear at the end of the
// dropdown. "Yearly Generation" is not an equipment table, so it is not returned by
// the equipment-types API and is added here explicitly.
const GENERATION_TYPES = ['Daily Generation', 'Monthly Generation', 'Yearly Generation']

// Friendly display labels for the generation reports (equipment types show their own
// name). Used in both the dropdown and the schedule table.
const REPORT_TYPE_LABELS = {
  'Daily Generation':   'DGR — Daily Generation Report',
  'Monthly Generation': 'MGR — Monthly Generation Report',
  'Yearly Generation':  'YGR — Yearly Generation Report',
}
const YGR_TYPE = 'Yearly Generation'          // plant-level: has no equipment identifier
const DEFAULT_TIME = '20:30'                   // default scheduled execution time (8:30 PM)

const typeLabel = (v) => REPORT_TYPE_LABELS[v] || v

// ── Recipients ────────────────────────────────────────────────────────────────
// Same address rule the backend enforces (email_service.EMAIL_RE), so the UI never
// accepts something the API would reject.
const EMAIL_RE = /^[^\s@,;]+@[^\s@,;]+\.[A-Za-z]{2,}$/
const isEmail = v => EMAIL_RE.test((v || '').trim())

// The stored form is a comma-separated string — the shape the `recipients` column has
// always used — so these two helpers are the only place the list/string conversion
// happens and the API payload is unchanged.
const splitRecipients = v =>
  String(v || '').split(/[,;\s]+/).map(s => s.trim()).filter(Boolean)
const joinRecipients = list => list.join(', ')

// Multi-email input: each valid address becomes a removable chip. Enter, comma and
// semicolon commit the address; paste splits on commas/semicolons/whitespace so a
// whole list can be pasted at once; Backspace on an empty box removes the last chip.
// Duplicates (case-insensitive) and malformed addresses are refused with a message.
function RecipientsInput({ value, onChange, fallback }) {
  const [draft, setDraft] = useState('')
  const [error, setError] = useState('')
  const list = useMemo(() => splitRecipients(value), [value])

  // Add every address in `raw`; reports the first problem it hits but still adds the
  // good ones, so pasting a mixed list doesn't lose the valid addresses.
  const add = raw => {
    const candidates = splitRecipients(raw)
    if (!candidates.length) return true
    const next = [...list]
    const seen = new Set(next.map(e => e.toLowerCase()))
    let msg = ''
    for (const c of candidates) {
      if (!isEmail(c)) { msg = msg || `"${c}" is not a valid email address`; continue }
      if (seen.has(c.toLowerCase())) { msg = msg || `"${c}" is already added`; continue }
      seen.add(c.toLowerCase()); next.push(c)
    }
    onChange(joinRecipients(next))
    setError(msg)
    return !msg
  }

  const remove = idx => {
    onChange(joinRecipients(list.filter((_, i) => i !== idx)))
    setError('')
  }

  const onKeyDown = e => {
    if (e.key === 'Enter' || e.key === ',' || e.key === ';') {
      e.preventDefault()                       // never submits the form
      if (draft.trim() && add(draft)) setDraft('')
    } else if (e.key === 'Backspace' && !draft && list.length) {
      remove(list.length - 1)
    }
  }

  const onPaste = e => {
    const text = e.clipboardData.getData('text')
    if (!/[,;\s]/.test(text)) return            // a single address types normally
    e.preventDefault()
    if (add(text)) setDraft('')
  }

  return (
    <div className="flex flex-col gap-1">
      <label className="form-label">Recipients *</label>
      <div className="form-control flex flex-wrap items-center gap-1.5 min-h-[38px] py-1.5">
        {list.map((email, i) => (
          <span key={`${email}-${i}`}
            className="inline-flex items-center gap-1 rounded bg-ge-blue/20 border border-ge-blue/40
                       text-ge-text1 font-mono text-[11px] pl-2 pr-1 py-0.5">
            {email}
            <button type="button" onClick={() => remove(i)} aria-label={`Remove ${email}`}
              className="text-ge-text3 hover:text-ge-danger leading-none px-0.5">×</button>
          </span>
        ))}
        <input
          type="text" value={draft}
          onChange={e => { setDraft(e.target.value); if (error) setError('') }}
          onKeyDown={onKeyDown} onPaste={onPaste}
          onBlur={() => { if (draft.trim() && add(draft)) setDraft('') }}
          placeholder={list.length ? 'Add another…' : (fallback || 'name@example.com')}
          className="flex-1 min-w-[150px] bg-transparent border-0 outline-none text-ge-text1
                     text-[12px] font-sans placeholder:text-ge-text3" />
      </div>
      {error
        ? <span className="text-[10px] text-ge-danger">{error}</span>
        : <span className="text-[10px] text-ge-text3">
            Press Enter, comma or semicolon to add. The report is e-mailed to every recipient.
          </span>}
    </div>
  )
}

function ScheduleForm({ initial, onSave, onCancel, recipient }) {
  const [form, setForm] = useState(
    initial
      ? { ...initial, format: normalizeFormat(initial.format), time: initial.time || DEFAULT_TIME }
      : {
          eq_type:'', eq_id:'', from:todayStr(), to:todayStr(),
          interval:'hourly', agg:DEFAULT_AGG, format:'Excel', freq:'Daily',
          time: DEFAULT_TIME,
          // A new schedule starts with the backend's configured address so the
          // default behaviour is unchanged; it can be removed or added to.
          recipients: recipient || '',
        }
  )

  // Equipment identifiers for the selected report type — read from the database
  // through the same endpoint the Reports page uses (never a hardcoded list).
  const [eqList,    setEqList]    = useState([])
  const [loadingEq, setLoadingEq] = useState(false)

  // Supported report types — every equipment/report type from the app's equipment-types
  // API (in its natural order), followed by the three generation reports (DGR/MGR/YGR)
  // appended at the end. Never a hardcoded equipment list.
  const [reportTypes, setReportTypes] = useState(GENERATION_TYPES)
  useEffect(() => {
    let cancelled = false
    fetchReportEquipmentTypes()
      .then(data => {
        const list = Array.isArray(data) ? data : (data?.items || data?.data || [])
        // Equipment types first, minus any generation types the API also returns
        // (Daily/Monthly Generation) so they aren't duplicated when appended below.
        const equip = list
          .map(t => t.equipment_type)
          .filter(t => t && !GENERATION_TYPES.includes(t))
        if (!cancelled) setReportTypes([...equip, ...GENERATION_TYPES])
      })
      .catch(() => { if (!cancelled) setReportTypes([...GENERATION_TYPES]) })
    return () => { cancelled = true }
  }, [])

  const set = (k, v) => setForm(f => ({ ...f, [k]: v }))
  // Set two keys to the same value in one update — used so the single Report Date
  // populates both from/to (scheduled reports use a single date, no range).
  const set2 = (k1, k2, v) => setForm(f => ({ ...f, [k1]: v, [k2]: v }))

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
      format:   normalizeFormat(preset.format),
    }))
  }

  useEffect(() => {
    const type = form.eq_type
    // YGR is a plant-level report — no equipment identifier to load.
    if (!type || type === YGR_TYPE) { setEqList([]); return }
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

  const preset = getPreset(form.eq_type)

  const handleSubmit = e => {
    e.preventDefault()
    if (!form.eq_type.trim())  return alert('Report type required')
    // Every chip must be a valid address and there must be at least one, so the
    // schedule can never be saved with nobody (or a typo) to send to.
    const emails = splitRecipients(form.recipients)
    if (!emails.length)          return alert('At least one recipient email is required')
    const bad = emails.filter(v => !isEmail(v))
    if (bad.length)              return alert(`Invalid email address: ${bad.join(', ')}`)
    onSave({ ...form, recipients: joinRecipients(emails) })
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-3">
      <div className="grid grid-cols-2 gap-3">
        <div className="flex flex-col gap-1">
          <label className="form-label">Report Type *</label>
          <select className="form-control" value={form.eq_type}
            onChange={e => changeType(e.target.value)}>
            <option value="">— Select —</option>
            {/* Show the currently-selected type even if it isn't in the dynamic list
                (e.g. editing an older DGR/MGR/YGR schedule) so its value is preserved. */}
            {form.eq_type && !reportTypes.includes(form.eq_type) && (
              <option value={form.eq_type}>{typeLabel(form.eq_type)}</option>
            )}
            {reportTypes.map(t => (
              <option key={t} value={t}>{typeLabel(t)}</option>
            ))}
          </select>
        </div>
        <div className="flex flex-col gap-1">
          <label className="form-label">Equipment Identifier</label>
          {form.eq_type === YGR_TYPE ? (
            <div className="form-control flex items-center text-ge-text3 text-[12px] bg-ge-elevated">
              Not applicable (plant-level report)
            </div>
          ) : loadingEq ? (
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

      {/* Scheduled reports use a SINGLE date (client requirement) — no To Date / date
          range. The one Report Date is written to BOTH from/to in the payload so the
          backend report-generation logic (which derives the report's day/month/year
          from the schedule's date) is unchanged. */}
      <div className="grid grid-cols-2 gap-3">
        <div className="flex flex-col gap-1">
          <label className="form-label">Report Date *</label>
          <input type="date" className="form-control"
            value={form.to || form.from || todayStr()}
            onChange={e => set2('from', 'to', e.target.value)} />
          <span className="text-[10px] text-ge-text3">
            The single date the scheduled report is generated for.
          </span>
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

      <div className="grid grid-cols-2 gap-3">
        <div className="flex flex-col gap-1">
          <label className="form-label">Schedule Time *</label>
          <input type="time" className="form-control"
            value={form.time || DEFAULT_TIME}
            onChange={e => set('time', e.target.value)} />
          <span className="text-[10px] text-ge-text3">Report runs daily/weekly/monthly at this time (default 8:30 PM).</span>
        </div>
      </div>

      {/* Recipients — one chip per address; the report is e-mailed to all of them.
          Stored on the schedule in the existing comma-separated `recipients` column. */}
      <RecipientsInput
        value={form.recipients}
        onChange={v => set('recipients', v)}
        fallback={recipient} />

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

  // Schedules come entirely from the database (report_schedules) — no dummy data.
  const [schedules, setSchedules] = useState([])
  const [loading,   setLoading]   = useState(true)
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

  // Always load schedules from the database. Called on mount and after every
  // create / edit / delete / run / pause / resume so the list reflects the DB.
  const loadSchedules = React.useCallback(async () => {
    try {
      const data = await fetchSchedules()
      setSchedules(Array.isArray(data) ? data : [])
    } catch {
      setSchedules([])
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { loadSchedules() }, [loadSchedules])

  // Real execution history for the open schedule (schedule_runs table).
  const [history, setHistory] = useState([])
  useEffect(() => {
    if (!histItem) { setHistory([]); return }
    let cancelled = false
    fetchScheduleRuns(histItem.id)
      .then(rows => { if (!cancelled) setHistory(Array.isArray(rows) ? rows : []) })
      .catch(() => { if (!cancelled) setHistory([]) })
    return () => { cancelled = true }
  }, [histItem])

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

  // Create or edit a schedule in the database, then refresh the list so the new /
  // updated row appears immediately without a page reload.
  const handleSave = async form => {
    try {
      if (form.id) {
        await updateSchedule(form.id, form)
        showToast('Schedule updated')
      } else {
        await createSchedule(form)
        showToast('Schedule created')
      }
      await loadSchedules()
      setShowForm(false)
      setEditItem(null)
    } catch (e) {
      showToast(`⚠ Save failed: ${e.message}`)
    }
  }

  const handleDelete = async id => {
    try {
      await deleteSchedule(id)
      if (histItem?.id === id) setHistItem(null)
      await loadSchedules()
      showToast('Schedule deleted')
    } catch (e) {
      showToast(`⚠ Delete failed: ${e.message}`)
    }
  }

  // Pause / Resume against the database, then refresh.
  const handleToggle = async id => {
    const sch = schedules.find(s => s.id === id)
    if (!sch) return
    try {
      if (sch.status === 'Active') await pauseSchedule(id)
      else                         await resumeSchedule(id)
      await loadSchedules()
    } catch (e) {
      showToast(`⚠ Update failed: ${e.message}`)
    }
  }

  // Run a schedule now: backend loads it from the DB, generates the real report,
  // e-mails it, logs the run, and updates Last Run / Next Run.
  const handleRun = async id => {
    setRunning(prev => new Set([...prev, id]))
    try {
      const res = await runSchedule(id)
      if (res?.status === 'Success') {
        showToast(`Report e-mailed to ${res.recipient}`)
      } else {
        showToast(`⚠ Email failed: ${res?.error || 'unknown error'}`)
      }
      await loadSchedules()
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
              ? `Edit Schedule — ${typeLabel(editItem.eq_type)}${editItem.eq_type !== YGR_TYPE && editItem.eq_id ? ` / ${editItem.eq_id}` : ''}`
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
                    <div className="text-[12px] text-ge-text1">{typeLabel(s.eq_type)}</div>
                    <div className="text-[10px] font-mono text-ge-text3">
                      {s.eq_type === YGR_TYPE
                        ? `Plant-level · ${s.time || DEFAULT_TIME}`
                        : `${s.eq_id} · ${s.interval} · ${s.agg?.toUpperCase()} · ${s.time || DEFAULT_TIME}`}
                    </div>
                  </td>
                  <td className="whitespace-nowrap w-px">
                    <span className="status-pill pill-blue text-[10px]">{s.format}</span>
                  </td>
                  <td className="font-mono text-[11px] whitespace-nowrap w-px">{s.freq}</td>
                  <td className="font-mono text-[11px] text-ge-accent whitespace-nowrap w-px">{s.next_run || nextRun(s.freq)}</td>
                  <td className="font-mono text-[11px] text-ge-text3 whitespace-nowrap w-px">{s.last_run || '—'}</td>
                  <td className="text-[11px] text-ge-blue max-w-[220px] truncate"
                    title={s.recipients || recipient}>
                    {s.recipients || recipient || '—'}
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
              {history.length === 0 ? (
                <tr>
                  <td colSpan={4} className="text-center text-ge-text3 py-6 text-[12px]">
                    No executions yet — click ▶ to run this schedule
                  </td>
                </tr>
              ) : history.map((h, i) => (
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