// src/utils/xlsx.js
//
// Tiny, dependency-free .xlsx (OOXML SpreadsheetML) writer. It produces a REAL multi-sheet
// Excel workbook — one worksheet per sheet spec — with bold + frozen header rows, numeric
// cells (kept numeric, never text), a light column-width auto-fit, and per-sheet number
// formats. No external library and no network: the workbook parts are assembled as XML and
// packed into an uncompressed (STORE) ZIP, which Excel opens natively.
//
// Usage:
//   const blob = makeXlsxBlob([
//     { name: 'Monthly Generation', columns: ['Date', 'Inverter 1', …],
//       rows: [['01/05/2024', 1234.56, …], …], numStyle: 2 },   // 2 → 0.00
//     { name: 'WMS Data', columns: ['Date', 'AVG_AIR_PRESSURE', …],
//       rows: [['01/05/2024', 970.123, …], …], numStyle: 3 },   // 3 → 0.000
//   ])
//
// A cell value may be a number (written as a numeric cell), a non-empty string (inline
// string), or null/'' (blank cell). Nothing is truncated — the full numeric value is stored;
// numStyle only controls how many decimals Excel DISPLAYS.

const XLSX_MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

const xesc = s => String(s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;')

// 1-based column index → spreadsheet column letters (1→A, 26→Z, 27→AA …).
function colLetter(n) {
  let s = ''
  while (n > 0) { const m = (n - 1) % 26; s = String.fromCharCode(65 + m) + s; n = Math.floor((n - 1) / 26) }
  return s
}

// ── Print / page layout ──────────────────────────────────────────────────────
// Every worksheet is print-ready: landscape A4, scaled to ONE page wide (height free, so a
// long table simply flows onto further pages). Because each report section lives on its OWN
// worksheet, Excel starts a new PRINT PAGE at each sheet — which is what makes "Export to
// PDF" put the MGR/YGR chart first and the WMS report on the following page, with nothing
// overlapping.
//
// OOXML element order inside <worksheet> is fixed by the schema:
//   sheetPr → sheetViews → sheetFormatPr → cols → sheetData → printOptions → pageMargins
//   → pageSetup → drawing
// `SHEET_PR` therefore goes first, `PAGE_SETUP` after sheetData and BEFORE any <drawing>.
const SHEET_PR = `<sheetPr><pageSetUpPr fitToPage="1"/></sheetPr>`
const PAGE_SETUP =
  `<printOptions horizontalCentered="0"/>` +
  `<pageMargins left="0.4" right="0.4" top="0.5" bottom="0.5" header="0.3" footer="0.3"/>` +
  `<pageSetup paperSize="9" orientation="landscape" fitToWidth="1" fitToHeight="0"/>`

// ── One worksheet part: SECTIONED sheet ──────────────────────────────────────
// A single worksheet that stacks several tables one after another (used by the YGR export so
// the Inverter and WMS reports live in the SAME sheet). Layout:
//   row 1        company title (s=4, left)          — if sheet.title
//   next row     subtitle line, e.g. "Year: 2024 | Inverters: 24" (bold) — if sheet.subtitle
//   (blank)
//   per section: heading (bold) → column header (bold) → data rows → (blank spacer)
// Each section keeps its own numStyle. Column widths auto-fit across every section. Nothing is
// truncated; blanks stay blank. Missing row numbers are legal OOXML blank rows (the spacers).
function sectionedSheetXml(sheet) {
  const sections = sheet.sections
  const maxCols = Math.max(1, ...sections.map(s => s.columns.length))

  const widths = []
  for (let c = 0; c < maxCols; c++) {
    let maxLen = 0
    if (c === 0 && sheet.title) maxLen = Math.max(maxLen, String(sheet.title).length)
    if (c === 0 && sheet.subtitle) maxLen = Math.max(maxLen, String(sheet.subtitle).length)
    for (const sec of sections) {
      const dp = sec.numStyle === 3 ? 3 : 2
      if (c === 0 && sec.heading) maxLen = Math.max(maxLen, String(sec.heading).length)
      if (c < sec.columns.length) maxLen = Math.max(maxLen, String(sec.columns[c] ?? '').length)
      for (const row of sec.rows) {
        const v = row[c]
        let len = 0
        if (v == null || v === '') len = 0
        else if (typeof v === 'number' && Number.isFinite(v)) len = v.toFixed(dp).length
        else len = String(v).length
        if (len > maxLen) maxLen = len
      }
    }
    widths.push(Math.min(42, Math.max(9, maxLen * 1.15 + 2)))
  }
  const colsXml = `<cols>${widths.map((w, i) =>
    `<col min="${i + 1}" max="${i + 1}" width="${w.toFixed(2)}" customWidth="1"/>`).join('')}</cols>`

  const dataCell = (ci, rr, v, numS) => {
    const ref = `${colLetter(ci + 1)}${rr}`
    if (v == null || v === '') return ''
    if (typeof v === 'number' && Number.isFinite(v)) return `<c r="${ref}" s="${numS}"><v>${v}</v></c>`
    return `<c r="${ref}" t="inlineStr"><is><t xml:space="preserve">${xesc(v)}</t></is></c>`
  }
  const boldTextRow = (rr, text) =>
    `<row r="${rr}"><c r="A${rr}" s="1" t="inlineStr"><is><t xml:space="preserve">${xesc(text)}</t></is></c></row>`

  const out = []
  let r = 1
  if (sheet.title) {
    out.push(`<row r="${r}" ht="24" customHeight="1"><c r="A${r}" s="4" t="inlineStr"><is><t xml:space="preserve">${xesc(sheet.title)}</t></is></c></row>`)
    r++
  }
  if (sheet.subtitle) { out.push(boldTextRow(r, sheet.subtitle)); r++ }
  r++   // blank spacer below the header block

  sections.forEach((sec, si) => {
    const numS = sec.numStyle === 3 ? 3 : 2
    if (sec.heading) { out.push(boldTextRow(r, sec.heading)); r++ }
    const hcells = sec.columns.map((h, i) =>
      `<c r="${colLetter(i + 1)}${r}" s="1" t="inlineStr"><is><t xml:space="preserve">${xesc(h)}</t></is></c>`).join('')
    out.push(`<row r="${r}">${hcells}</row>`); r++
    for (const row of sec.rows) {
      const cells = row.map((v, ci) => dataCell(ci, r, v, numS)).join('')
      out.push(`<row r="${r}">${cells}</row>`); r++
    }
    if (si < sections.length - 1) r++   // blank spacer between sections
  })

  // Freeze the title row (if any) so the company title stays pinned; the two section headers
  // scroll with their tables.
  const views = sheet.title
    ? `<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/><selection pane="bottomLeft" activeCell="A2" sqref="A2"/></sheetView></sheetViews>`
    : `<sheetViews><sheetView workbookViewId="0"/></sheetViews>`

  return `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
    `<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">` +
    SHEET_PR + views + `<sheetFormatPr defaultRowHeight="15"/>` + colsXml +
    `<sheetData>${out.join('')}</sheetData>` + PAGE_SETUP + `</worksheet>`
}

// ── One worksheet part ───────────────────────────────────────────────────────
function sheetXml(sheet) {
  if (sheet.sections) return sectionedSheetXml(sheet)
  const dp = sheet.numStyle === 3 ? 3 : 2
  const numS = sheet.numStyle === 3 ? 3 : 2          // cellXf index for numeric cells
  const cols = sheet.columns
  const ncols = cols.length

  // Column widths: widest of the header and the displayed values in that column.
  const widths = []
  for (let c = 0; c < ncols; c++) {
    let maxLen = String(cols[c] ?? '').length
    for (const row of sheet.rows) {
      const v = row[c]
      let len = 0
      if (v == null || v === '') len = 0
      else if (typeof v === 'number' && Number.isFinite(v)) len = v.toFixed(dp).length
      else len = String(v).length
      if (len > maxLen) maxLen = len
    }
    widths.push(Math.min(42, Math.max(9, maxLen * 1.15 + 2)))
  }
  const colsXml = `<cols>${widths.map((w, i) =>
    `<col min="${i + 1}" max="${i + 1}" width="${w.toFixed(2)}" customWidth="1"/>`).join('')}</cols>`

  // Optional report title (company title) at A1, LEFT-aligned (style s=4), NOT merged and NOT
  // centered — left-aligned text overflows the empty cells to its right, so it always starts
  // at Column A regardless of column count. When present, the header shifts to row 2 and data
  // starts at row 3. `hasTitle` toggles the whole offset.
  const hasTitle = !!sheet.title
  const headerRowNum = hasTitle ? 2 : 1
  const firstDataRow = headerRowNum + 1
  const titleRow = hasTitle
    ? `<row r="1" ht="24" customHeight="1"><c r="A1" s="4" t="inlineStr"><is><t xml:space="preserve">${xesc(sheet.title)}</t></is></c></row>`
    : ''

  // Header row — bold (style s=1).
  const headerCells = cols.map((h, i) =>
    `<c r="${colLetter(i + 1)}${headerRowNum}" s="1" t="inlineStr"><is><t xml:space="preserve">${xesc(h)}</t></is></c>`).join('')
  const headerRow = `<row r="${headerRowNum}">${headerCells}</row>`

  const dataRows = sheet.rows.map((row, ri) => {
    const r = ri + firstDataRow
    const cells = row.map((v, ci) => {
      const ref = `${colLetter(ci + 1)}${r}`
      if (v == null || v === '') return ''                          // blank cell
      if (typeof v === 'number' && Number.isFinite(v))
        return `<c r="${ref}" s="${numS}"><v>${v}</v></c>`          // numeric (stays numeric)
      return `<c r="${ref}" t="inlineStr"><is><t xml:space="preserve">${xesc(v)}</t></is></c>`
    }).join('')
    return `<row r="${r}">${cells}</row>`
  }).join('')

  // Freeze the title (if any) + header row so they stay visible while scrolling.
  const topLeft = `A${firstDataRow}`
  const views = `<sheetViews><sheetView workbookViewId="0">` +
    `<pane ySplit="${headerRowNum}" topLeftCell="${topLeft}" activePane="bottomLeft" state="frozen"/>` +
    `<selection pane="bottomLeft" activeCell="${topLeft}" sqref="${topLeft}"/></sheetView></sheetViews>`

  return `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
    `<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">` +
    SHEET_PR +
    views +
    `<sheetFormatPr defaultRowHeight="15"/>` +
    colsXml +
    `<sheetData>${titleRow}${headerRow}${dataRows}</sheetData>` +
    PAGE_SETUP +
    `</worksheet>`
}

// ── Fixed workbook parts ─────────────────────────────────────────────────────
function contentTypesXml(n) {
  const overrides = Array.from({ length: n }, (_, i) =>
    `<Override PartName="/xl/worksheets/sheet${i + 1}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>`).join('')
  return `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
    `<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">` +
    `<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>` +
    `<Default Extension="xml" ContentType="application/xml"/>` +
    `<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>` +
    `<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>` +
    overrides + `</Types>`
}

const ROOT_RELS = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
  `<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">` +
  `<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>` +
  `</Relationships>`

function workbookXml(sheets) {
  const s = sheets.map((sh, i) =>
    `<sheet name="${xesc(sh.name)}" sheetId="${i + 1}" r:id="rId${i + 1}"/>`).join('')
  return `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
    `<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" ` +
    `xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">` +
    `<sheets>${s}</sheets></workbook>`
}

function workbookRelsXml(n) {
  const sheetRels = Array.from({ length: n }, (_, i) =>
    `<Relationship Id="rId${i + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet${i + 1}.xml"/>`).join('')
  const stylesRel = `<Relationship Id="rId${n + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>`
  return `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
    `<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">` +
    sheetRels + stylesRel + `</Relationships>`
}

// Styles: s=0 normal, s=1 bold (headers), s=2 numFmt 0.00, s=3 numFmt 0.000,
// s=4 report title (bold 16pt, LEFT-aligned) — the company title at the top-left of each sheet.
const STYLES_XML = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
  `<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">` +
  `<numFmts count="2"><numFmt numFmtId="164" formatCode="0.00"/><numFmt numFmtId="165" formatCode="0.000"/></numFmts>` +
  `<fonts count="3"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="16"/><name val="Calibri"/></font></fonts>` +
  `<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>` +
  `<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>` +
  `<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>` +
  `<cellXfs count="5">` +
  `<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>` +
  `<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>` +
  `<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>` +
  `<xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>` +
  `<xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1" applyAlignment="1"><alignment horizontal="left" vertical="center"/></xf>` +
  `</cellXfs>` +
  `<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>` +
  `</styleSheet>`

// ── Minimal STORE (uncompressed) ZIP ─────────────────────────────────────────
const CRC_TABLE = (() => {
  const t = new Uint32Array(256)
  for (let n = 0; n < 256; n++) {
    let c = n
    for (let k = 0; k < 8; k++) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1)
    t[n] = c >>> 0
  }
  return t
})()
function crc32(bytes) {
  let crc = 0xFFFFFFFF
  for (let i = 0; i < bytes.length; i++) crc = CRC_TABLE[(crc ^ bytes[i]) & 0xFF] ^ (crc >>> 8)
  return (crc ^ 0xFFFFFFFF) >>> 0
}

