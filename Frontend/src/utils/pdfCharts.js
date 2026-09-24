// src/utils/pdfCharts.js
//
// Native VECTOR charts for jsPDF — axes, gridlines, lines, bars, value labels and legends
// drawn with PDF primitives straight from the same data arrays the on-screen Recharts
// components render. Nothing is rasterised and nothing depends on the DOM, so a chart can
// be exported whether or not it is currently mounted (WMS / PPC / Inverter views), it stays
// sharp at any zoom, and it prints on a white page without the dark UI background.
//
// A chart is described by a plain spec (see `drawVectorChart`); pdfReport.js places each
// spec on the page and handles page breaks. Values that are null/undefined are GAPS, never
// zeros, so missing telemetry can not be mistaken for a real 0 reading.

const INK   = [28, 34, 51]
const MUTED = [107, 122, 153]
const GRID  = [226, 231, 239]
const AXIS  = [170, 180, 198]
const PANEL = [250, 251, 253]

const hexToRgb = (hex) => {
  const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(String(hex || ''))
  return m ? [parseInt(m[1], 16), parseInt(m[2], 16), parseInt(m[3], 16)] : [0, 153, 255]
}

const isNum = v => v !== null && v !== undefined && v !== '' && Number.isFinite(Number(v))

// Thousands-grouped, up to `d` decimals — the same presentation as the Analytics UI.
export const fmtNum = (v, d = 1) =>
  isNum(v) ? Number(v).toLocaleString('en-US', { maximumFractionDigits: d }) : '—'

// Axis ticks on "nice" round steps (1, 2, 2.5, 5 × 10ⁿ) covering [lo, hi].
function niceTicks(lo, hi, count = 5) {
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) return { min: 0, max: 1, ticks: [0, 1] }
  if (lo === hi) { hi = lo === 0 ? 1 : lo + Math.abs(lo) * 0.1; lo = lo === 0 ? 0 : lo - Math.abs(lo) * 0.1 }
  const raw = (hi - lo) / Math.max(1, count)
  const mag = Math.pow(10, Math.floor(Math.log10(raw)))
  const step = [1, 2, 2.5, 5, 10].map(s => s * mag).find(s => s >= raw) || 10 * mag
  const min = Math.floor(lo / step) * step
  const max = Math.ceil(hi / step) * step
  const ticks = []
  for (let v = min; v <= max + step / 2; v += step) ticks.push(+v.toFixed(10))
  return { min, max, ticks }
}

// Compact tick text so wide values never collide with the plot (12,500 → 12.5k).
const tickText = (v) => {
  const a = Math.abs(v)
  if (a >= 1e6) return `${fmtNum(v / 1e6, 1)}M`
  if (a >= 1e4) return `${fmtNum(v / 1e3, 1)}k`
  return fmtNum(v, a < 10 ? 2 : 1)
}

function drawLegend(doc, series, x, y, maxW) {
  doc.setFontSize(7.5)
  doc.setFont('helvetica', 'normal')
  let cx = x, cy = y
  for (const s of series) {
    const label = String(s.name)
    const w = 14 + doc.getTextWidth(label) + 14
    if (cx + w > x + maxW) { cx = x; cy += 11 }
    doc.setDrawColor(...hexToRgb(s.color))
    doc.setFillColor(...hexToRgb(s.color))
    doc.setLineWidth(1.8)
    doc.line(cx, cy - 2.5, cx + 10, cy - 2.5)
    doc.setTextColor(...INK)
    doc.text(label, cx + 14, cy)
    cx += w
  }
  return cy
}

/**
 * Draw one chart inside the box (x, y, w, h), in PDF points.
 *
 * spec = {
 *   type:       'line' | 'area' | 'bar' | 'hbar',
 *   title:      string,
 *   xLabel:     string,                     // axis title (category axis for hbar: yLabel)
 *   yLabel:     string,
 *   categories: string[],                   // x labels (line/area/bar) or row labels (hbar)
 *   series:     [{ name, color, values: (number|null)[], width? }],
 *   yMin?, yMax?,                           // fixed value-axis domain (e.g. PR 0–100)
 *   valueLabels?: boolean,                  // print each bar's value (bar / hbar)
 *   valueDecimals?: number,
 *   legend?: boolean,                       // default: shown when >1 series
 * }
 */
