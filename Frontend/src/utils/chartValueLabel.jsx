// src/utils/chartValueLabel.jsx
//
// Permanent value labels for generation/energy bar charts — shown on/above every bar
// in BOTH the web UI and (because the PDF export rasterises the on-screen SVG) the
// exported PDF. This is the single, shared implementation so DGR / MGR / YGR / Analytics
// / Dashboard all label their bars identically. It only draws text; it never touches the
// data, the SQL, or the calculations behind the bars.
//
//  · Values are thousands-separated with their unit, e.g. "975,569 kWh", "1,002.6 MWh".
//  · Column charts get VERTICAL labels (each label stays inside its own bar column, so
//    28–31 daily bars never collide); font auto-shrinks as the bar count grows.
//  · Horizontal bar charts get labels to the RIGHT of each bar end.
//  · `domainMax()` pads the value axis so the tallest label can never clip the top edge.
import React from 'react'

// The single readable label colour for bar charts. Used for the value labels AND (so the
// whole chart reads consistently on the dark theme) for the axis tick values and the axis
// titles. Bright enough to be clearly legible against the dark dashboard background.
export const CHART_LABEL_COLOR = '#e8eefc'

// "975,569 kWh" / "1,002.6 MWh". Large magnitudes drop the decimals (they're noise on a
// 6–7 digit kWh total); smaller magnitudes keep up to 2 so MWh figures stay meaningful.
export const fmtChartValue = (v, unit = '') => {
  const n = Number(v)
  if (!Number.isFinite(n)) return ''
  const s = n.toLocaleString('en-US', { maximumFractionDigits: Math.abs(n) >= 1000 ? 0 : 2 })
  return unit ? `${s} ${unit}` : s
}

// Content renderer for COLUMN charts (bars grow upward). Label sits just above the bar,
// rotated vertical and centred on the column. `barCount` drives the font size so busy
// months (up to 31 bars) still fit; small bars keep their label above the bar (readable),
// large bars rely on the padded Y domain (see domainMax) so the label is never cut off.
export function columnValueLabel({ unit = '', barCount = 12, color = CHART_LABEL_COLOR } = {}) {
  const font = barCount <= 8 ? 12 : barCount <= 14 ? 11 : barCount <= 24 ? 10 : 9
  const Label = (props) => {
    const { x, y, width, value } = props
    const n = Number(value)
    if (!Number.isFinite(n)) return null
    const cx = x + (width || 0) / 2
    const ly = y - 4
    return (
      <text x={cx} y={ly} fill={color} fontSize={font} fontFamily="IBM Plex Mono, monospace"
        textAnchor="start" dominantBaseline="central" transform={`rotate(-90 ${cx} ${ly})`}>
        {fmtChartValue(n, unit)}
      </text>
    )
  }
  return Label
}

// Content renderer for HORIZONTAL bar charts (bars grow rightward). Label sits just past
// the bar end. Pair with extra right margin + a padded value-axis domain so long labels
// (e.g. "1,025,450 kWh") are never clipped by the plot's right edge.
export function rowValueLabel({ unit = '', fontSize = 10, color = CHART_LABEL_COLOR } = {}) {
  const Label = (props) => {
    const { x, y, width, height, value } = props
    const n = Number(value)
    if (!Number.isFinite(n)) return null
    return (
      <text x={(x || 0) + (width || 0) + 6} y={(y || 0) + (height || 0) / 2} fill={color}
        fontSize={fontSize} fontFamily="IBM Plex Mono, monospace"
        textAnchor="start" dominantBaseline="central">
        {fmtChartValue(n, unit)}
      </text>
    )
  }
  return Label
}

// Value-axis max with headroom above the tallest bar so vertical/edge labels don't clip.
export function domainMax(data, key, factor = 1.45) {
  const vals = (data || []).map(d => Number(d?.[key]) || 0)
  const m = vals.length ? Math.max(...vals) : 0
  return m > 0 ? Math.ceil(m * factor) : 1
}
