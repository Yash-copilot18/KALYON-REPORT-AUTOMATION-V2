// src/utils/intervals.js
//
// Single source of truth for the Time Interval selector and its aggregation rule.
//
// SCADA rule: instant telemetry is never aggregated. A 1-minute interval is the
// plant's native sample rate, so it is fetched as raw records and NO aggregation
// parameter is sent. Every coarser interval buckets the samples and aggregates
// them (Average by default).
//
// Any screen with a Time Interval selector should import from here rather than
// re-declaring the list or re-testing the interval string.

export const INTERVALS = ['1min', '5min', '15min', '30min', 'hourly', 'daily', 'monthly']

export const INTERVAL_LABELS = {
  raw:       'Raw (all records)',
  '1min':    '1 Minute (Instant Data)',
  '5min':    '5 Minutes',
  '15min':   '15 Minutes',
  '30min':   '30 Minutes',
  hourly:    'Hourly',
  daily:     'Daily',
  monthly:   'Monthly',
}

// Intervals that return raw records — no aggregation is applied or displayed.
export const INSTANT_INTERVALS = ['raw', '1min']

export const AGG_OPTIONS = [
  { value: 'avg', label: 'Average' },
  { value: 'min', label: 'Minimum' },
  { value: 'max', label: 'Maximum' },
  { value: 'sum', label: 'Sum' },
]

export const DEFAULT_AGG = 'avg'

// ── Which intervals a date range may use ─────────────────────────────────────
// Client rule, applied to EVERY report/equipment type on the Reports page:
//   · range of ONE MONTH OR LESS → 1 Minute, 5 Minutes, 15 Minutes, 30 Minutes, Hourly
//   · range of MORE THAN a month → 30 Minutes, Hourly only
// Daily and Monthly are never offered from this selector (the generation reports
// DGR/MGR/YGR set those intervals themselves and do not use this list).
export const SHORT_RANGE_INTERVALS = ['1min', '5min', '15min', '30min', 'hourly']
export const LONG_RANGE_INTERVALS  = ['30min', 'hourly']
export const DEFAULT_INTERVAL      = 'hourly'

// 'YYYY-MM-DDTHH:mm' | 'YYYY-MM-DD' → {y, mo, d}; null when unparseable.
// Only the CALENDAR DAY is read: the boundary is judged on the dates the operator
// picked, so 08/02 → 08/03 counts as exactly one month whatever the times say.
const _datePart = (value) => {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(value || ''))
  return m ? { y: +m[1], mo: +m[2], d: +m[3] } : null
}

/**
 * True when To is at most ONE CALENDAR MONTH after From — "exactly one month" counts
 * as within. The limit is From's own day-of-month in the following month (not a fixed
 * 30/31 days and not the calendar month boundary), with the day clamped to that
 * month's length so 31/01 → 29/02 in a leap year is still one month. Comparison is a
 * plain YYYYMMDD integer test, so no timezone or DST shift can move the boundary.
 * An incomplete/unparseable range is treated as short, i.e. the fuller option list.
 */
export function isWithinOneMonth(from, to) {
  const f = _datePart(from), t = _datePart(to)
  if (!f || !t) return true
  let y = f.y, mo = f.mo + 1
  if (mo > 12) { mo = 1; y += 1 }
  const lastDayOfTargetMonth = new Date(y, mo, 0).getDate()   // mo is 1-based here
  const d = Math.min(f.d, lastDayOfTargetMonth)
  const limit = y * 10000 + mo * 100 + d                      // From + 1 month
  const end   = t.y * 10000 + t.mo * 100 + t.d
  return end <= limit
}

/** The Time Interval options allowed for this From→To range, in display order. */
export function intervalsForRange(from, to) {
  return isWithinOneMonth(from, to) ? SHORT_RANGE_INTERVALS : LONG_RANGE_INTERVALS
}

/** Whether `interval` may be used with this range. */
export const isIntervalAllowed = (interval, from, to) =>
  intervalsForRange(from, to).includes(interval)

/**
 * Keep `current` if the range still allows it, otherwise fall back to a valid option —
 * Hourly by preference. Used whenever From/To changes so the selector can never be
 * left showing an interval that is no longer offered.
 */
export function resolveInterval(current, from, to) {
  const allowed = intervalsForRange(from, to)
  if (allowed.includes(current)) return current
  return allowed.includes(DEFAULT_INTERVAL) ? DEFAULT_INTERVAL : allowed[0]
}

/** True when the interval is instant telemetry (raw records, never aggregated). */
export const isInstant = interval => INSTANT_INTERVALS.includes(interval)

/** Whether the Aggregation field should be rendered for this interval. */
export const showsAggregation = interval => !isInstant(interval)

/** The aggregation to apply — null for instant intervals. */
export const effectiveAgg = (interval, agg = DEFAULT_AGG) =>
  isInstant(interval) ? null : (agg || DEFAULT_AGG)

export const intervalLabel = interval => INTERVAL_LABELS[interval] || interval

/**
 * Merge the interval + aggregation into an API payload.
 * On an instant interval the `agg_function` key is omitted entirely, so no
 * aggregation parameter ever reaches the backend.
 */
export function withAggregation(payload, interval, agg = DEFAULT_AGG) {
  const resolved = effectiveAgg(interval, agg)
  const next = { ...payload, interval }
  if (resolved === null) {
    delete next.agg_function
    return next
  }
  return { ...next, agg_function: resolved }
}
