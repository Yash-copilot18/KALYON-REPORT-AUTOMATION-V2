// src/utils/reportPresets.js
//
// Preconfigured Reports — the single source of truth for the defaults that get
// applied when a report type is chosen.
//
// There are NO extra report templates here: every key below is an Equipment Type
// that already exists in the backend registry (EQUIPMENT_REGISTRY) and is already
// listed in the Equipment Type dropdown. A preset only says "when this report type
// is selected, start from these settings" — the user can change every one of them
// before loading, exporting, or saving a schedule.
//
// A preset never hardcodes equipment names or counts: `equipment` is a policy
// ('all' | 'first') resolved against the equipment list the backend returns for
// the type, and tags are always taken from the database schema for the selected
// equipment (see `tags: 'all'`).
//
// Imported by:
//   · pages/Reports/Reports.jsx    — Report Automation screen
//   · pages/Scheduled/Scheduled.jsx — Create/Edit Schedule form

import { DEFAULT_AGG } from './intervals'

// equipment : 'all'   → preselect every identifier the type exposes
//             'first' → preselect the first identifier only (heavy/wide sources)
// interval  : default Time Interval (key from utils/intervals INTERVALS)
// agg       : default Aggregation — ignored by instant intervals
// format    : default Export Format ('Excel' | 'CSV')
// tags      : 'all' → every tag the selected equipment exposes in the database
export const REPORT_PRESETS = {
  'Inverter': {
    equipment: 'all', interval: 'hourly', agg: 'avg', format: 'Excel', tags: 'all',
    note: 'All inverters · hourly averages',
  },
  'String Combiner': {
    // 336 SCB columns per inverter — start with one device, add more as needed.
    equipment: 'first', interval: 'hourly', agg: 'avg', format: 'Excel', tags: 'all',
    note: 'First SMB device · hourly averages (very wide source)',
  },
  'WMS': {
    equipment: 'all', interval: '15min', agg: 'avg', format: 'Excel', tags: 'all',
    note: 'Weather station · 15-minute averages',
  },
  'PPC': {
    equipment: 'all', interval: '15min', agg: 'avg', format: 'Excel', tags: 'all',
    note: 'Plant controller · 15-minute averages',
  },
  'PPC Trend': {
    equipment: 'all', interval: 'hourly', agg: 'avg', format: 'Excel', tags: 'all',
    note: 'Controller trend · hourly averages',
  },
  'Power Graph': {
    equipment: 'all', interval: '15min', agg: 'avg', format: 'Excel', tags: 'all',
    note: 'Per-inverter power · 15-minute averages',
  },
  'Power vs Irradiance': {
    equipment: 'all', interval: '15min', agg: 'avg', format: 'Excel', tags: 'all',
    note: 'Power/irradiance correlation · 15-minute averages',
  },
  'Daily Generation': {
    // INVERTER_xx_GEN are daily-resetting cumulative kWh counters, so the day's
    // total is the MAXIMUM within the day — never an average or a sum.
    equipment: 'all', interval: 'daily', agg: 'max', format: 'Excel', tags: 'all',
    note: 'Daily energy counters · daily maximum (counter reading)',
  },
  'Monthly Generation': {
    equipment: 'all', interval: 'monthly', agg: 'max', format: 'Excel', tags: 'all',
    note: 'Monthly energy counters · monthly maximum (counter reading)',
  },
  'Inverter Temperature': {
    equipment: 'all', interval: 'hourly', agg: 'avg', format: 'Excel', tags: 'all',
    note: 'Inverter temperatures · hourly averages',
  },
  'T1 Isolation': {
    equipment: 'all', interval: 'hourly', agg: 'avg', format: 'Excel', tags: 'all',
    note: 'All T1 isolation devices · hourly averages',
  },
  'T2 Isolation': {
    equipment: 'all', interval: 'hourly', agg: 'avg', format: 'Excel', tags: 'all',
    note: 'All T2 isolation devices · hourly averages',
  },
  // Operator-facing merge of T1 + T2 Isolation (devices shown as Tracker1…Tracker24).
  'Tracker': {
    equipment: 'all', interval: 'hourly', agg: 'avg', format: 'Excel', tags: 'all',
    note: 'All tracker devices · hourly averages',
  },
}

// The report types offered as preconfigured templates, in dropdown order.
export const PRESET_TYPES = Object.keys(REPORT_PRESETS)

// Applied to any report type without an explicit preset (e.g. a legacy schedule
// saved against a type that is no longer listed) so callers never see undefined.
export const FALLBACK_PRESET = {
  equipment: 'first', interval: 'hourly', agg: DEFAULT_AGG, format: 'Excel',
  tags: 'all', note: '',
}

export const hasPreset = type =>
  Object.prototype.hasOwnProperty.call(REPORT_PRESETS, type)

export const getPreset = type => REPORT_PRESETS[type] || FALLBACK_PRESET

/**
 * Resolve a preset's equipment policy against the identifiers the backend
 * returned for the type. Returns an array of equipment_id strings (possibly
 * empty when the list has not loaded yet).
 */
export function presetEquipmentIds(type, eqList) {
  const ids = (eqList || []).map(e => e?.equipment_id).filter(Boolean)
  if (ids.length === 0) return []
  return getPreset(type).equipment === 'all' ? ids : ids.slice(0, 1)
}
