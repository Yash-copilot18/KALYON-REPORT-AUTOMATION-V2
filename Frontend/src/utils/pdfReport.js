// src/utils/pdfReport.js
//
// Production-grade PDF builder for the Generation Reports (DGR / MGR / YGR).
// One place owns branding, the running header, the footer (Page X of Y + generated
// timestamp), chart images and multi-page tables — so every report looks identical.
//
// A4 landscape by default. Tables flow across as many pages as needed with their
// column headers repeated automatically (jspdf-autotable). Charts are embedded as
// high-resolution PNGs captured from the live Recharts SVG.

import { jsPDF } from 'jspdf'
import autoTableImport from 'jspdf-autotable'

// jspdf-autotable's default export is the function under a bundler (Vite) but an
// interop object under Node ESM — resolve to the callable either way.
const autoTable = typeof autoTableImport === 'function'
  ? autoTableImport
  : autoTableImport.default

// ── Brand palette (matches the app: navy surface, teal accent) ───────────────
const NAVY   = [15, 21, 36]
const ACCENT = [0, 212, 170]
const INK    = [28, 34, 51]
const MUTED  = [107, 122, 153]
const STRIPE = [244, 247, 250]
const LINE   = [210, 217, 228]

const MARGIN = 36
const HEADER_H = 74      // running header height
const FOOTER_H = 34      // footer height

