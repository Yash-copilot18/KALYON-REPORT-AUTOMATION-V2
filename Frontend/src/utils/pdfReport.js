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
function paintChrome(doc, { plant, reportTitle, subtitle, generatedAt }) {
  const pages = doc.internal.getNumberOfPages()
  const W = doc.internal.pageSize.getWidth()
  const H = doc.internal.pageSize.getHeight()

  for (let p = 1; p <= pages; p++) {
    doc.setPage(p)

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

    // Footer
    doc.setDrawColor(...LINE)
    doc.setLineWidth(0.6)
    doc.line(MARGIN, H - FOOTER_H, W - MARGIN, H - FOOTER_H)
    doc.setTextColor(...MUTED)
    doc.setFont('helvetica', 'normal')
    doc.setFontSize(8)
    doc.text(`Generated ${generatedAt}`, MARGIN, H - FOOTER_H + 14)
    doc.text('Kalyon Solar Monitoring — Report Automation', W / 2, H - FOOTER_H + 14, { align: 'center' })
    doc.text(`Page ${p} of ${pages}`, W - MARGIN, H - FOOTER_H + 14, { align: 'right' })
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
const contentTop = () => HEADER_H + 16

// ── Chart image, scaled to content width, page-broken if needed ──────────────
function drawChart(doc, chart, y) {
  const W = doc.internal.pageSize.getWidth()
  const contentW = W - MARGIN * 2
  const drawW = contentW
  const drawH = (chart.height / chart.width) * drawW

  if (y + drawH + 24 > bottomLimit(doc)) {
    doc.addPage()
    y = contentTop()
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

// ── One table via autotable (headers repeat on every page automatically) ─────
function drawTable(doc, table, startY) {
  const W = doc.internal.pageSize.getWidth()
  // Title sits just above the table; if it won't fit, start on a fresh page.
  let y = startY
  if (y + 60 > bottomLimit(doc)) { doc.addPage(); y = contentTop() }
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
  const columnStyles = {}
  table.columns.forEach(c => {
    if (c.align) columnStyles[c.dataKey] = { halign: c.align }
  })

  autoTable(doc, {
    columns,
    body,
    startY: y,
    margin: { top: contentTop(), bottom: FOOTER_H + 12, left: MARGIN, right: MARGIN },
    theme: 'grid',
    tableWidth: W - MARGIN * 2,
    styles: { font: 'helvetica', fontSize: 8, cellPadding: 3.5, textColor: INK,
              lineColor: LINE, lineWidth: 0.4, overflow: 'linebreak' },
    headStyles: { fillColor: NAVY, textColor: ACCENT, fontStyle: 'bold', fontSize: 8.5,
                  halign: 'center', lineColor: NAVY },
    alternateRowStyles: { fillColor: STRIPE },
    columnStyles,
    // A repeated header is autotable's default; footStyles/showFoot left off.
  })
  return doc.lastAutoTable.finalY + 16
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
 * @param {Array}  cfg.tables        [{title, columns:[{header,dataKey,align,format}], rows}]
 * @param {string} [cfg.orientation] 'landscape' (default) | 'portrait'
 */
export function renderReportDoc(cfg) {
  const {
    plant, reportTitle, subtitle,
    meta = [], charts = [], tables = [], orientation = 'landscape', compress = true,
  } = cfg

  const doc = new jsPDF({ orientation, unit: 'pt', format: 'a4', compress })
  const generatedAt = stamp()

  let y = drawMeta(doc, meta, contentTop())
  for (const chart of charts) if (chart?.dataUrl) y = drawChart(doc, chart, y)
  for (const table of tables) y = drawTable(doc, table, y)

  // Chrome last, once the total page count is known (enables "Page X of Y").
  paintChrome(doc, { plant, reportTitle, subtitle, generatedAt })
  return doc
}

export function buildReportPdf(cfg) {
  renderReportDoc(cfg).save(cfg.filename)
}