export function drawVectorChart(doc, spec, x, y, w, h) {
  const { type = 'line', title = '', xLabel = '', yLabel = '', categories = [],
          series = [], valueLabels = false, valueDecimals = 1 } = spec
  const showLegend = spec.legend ?? series.length > 1

  // Frame + title.
  doc.setDrawColor(...GRID); doc.setLineWidth(0.6)
  doc.setFillColor(...PANEL)
  doc.roundedRect(x, y, w, h, 3, 3, 'FD')
  doc.setTextColor(...INK); doc.setFont('helvetica', 'bold'); doc.setFontSize(10)
  doc.text(title, x + 10, y + 16)

  // A chart with no real values says so, rather than drawing blank axes that read as a
  // rendering failure (or, worse, as a flat line of zeros).
  if (!series.some(s => (s.values || []).some(isNum))) {
    doc.setTextColor(...MUTED); doc.setFont('helvetica', 'normal'); doc.setFontSize(9.5)
    doc.text(spec.emptyText || 'No data available for the selected date range.',
      x + w / 2, y + h / 2 + 4, { align: 'center' })
    return
  }

  // Legend row directly under the title.
  let top = y + 26
  if (showLegend && series.length) top = drawLegend(doc, series, x + 10, top + 6, w - 20) + 8

  // Value domain from the data actually present (nulls ignored).
  const all = series.flatMap(s => s.values.filter(isNum).map(Number))
  const hasData = all.length > 0
  const dataLo = hasData ? Math.min(...all) : 0
  const dataHi = hasData ? Math.max(...all) : 1
  // Bars always start at zero; lines start at zero unless the data is clearly offset
  // (e.g. grid frequency ~50 Hz), where a zero baseline would flatten every change.
  const zeroBase = type === 'bar' || type === 'hbar' || type === 'area' || dataLo >= 0 && dataLo < (dataHi - dataLo)
  let lo = spec.yMin ?? (zeroBase ? Math.min(0, dataLo) : dataLo)
  let hi = spec.yMax ?? dataHi
  if (valueLabels && type === 'bar' && spec.yMax == null) hi = hi * 1.12   // headroom above bars for labels
  const nt = niceTicks(lo, hi)
  lo = spec.yMin ?? nt.min
  hi = spec.yMax ?? nt.max
  const ticks = nt.ticks.filter(t => t >= lo - 1e-9 && t <= hi + 1e-9)

  doc.setFontSize(7.5); doc.setFont('helvetica', 'normal')

  if (type === 'hbar') {
    // ── Horizontal bars: categories down the left, values along the bottom ──
    const labW = Math.min(w * 0.28, Math.max(...categories.map(c => doc.getTextWidth(String(c))), 20) + 12)
    const valLabW = valueLabels ? Math.max(...series[0].values.map(v => doc.getTextWidth(fmtNum(v, valueDecimals))), 20) + 10 : 0
    const px = x + 12 + labW, py = top + 4
    const pw = w - (px - x) - 14 - valLabW, ph = h - (py - y) - 30
    const vx = v => px + ((v - lo) / (hi - lo || 1)) * pw

    // vertical gridlines + value ticks
    doc.setLineDashPattern([2, 2], 0)
    for (const t of ticks) {
      doc.setDrawColor(...GRID); doc.setLineWidth(0.5); doc.line(vx(t), py, vx(t), py + ph)
      doc.setTextColor(...MUTED); doc.text(tickText(t), vx(t), py + ph + 10, { align: 'center' })
    }
    doc.setLineDashPattern([], 0)
    const n = Math.max(1, categories.length)
    const slot = ph / n, barH = Math.min(18, slot * 0.62)
    const s = series[0]
    categories.forEach((c, i) => {
      const cy = py + slot * i + slot / 2
      doc.setTextColor(...INK)
      doc.text(String(c), px - 6, cy + 2.5, { align: 'right' })
      const v = s.values[i]
      if (!isNum(v)) return
      doc.setFillColor(...hexToRgb(s.color))
      doc.rect(px, cy - barH / 2, Math.max(0.5, vx(Number(v)) - px), barH, 'F')
      if (valueLabels) {
        doc.setTextColor(...INK)
        doc.text(fmtNum(v, valueDecimals), vx(Number(v)) + 4, cy + 2.5)
      }
    })
    doc.setDrawColor(...AXIS); doc.setLineWidth(0.7)
    doc.line(px, py, px, py + ph); doc.line(px, py + ph, px + pw, py + ph)
    if (xLabel) { doc.setTextColor(...MUTED); doc.text(xLabel, px + pw / 2, y + h - 8, { align: 'center' }) }
    return
  }

  // ── Line / area / vertical bar: categories along the bottom ──
  const yTickW = Math.max(...ticks.map(t => doc.getTextWidth(tickText(t))), 14)
  const px = x + 12 + (yLabel ? 12 : 0) + yTickW + 6
  const py = top + 4
  const pw = w - (px - x) - 16
  const ph = h - (py - y) - 36
  const vy = v => py + ph - ((v - lo) / (hi - lo || 1)) * ph
  const n = Math.max(1, categories.length)
  const band = type === 'bar'
  const cx = i => band ? px + (pw / n) * (i + 0.5) : px + (n === 1 ? pw / 2 : (pw / (n - 1)) * i)

  // horizontal gridlines + value ticks
  doc.setLineDashPattern([2, 2], 0)
  for (const t of ticks) {
    doc.setDrawColor(...GRID); doc.setLineWidth(0.5); doc.line(px, vy(t), px + pw, vy(t))
    doc.setTextColor(...MUTED); doc.text(tickText(t), px - 5, vy(t) + 2.5, { align: 'right' })
  }
  doc.setLineDashPattern([], 0)

  // category labels, thinned so they never overlap however long the range is
  const labW = Math.max(...categories.map(c => doc.getTextWidth(String(c))), 10) + 8
  const step = Math.max(1, Math.ceil(n / Math.max(1, Math.floor(pw / labW))))
  // Every `step`-th label, always ending on the LAST category (the end of the range is the
  // date readers look for). If the last one would sit too close to the previous thinned
  // label, it takes that label's place instead of overlapping it.
  const shown = []
  for (let i = 0; i < n; i += step) shown.push(i)
  if (n > 1 && shown[shown.length - 1] !== n - 1) {
    if ((n - 1) - shown[shown.length - 1] < step * 0.6) shown[shown.length - 1] = n - 1
    else shown.push(n - 1)
  }
  doc.setTextColor(...MUTED)
  for (const i of shown) doc.text(String(categories[i]), cx(i), py + ph + 10, { align: 'center' })

  if (band) {
    const s = series[0]
    const bw = Math.min(46, (pw / n) * 0.62)
    const barRgb = hexToRgb(s.color)
    s.values.forEach((v, i) => {
      if (!isNum(v)) return
      const top_ = vy(Math.max(Number(v), lo)), base = vy(Math.max(0, lo))
      // Set per bar: jsPDF uses the same fill operator for TEXT colour, so drawing the
      // previous bar's value label would otherwise paint every later bar in the ink colour.
      doc.setFillColor(...barRgb)
      doc.rect(cx(i) - bw / 2, top_, bw, Math.max(0.5, base - top_), 'F')
      if (valueLabels) {
        doc.setTextColor(...INK)
        doc.text(fmtNum(v, valueDecimals), cx(i), top_ - 3, { align: 'center' })
      }
    })
  } else {
    for (const s of series) {
      const rgb = hexToRgb(s.color)
      // Split into runs of consecutive real values: a null ends a run (a visible gap).
      const runs = []; let cur = []
      s.values.forEach((v, i) => {
        if (isNum(v)) cur.push([cx(i), vy(Number(v))])
        else if (cur.length) { runs.push(cur); cur = [] }
      })
      if (cur.length) runs.push(cur)

      if (type === 'area') {
        const base = vy(Math.max(0, lo))
        for (const run of runs) {
          if (run.length < 2) continue
          const pts = [[run[0][0], base], ...run, [run[run.length - 1][0], base]]
          const segs = pts.slice(1).map((p, k) => [p[0] - pts[k][0], p[1] - pts[k][1]])
          doc.saveGraphicsState()
          doc.setGState(new doc.GState({ opacity: 0.16 }))
          doc.setFillColor(...rgb)
          doc.lines(segs, pts[0][0], pts[0][1], [1, 1], 'F', true)
          doc.restoreGraphicsState()
        }
      }
      doc.setDrawColor(...rgb); doc.setLineWidth(s.width ?? 1.6)
      doc.setLineJoin?.('round'); doc.setLineCap?.('round')
      for (const run of runs) {
        if (run.length === 1) {                     // an isolated point: show it as a dot
          doc.setFillColor(...rgb); doc.circle(run[0][0], run[0][1], 1.6, 'F'); continue
        }
        const segs = run.slice(1).map((p, k) => [p[0] - run[k][0], p[1] - run[k][1]])
        doc.lines(segs, run[0][0], run[0][1], [1, 1], 'S', false)
      }
    }
  }

  // axes + titles
  doc.setDrawColor(...AXIS); doc.setLineWidth(0.7)
  doc.line(px, py, px, py + ph); doc.line(px, py + ph, px + pw, py + ph)
  doc.setTextColor(...MUTED); doc.setFontSize(7.5)
  if (xLabel) doc.text(xLabel, px + pw / 2, y + h - 8, { align: 'center' })
  if (yLabel) doc.text(yLabel, x + 17, py + ph / 2 + doc.getTextWidth(yLabel) / 2, { angle: 90 })
}

/** True when a spec has at least one real value to plot. */
export const specHasData = (spec) =>
  (spec?.series || []).some(s => (s.values || []).some(isNum))

/**
 * A boxed notice (e.g. "No data available for the selected date range.") drawn in place
 * of charts — used instead of blank axes, which read as a rendering failure.
 */
export function drawNoticeBox(doc, text, detail, x, y, w, h = 70) {
  doc.setDrawColor(...GRID); doc.setLineWidth(0.6); doc.setFillColor(...PANEL)
  doc.roundedRect(x, y, w, h, 3, 3, 'FD')
  doc.setTextColor(...INK); doc.setFont('helvetica', 'bold'); doc.setFontSize(11)
  doc.text(text, x + w / 2, y + h / 2 - (detail ? 3 : -3), { align: 'center' })
  if (detail) {
    doc.setTextColor(...MUTED); doc.setFont('helvetica', 'normal'); doc.setFontSize(8.5)
    doc.text(detail, x + w / 2, y + h / 2 + 12, { align: 'center' })
  }
  return y + h
}