function zipStore(files) {
  const enc = new TextEncoder()
  const u16 = n => [n & 0xff, (n >>> 8) & 0xff]
  const u32 = n => [n & 0xff, (n >>> 8) & 0xff, (n >>> 16) & 0xff, (n >>> 24) & 0xff]
  const chunks = []
  const central = []
  let offset = 0

  for (const f of files) {
    const nameBytes = enc.encode(f.name)
    const data = f.data
    const crc = crc32(data)
    const local = [
      ...u32(0x04034b50), ...u16(20), ...u16(0), ...u16(0),   // sig, ver, flags, method=0 (store)
      ...u16(0), ...u16(0),                                   // mod time, date
      ...u32(crc), ...u32(data.length), ...u32(data.length),  // crc, comp size, uncomp size
      ...u16(nameBytes.length), ...u16(0),                    // name len, extra len
    ]
    chunks.push(new Uint8Array(local), nameBytes, data)
    central.push({ name: nameBytes, crc, size: data.length, offset })
    offset += local.length + nameBytes.length + data.length
  }

  const cdStart = offset
  for (const c of central) {
    const rec = [
      ...u32(0x02014b50), ...u16(20), ...u16(20), ...u16(0), ...u16(0),  // sig, verMade, verNeed, flags, method
      ...u16(0), ...u16(0),                                              // time, date
      ...u32(c.crc), ...u32(c.size), ...u32(c.size),                     // crc, comp, uncomp
      ...u16(c.name.length), ...u16(0), ...u16(0),                       // name, extra, comment
      ...u16(0), ...u16(0), ...u32(0), ...u32(c.offset),                 // disk, intAttr, extAttr, local offset
    ]
    chunks.push(new Uint8Array(rec), c.name)
    offset += rec.length + c.name.length
  }

  const end = [
    ...u32(0x06054b50), ...u16(0), ...u16(0),
    ...u16(central.length), ...u16(central.length),
    ...u32(offset - cdStart), ...u32(cdStart), ...u16(0),
  ]
  chunks.push(new Uint8Array(end))
  return new Blob(chunks, { type: XLSX_MIME })
}

