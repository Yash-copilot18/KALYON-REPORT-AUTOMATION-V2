// src/pages/Preconfigured/Preconfigured.jsx
//
// Preconfigured Reports — build one report from columns across several equipment
// types at once (Inverter + WMS + PPC), then export it as a single workbook.
//
// Every selected type stays selected: adding a second type never clears the first
// one's chosen columns. Selections are held in ONE ordered list spanning all
// types, so the report's column order is exactly the order the user picked them,
// and the Selected panel simply groups that list by equipment type for reading.
//
// It is independent of the Reports, DGR, Analytics and Scheduled pages: nothing
// here is imported by them and nothing there is modified. Data comes from the
// multi-equipment endpoints, which merge each source on the interval bucket.
//
// Excel is the only export format offered on this page by design.

import React, { useState, useEffect, useMemo, useCallback, useRef } from 'react'
import {
  PageHeader, FormGroup, Spinner, Skeleton, ConfirmDialog, PromptDialog,
} from '../../components/Common'
import { useApp } from '../../utils/AppContext'
import {
  INTERVAL_LABELS, AGG_OPTIONS, DEFAULT_AGG, withAggregation,
} from '../../utils/intervals'
import { getPreset } from '../../utils/reportPresets'
import {
  listTemplates, createAutoTemplate, updateTemplate,
  renameTemplate, deleteTemplate, TEMPLATE_VERSION,
} from '../../utils/reportTemplates'
import {
  fetchReportEquipmentList,
  fetchReportTags,
  fetchMultiReportData,
  exportMultiReportExcel,
  listSavedReports,
  fetchSavedReportCapacity,
  getSavedReport,
  updateSavedReport,
  deleteSavedReport,
  fetchMergedPage,
  exportReportExcelV2,
} from '../../services/api'

// The only report types this page supports.
const EQ_TYPES  = ['Inverter', 'WMS', 'PPC']
const PAGE_SIZES = [50, 100]
const AGG_LABEL = Object.fromEntries(AGG_OPTIONS.map(o => [o.value, o.label]))

function fmtTimestamp(val) {
  if (!val) return '—'
  const s = String(val).replace('T', ' ').slice(0, 19)
  const [date, time] = s.split(' ')
  if (!date) return s
  const [y, mo, d] = date.split('-')
  return `${d}/${mo}/${y} ${time || '00:00:00'}`
}

const fmtCell = v =>
  v === null || v === undefined ? '—' : (typeof v === 'number' ? v.toFixed(3) : String(v))

const todayDMY = () => {
  const d = new Date()
  return `${String(d.getDate()).padStart(2, '0')}-${String(d.getMonth() + 1).padStart(2, '0')}-${d.getFullYear()}`
}

const toISO = dtLocal => (dtLocal ? new Date(dtLocal).toISOString() : '')

function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url; a.download = filename; a.style.display = 'none'
  document.body.appendChild(a); a.click(); document.body.removeChild(a)
  setTimeout(() => URL.revokeObjectURL(url), 1500)
}

// A selection is identified by type + equipment + column, so the same column name
// on two different tables never collides.
const selKey = s => `${s.type}|${s.eqId}|${s.column}`

// Build the multi-report request body from a plain config object (either the live
// page state or a saved template's config). Kept pure so "Open" can preview a
// template straight from its stored config, before the async tag loads finish.
function payloadFromConfig(c, pageNum, size) {
  const byEquipment = new Map()
  ;(c.selected || []).forEach(s => {
    const k = `${s.type}|${s.eqId}`
    if (!byEquipment.has(k)) {
      byEquipment.set(k, { equipment_type: s.type, equipment_id: s.eqId, tags: [] })
    }
    byEquipment.get(k).tags.push(s.column)
  })
  return {
    sources:       [...byEquipment.values()],
    from_datetime: toISO(c.fromDate),
    to_datetime:   toISO(c.toDate),
    interval:      c.interval,
    agg_function:  c.agg,
    page:          pageNum,
    page_size:     size,
  }
}

// Build the report-data request for a backend saved report (from the Reports page),
// using the SAME merged-page endpoint the Reports page uses — so the inline table
// shows identical columns/data. withAggregation() drops the aggregation on instant
// intervals; equipment_ids drives the merged multi-equipment view.
function backendReportPayload(r, pageNum, size) {
  const ids = (r.equipment_ids || []).filter(id => id && String(id).trim())
  return withAggregation({
    equipment_type: r.equipment_type,
    equipment_id:   ids[0] || '',
    equipment_ids:  ids,
    tags:           r.tags || [],
    from_datetime:  toISO(r.from_date),
    to_datetime:    toISO(r.to_date),
    page:           pageNum,
    page_size:      size,
  }, r.interval, r.agg_function)
}