const pad = n => String(n).padStart(2, '0')
function stamp() {
  const d = new Date()
  return `${pad(d.getDate())}/${pad(d.getMonth() + 1)}/${d.getFullYear()} ` +
         `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

// ── Running header + footer, drawn on every page in a final pass ─────────────
// `footer` modes:
//   true       → full footer: rule + "Generated …" + centre text + "Page X of Y"  (MGR / YGR)
//   'minimal'  → ONLY "Page X of Y" on every page, no rule/other text             (DGR)
//   false      → no footer at all
// "Page X of Y" is computed from the real page count, so it adjusts automatically to more or
// fewer pages. The main header band is always page-1-only.
function paintChrome(doc, { plant, reportTitle, subtitle, generatedAt, footer = true }) {
  const pages = doc.internal.getNumberOfPages()
  const W = doc.internal.pageSize.getWidth()
  const H = doc.internal.pageSize.getHeight()

  for (let p = 1; p <= pages; p++) {
    doc.setPage(p)

    // ── Main report header — FIRST PAGE ONLY (req 1 & 2) ─────────────────────
    // The plant name, report title, period and brand band are drawn only on page 1.
    // Pages 2+ deliberately get NO header band, so the report content simply
    // continues from the top (req 3). Identical styling to before — just not repeated.
    if (p === 1) {
      // Header band (dark theme) + green accent underline
      doc.setFillColor(...NAVY)
      doc.rect(0, 0, W, HEADER_H, 'F')
      doc.setFillColor(...ACCENT)
      doc.rect(0, HEADER_H, W, 2.5, 'F')     // accent underline

      // Clean corporate header — no logo/image. Plant name (large, bold) with the
      // report title and period centred beneath it.
      const cxPage = W / 2
      doc.setTextColor(255, 255, 255)
      doc.setFont('helvetica', 'bold')
      doc.setFontSize(18)
      doc.text(plant, cxPage, 28, { align: 'center' })

      doc.setTextColor(...ACCENT)
      doc.setFont('helvetica', 'bold')
      doc.setFontSize(11)
      doc.text(reportTitle, cxPage, 46, { align: 'center' })

      if (subtitle) {
        doc.setTextColor(220, 226, 236)
        doc.setFont('helvetica', 'normal')
        doc.setFontSize(9.5)
        doc.text(subtitle, cxPage, 61, { align: 'center' })
      }
    }

    // ── Footer. The bottom margin (FOOTER_H) is always reserved so nothing overlaps content.
    if (footer) {
      doc.setTextColor(...MUTED)
      doc.setFont('helvetica', 'normal')
      doc.setFontSize(8)
      // Full footer only: rule + generated time + centre text. Skipped for 'minimal'.
      if (footer !== 'minimal') {
        doc.setDrawColor(...LINE)
        doc.setLineWidth(0.6)
        doc.line(MARGIN, H - FOOTER_H, W - MARGIN, H - FOOTER_H)
        doc.text(`Generated ${generatedAt}`, MARGIN, H - FOOTER_H + 14)
        doc.text('Kalyon Solar Monitoring — Report Automation', W / 2, H - FOOTER_H + 14, { align: 'center' })
      }
      // Page number — EVERY page, in both full and minimal modes (dynamic "Page X of Y").
      doc.text(`Page ${p} of ${pages}`, W - MARGIN, H - FOOTER_H + 14, { align: 'right' })
    }
  }
}

// ── Section title + meta card on the first page ──────────────────────────────
function drawMeta(doc, meta, startY) {
  const W = doc.internal.pageSize.getWidth()
  const contentW = W - MARGIN * 2
  const cols = 3
  const cellW = contentW / cols
  const rows = Math.ceil(meta.length / cols)
  const rowH = 22
  const boxH = rows * rowH

  doc.setDrawColor(...LINE)
  doc.setLineWidth(0.6)
  doc.roundedRect(MARGIN, startY, contentW, boxH, 3, 3, 'S')

  meta.forEach((m, i) => {
    const r = Math.floor(i / cols), c = i % cols
    const x = MARGIN + c * cellW + 8
    const y = startY + r * rowH
    doc.setTextColor(...MUTED)
    doc.setFont('helvetica', 'bold')
    doc.setFontSize(7)
    doc.text(String(m.label).toUpperCase(), x, y + 9)
    doc.setTextColor(...INK)
    doc.setFont('helvetica', 'normal')
    doc.setFontSize(9.5)
    doc.text(String(m.value ?? '—'), x, y + 18)
  })
  return startY + boxH + 14
}

// Space available before the footer on the current page.
const bottomLimit = doc => doc.internal.pageSize.getHeight() - FOOTER_H - 12
// PAGE 1: content sits below the full report header band.
const contentTop = () => HEADER_H + 16
// PAGE 2 ONWARDS: the report header is NOT repeated (req 1–3). Content continues from
// the top margin instead, reclaiming that space; table column headers still repeat.
const contentTopCont = () => MARGIN + 12

// ── Chart image, scaled to content width, page-broken if needed ──────────────
function drawChart(doc, chart, y) {
  const W = doc.internal.pageSize.getWidth()
  const contentW = W - MARGIN * 2
  const drawW = contentW
  const drawH = (chart.height / chart.width) * drawW

  if (y + drawH + 24 > bottomLimit(doc)) {
    doc.addPage()
    y = contentTopCont()   // continuation page → no header, resume from the top margin
  }
  doc.setTextColor(...INK)
  doc.setFont('helvetica', 'bold')
  doc.setFontSize(10)
  doc.text(chart.title, MARGIN, y + 2)
  y += 10
  try {
    doc.addImage(chart.dataUrl, 'PNG', MARGIN, y, drawW, drawH, undefined, 'FAST')
    return y + drawH + 18
  } catch (e) {
    // A malformed chart image must never abort the report — the tables are the data
    // of record. Drop a placeholder line and carry on.
    // eslint-disable-next-line no-console
    console.warn('PDF: chart image could not be embedded, skipping it —', e?.message)
    doc.setTextColor(...MUTED)
    doc.setFont('helvetica', 'italic')
    doc.setFontSize(9)
    doc.text('(chart unavailable)', MARGIN, y + 12)
    return y + 22
  }
}

// ── One table via autotable (column header on the first page only) ───────────
function drawTable(doc, table, startY) {
  const W = doc.internal.pageSize.getWidth()
  // Title sits just above the table; if it won't fit, start on a fresh page.
  let y = startY
  if (y + 60 > bottomLimit(doc)) { doc.addPage(); y = contentTopCont() }
  doc.setTextColor(...INK)
  doc.setFont('helvetica', 'bold')
  doc.setFontSize(10.5)
  doc.text(table.title, MARGIN, y + 2)
  y += 8

  const columns = table.columns.map(c => ({ header: c.header, dataKey: c.dataKey }))
  const body = table.rows.map(row => {
    const o = {}
    table.columns.forEach(c => {
      const raw = row[c.dataKey]
      o[c.dataKey] = c.format ? c.format(raw) : (raw ?? '')
    })
    return o
  })
  // Per-column alignment applied to BOTH the header and the body cells (autotable
  // applies columnStyles to head + body), so every header lines up with its values.
  // Default numeric-looking columns to centre if the caller didn't specify (req 1 & 2).
  const columnStyles = {}
  table.columns.forEach(c => {
    columnStyles[c.dataKey] = { halign: c.align || 'left' }
  })

  autoTable(doc, {
    columns,
    body,
    startY: y,
    // `top` is the margin autotable uses on CONTINUATION pages (page 2+): no report
    // header there, so the table's data rows resume near the top of the page. Page 1
    // uses `startY` (below the report header) instead.
    margin: { top: contentTopCont(), bottom: FOOTER_H + 12, left: MARGIN, right: MARGIN },
    theme: 'grid',
    tableWidth: W - MARGIN * 2,
    // Column header (e.g. "Date | Inverter 1 … 24") appears ONLY on the first page of the
    // table; page 2 onward continues with just the data rows — no repeated header (client
    // request). Applies to every PDF table (MGR, DGR, YGR) since they all route through here.
    showHead: 'firstPage',
    styles: { font: 'helvetica', fontSize: 8, cellPadding: 3.5, textColor: INK,
              lineColor: LINE, lineWidth: 0.4, overflow: 'linebreak' },
    // No halign here → the header inherits each column's own alignment (columnStyles),
    // so headers sit directly above their values instead of all being centre-forced.
    headStyles: { fillColor: NAVY, textColor: ACCENT, fontStyle: 'bold', fontSize: 8.5,
                  lineColor: NAVY },
    alternateRowStyles: { fillColor: STRIPE },
    columnStyles,
  })
  return doc.lastAutoTable.finalY + 16
}

// Truncate a string with an ellipsis so it fits `maxW` pt at the CURRENT font. Used for
// long WMS parameter NAMES only — never for values (values are always drawn in full).
function fitText(doc, str, maxW) {
  if (maxW <= 0) return ''
  if (doc.getTextWidth(str) <= maxW) return str
  let s = String(str)
  while (s.length > 1 && doc.getTextWidth(s + '…') > maxW) s = s.slice(0, -1)
  return s + '…'
}

// ── DGR WMS cards: station columns (Average WMS + WMS 1 … N), each a compact
// Parameter | Value list. Up to 4 cards per page (client layout): the first WMS page carries
// Average WMS + WMS 1 + WMS 2 + WMS 3, the next carries WMS 4 and any further units, and so on.
// Each page draws its own "WMS Data" heading, then one row of ≤4 whole cards — an individual
// card is NEVER split across pages, so every parameter name AND value stays together and
// readable. The value formatting mirrors the dashboard's fmt3 exactly (blank only when the
// source value is truly absent; otherwise the number to 3 dp). Returns the y below the cards.
const WMS_PER_PAGE  = 4     // Average WMS + WMS 1..3 on the first WMS page; WMS 4+ on the next
const WMS_TITLE_H   = 16
const WMS_ROW_H     = 13    // fixed, readable — never auto-shrunk to fit a page
const WMS_ROW_FONT  = 8
// Same rule as the dashboard's fmt3: a real number → 3 dp; genuinely absent → blank (never a
// dash). A loaded value therefore always prints — pagination cannot turn it blank.
const wmsValStr = v =>
  (v === null || v === undefined || v === '' || !Number.isFinite(Number(v)))
    ? '' : Number(v).toFixed(3)
function drawWmsCards(doc, cards, startY) {
  if (!cards?.length) return startY
  const W = doc.internal.pageSize.getWidth()
  const contentW = W - MARGIN * 2
  const gap = 8
  const perRow = Math.min(cards.length, WMS_PER_PAGE)
  const cardW = (contentW - gap * (perRow - 1)) / perRow
  const baseline = WMS_ROW_H - 4                          // vertical centring of row text

  let y = startY
  for (let i = 0; i < cards.length; i += WMS_PER_PAGE) {
    const pageCards = cards.slice(i, i + WMS_PER_PAGE)

    // Every group of ≤4 cards gets its own page (first group stays on the page the caller
    // opened). This is what places WMS 4+ on the following page — never mid-card.
    if (i > 0) { doc.addPage() }
    y = i === 0 ? startY : contentTopCont()

    // "WMS Data" heading at the top of each WMS page (continuation pages are marked).
    doc.setTextColor(...INK)
    doc.setFont('helvetica', 'bold')
    doc.setFontSize(11)
    doc.text(i === 0 ? 'WMS Data' : 'WMS Data (continued)', MARGIN, y + 2)
    y += 14

    const maxRows = Math.max(1, ...pageCards.map(c => c.params.length))
    const cardH = WMS_TITLE_H + maxRows * WMS_ROW_H

    pageCards.forEach((card, ci) => {
      const x = MARGIN + ci * (cardW + gap)

      // Title bar (navy) + white station name.
      doc.setFillColor(...NAVY)
      doc.rect(x, y, cardW, WMS_TITLE_H, 'F')
      doc.setTextColor(255, 255, 255)
      doc.setFont('helvetica', 'bold')
      doc.setFontSize(9)
      doc.text(fitText(doc, card.title, cardW - 8), x + 4, y + WMS_TITLE_H - 5)

      // Parameter rows — value always drawn in full (no truncation); only an over-long
      // parameter NAME is ellipsised to fit the space left of its value.
      card.params.forEach((p, ri) => {
        const ry = y + WMS_TITLE_H + ri * WMS_ROW_H
        if (ri % 2 === 1) { doc.setFillColor(...STRIPE); doc.rect(x, ry, cardW, WMS_ROW_H, 'F') }

        doc.setFont('helvetica', 'normal')
        doc.setFontSize(WMS_ROW_FONT)
        const valStr = wmsValStr(p.value)
        const valW = doc.getTextWidth(valStr)
        doc.setTextColor(...INK)
        doc.text(valStr, x + cardW - 4, ry + baseline, { align: 'right' })

        doc.setTextColor(...MUTED)
        doc.text(fitText(doc, String(p.name), cardW - 8 - valW - 6), x + 4, ry + baseline)
      })

      // Card outline.
      doc.setDrawColor(...LINE)
      doc.setLineWidth(0.5)
      doc.rect(x, y, cardW, cardH, 'S')
    })
    y += cardH + 10
  }
  return y
}

// ── DGR inverter-wise generation grid: equal blue blocks (INV1 … INVn), each showing the
// inverter name + its kWh — the SAME blocks the dashboard renders. Laid out as a responsive
// grid (8 columns on landscape) that flows cleanly onto continuation pages when the blocks
// don't all fit, so nothing overlaps and no value is dropped. Values are display-only (the
// caller passes the exact dashboard data). Returns the y below the grid.
const INV_BLUE = [30, 95, 216]   // #1e5fd8 — matches the on-screen inverter boxes
function drawInverterGrid(doc, blocks, startY) {
  if (!blocks?.length) return startY
  const W = doc.internal.pageSize.getWidth()
  const contentW = W - MARGIN * 2
  const cols = Math.min(8, blocks.length)
  const gap = 6
  const boxW = (contentW - gap * (cols - 1)) / cols
  const boxH = 32
  const rowGap = 6

  // Section title above the grid.
  let y = startY
  if (y + 14 + boxH > bottomLimit(doc)) { doc.addPage(); y = contentTopCont() }
  doc.setTextColor(...INK)
  doc.setFont('helvetica', 'bold')
  doc.setFontSize(10.5)
  doc.text('Inverter-wise Generation (kWh)', MARGIN, y + 2)
  y += 12

  blocks.forEach((b, i) => {
    const c = i % cols
    // At the start of each row, break to a new page if the row won't fit above the footer.
    if (c === 0 && i > 0) {
      y += boxH + rowGap
      if (y + boxH > bottomLimit(doc)) { doc.addPage(); y = contentTopCont() }
    }
    const x = MARGIN + c * (boxW + gap)

    doc.setFillColor(...INV_BLUE)
    doc.roundedRect(x, y, boxW, boxH, 2.5, 2.5, 'F')

    // Inverter name (bold, white) then the kWh value beneath it (white). Both auto-fit the
    // block width so long values stay readable and never overflow their block.
    doc.setTextColor(255, 255, 255)
    doc.setFont('helvetica', 'bold')
    doc.setFontSize(8.5)
    doc.text(fitText(doc, b.name, boxW - 8), x + boxW / 2, y + 12.5, { align: 'center' })

    // Value is BOLD too (client request) — both the name and the kWh value stand out.
    const valStr = `${b.value} kWh`
    doc.setFont('helvetica', 'bold')
    doc.setFontSize(7.5)
    doc.text(fitText(doc, valStr, boxW - 6), x + boxW / 2, y + 23, { align: 'center' })
  })

  return y + boxH + 16
}

/**
 * Build and download a report PDF.
 *
 * @param {object} cfg
 * @param {string} cfg.filename
 * @param {string} cfg.plant         plant name (branding)
 * @param {string} cfg.reportTitle   e.g. "Daily Generation Report"
 * @param {string} cfg.subtitle      the applied period, e.g. "08/02/2024"
 * @param {Array}  cfg.meta          [{label, value}] key facts card
 * @param {Array}  cfg.charts        [{title, dataUrl, width, height}]
 * @param {Array}  [cfg.inverterBlocks] [{name, value}] — DGR only: the inverter-wise
 *                                    generation blocks (INV1 … INVn), drawn as a responsive
 *                                    blue grid above the tables; paginates cleanly if needed.
 * @param {Array}  cfg.tables        [{title, columns:[{header,dataKey,align,format}], rows}]
 * @param {Array}  [cfg.wmsCards]    [{title, params:[{name, value}]}] — DGR only: drawn as
 *                                    auto-sized station columns BELOW the table on the SAME
 *                                    page (never paginates), keeping DGR to a single page.
 * @param {string} [cfg.orientation] 'landscape' (default) | 'portrait'
 */
export function renderReportDoc(cfg) {
  const {
    plant, reportTitle, subtitle,
    meta = [], charts = [], inverterBlocks = [], tables = [], wmsCards = [],
    orientation = 'landscape', compress = true, footer = true,
  } = cfg

  const doc = new jsPDF({ orientation, unit: 'pt', format: 'a4', compress })
  const generatedAt = stamp()

  let y = drawMeta(doc, meta, contentTop())
  for (const chart of charts) if (chart?.dataUrl) y = drawChart(doc, chart, y)
  // DGR inverter-wise generation blocks — above the tables (req 5 ordering).
  if (inverterBlocks.length) y = drawInverterGrid(doc, inverterBlocks, y)
  for (const table of tables) y = drawTable(doc, table, y)

  // DGR WMS section — moved to its OWN page (starts on page 2) so the WMS blocks are shown at
  // full, readable size instead of being squeezed under the table. drawWmsCards owns the
  // "WMS Data" heading + the ≤4-cards-per-page layout, adding further pages as needed (whole
  // cards only). Page 1 keeps the inverter grid + Daily Generation table.
  if (wmsCards.length) {
    doc.addPage()
    y = drawWmsCards(doc, wmsCards, contentTopCont())
  }

  // Chrome last, once the total page count is known (enables "Page X of Y"). `footer:false`
  // (DGR) suppresses the footer entirely while keeping the page-1-only header and all pages.
  paintChrome(doc, { plant, reportTitle, subtitle, generatedAt, footer })
  return doc
}

export function buildReportPdf(cfg) {
  renderReportDoc(cfg).save(cfg.filename)
}