// ── Chart workbook: ONE sheet, a native bar chart + stacked tables ───────────
// Used by the MGR Excel export. Layout on a single sheet:
//   row 1  company title (s=4)
//   row 2  subtitle, e.g. "Year: 2024" (bold)
//   row 4  graph heading (bold)
//   row 5  graph data header  [catHeader | valHeader]   (bold)
//   row 6… graph data rows     [category  | value]       ← the bar chart's source cells
//          a native clustered bar chart floats to the RIGHT of that little table
//   then   each section: heading (bold) → column header (bold) → data rows → blank
// The chart is a REAL, editable Excel chart (c:barChart) that references the graph cells — no
// image, nothing "linked". Values are visualised, never changed. Returns a Blob.
//
// `sheet.extraSheets` (optional) is a list of ordinary sheet specs appended as worksheets
// 2..N of the SAME workbook. Use it for any section that must not share the chart's sheet —
// each extra worksheet is also its own print page, so it starts on a new page when printed
// or exported to PDF.
export function makeChartWorkbookBlob(sheet) {
  const enc = new TextEncoder()
  const g = sheet.graph
  const cats = g.cats || []
  const vals = g.vals || []
  const K = cats.length
  const numSg = g.numStyle === 3 ? 3 : 2
  const sections = sheet.sections || []
  const maxCols = Math.max(2, ...sections.map(s => s.columns.length))
  const qName = sheet.name.replace(/'/g, "''")           // sheet name for chart formulas

  // ── column widths across the graph table + every section ──
  const widths = []
  for (let c = 0; c < maxCols; c++) {
    let maxLen = 0
    if (c === 0 && sheet.title) maxLen = Math.max(maxLen, String(sheet.title).length)
    if (c === 0 && sheet.subtitle) maxLen = Math.max(maxLen, String(sheet.subtitle).length)
    if (c < 2) {
      maxLen = Math.max(maxLen, String([g.catHeader, g.valHeader][c] ?? '').length)
      for (const v of (c === 0 ? cats : vals)) {
        const len = v == null ? 0 : (typeof v === 'number' ? v.toFixed(numSg).length : String(v).length)
        if (len > maxLen) maxLen = len
      }
    }
    for (const sec of sections) {
      const dp = sec.numStyle === 3 ? 3 : 2
      if (c === 0 && sec.heading) maxLen = Math.max(maxLen, String(sec.heading).length)
      if (c < sec.columns.length) maxLen = Math.max(maxLen, String(sec.columns[c] ?? '').length)
      for (const row of sec.rows) {
        const v = row[c]
        const len = v == null || v === '' ? 0
          : (typeof v === 'number' && Number.isFinite(v) ? v.toFixed(dp).length : String(v).length)
        if (len > maxLen) maxLen = len
      }
    }
    widths.push(Math.min(42, Math.max(9, maxLen * 1.15 + 2)))
  }
  const colsXml = `<cols>${widths.map((w, i) =>
    `<col min="${i + 1}" max="${i + 1}" width="${w.toFixed(2)}" customWidth="1"/>`).join('')}</cols>`

  const numS = ns => (ns === 3 ? 3 : 2)
  const dataCell = (ci, rr, v, ns) => {
    const ref = `${colLetter(ci + 1)}${rr}`
    if (v == null || v === '') return ''
    if (typeof v === 'number' && Number.isFinite(v)) return `<c r="${ref}" s="${ns}"><v>${v}</v></c>`
    return `<c r="${ref}" t="inlineStr"><is><t xml:space="preserve">${xesc(v)}</t></is></c>`
  }
  const boldRow = (rr, text) =>
    `<row r="${rr}"><c r="A${rr}" s="1" t="inlineStr"><is><t xml:space="preserve">${xesc(text)}</t></is></c></row>`

  const out = []
  let r = 1
  out.push(`<row r="${r}" ht="24" customHeight="1"><c r="A${r}" s="4" t="inlineStr"><is><t xml:space="preserve">${xesc(sheet.title)}</t></is></c></row>`); r++
  if (sheet.subtitle) { out.push(boldRow(r, sheet.subtitle)); r++ }
  r++                                                   // blank spacer
  out.push(boldRow(r, sheet.graphHeading)); r++
  // graph data header + rows (the chart's source range)
  out.push(`<row r="${r}"><c r="A${r}" s="1" t="inlineStr"><is><t xml:space="preserve">${xesc(g.catHeader)}</t></is></c><c r="B${r}" s="1" t="inlineStr"><is><t xml:space="preserve">${xesc(g.valHeader)}</t></is></c></row>`); r++
  const graphFirst = r
  for (let i = 0; i < K; i++) {
    out.push(`<row r="${r}">${dataCell(0, r, cats[i], 0)}${dataCell(1, r, vals[i], numSg)}</row>`); r++
  }
  const graphLast = Math.max(graphFirst, r - 1)

  // Reserve space for the floating chart, then continue below whichever is taller (chart / table).
  const chartHeightRows = Math.max(15, K)
  const chartBottom1 = 4 + chartHeightRows                // chart occupies rows 4..chartBottom1
  r = Math.max(graphLast, chartBottom1) + 2

  sections.forEach((sec, si) => {
    const ns = numS(sec.numStyle)
    if (sec.heading) { out.push(boldRow(r, sec.heading)); r++ }
    out.push(`<row r="${r}">${sec.columns.map((h, i) =>
      `<c r="${colLetter(i + 1)}${r}" s="1" t="inlineStr"><is><t xml:space="preserve">${xesc(h)}</t></is></c>`).join('')}</row>`); r++
    for (const row of sec.rows) {
      out.push(`<row r="${r}">${row.map((v, ci) => dataCell(ci, r, v, ns)).join('')}</row>`); r++
    }
    if (si < sections.length - 1) r++
  })

  const views = `<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/><selection pane="bottomLeft" activeCell="A2" sqref="A2"/></sheetView></sheetViews>`
  // <drawing> is the LAST child of <worksheet>, so the print settings are emitted just before
  // it (schema order: sheetData → printOptions → pageMargins → pageSetup → drawing).
  const wsXml = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
    `<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" ` +
    `xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">` +
    SHEET_PR + views + `<sheetFormatPr defaultRowHeight="15"/>` + colsXml +
    `<sheetData>${out.join('')}</sheetData>` + PAGE_SETUP + `<drawing r:id="rId1"/></worksheet>`

  // ── chart1.xml: clustered column (bar) chart referencing the graph cells ──
  const catRange = `'${qName}'!$A$${graphFirst}:$A$${graphLast}`
  const valRange = `'${qName}'!$B$${graphFirst}:$B$${graphLast}`
  const fmtCode = numSg === 3 ? '0.000' : '0.00'
  const strPts = cats.map((c, i) => `<c:pt idx="${i}"><c:v>${xesc(c)}</c:v></c:pt>`).join('')
  const numPts = vals.map((v, i) =>
    (v == null || !Number.isFinite(Number(v))) ? '' : `<c:pt idx="${i}"><c:v>${Number(v)}</c:v></c:pt>`).join('')
  const barColor = sheet.barColor || '1E5FD8'
  const chartXml = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
    `<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" ` +
    `xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" ` +
    `xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">` +
    `<c:chart>` +
    `<c:title><c:tx><c:rich><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>${xesc(sheet.chartTitle || sheet.graphHeading)}</a:t></a:r></a:p></c:rich></c:tx><c:overlay val="0"/></c:title>` +
    `<c:autoTitleDeleted val="0"/>` +
    `<c:plotArea><c:layout/>` +
    `<c:barChart><c:barDir val="col"/><c:grouping val="clustered"/><c:varyColors val="0"/>` +
    `<c:ser><c:idx val="0"/><c:order val="0"/>` +
    `<c:tx><c:strRef><c:f>'${qName}'!$B$${graphFirst - 1}</c:f><c:strCache><c:ptCount val="1"/><c:pt idx="0"><c:v>${xesc(g.valHeader)}</c:v></c:pt></c:strCache></c:strRef></c:tx>` +
    `<c:spPr><a:solidFill><a:srgbClr val="${barColor}"/></a:solidFill></c:spPr>` +
    `<c:cat><c:strRef><c:f>${xesc(catRange)}</c:f><c:strCache><c:ptCount val="${K}"/>${strPts}</c:strCache></c:strRef></c:cat>` +
    `<c:val><c:numRef><c:f>${xesc(valRange)}</c:f><c:numCache><c:formatCode>${fmtCode}</c:formatCode><c:ptCount val="${K}"/>${numPts}</c:numCache></c:numRef></c:val>` +
    `</c:ser>` +
    `<c:axId val="111111111"/><c:axId val="222222222"/>` +
    `</c:barChart>` +
    `<c:catAx><c:axId val="111111111"/><c:scaling><c:orientation val="minMax"/></c:scaling><c:delete val="0"/><c:axPos val="b"/><c:crossAx val="222222222"/></c:catAx>` +
    `<c:valAx><c:axId val="222222222"/><c:scaling><c:orientation val="minMax"/></c:scaling><c:delete val="0"/><c:axPos val="l"/><c:numFmt formatCode="${fmtCode}" sourceLinked="0"/><c:crossAx val="111111111"/></c:valAx>` +
    `</c:plotArea>` +
    `<c:legend><c:legendPos val="b"/><c:overlay val="0"/></c:legend>` +
    `<c:plotVisOnly val="1"/><c:dispBlanksAs val="gap"/>` +
    `</c:chart></c:chartSpace>`

  // ── drawing1.xml: anchor the chart over D4:N(chartBottom) ──
  const drawingXml = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
    `<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" ` +
    `xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">` +
    `<xdr:twoCellAnchor>` +
    `<xdr:from><xdr:col>3</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>3</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>` +
    `<xdr:to><xdr:col>13</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>${chartBottom1}</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to>` +
    `<xdr:graphicFrame macro="">` +
    `<xdr:nvGraphicFramePr><xdr:cNvPr id="2" name="InverterChart"/><xdr:cNvGraphicFramePr/></xdr:nvGraphicFramePr>` +
    `<xdr:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/></xdr:xfrm>` +
    `<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/chart">` +
    `<c:chart xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" ` +
    `xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" r:id="rId1"/>` +
    `</a:graphicData></a:graphic>` +
    `</xdr:graphicFrame><xdr:clientData/></xdr:twoCellAnchor></xdr:wsDr>`

  // Any FURTHER worksheets (sheet.extraSheets) become sheets 2..N of the same workbook, each
  // rendered by the ordinary sheet writer. Sheet 1 keeps the chart; the extras carry no
  // drawing, so a section moved into one is guaranteed not to overlap the chart.
  const extras = sheet.extraSheets || []
  const nSheets = 1 + extras.length

  const ctypes = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
    `<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">` +
    `<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>` +
    `<Default Extension="xml" ContentType="application/xml"/>` +
    `<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>` +
    `<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>` +
    Array.from({ length: nSheets }, (_, i) =>
      `<Override PartName="/xl/worksheets/sheet${i + 1}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>`).join('') +
    `<Override PartName="/xl/drawings/drawing1.xml" ContentType="application/vnd.openxmlformats-officedocument.drawing+xml"/>` +
    `<Override PartName="/xl/charts/chart1.xml" ContentType="application/vnd.openxmlformats-officedocument.drawingml.chart+xml"/>` +
    `</Types>`

  const sheetRels = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
    `<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">` +
    `<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing" Target="../drawings/drawing1.xml"/>` +
    `</Relationships>`
  const drawingRels = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
    `<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">` +
    `<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart" Target="../charts/chart1.xml"/>` +
    `</Relationships>`

  const parts = [
    { name: '[Content_Types].xml', data: enc.encode(ctypes) },
    { name: '_rels/.rels', data: enc.encode(ROOT_RELS) },
    { name: 'xl/workbook.xml', data: enc.encode(workbookXml([sheet, ...extras])) },
    { name: 'xl/_rels/workbook.xml.rels', data: enc.encode(workbookRelsXml(nSheets)) },
    { name: 'xl/styles.xml', data: enc.encode(STYLES_XML) },
    { name: 'xl/worksheets/sheet1.xml', data: enc.encode(wsXml) },
    { name: 'xl/worksheets/_rels/sheet1.xml.rels', data: enc.encode(sheetRels) },
    ...extras.map((s, i) => ({
      name: `xl/worksheets/sheet${i + 2}.xml`, data: enc.encode(sheetXml(s)),
    })),
    { name: 'xl/drawings/drawing1.xml', data: enc.encode(drawingXml) },
    { name: 'xl/drawings/_rels/drawing1.xml.rels', data: enc.encode(drawingRels) },
    { name: 'xl/charts/chart1.xml', data: enc.encode(chartXml) },
  ]
  return zipStore(parts)
}

// ── Public API ───────────────────────────────────────────────────────────────
export function makeXlsxBlob(sheets) {
  const enc = new TextEncoder()
  const parts = [
    { name: '[Content_Types].xml', data: enc.encode(contentTypesXml(sheets.length)) },
    { name: '_rels/.rels', data: enc.encode(ROOT_RELS) },
    { name: 'xl/workbook.xml', data: enc.encode(workbookXml(sheets)) },
    { name: 'xl/_rels/workbook.xml.rels', data: enc.encode(workbookRelsXml(sheets.length)) },
    { name: 'xl/styles.xml', data: enc.encode(STYLES_XML) },
    ...sheets.map((s, i) => ({ name: `xl/worksheets/sheet${i + 1}.xml`, data: enc.encode(sheetXml(s)) })),
  ]
  return zipStore(parts)
}
