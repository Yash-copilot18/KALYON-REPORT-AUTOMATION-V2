// src/utils/reportTemplates.js
//
// Preconfigured Reports — user-defined report TEMPLATES.
//
// A template is a saved snapshot of the current Preconfigured Reports configuration
// (equipment types, identifiers, selected columns, date range, interval,
// aggregation, export format). It is a pure front-end convenience: templates live
// in the browser's localStorage and never touch the backend, so report generation
// and Excel export are completely unaffected.
//
// These are NOT the built-in report presets in reportPresets.js (Inverter/WMS/PPC
// defaults) and they are NOT predefined reports such as DGR/MGR/YGR/Alarm — every
// template here is created by the user from whatever they have on screen.
//
// Storage layout (one JSON array under a single key):
//   [{ id, name, createdAt, updatedAt, config: {...} }, …]

const STORAGE_KEY = 'kalyon.preconfigured.templates.v1'

// Bump if the saved `config` shape ever changes in a breaking way.
export const TEMPLATE_VERSION = 1

function _readAll() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed) ? parsed : []
  } catch {
    // Corrupt / unparseable storage should never crash the page — start clean.
    return []
  }
}

function _writeAll(list) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(list))
    return true
  } catch {
    // Quota exceeded or storage disabled (private mode): report failure to caller.
    return false
  }
}

const _id = () =>
  `tpl_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`

// Case-insensitive, trimmed name comparison so "Daily" and "daily " collide.
const _norm = s => String(s || '').trim().toLowerCase()

// ── Public API ────────────────────────────────────────────────────────────────

/** Every saved template, newest-updated first. */
export function listTemplates() {
  return _readAll().sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0))
}

export function getTemplate(id) {
  return _readAll().find(t => t.id === id) || null
}

export function nameExists(name, exceptId = null) {
  const n = _norm(name)
  return _readAll().some(t => _norm(t.name) === n && t.id !== exceptId)
}

/**
 * Create a new template from a config snapshot.
 * Returns { ok, template?, error? }. Rejects blank or duplicate names.
 */
export function createTemplate(name, config) {
  const clean = String(name || '').trim()
  if (!clean) return { ok: false, error: 'Template name is required' }
  if (nameExists(clean)) return { ok: false, error: `A template named "${clean}" already exists` }

  const now = Date.now()
  const template = {
    id: _id(),
    name: clean,
    version: TEMPLATE_VERSION,
    createdAt: now,
    updatedAt: now,
    config,
  }
  const list = _readAll()
  list.push(template)
  if (!_writeAll(list)) return { ok: false, error: 'Could not save — browser storage is full or disabled' }
  return { ok: true, template }
}

/**
 * Record a template automatically (used when the user clicks Generate Report).
 * Unlike createTemplate this NEVER fails on a duplicate name — a clashing name is
 * made unique with a numeric suffix — because the user did not type it and must
 * not be blocked from generating. Returns { ok, template?, error? }.
 */
export function createAutoTemplate(name, config) {
  const base = String(name || '').trim() || 'Report'
  let clean = base
  for (let i = 2; nameExists(clean); i++) clean = `${base} (${i})`

  const now = Date.now()
  const template = {
    id: _id(),
    name: clean,
    version: TEMPLATE_VERSION,
    createdAt: now,
    updatedAt: now,
    auto: true,          // distinguishes generated history from hand-named templates
    config,
  }
  const list = _readAll()
  list.push(template)
  if (!_writeAll(list)) return { ok: false, error: 'Could not save — browser storage is full or disabled' }
  return { ok: true, template }
}

/**
 * Overwrite an existing template's config (Save changes). Name is left as-is
 * unless `name` is provided (Rename + Save in one call).
 */
export function updateTemplate(id, config, name = undefined) {
  const list = _readAll()
  const i = list.findIndex(t => t.id === id)
  if (i === -1) return { ok: false, error: 'Template not found' }

  if (name !== undefined) {
    const clean = String(name).trim()
    if (!clean) return { ok: false, error: 'Template name is required' }
    if (nameExists(clean, id)) return { ok: false, error: `A template named "${clean}" already exists` }
    list[i].name = clean
  }
  if (config !== undefined) list[i].config = config
  list[i].updatedAt = Date.now()

  if (!_writeAll(list)) return { ok: false, error: 'Could not save — browser storage is full or disabled' }
  return { ok: true, template: list[i] }
}

/** Rename only (no config change). */
export function renameTemplate(id, name) {
  return updateTemplate(id, undefined, name)
}

export function deleteTemplate(id) {
  const list = _readAll()
  const next = list.filter(t => t.id !== id)
  if (next.length === list.length) return { ok: false, error: 'Template not found' }
  if (!_writeAll(next)) return { ok: false, error: 'Could not update browser storage' }
  return { ok: true }
}
