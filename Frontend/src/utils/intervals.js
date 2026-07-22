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