// ── Main page ────────────────────────────────────────────────────────────────
export default function Preconfigured() {
  const { showToast } = useApp()

  // Selected equipment types, in the order they were added. Adding one never
  // touches the others' state or their already-chosen columns.
  const [types,      setTypes]      = useState([])
  const [activeType, setActiveType] = useState('')

  // Per-type source state: { eqList, eqId, tags, loadingEq, loadingTags }
  const [srcs, setSrcs] = useState({})

  // ONE ordered list across every type — this order is the report's column order.
  const [selected, setSelected] = useState([])

  const [query,    setQuery]    = useState('')
  const [fromDate, setFromDate] = useState('2024-02-08T00:00')
  const [toDate,   setToDate]   = useState('2024-05-21T23:59')
  const [interval, setInterval] = useState('hourly')
  const [agg,      setAgg]      = useState(DEFAULT_AGG)

  const [result,     setResult]     = useState(null)
  const [error,      setError]      = useState(null)
  const [generating, setGenerating] = useState(false)
  const [exporting,  setExporting]  = useState(false)
  const [page,       setPage]       = useState(1)
  const [pageSize,   setPageSize]   = useState(PAGE_SIZES[0])

  // ── Saved Templates — UNIFIED from two stores ─────────────────────────────
  // (1) Reports saved from the Reports page → backend DB (durable, survives refresh
  //     and works across browsers). Loaded via listSavedReports().
  // (2) Multi-equipment builder templates generated on THIS page → localStorage.
  // Both are shown together in the one "Saved Templates" dropdown so a report saved
  // on the Reports page appears here immediately (root cause of the bug: this page
  // previously read ONLY localStorage and never the backend).
  const [templates,  setTemplates]  = useState(() => listTemplates())   // localStorage builder templates
  const [savedReports, setSavedReports] = useState([])                  // backend reports (Reports page)
  const [loadingSaved, setLoadingSaved] = useState(true)
  const [tplId,      setTplId]      = useState('')   // unified dropdown key (see itemKey)
  // Saved-template allowance straight from the database: { count, max, remaining,
  // limit_reached, message }. The maximum is a single backend constant — nothing here
  // hardcodes 20 — and it is re-read on every list refresh so the badge stays true
  // after a create/delete anywhere, including another browser.
  const [cap, setCap] = useState(null)
  const refreshTemplates = useCallback(() => setTemplates(listTemplates()), [])
  const refreshSavedReports = useCallback(() => {
    setLoadingSaved(true)
    fetchSavedReportCapacity().then(setCap).catch(() => {})
    return listSavedReports()
      .then(data => setSavedReports(Array.isArray(data) ? data : []))
      .catch(() => setSavedReports([]))
      .finally(() => setLoadingSaved(false))
  }, [])

  // Load backend saved reports on mount so newly-saved reports appear here.
  useEffect(() => { refreshSavedReports() }, [refreshSavedReports])

  // Unified dropdown model. Backend records are keyed "db:<id>" so they never collide
  // with localStorage ids; each item carries its source + original record so the
  // action handlers route to the correct store.
  const dropdownItems = useMemo(() => ([
    ...savedReports.map(r => ({ key: `db:${r.id}`, name: r.name, source: 'backend', record: r })),
    ...templates.map(t => ({ key: t.id, name: t.name, source: 'local', record: t })),
  ]), [savedReports, templates])

  const selectedItem = useMemo(
    () => dropdownItems.find(i => i.key === tplId) || null, [dropdownItems, tplId])

  // What the bottom "Report Data" table currently shows: a builder ('local') report
  // or an opened backend saved report ('backend'). It changes how column labels are
  // resolved and which endpoint pagination uses — the table markup stays shared, so
  // the report table is never duplicated across pages.
  const [viewSource,     setViewSource]     = useState(null)   // 'local' | 'backend' | null
  const [backendRec,     setBackendRec]     = useState(null)   // the opened backend record
  const [backendTagMeta, setBackendTagMeta] = useState({})     // column_name → {tag, unit}

  // Load one page of an opened backend saved report INLINE (no navigation), reusing
  // the shared `result` state + the page's existing Report Data table.
  const loadBackendPage = useCallback(async (rec, pageNum, size) => {
    if (!rec) return
    setGenerating(true); setError(null)
    try {
      const data = await fetchMergedPage(backendReportPayload(rec, pageNum, size))
      const count = data.equipment_count || (rec.equipment_ids || []).length
      setResult({
        columns:       data.columns || [],
        rows:          data.rows || [],
        total_records: data.total_records || 0,
        interval:      data.interval,
        sources:       Array.from({ length: count }, (_, i) => i),  // for the header count
      })
      setPage(pageNum); setPageSize(size)
    } catch (e) {
      setError(e.message || 'Failed to load report'); setResult(null)
    } finally {
      setGenerating(false)
    }
  }, [])

  // The template currently OPENED on the page — the single target for Edit / Save
  // Changes / the status line. { key, source:'local'|'backend', id, name } | null.
  // Works for BOTH stores (local builder templates AND backend saved reports), so the
  // action buttons enable/disable identically regardless of where the template lives.
  const [openedTpl, setOpenedTpl] = useState(null)
  const [editing,   setEditing]   = useState(false)   // Edit mode is ON (controls unlocked)
  const [dirty,     setDirty]     = useState(false)   // an ACTUAL change was made since Edit

  // Editable column (tag) selection for an opened BACKEND saved report — its type
  // (String Combiner, Tracker, Inverter, …) can't be represented by the multi-type
  // builder, so Edit exposes its own tag checklist here. Loaded when Edit is clicked.
  const [beTags,        setBeTags]        = useState([])   // full tag list of the report's equipment
  const [beSelected,    setBeSelected]    = useState([])   // ordered column_names currently chosen
  const [beLoadingTags, setBeLoadingTags] = useState(false)

  // The builder (local templates) is read-only while an opened LOCAL template is not
  // being edited; a fresh/generated config, or an active Edit, is fully editable.
  const locked = !!openedTpl && openedTpl.source === 'local' && !editing

  // Set true immediately before a template load so the "seed interval/agg from the
  // first type" effect below does NOT overwrite the values the template restored.
  const restoringRef = useRef(false)

  const patch = useCallback((type, changes) => {
    setSrcs(prev => ({ ...prev, [type]: { ...(prev[type] || {}), ...changes } }))
  }, [])

  // ── Load a type's columns for a given equipment ───────────────────────────
  const loadTags = useCallback((type, eqId) => {
    if (!eqId) { patch(type, { tags: [], loadingTags: false }); return }
    patch(type, { loadingTags: true })
    fetchReportTags(type, eqId)
      .then(d => patch(type, { tags: Array.isArray(d) ? d : (d?.tags || []), loadingTags: false }))
      .catch(() => patch(type, { tags: [], loadingTags: false }))
  }, [patch])

  // ── Add / remove an equipment type ────────────────────────────────────────
  const toggleType = useCallback((type) => {
    if (locked) return                       // opened template is read-only until Edit
    setTypes(prev => {
      if (prev.includes(type)) {
        // Removing a type also drops the columns that belonged to it.
        setSelected(sel => sel.filter(s => s.type !== type))
        setSrcs(s => { const next = { ...s }; delete next[type]; return next })
        setActiveType(a => (a === type ? prev.filter(t => t !== type)[0] || '' : a))
        return prev.filter(t => t !== type)
      }

      // Adding a type leaves every existing selection untouched.
      setActiveType(type)
      patch(type, { loadingEq: true, eqList: [], eqId: '', tags: [] })
      fetchReportEquipmentList(type)
        .then(d => {
          const list = Array.isArray(d) ? d : (d?.items || d?.data || [])
          const eqId = list[0]?.equipment_id || ''
          patch(type, { eqList: list, eqId, loadingEq: false })
          loadTags(type, eqId)
        })
        .catch(() => patch(type, { eqList: [], eqId: '', loadingEq: false }))
      return [...prev, type]
    })
    setResult(null)
  }, [locked, patch, loadTags])

  // Switching a type's equipment invalidates that type's picks (they belong to
  // the previous device); every other type's selection is preserved.
  const changeEquipment = useCallback((type, eqId) => {
    if (locked) return
    patch(type, { eqId })
    setSelected(sel => sel.filter(s => s.type !== type))
    setResult(null)
    setDirty(true)                    // an edit → Save Changes becomes enabled
    loadTags(type, eqId)
  }, [locked, patch, loadTags])

  // Seed interval/aggregation from the first type added (same presets the Reports
  // and Scheduled screens use). Both remain editable.
  //
  // Skipped exactly once right after a template load: the template already carries
  // its own interval/agg and must not be clobbered by the preset for its first type.
  useEffect(() => {
    if (restoringRef.current) { restoringRef.current = false; return }
    if (types.length !== 1) return
    const preset = getPreset(types[0])
    setInterval(preset.interval)
    setAgg(preset.agg)
  }, [types])

  // ── Column selection ──────────────────────────────────────────────────────
  const toggleColumn = useCallback((type, eqId, tag) => {
    if (locked) return
    const entry = { type, eqId, column: tag.column_name, label: tag.tag, unit: tag.unit }
    setSelected(prev => prev.some(s => selKey(s) === selKey(entry))
      ? prev.filter(s => selKey(s) !== selKey(entry))
      : [...prev, entry])          // appended → selection order is the column order
    setResult(null)
    setDirty(true)                 // an edit → Save Changes becomes enabled
  }, [locked])

  const selectedKeys = useMemo(() => new Set(selected.map(selKey)), [selected])

  // Selected columns grouped by equipment type for display, each group keeping
  // its slice of the global pick order.
  const grouped = useMemo(() => {
    const g = new Map()
    selected.forEach(s => {
      if (!g.has(s.type)) g.set(s.type, [])
      g.get(s.type).push(s)
    })
    return [...g.entries()]
  }, [selected])

  const active     = srcs[activeType] || {}
  const activeTags = active.tags || []

  const shownTags = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return activeTags
    return activeTags.filter(t =>
      t.tag.toLowerCase().includes(q) || t.column_name.toLowerCase().includes(q))
  }, [activeTags, query])

  // ── Request payload ───────────────────────────────────────────────────────
  // Selections collapse into one source per equipment, tags in pick order.
  const buildPayload = useCallback((pageNum, size) =>
    payloadFromConfig(
      { selected, fromDate, toDate, interval, agg }, pageNum, size),
  [selected, fromDate, toDate, interval, agg])

  // An opened BACKEND template is its own report: its columns live in the saved record
  // (or in `beSelected` while editing), NOT in the multi-equipment builder above — which
  // is deliberately cleared when such a template is opened. Generate/Export therefore
  // have to read their configuration from the template, otherwise they are permanently
  // disabled (builder empty) and, if enabled, would run an unrelated configuration.
  const backendMode  = viewSource === 'backend' && !!backendRec
  const backendCols  = backendMode ? (editing ? beSelected : (backendRec.tags || [])) : []
  const activeCols   = backendMode ? backendCols : selected
  const ready        = activeCols.length > 0

  // Generating/exporting always uses the configuration as SAVED. Re-read it by id so the
  // request can never be built from frontend state that drifted from the database.
  const freshTemplate = useCallback(async (rec) => {
    try {
      const latest = await getSavedReport(rec.id)
      if (latest && latest.id) {
        setBackendRec(latest)
        if (!editing) setBeSelected(latest.tags || [])
        return latest
      }
    } catch { /* fall back to the record in hand rather than blocking the action */ }
    return rec
  }, [editing])

  const runReport = useCallback(async (pageNum, size) => {
    setGenerating(true); setError(null)
    try {
      const data = await fetchMultiReportData(buildPayload(pageNum, size))
      setResult(data); setPage(pageNum); setPageSize(size)
      return true
    } catch (e) {
      setError(e.message || 'Failed to generate report'); setResult(null)
      return false
    } finally {
      setGenerating(false)
    }
  }, [buildPayload])

  const handleExportExcel = async () => {
    if (!ready) return showToast('Select at least one column')
    if (backendMode) {
      // Export the OPENED template through the same endpoint the Reports page uses for
      // this configuration, so the workbook's structure/formatting is unchanged and its
      // columns match the Report Data table exactly.
      if (editing && dirty) return showToast('Save Changes first — then export the saved template')
      setExporting(true)
      try {
        const rec = await freshTemplate(backendRec)
        // `use_selected_tags` makes the workbook carry EXACTLY the template's saved
        // columns, in their saved order. Without it the export falls back to each
        // equipment's complete tag set (64 columns for an inverter), which is right for
        // the Reports page but wrong here, where the chosen columns ARE the report.
        const blob = await exportReportExcelV2({
          ...backendReportPayload(rec, 1, pageSize), use_selected_tags: true,
        })
        if (!(blob instanceof Blob) || blob.size === 0) throw new Error('Empty file received from server')
        const safe = String(rec.name || 'Report').replace(/[\/:*?"<>|]+/g, '_').trim() || 'Report'
        saveBlob(blob, `${safe}_${todayDMY()}.xlsx`)
        showToast(`Excel exported · ${(rec.tags || []).length} columns`)
      } catch (e) {
        showToast(`Export failed: ${e.message}`)
      } finally {
        setExporting(false)
      }
      return
    }
    setExporting(true)
    try {
      // page_size is ignored by the export — the workbook holds the whole range.
      const blob = await exportMultiReportExcel(buildPayload(1, 100))
      if (!(blob instanceof Blob) || blob.size === 0) throw new Error('Empty file received from server')
      saveBlob(blob, `Multi_Equipment_Report_${todayDMY()}.xlsx`)
      showToast('Excel exported')
    } catch (e) {
      showToast(`Export failed: ${e.message}`)
    } finally {
      setExporting(false)
    }
  }

  // ── Template snapshot / restore ───────────────────────────────────────────
  // Capture the WHOLE current configuration. `selected` is self-contained (each
  // entry carries type/eqId/column/label/unit), so restoring columns needs no
  // network round-trip; only the per-type equipment lists are re-fetched on load.
  const snapshotConfig = useCallback(() => ({
    version:      TEMPLATE_VERSION,
    types,
    eqIds:        Object.fromEntries(types.map(t => [t, (srcs[t] || {}).eqId || ''])),
    selected,
    fromDate, toDate, interval, agg,
    exportFormat: 'Excel',        // this page exports Excel only, by design
  }), [types, srcs, selected, fromDate, toDate, interval, agg])

  // Populate every existing control from a saved template. Existing components and
  // their behaviour are untouched — this only sets the same state the user would.
  const applyTemplate = useCallback((tpl) => {
    const c = tpl?.config || {}
    restoringRef.current = true
    setError(null); setResult(null)

    const tps = Array.isArray(c.types) ? c.types : []
    setTypes(tps)
    setActiveType(tps[0] || '')
    setSelected(Array.isArray(c.selected) ? c.selected : [])
    if (c.fromDate) setFromDate(c.fromDate)
    if (c.toDate)   setToDate(c.toDate)
    if (c.interval) setInterval(c.interval)
    if (c.agg)      setAgg(c.agg)

    // Rebuild each type's source: fetch its equipment list + the saved device's
    // columns so the Available panel repopulates exactly as if picked by hand.
    setSrcs({})
    tps.forEach(type => {
      const savedEqId = (c.eqIds || {})[type] || ''
      patch(type, { loadingEq: true, eqList: [], eqId: savedEqId, tags: [] })
      fetchReportEquipmentList(type)
        .then(d => {
          const list = Array.isArray(d) ? d : (d?.items || d?.data || [])
          const eqId = list.some(e => e.equipment_id === savedEqId)
            ? savedEqId : (list[0]?.equipment_id || '')
          patch(type, { eqList: list, eqId, loadingEq: false })
          loadTags(type, eqId)
        })
        .catch(() => patch(type, { eqList: [], eqId: savedEqId, loadingEq: false }))
    })
  }, [patch, loadTags])

  // ── Generate Report — also records the configuration as a template ────────
  // The template history is a by-product of generating, so the user never has to
  // create one by hand. Recording happens only after a SUCCESSFUL generate, and
  // only from this explicit action — pagination / rows-per-page call runReport
  // directly and never add to the history.
  const autoTemplateName = useCallback(() => {
    const d = new Date()
    const p = n => String(n).padStart(2, '0')
    const stamp = `${p(d.getDate())}/${p(d.getMonth() + 1)}/${d.getFullYear()} ` +
                  `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
    const label = types.length ? types.join(' + ') : 'Report'
    return `${label} · ${stamp}`
  }, [types])

  const handleGenerate = useCallback(async () => {
    if (!ready) return showToast('Select at least one column')
    if (backendMode) {
      // Re-run the OPENED template from its latest saved configuration. This is not a new
      // configuration, so the opened/edit state is kept and NO auto-template is recorded —
      // generating must never spawn a duplicate of the template being worked on.
      if (editing && dirty) return showToast('Save Changes first — then generate the saved template')
      const rec = await freshTemplate(backendRec)
      await loadBackendPage(rec, 1, pageSize)
      showToast(`Generated "${rec.name}" · ${(rec.tags || []).length} columns`)
      return
    }
    setViewSource('local'); setBackendRec(null)   // the table now shows a builder report
    const ok = await runReport(1, pageSize)
    if (!ok) return
    // A fresh generate is a NEW configuration — it is never the opened template, so
    // clear any opened/edit state so the page is fully editable again.
    setOpenedTpl(null); setEditing(false); setDirty(false)
    const res = createAutoTemplate(autoTemplateName(), snapshotConfig())
    if (res.ok) {
      refreshTemplates()
      setTplId(res.template.id)
    }
    // A failure here (storage full/disabled) must never block the generated report,
    // which is already on screen — so it is intentionally silent.
  }, [ready, backendMode, editing, dirty, backendRec, freshTemplate, loadBackendPage,
      pageSize, runReport, autoTemplateName, snapshotConfig, refreshTemplates, showToast])

  // ── Template actions (Open · Edit · Save Changes · Rename · Delete) ────────

  // Open: load the selected saved template's whole configuration and show its report
  // table INLINE on this page (never navigates away, never duplicates the table).
  //  · Backend saved report (from the Reports page) → load its data through the same
  //    merged-page endpoint the Reports page uses and render it here.
  //  · localStorage builder template → load into THIS page's builder + preview.
  const handleTplOpen = useCallback(async () => {
    if (!selectedItem) return showToast('Select a template to open')

    // Entering Open always starts a fresh, non-editing view of the chosen template.
    setEditing(false); setDirty(false)
    setBeTags([]); setBeSelected([])

    if (selectedItem.source === 'backend') {
      const rec = selectedItem.record
      setViewSource('backend'); setBackendRec(rec)
      // Mark this backend report as the ACTIVE opened template so Edit/Save/Rename/Delete
      // all target it (its columns are edited via the tag checklist under Report Columns).
      setOpenedTpl({ key: selectedItem.key, source: 'backend', id: rec.id, name: rec.name })
      setBackendTagMeta({})
      // Clear the multi-equipment builder above so it doesn't show stale, unrelated
      // selections — this backend report's configuration is summarised in the Report
      // Data card below, next to its table.
      setTypes([]); setActiveType(''); setSelected([]); setSrcs({})
      // Friendly column labels/units (best-effort), same as the Reports page headers.
      const primary = (rec.equipment_ids || [])[0]
      if (primary) {
        fetchReportTags(rec.equipment_type, primary)
          .then(d => {
            const list = Array.isArray(d) ? d : (d?.tags || [])
            const m = {}; list.forEach(t => { m[t.column_name] = t })
            setBackendTagMeta(m)
          })
          .catch(() => {})
      }
      await loadBackendPage(rec, 1, pageSize)
      showToast(`Opened "${rec.name}"`)
      return
    }

    const tpl = selectedItem.record
    setViewSource('local'); setBackendRec(null)
    applyTemplate(tpl)                 // populate every control from the config
    setOpenedTpl({ key: selectedItem.key, source: 'local', id: tpl.id, name: tpl.name })

    // Preview straight from the stored config so the user immediately sees the same
    // report that was generated before (no wait for the async tag loads).
    setGenerating(true); setError(null)
    try {
      const data = await fetchMultiReportData(payloadFromConfig(tpl.config, 1, pageSize))
      setResult(data); setPage(1)
    } catch (e) {
      setError(e.message || 'Failed to load report preview'); setResult(null)
    } finally {
      setGenerating(false)
    }
    showToast(`Opened "${tpl.name}"`)
  }, [selectedItem, applyTemplate, loadBackendPage, pageSize, showToast])

  // Edit: enter edit mode for the OPENED template (local OR backend). For a backend
  // saved report, load its equipment's full tag list so its columns can be modified via
  // the checklist under Report Columns, pre-checking the report's current tags.
  const handleTplEdit = useCallback(async () => {
    if (!openedTpl) return showToast('Open a template first, then Edit')
    setEditing(true); setDirty(false)

    if (openedTpl.source === 'backend' && backendRec) {
      setBeLoadingTags(true)
      const primary = (backendRec.equipment_ids || [])[0]
      try {
        const d = await fetchReportTags(backendRec.equipment_type, primary)
        const list = Array.isArray(d) ? d : (d?.tags || [])
        setBeTags(list)
        // Pre-select the report's saved tags that still exist on the equipment.
        const have = new Set(list.map(t => t.column_name))
        setBeSelected((backendRec.tags || []).filter(c => have.has(c)))
      } catch {
        setBeTags([]); setBeSelected([...(backendRec.tags || [])])
      } finally {
        setBeLoadingTags(false)
      }
    }
    showToast(`Editing "${openedTpl.name}" — change columns, then Save Changes`)
  }, [openedTpl, backendRec, showToast])

  // Save Changes: persist the modifications back to the SAME opened template (same id,
  // never a new one). Local → localStorage config; backend → PUT /saved-reports/{id}
  // with the edited tag list. Reopening/refresh then shows the updated values.
  const handleTplUpdate = useCallback(async () => {
    if (!openedTpl) return showToast('Open a saved template first')

    if (openedTpl.source === 'backend') {
      const r = backendRec
      if (!r) return showToast('Open the report first')
      if (!beSelected.length) return showToast('Select at least one column')
      try {
        const saved = await updateSavedReport(r.id, {
          name:           r.name,                 // rename is a separate action
          equipment_type: r.equipment_type,
          equipment_ids:  r.equipment_ids || [],
          tags:           beSelected,             // the EDITED columns
          from_date:      r.from_date,
          to_date:        r.to_date,
          interval:       r.interval,
          agg_function:   r.agg_function,
          page_size:      r.page_size,
        })
        await refreshSavedReports()
        // Adopt what the backend actually stored (falling back to a fresh read), so the
        // page can never hold a version of the template the database does not have.
        let updated = saved && saved.id ? saved : null
        if (!updated) { try { updated = await getSavedReport(r.id) } catch { /* handled below */ } }
        if (!updated) updated = { ...r, tags: beSelected, tag_count: beSelected.length }
        setBackendRec(updated)
        setBeSelected(updated.tags || [])
        setEditing(false); setDirty(false)
        await loadBackendPage(updated, 1, pageSize)   // reload the table with new columns
        showToast(`Saved changes to "${r.name}"`)
      } catch (e) {
        showToast(`Save failed: ${e.message}`)
      }
      return
    }

    const res = updateTemplate(openedTpl.id, snapshotConfig())
    if (!res.ok) return showToast(res.error)
    refreshTemplates()
    setEditing(false); setDirty(false)             // saved → back to read-only view
    showToast(`Saved changes to "${res.template.name}"`)
  }, [openedTpl, backendRec, beSelected, snapshotConfig, refreshSavedReports,
      refreshTemplates, loadBackendPage, pageSize, showToast])

  // ── Rename — in-app dialog ────────────────────────────────────────────────
  // Like the delete confirmation, `renaming` holds the ITEM being renamed (null =
  // closed), so the dialog keeps naming the template the user clicked even if the
  // dropdown moves underneath it. `renameBusy` locks the dialog while the request is
  // in flight — that is what stops a second rename being sent.
  const [renaming,    setRenaming]    = useState(null)
  const [renameBusy,  setRenameBusy]  = useState(false)
  const [renameError, setRenameError] = useState('')

  const askTplRename = useCallback(() => {
    if (!selectedItem) return showToast('Select a template to rename')
    setRenameError('')
    setRenaming(selectedItem)
  }, [selectedItem, showToast])

  const cancelTplRename = useCallback(() => {
    if (renameBusy) return             // never dismiss a rename already running
    setRenaming(null); setRenameError('')
  }, [renameBusy])

  /**
   * Validation for the rename dialog, run on the TRIMMED value.
   * Returns an error string to keep Rename Template disabled, or null to allow it.
   *
   * The duplicate check runs against the loaded template list (both stores), which is
   * the rule the localStorage store already enforces; the database has no unique
   * constraint on the name and none was added, so for a saved report this is a UI
   * guard that keeps the dropdown unambiguous.
   */
  const validateRename = useCallback((name) => {
    if (!name) return 'Template name is required'
    if (!renaming) return null
    if (name === (renaming.name || '').trim()) return 'Enter a name different from the current one'
    if (name.length > 200) return 'Template name must be 200 characters or fewer'
    const clash = dropdownItems.some(
      it => it.key !== renaming.key && (it.name || '').trim().toLowerCase() === name.toLowerCase())
    if (clash) return `A template named "${name}" already exists`
    return null
  }, [renaming, dropdownItems])

  const handleTplRename = useCallback(async (clean) => {
    const selectedItem = renaming
    if (!selectedItem || renameBusy) return
    setRenameBusy(true); setRenameError('')

    if (selectedItem.source === 'backend') {
      // PUT requires the full config (backend validates it) — resend the record with
      // only the name changed, so nothing else is modified.
      const r = selectedItem.record
      try {
        await updateSavedReport(r.id, {
          name: clean,
          equipment_type: r.equipment_type,
          equipment_ids:  r.equipment_ids || [],
          tags:           r.tags || [],
          from_date:      r.from_date,
          to_date:        r.to_date,
          interval:       r.interval,
          agg_function:   r.agg_function,
          page_size:      r.page_size,
        })
        await refreshSavedReports()
        // The record keeps its id, so `tplId` still points at it — the renamed
        // template stays selected, and stays opened if it was open, with the new
        // name showing in the dropdown, the status line and the table header.
        if (openedTpl?.key === selectedItem.key) {
          setOpenedTpl(t => (t ? { ...t, name: clean } : t))
          setBackendRec(rec => (rec ? { ...rec, name: clean } : rec))
        }
        setRenaming(null)
        showToast(`Renamed to "${clean}"`)
      } catch (e) {
        // Nothing changed server-side — leave the list and the selection alone and
        // keep the dialog open with the reason shown inline so the user can retry.
        setRenameError(e.message || 'Rename failed')
        showToast(`Rename failed: ${e.message}`, 'error')
      } finally {
        setRenameBusy(false)
      }
      return
    }

    const res = renameTemplate(selectedItem.record.id, clean)
    if (!res.ok) {
      setRenameBusy(false); setRenameError(res.error)
      return showToast(res.error, 'error')
    }
    refreshTemplates()
    if (openedTpl?.key === selectedItem.key) setOpenedTpl(t => (t ? { ...t, name: clean } : t))
    setRenaming(null); setRenameBusy(false)
    showToast(`Renamed to "${res.template.name}"`)
  }, [renaming, renameBusy, openedTpl, refreshSavedReports, refreshTemplates, showToast])

  // ── Delete — in-app confirmation ──────────────────────────────────────────
  // `confirmDelete` holds the item the dialog is asking about (null = closed) rather
  // than a bare boolean, so the dialog always names the template the user clicked even
  // if the dropdown selection changes underneath it. `deleting` locks the dialog while
  // the request is in flight, which is what prevents a double delete.
  const [confirmDelete, setConfirmDelete] = useState(null)
  const [deleting,      setDeleting]      = useState(false)

  const askTplDelete = useCallback(() => {
    if (!selectedItem) return showToast('Select a template to delete')
    setConfirmDelete(selectedItem)
  }, [selectedItem, showToast])

  const cancelTplDelete = useCallback(() => {
    if (deleting) return          // never dismiss a delete that is already running
    setConfirmDelete(null)
  }, [deleting])

  const handleTplDelete = useCallback(async () => {
    const target = confirmDelete
    if (!target || deleting) return
    setDeleting(true)

    // Deleting only removes the saved CONFIGURATION (DB row / localStorage entry); it
    // never touches generated Excel files under Downloads\Reports.
    const wasOpened = openedTpl?.key === target.key
    const clearOpened = () => {
      if (wasOpened) {
        setOpenedTpl(null); setEditing(false); setDirty(false)
        setViewSource(null); setBackendRec(null); setResult(null)
        setBeTags([]); setBeSelected([])
      }
    }

    if (target.source === 'backend') {
      try {
        await deleteSavedReport(target.record.id)
        // refreshSavedReports re-reads BOTH the list and the saved-template count, so
        // the "Saved Templates: N / 20" heading updates in the same pass.
        await refreshSavedReports()
        clearOpened()
        setTplId('')
        setConfirmDelete(null)
        showToast('Saved report deleted')
      } catch (e) {
        // The row is still there — leave the list, the selection and the count alone.
        // The dialog stays open so the user can retry or cancel; the failure is
        // reported through the app's toast, never a browser alert().
        showToast(`Delete failed: ${e.message}`, 'error')
      } finally {
        setDeleting(false)
      }
      return
    }

    const res = deleteTemplate(target.record.id)
    if (!res.ok) { setDeleting(false); return showToast(res.error, 'error') }
    refreshTemplates()
    clearOpened()
    setTplId('')
    setConfirmDelete(null)
    setDeleting(false)
    showToast('Template deleted')
  }, [confirmDelete, deleting, openedTpl, refreshSavedReports, refreshTemplates, showToast])

  // ── Preview table ─────────────────────────────────────────────────────────
  // Shared by BOTH view sources so the report table is never duplicated:
  //  · builder ('local')  → labels come from the multi-report result.labels.
  //  · backend saved report → merged columns include "_equipment"; tag labels come
  //    from the fetched tag metadata (same friendly "Tag (unit)" as the Reports page).
  const tableCols = useMemo(() => {
    const cols   = result?.columns || []
    const labels = result?.labels  || {}
    const ordered = [
      ...cols.filter(c => c === '_equipment'),
      ...cols.filter(c => c === 'timestamp'),
      ...cols.filter(c => c !== 'timestamp' && c !== '_equipment'),
    ]
    return ordered.map(c => {
      if (c === '_equipment') return { key: c, isTs: false, isEq: true, label: 'Equipment' }
      if (c === 'timestamp')  return { key: c, isTs: true, label: 'Timestamp (DD/MM/YYYY HH:MM:SS)' }
      if (viewSource === 'backend') {
        const m = backendTagMeta[c]
        const label = m
          ? (m.unit ? `${m.tag} (${m.unit})` : m.tag)
          : c.replace(/_/g, ' ').replace(/\b\w/g, ch => ch.toUpperCase())
        return { key: c, isTs: false, label }
      }
      return { key: c, isTs: false, label: labels[c] || c }
    })
  }, [result?.columns, result?.labels, viewSource, backendTagMeta])

  const rows       = result?.rows || []
  const totalRows  = result?.total_records || 0
  const totalPages = Math.max(1, Math.ceil(totalRows / pageSize))

  return (
    <div>
      <PageHeader title="Preconfigured Reports" />

      {/* Saved Templates — a history of generated report configurations. Every Generate
          Report below adds the configuration here automatically; the user never
          creates one by hand. Pure front-end (localStorage); does not affect report
          generation or Excel export. */}
      <div className="card mb-3">
        {/* ONE heading for this section, carrying the live count: "Saved Templates: 6 / 20".
            The count comes from the database (see `cap`); the maximum is the backend
            constant, never hardcoded here. At the cap the heading turns red and the full
            message is shown below it. */}
        <div className={`card-title ${cap?.limit_reached ? 'text-red-400' : ''}`}>
          Saved Templates{cap ? `: ${cap.count} / ${cap.max}` : ''}
        </div>
        {cap?.limit_reached && (
          <div className="text-[11px] text-red-400 mb-2 leading-tight">{cap.message}</div>
        )}
        <div className="flex flex-wrap items-end gap-x-4 gap-y-3">
          {/* No label here — the section heading above already names it. */}
          <FormGroup>
            <select className="form-control" style={{ minWidth: 260 }}
              value={tplId}
              onChange={e => setTplId(e.target.value)}>
              <option value="">— Select a saved template —</option>
              {dropdownItems.map(it => (
                <option key={it.key} value={it.key}>{it.name}</option>
              ))}
            </select>
          </FormGroup>

          <div className="flex flex-wrap items-center gap-3 pb-0.5">
            <button type="button" className="btn btn-success btn-sm"
              onClick={handleTplOpen} disabled={!tplId}
              title="Load this configuration and preview into the page below">
              📂 Open
            </button>
            <button type="button" className="btn btn-primary btn-sm"
              onClick={handleTplEdit} disabled={!openedTpl || editing}
              title="Enable editing of the opened template">
              ✏ Edit
            </button>
            <button type="button" className="btn btn-outline btn-sm"
              onClick={handleTplUpdate} disabled={!editing || !dirty}
              title="Save your modifications back to the opened template">
              ✔ Save Changes
            </button>
            <button type="button" className="btn btn-outline btn-sm"
              onClick={askTplRename} disabled={!tplId || renameBusy}
              title="Rename the selected template">
              🏷 Rename
            </button>
            <button type="button" className="btn btn-outline btn-sm"
              onClick={askTplDelete} disabled={!tplId || deleting}
              title="Delete the selected template">
              🗑 Delete
            </button>
          </div>

          <span className="ml-auto self-end pb-2 text-[11px] font-mono text-ge-text3">
            {openedTpl
              ? (editing
                  ? (dirty ? '✏ Editing — Save Changes to persist' : '✏ Editing — change columns, then Save Changes')
                  : '🔒 Opened (read-only) — click Edit to modify')
              : loadingSaved
                ? 'Loading saved templates…'
                : `${dropdownItems.length} saved template${dropdownItems.length === 1 ? '' : 's'}`}
          </span>
        </div>
      </div>

      {/* Equipment Types section removed from the UI per client request. The equipment
          selection state (types / selected columns / equipment identifiers) is preserved
          and is populated by opening a Saved Template above — report generation and Excel
          export logic are unchanged. */}

      {/* Report Configuration section (From/To Date, Time Interval, Aggregation) removed
          from the UI per client request. The underlying date/interval/aggregation state is
          KEPT — it still drives report generation, Excel export and template save/restore,
          and is populated by opening a Saved Template above (or its defaults). The page now
          goes straight from Saved Templates to Report Columns. */}

      {/* Columns */}
      <div className="card mb-3">
        <div className="card-title">Report Columns</div>

        {openedTpl?.source === 'backend' ? (
          !editing ? (
            <div className="py-6 text-center text-[12px] text-ge-text3">
              Click <span className="text-ge-text1 font-semibold">Edit</span> to modify the columns of “{openedTpl.name}”.
            </div>
          ) : (
            /* Editable column (tag) checklist for the opened BACKEND saved report. */
            <div>
              <div className="flex flex-wrap items-center gap-2 mb-2">
                <span className="text-[11px] text-ge-text2">
                  Editing columns · <span className="font-semibold">{backendRec?.equipment_type}</span>
                  {' · '}{(backendRec?.equipment_ids || []).length} equipment
                </span>
                <div className="ml-auto flex items-center gap-1.5">
                  <span className="text-[10px] font-mono text-ge-text3">{beSelected.length} / {beTags.length}</span>
                  <button type="button" className="btn btn-outline btn-sm" disabled={!beTags.length}
                    onClick={() => { setBeSelected(beTags.map(t => t.column_name)); setDirty(true) }}>
                    ✓ Select All
                  </button>
                  <button type="button" className="btn btn-outline btn-sm" disabled={!beSelected.length}
                    onClick={() => { setBeSelected([]); setDirty(true) }}>
                    ✕ Clear
                  </button>
                </div>
              </div>
              <div className="relative mb-2">
                <span className="absolute left-2.5 top-1/2 -translate-y-1/2 text-ge-text3 text-[11px]">🔍</span>
                <input type="text" value={query} onChange={e => setQuery(e.target.value)}
                  placeholder="Search columns..." className="form-control pl-7 text-[12px]" />
              </div>
              <div className="bg-ge-elevated border border-ge-border rounded-md overflow-y-auto max-h-72">
                {beLoadingTags ? (
                  <div className="flex items-center gap-2 px-3 py-6 text-[12px] text-ge-text3">
                    <Spinner size={13} /> Loading columns from database...
                  </div>
                ) : (() => {
                  const q = query.trim().toLowerCase()
                  const shown = q
                    ? beTags.filter(t => t.tag.toLowerCase().includes(q) || t.column_name.toLowerCase().includes(q))
                    : beTags
                  if (shown.length === 0) return (
                    <div className="px-3 py-6 text-center text-[12px] text-ge-text3 italic">
                      {beTags.length ? `No columns matching "${query}"` : 'No columns found'}
                    </div>
                  )
                  return shown.map(t => {
                    const isSel = beSelected.includes(t.column_name)
                    return (
                      <div key={t.column_name}
                        onClick={() => { setBeSelected(prev => isSel ? prev.filter(c => c !== t.column_name) : [...prev, t.column_name]); setDirty(true) }}
                        className={`flex items-center gap-2 px-2.5 py-1.5 cursor-pointer select-none
                                    border-b border-ge-border last:border-b-0 transition-colors
                                    hover:bg-ge-surface ${isSel ? 'bg-ge-blue/10' : ''}`}>
                        <div className={`w-3.5 h-3.5 rounded border flex items-center justify-center text-[9px] flex-shrink-0
                                        ${isSel ? 'bg-ge-blue border-ge-blue text-white' : 'border-ge-border2'}`}>
                          {isSel && '✓'}
                        </div>
                        <span className="text-[12px] text-ge-text1 flex-1 leading-tight truncate">{t.tag}</span>
                        {t.unit && <span className="text-[10px] font-mono text-ge-text3">{t.unit}</span>}
                      </div>
                    )
                  })
                })()}
              </div>
            </div>
          )
        ) : types.length === 0 ? (
          <div className="py-6 text-center text-[12px] text-ge-text3">
            Select one or more equipment types above to load their columns
          </div>
        ) : (
          <div className="grid gap-3 md:grid-cols-2">
            {/* Available — browsing one type at a time; selections persist across tabs */}
            <div>
              <div className="flex flex-wrap items-center gap-1.5 mb-2">
                {types.map(t => (
                  <button key={t} onClick={() => { setActiveType(t); setQuery('') }}
                    className={`px-2.5 py-1 text-[11px] rounded border transition-colors
                      ${activeType === t
                        ? 'bg-ge-blue text-white border-ge-blue'
                        : 'bg-ge-elevated border-ge-border text-ge-text2 hover:text-ge-text1'}`}>
                    {t}
                  </button>
                ))}
                {/* Bulk actions sit with the tabs and act ONLY on the active type —
                    every other type's selected columns are left untouched. */}
                <div className="ml-auto flex items-center gap-1.5">
                  <span className="text-[10px] font-mono text-ge-text3">
                    {shownTags.length} / {activeTags.length}
                  </span>
                  <button type="button" className="btn btn-outline btn-sm"
                    disabled={locked || !activeTags.length}
                    title={`Select every ${activeType} column`}
                    onClick={() => { setSelected(prev => {
                      const have = new Set(prev.map(selKey))
                      const add  = activeTags
                        .map(t => ({ type: activeType, eqId: active.eqId, column: t.column_name,
                                     label: t.tag, unit: t.unit }))
                        .filter(e => !have.has(selKey(e)))
                      return [...prev, ...add]
                    }); setDirty(true) }}>
                    ✓ Select All
                  </button>
                  <button type="button" className="btn btn-outline btn-sm"
                    disabled={locked || !selected.some(s => s.type === activeType)}
                    title={`Clear the selected ${activeType} columns`}
                    onClick={() => { setSelected(prev => prev.filter(s => s.type !== activeType)); setDirty(true) }}>
                    ✕ Clear
                  </button>
                </div>
              </div>

              {/* Equipment identifier for the ACTIVE type. Locked while merely viewing an
                  opened template (no accidental changes); editable after Edit — switching
                  the device reloads that type's columns via the existing changeEquipment
                  handler, and the choice is persisted by Save Changes. */}
              <div className="mb-2">
                <label className="block text-[10px] font-semibold uppercase tracking-widest text-ge-text3 mb-1">
                  {activeType} Equipment
                </label>
                <select className="form-control text-[12px]"
                  value={active.eqId || ''}
                  disabled={locked || active.loadingEq}
                  onChange={e => changeEquipment(activeType, e.target.value)}>
                  {(active.eqList || []).length === 0 && (
                    <option value="">{active.loadingEq ? 'Loading…' : 'No equipment'}</option>
                  )}
                  {(active.eqList || []).map(eq => (
                    <option key={eq.equipment_id} value={eq.equipment_id}>
                      {eq.display_name || eq.equipment_id}
                    </option>
                  ))}
                </select>
              </div>

              <div className="relative mb-2">
                <span className="absolute left-2.5 top-1/2 -translate-y-1/2 text-ge-text3 text-[11px]">🔍</span>
                <input type="text" value={query} onChange={e => setQuery(e.target.value)}
                  placeholder={`Search ${activeType} columns...`}
                  className="form-control pl-7 text-[12px]" />
              </div>

              <div className={`bg-ge-elevated border border-ge-border rounded-md overflow-y-auto max-h-72
                              ${locked ? 'opacity-60 pointer-events-none' : ''}`}>
                {active.loadingTags ? (
                  <div className="flex items-center gap-2 px-3 py-6 text-[12px] text-ge-text3">
                    <Spinner size={13} /> Loading columns from database...
                  </div>
                ) : shownTags.length === 0 ? (
                  <div className="px-3 py-6 text-center text-[12px] text-ge-text3 italic">
                    {activeTags.length ? `No columns matching "${query}"` : 'No columns found'}
                  </div>
                ) : shownTags.map(t => {
                  const isSel = selectedKeys.has(`${activeType}|${active.eqId}|${t.column_name}`)
                  return (
                    <div key={t.column_name}
                      onClick={() => toggleColumn(activeType, active.eqId, t)}
                      className={`flex items-center gap-2 px-2.5 py-1.5 cursor-pointer select-none
                                  border-b border-ge-border last:border-b-0 transition-colors
                                  hover:bg-ge-surface ${isSel ? 'bg-ge-blue/10' : ''}`}>
                      <div className={`w-3.5 h-3.5 rounded border flex items-center justify-center
                                      text-[9px] flex-shrink-0
                                      ${isSel ? 'bg-ge-blue border-ge-blue text-white' : 'border-ge-border2'}`}>
                        {isSel && '✓'}
                      </div>
                      <span className="text-[12px] text-ge-text1 flex-1 leading-tight truncate">{t.tag}</span>
                      {t.unit && <span className="text-[10px] font-mono text-ge-text3">{t.unit}</span>}
                    </div>
                  )
                })}
              </div>

            </div>

            {/* Selected — grouped by equipment type, in pick order */}
            <div>
              <div className="flex items-center justify-between mb-2">
                <span className="text-[10px] font-semibold uppercase tracking-widest text-ge-text3">
                  Selected Report Columns
                </span>
                <span className="text-[10px] font-mono text-ge-accent">{selected.length} selected</span>
              </div>

              <div className="bg-ge-elevated border border-ge-border rounded-md overflow-y-auto max-h-[352px]">
                {selected.length === 0 ? (
                  <div className="px-3 py-8 text-center text-[12px] text-ge-text3">
                    No columns selected — pick them on the left
                  </div>
                ) : grouped.map(([type, items]) => (
                  <div key={type}>
                    <div className="sticky top-0 z-10 flex items-center gap-2 px-2.5 py-1
                                    bg-ge-surface border-b border-ge-border">
                      <span className="text-[10px] font-semibold uppercase tracking-widest text-ge-text2">
                        {type}
                      </span>
                      <span className="text-[10px] font-mono text-ge-text3">
                        {items[0]?.eqId}
                      </span>
                      <span className="ml-auto text-[10px] font-mono text-ge-accent">
                        {items.length}
                      </span>
                    </div>
                    {items.map(s => (
                      <div key={selKey(s)}
                        className="flex items-center gap-2 px-2.5 py-1.5
                                   border-b border-ge-border last:border-b-0 bg-ge-blue/5">
                        <span className="text-[12px] text-ge-text1 flex-1 leading-tight truncate"
                          title={s.column}>
                          {s.label || s.column}
                        </span>
                        {s.unit && <span className="text-[10px] font-mono text-ge-text3">{s.unit}</span>}
                        <button type="button" title="Remove" disabled={locked}
                          onClick={() => { setSelected(prev => prev.filter(x => selKey(x) !== selKey(s))); setDirty(true) }}
                          className="px-1 text-[11px] text-ge-text3 hover:text-ge-danger disabled:opacity-40">✕</button>
                      </div>
                    ))}
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Actions */}
      <div className="card mb-3">
        <div className="card-title">Actions</div>
        <div className="flex flex-wrap items-center gap-2">
          <button className="btn btn-primary btn-sm" onClick={handleGenerate}
            disabled={generating || !ready}>
            {generating
              ? <><Spinner size={12} /> Generating...</>
              : <>📥 Generate Report · {activeCols.length} columns</>}
          </button>
          <button className="btn btn-success btn-sm" onClick={handleExportExcel}
            disabled={exporting || !ready}>
            {exporting
              ? <><Spinner size={12} /> Exporting...</>
              : <>📗 Export Excel</>}
          </button>
        </div>
      </div>

      {error && (
        <div className="card mb-3 bg-ge-danger/5 border-ge-danger/40">
          <p className="text-[12px] text-ge-danger">⚠ {error}</p>
        </div>
      )}

      {/* Report Data */}
      <div className="card">
        <div className="card-title justify-between">
          <span>📋 Report Data{viewSource === 'backend' && backendRec ? ` — ${backendRec.name}` : ''}</span>
          {result && (
            <span className="text-[11px] font-mono text-ge-text3">
              {(result.sources || []).length} equipment ·{' '}
              {INTERVAL_LABELS[result.interval] || result.interval} ·{' '}
              {totalRows.toLocaleString()} rows
            </span>
          )}
        </div>

        {/* Saved report configuration summary (backend saved reports only) — the
            builder controls above can't represent a single-type multi-equipment
            report, so its configuration is shown read-only here alongside the table. */}
        {viewSource === 'backend' && backendRec && (
          <div className="mb-3 flex flex-wrap gap-x-4 gap-y-1 border-b border-ge-border pb-2
                          text-[11px] text-ge-text3">
            <span><span className="text-ge-text2">Type:</span> {backendRec.equipment_type}</span>
            <span><span className="text-ge-text2">Equipment:</span>{' '}
              {backendRec.equipment_label || (backendRec.equipment_ids || []).join(', ') || '—'}</span>
            <span><span className="text-ge-text2">Tags:</span>{' '}
              {backendRec.tag_count ?? (backendRec.tags || []).length}</span>
            <span><span className="text-ge-text2">Range:</span> {backendRec.date_range || '—'}</span>
            <span><span className="text-ge-text2">Interval:</span>{' '}
              {INTERVAL_LABELS[backendRec.interval] || backendRec.interval}</span>
            <span><span className="text-ge-text2">Aggregation:</span>{' '}
              {AGG_LABEL[backendRec.agg_function] || backendRec.agg_function}</span>
          </div>
        )}

        {generating ? (
          <div className="space-y-1">
            {[...Array(6)].map((_, i) => <Skeleton key={i} h="h-8" />)}
          </div>
        ) : !result ? (
          <div className="py-14 text-center">
            <div className="text-4xl mb-3">📊</div>
            <div className="text-[13px] text-ge-text3 mb-1 font-medium">No report generated yet</div>
            <div className="text-[11px] text-ge-text3">
              Select equipment types → columns → click Generate Report
            </div>
          </div>
        ) : rows.length === 0 ? (
          <div className="py-12 text-center text-[12px] text-ge-text3">
            No data available for the selected criteria
          </div>
        ) : (
          <>
            <div className="overflow-auto border border-ge-border rounded" style={{ maxHeight: 520 }}>
              <table className="data-table">
                <thead>
                  <tr>
                    {tableCols.map(c => (
                      <th key={c.key} className="text-[10px] sticky top-0 z-10 whitespace-nowrap">
                        {c.label}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row, i) => (
                    <tr key={i}>
                      {tableCols.map(c => (
                        <td key={c.key} className="font-mono text-[11px] whitespace-nowrap">
                          {c.isTs
                            ? <span className="text-ge-text3">{fmtTimestamp(row[c.key])}</span>
                            : c.isEq
                              ? <span className="text-ge-text2">{row[c.key]}</span>
                              : <span className="text-ge-accent">{fmtCell(row[c.key])}</span>}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="flex flex-wrap items-center gap-2 mt-3">
              <label className="text-[11px] font-mono text-ge-text3 flex items-center gap-1.5">
                Rows/page
                <select value={pageSize} disabled={generating}
                  onChange={e => {
                    const s = Number(e.target.value)
                    return viewSource === 'backend' ? loadBackendPage(backendRec, 1, s) : runReport(1, s)
                  }}
                  className="bg-ge-elevated border border-ge-border rounded px-1.5 py-1
                             text-[11px] text-ge-text1 disabled:opacity-40">
                  {PAGE_SIZES.map(s => <option key={s} value={s}>{s}</option>)}
                </select>
              </label>
              <button onClick={() => viewSource === 'backend'
                  ? loadBackendPage(backendRec, page - 1, pageSize) : runReport(page - 1, pageSize)}
                disabled={page <= 1 || generating}
                className="px-2.5 py-1 text-[11px] font-mono rounded border
                           bg-ge-elevated border-ge-border text-ge-text2
                           hover:text-ge-text1 disabled:opacity-40">◀ Prev</button>
              <button onClick={() => viewSource === 'backend'
                  ? loadBackendPage(backendRec, page + 1, pageSize) : runReport(page + 1, pageSize)}
                disabled={page >= totalPages || generating}
                className="px-2.5 py-1 text-[11px] font-mono rounded border
                           bg-ge-elevated border-ge-border text-ge-text2
                           hover:text-ge-text1 disabled:opacity-40">Next ▶</button>
              <span className="ml-auto text-[11px] font-mono text-ge-text3">
                Page {page.toLocaleString()} of {totalPages.toLocaleString()}
              </span>
            </div>
          </>
        )}
      </div>

      {/* Rename — the app's own dialog, not window.prompt(). */}
      <PromptDialog
        open={!!renaming}
        title="Rename Saved Template"
        label="New Template Name"
        submitLabel="Rename Template"
        busyLabel="Renaming…"
        placeholder="Enter a template name"
        initialValue={renaming?.name || ''}
        validate={validateRename}
        error={renameError}
        busy={renameBusy}
        onCancel={cancelTplRename}
        onSubmit={handleTplRename}
        description={
          <p>
            Current template:{' '}
            <span className="text-ge-text1 font-medium break-words">
              &ldquo;{renaming?.name}&rdquo;
            </span>
          </p>
        }
      />

      {/* Destructive confirmation — the app's own dialog, not window.confirm(). */}
      <ConfirmDialog
        open={!!confirmDelete}
        title="Delete Saved Template"
        confirmLabel="Delete Template"
        busyLabel="Deleting…"
        busy={deleting}
        onCancel={cancelTplDelete}
        onConfirm={handleTplDelete}
      >
        <p>
          Are you sure you want to delete{' '}
          <span className="text-ge-text1 font-medium break-words">
            &ldquo;{confirmDelete?.name}&rdquo;
          </span>?
        </p>
        <p className="mt-2 text-ge-text3">This action cannot be undone.</p>
      </ConfirmDialog>
    </div>
  )
}
