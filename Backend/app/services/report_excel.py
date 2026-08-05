# app/services/report_excel.py
"""
Single, shared Excel export service for the Reports module.

Every /export/excel path funnels through here — there is exactly ONE professional
sheet renderer and ONE workbook builder, so formatting never drifts and there are
no duplicate export code paths.

Design:
  • XlsxWriter in `constant_memory` mode — formats are created once and reused,
    rows stream to a temp buffer (low memory) even for hundreds of worksheets.
  • `build_workbook()` consumes a GENERATOR of sheet-specs. Each producer fetches
    one unit's data at a time and yields a spec, so only one sheet's rows are ever
    held in memory (req: stream rows directly into Excel).
  • Producers:
      - `iter_equipment_sheets`  → one worksheet per equipment (Inverter_01 …)
      - the String Combiner producer (smb_excel) → one worksheet per SMB
    both render through `write_report_sheet`, so the layout is identical.

Layout (per sheet): company header, report title, metadata block, bold/centered
unit-labelled headers, zebra data rows, borders, frozen header, auto-filter,
auto-fit columns, DD/MM/YYYY HH:MM:SS timestamps, 3-decimal numbers.
"""

import io
import time
import logging
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import xlsxwriter

from app.database.session import SessionLocal
from app.repositories.reports_repository import ReportsRepository
from app.services import intervals

logger = logging.getLogger(__name__)

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
COMPANY_TITLE = "Kalyon Solar Monitoring — Report Automation"

_MAX_ROWS      = 10_000     # per-sheet row cap (bounds memory / matches preview export)
_FETCH_WORKERS = 5          # concurrent equipment fetches

# ── Palette — clean white/black corporate theme (printer-friendly) ───────────
BLACK    = "#000000"     # every title, header and data value
WHITE    = "#FFFFFF"     # sheet / primary row background
GRAY_HDR = "#F2F2F2"     # section + table header fill
ROW_ALT  = "#FAFAFA"     # alternating data row
BORDER   = "#000000"     # thin black table borders

_INVALID_SHEET = set(r":\/?*[]")


# ── Formatting helpers ───────────────────────────────────────────────────────
def make_formats(wb) -> Dict:
    """Every cell format created once and reused across all sheets/cells."""
    base = {"border": 1, "border_color": BORDER}

    def data(align, bg, num=False):
        d = {"font_name": "Calibri", "font_size": 9, "font_color": BLACK,
             "align": align, "valign": "vcenter", "bg_color": bg, **base}
        if num:
            d["num_format"] = "#,##0.000"
        return wb.add_format(d)

    return {
        # Report title — bold 16pt black, left-aligned, no fill (white sheet).
        "title": wb.add_format({"font_name": "Calibri", "font_size": 16, "bold": True,
                                "font_color": BLACK, "align": "left", "valign": "vcenter"}),
        "subtitle": wb.add_format({"font_name": "Calibri", "font_size": 11, "bold": True,
                                   "font_color": BLACK, "align": "left", "valign": "vcenter"}),
        # Section band (e.g. "SMB1") — light gray fill, bold black text.
        "section": wb.add_format({"font_name": "Calibri", "font_size": 11, "bold": True,
                                  "font_color": BLACK, "bg_color": GRAY_HDR,
                                  "align": "left", "valign": "vcenter", **base}),
        # Metadata — plain black label/value, left-aligned, no fill or border.
        "meta_lbl": wb.add_format({"font_name": "Calibri", "font_size": 9, "bold": True,
                                   "font_color": BLACK, "align": "left", "valign": "vcenter"}),
        "meta": wb.add_format({"font_name": "Calibri", "font_size": 9, "font_color": BLACK,
                               "align": "left", "valign": "vcenter"}),
        # Column headers — light gray fill, bold black, thin black border.
        "hdr": wb.add_format({"font_name": "Calibri", "bold": True, "font_color": BLACK,
                              "bg_color": GRAY_HDR, "align": "center", "valign": "vcenter",
                              "text_wrap": True, **base}),
        "foot": wb.add_format({"font_name": "Calibri", "font_size": 8, "font_color": BLACK,
                               "align": "left", "valign": "vcenter"}),
        "nodata": wb.add_format({"font_name": "Calibri", "bold": True, "font_color": BLACK,
                                 "bg_color": WHITE, "align": "left", "valign": "vcenter", **base}),
        # Data rows alternate white / #FAFAFA; numbers right, text left.
        "ts_even": data("left", WHITE),   "ts_odd": data("left", ROW_ALT),
        "num_even": data("right", WHITE, num=True), "num_odd": data("right", ROW_ALT, num=True),
        "int_even": data("right", WHITE), "int_odd": data("right", ROW_ALT),
        "txt_even": data("left", WHITE),  "txt_odd": data("left", ROW_ALT),
        "na_even": data("center", WHITE), "na_odd": data("center", ROW_ALT),
    }


def fmt_ts(val) -> str:
    """Any timestamp → DD/MM/YYYY HH:MM:SS."""
    if not val:
        return ""
    if isinstance(val, datetime):
        return val.strftime("%d/%m/%Y %H:%M:%S")
    s = str(val).replace("T", " ").replace("Z", "").strip().split(".")[0]
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, f).strftime("%d/%m/%Y %H:%M:%S")
        except Exception:
            continue
    return s


def safe_sheet_name(name: str, used: set) -> str:
    """Excel-legal (≤31 chars), unique worksheet name."""
    s = "".join("_" if ch in _INVALID_SHEET else ch for ch in str(name)).strip() or "Sheet"
    s = s[:31]
    base, i = s, 1
    while s.lower() in used:
        suffix = f"_{i}"
        s = base[:31 - len(suffix)] + suffix
        i += 1
    used.add(s.lower())
    return s


def _meta_fields(header: dict):
    """
    Ordered (label, value) metadata pairs shared by both sheet renderers.

    A caller may override the exact rows by putting a `meta_fields` list of
    (label, value) tuples on the header (e.g. to drop the Aggregation row for a
    specific report); when absent, the default six-row block is used, so every
    existing caller is unaffected.
    """
    custom = header.get("meta_fields")
    if custom is not None:
        return list(custom)
    return [
        ("Equipment Type", header.get("equipment_type", "")),
        ("Equipment ID",   header.get("equipment_id", "")),
        ("From",           header.get("from", "")),
        ("To",             header.get("to", "")),
        ("Interval",       header.get("interval", "")),
        ("Aggregation",    header.get("agg", "")),
    ]


# ── The one professional sheet renderer ──────────────────────────────────────
def _apply_wide_print_layout(ws, hdr_row: int, last_row: int, last_col: int,
                             sheet_name: str) -> None:
    """
    Print setup for a WIDE single-table sheet (e.g. the 336-column SMB export).

    No scale-to-fit is attempted: a few hundred columns cannot be squeezed onto one
    page at a legible size. Instead the sheet is made navigable across the pages it
    genuinely needs — the header row repeats down the pages and the timestamp column
    repeats across them, so no page is ever a block of numbers with no time
    reference. Opt-in only; narrow reports keep their existing print behaviour.
    """
    ws.set_landscape()
    ws.set_paper(_PAPER_A4)
    ws.set_margins(left=_MARGIN_LR, right=_MARGIN_LR, top=_MARGIN_TB, bottom=_MARGIN_TB)
    ws.print_area(0, 0, max(last_row, hdr_row), last_col)
    ws.repeat_rows(hdr_row, hdr_row)     # column headers on every page down
    ws.repeat_columns(0, 0)              # timestamp column on every page across
    ws.set_footer(f"&L&\"Calibri,Regular\"&8{sheet_name}"
                  f"&R&\"Calibri,Regular\"&8Page &P of &N")


def write_report_sheet(wb, fmt, sheet_name: str, header: dict,
                       columns: List[str], rows: List[dict],
                       label_fn: Callable[[str], str],
                       wide_print_layout: bool = False) -> None:
    """
    Render one worksheet. Empty rows → 'No Data Available' (never skipped).

    `wide_print_layout` opts into print settings tuned for very wide tables; it
    defaults off so every existing caller renders exactly as before.
    """
    ws = wb.add_worksheet(sheet_name)
    n = max(len(columns), 1)
    last_col = n - 1

    # Auto-fit widths (computed up front — constant_memory needs set_column early).
    widths = []
    for col in columns:
        w = max((len(x) for x in label_fn(col).split()), default=8) + 2
        for row in rows:
            v = row.get(col)
            if col == "timestamp":
                s = fmt_ts(v) if v else ""
            elif isinstance(v, float):
                s = f"{v:.3f}"
            elif v is not None:
                s = str(v)
            else:
                s = ""
            w = max(w, len(s))
        widths.append(min(max(w + 1, 12), 40))
    for ci, w in enumerate(widths):
        ws.set_column(ci, ci, w)

    # Row 0 — report title · Row 1 — subtitle. Written to column A (no merged
    # cells); left-aligned text simply overflows the empty cells beside it.
    ws.write(0, 0, COMPANY_TITLE, fmt["title"]); ws.set_row(0, 24)
    ws.write(1, 0, header.get("subtitle", ""), fmt["subtitle"]); ws.set_row(1, 18)

    # Rows 3–8 — metadata as label | value pairs, left-aligned, no merged cells.
    for i, (k, v) in enumerate(_meta_fields(header)):
        r = 3 + i
        ws.write(r, 0, k, fmt["meta_lbl"])
        ws.write(r, 1, str(v), fmt["meta"])
        ws.set_row(r, 14)

    HDR = 10
    for ci, col in enumerate(columns):
        ws.write(HDR, ci, label_fn(col), fmt["hdr"])
    ws.set_row(HDR, 30)
    ws.freeze_panes(HDR + 1, 0)

    if not rows:
        ws.write(HDR + 1, 0, "No Data Available", fmt["nodata"])
        if wide_print_layout:
            _apply_wide_print_layout(ws, HDR, HDR + 1, last_col, sheet_name)
        return

    r = HDR + 1
    for i, row in enumerate(rows):
        even = (i % 2 == 0)
        for ci, col in enumerate(columns):
            v = row.get(col)
            if col == "timestamp":
                ws.write(r, ci, fmt_ts(v) if v else "—", fmt["ts_even" if even else "ts_odd"])
            elif v is None:
                ws.write(r, ci, "—", fmt["na_even" if even else "na_odd"])
            elif isinstance(v, bool):
                ws.write(r, ci, int(v), fmt["int_even" if even else "int_odd"])
            elif isinstance(v, (int, float)):
                ws.write_number(r, ci, float(v), fmt["num_even" if even else "num_odd"])
            else:
                ws.write(r, ci, str(v), fmt["txt_even" if even else "txt_odd"])
        r += 1

    ws.write(r, 0,
             f"{len(rows):,} records  ·  Generated {time.strftime('%d/%m/%Y %H:%M:%S')}",
             fmt["foot"])
    ws.autofilter(HDR, 0, r - 1, last_col)

    if wide_print_layout:
        _apply_wide_print_layout(ws, HDR, r, last_col, sheet_name)


# ── Streaming sheet renderer (rows consumed lazily from a generator) ─────────
def write_report_sheet_streaming(wb, fmt, sheet_name: str, header: dict,
                                 columns: List[str], rows_iter,
                                 label_fn: Callable[[str], str],
                                 wide_print_layout: bool = False) -> int:
    """
    Byte-for-byte the SAME sheet as `write_report_sheet`, but `rows_iter` is an
    ITERATOR yielded lazily (e.g. a batched DB reader) instead of a materialised
    list — so a sheet of any size uses constant memory. Crucially it is also a
    SINGLE pass: `write_report_sheet` measures every cell once for auto-fit widths
    and then again to write it (two passes); this formats each cell once, growing
    the width as it writes — halving the per-cell Python work for the same output.

    Column auto-fit still matches exactly: widths are seeded from the header labels
    and grown per cell as rows stream by (O(columns) memory), then applied with
    `set_column` at the end. XlsxWriter serialises column info at close(), so a
    deferred set_column in constant_memory mode is honoured (verified).

    `wide_print_layout` mirrors the same option on `write_report_sheet` so callers
    that use it (e.g. the String Combiner export) get identical print setup.

    Returns the number of data rows written (for logging / progress).
    """
    ws = wb.add_worksheet(sheet_name)
    n = max(len(columns), 1)
    last_col = n - 1

    # Width seed = the header label (same as the batch renderer's starting point).
    widths = [max((len(x) for x in label_fn(col).split()), default=8) + 2 for col in columns]

    # Report header block — identical rows/heights to write_report_sheet.
    ws.write(0, 0, COMPANY_TITLE, fmt["title"]); ws.set_row(0, 24)
    ws.write(1, 0, header.get("subtitle", ""), fmt["subtitle"]); ws.set_row(1, 18)
    for i, (k, v) in enumerate(_meta_fields(header)):
        r = 3 + i
        ws.write(r, 0, k, fmt["meta_lbl"])
        ws.write(r, 1, str(v), fmt["meta"])
        ws.set_row(r, 14)

    HDR = 10
    for ci, col in enumerate(columns):
        ws.write(HDR, ci, label_fn(col), fmt["hdr"])
    ws.set_row(HDR, 30)
    ws.freeze_panes(HDR + 1, 0)

    # Hot-path locals: on a large export this loop runs millions of times, so binding
    # the two write methods and the (even, odd) format pairs to locals removes a
    # method-attribute lookup and a dict lookup from EVERY cell. Output is unchanged —
    # only the number of Python name lookups drops.
    _write, _write_number = ws.write, ws.write_number
    _ts  = (fmt["ts_even"],  fmt["ts_odd"])
    _na  = (fmt["na_even"],  fmt["na_odd"])
    _int = (fmt["int_even"], fmt["int_odd"])
    _num = (fmt["num_even"], fmt["num_odd"])
    _txt = (fmt["txt_even"], fmt["txt_odd"])

    r = HDR + 1
    count = 0
    for row in rows_iter:
        p = count & 1                 # even row → 0, odd row → 1 (indexes the fmt pair)
        f_ts, f_na, f_int, f_num, f_txt = _ts[p], _na[p], _int[p], _num[p], _txt[p]
        get = row.get
        for ci, col in enumerate(columns):
            v = get(col)
            # `s` is the width-measuring string — computed identically to the batch
            # renderer's auto-fit pass so column widths come out the same.
            if col == "timestamp":
                tv = fmt_ts(v) if v else ""     # format once, reuse for width + cell
                s = tv
                _write(r, ci, tv if v else "—", f_ts)
            elif v is None:
                s = ""
                _write(r, ci, "—", f_na)
            elif isinstance(v, bool):
                s = str(v)
                _write(r, ci, int(v), f_int)
            elif isinstance(v, (int, float)):
                s = f"{v:.3f}" if isinstance(v, float) else str(v)
                _write_number(r, ci, float(v), f_num)
            else:
                s = str(v)
                _write(r, ci, str(v), f_txt)
            if len(s) > widths[ci]:
                widths[ci] = len(s)
        r += 1
        count += 1

    if count == 0:
        ws.write(HDR + 1, 0, "No Data Available", fmt["nodata"])
        if wide_print_layout:
            _apply_wide_print_layout(ws, HDR, HDR + 1, last_col, sheet_name)
    else:
        ws.write(r, 0,
                 f"{count:,} records  ·  Generated {time.strftime('%d/%m/%Y %H:%M:%S')}",
                 fmt["foot"])
        ws.autofilter(HDR, 0, r - 1, last_col)
        if wide_print_layout:
            _apply_wide_print_layout(ws, HDR, r, last_col, sheet_name)

    for ci, w in enumerate(widths):
        ws.set_column(ci, ci, min(max(w + 1, 12), 40))
    return count


def build_workbook_streaming(specs: Iterable, progress: Optional[Callable[[int, str], None]] = None,
                             total: int = 0, prog_lo: int = 0, prog_hi: int = 100,
                             wide_print_layout: bool = False) -> bytes:
    """
    Like `build_workbook`, but each spec's rows are an ITERATOR consumed lazily, so
    only one batch of rows is ever in memory across the whole workbook, and each
    sheet is written in a SINGLE pass (no separate auto-fit scan). Each spec is
    (sheet_name, header, columns, rows_iter, label_fn). `wide_print_layout` is
    forwarded to every sheet. Returns bytes.
    """
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"constant_memory": True, "in_memory": True})
    fmt = make_formats(wb)
    used: set = set()
    i = 0
    for name, header, columns, rows_iter, label_fn in specs:
        write_report_sheet_streaming(wb, fmt, safe_sheet_name(name, used), header,
                                     columns, rows_iter, label_fn,
                                     wide_print_layout=wide_print_layout)
        i += 1
        if progress and total:
            pct = prog_lo + int(i / total * (prog_hi - prog_lo))
            progress(min(pct, prog_hi), f"Writing {name}… ({i}/{total})")
    if i == 0:
        write_report_sheet_streaming(wb, fmt, "Report", {"subtitle": "No data"},
                                     ["timestamp"], iter([]), lambda c: "Timestamp")
    wb.close()
    return buf.getvalue()


# ── Sectioned sheet renderer (many tables stacked on ONE worksheet) ──────────
# Excel's hard limit. A sectioned sheet stacks N tables, so the row budget is
# shared — we cap rows per section rather than let XlsxWriter overflow.
EXCEL_MAX_ROWS = 1_048_576
_BLANK_ROWS_BETWEEN = 3        # readability gap between sections
_SECTION_OVERHEAD = 1 + _BLANK_ROWS_BETWEEN   # header row + gap (no title band)

# Report header block occupies rows 0-9; the first section starts at row 10.
# These rows are ALSO the print titles repeated at the top of every printed page.
_HDR_BLOCK_LAST_ROW = 9
_SECTION_START_ROW  = 10


def _section_row_cap(sections: List[Tuple[str, List[str], List[dict]]]) -> int:
    """Max data rows each section may write so the sheet stays inside Excel's limit."""
    n = max(len(sections), 1)
    budget = EXCEL_MAX_ROWS - 12 - (_SECTION_OVERHEAD * n)  # 12 = report header block
    return max(budget // n, 1)


# ── Print layout — one section (SMB) per printed page ────────────────────────
# A4 landscape. Every section starts on a fresh page, and the report title +
# equipment metadata reprint at the top of each page via Excel's print titles.
_PAPER_A4        = 9
_MARGIN_LR       = 0.3        # inches
_MARGIN_TB       = 0.4        # inches
_A4_LANDSCAPE_W  = 11.69      # inches
_A4_LANDSCAPE_H  = 8.27       # inches
_PT_PER_INCH     = 72.0
_DEFAULT_ROW_PT  = 15.0       # XlsxWriter default row height


def _table_width_inches(widths: List[int]) -> float:
    """
    Printed width of the table in inches.

    Excel column widths are in "characters"; the pixel conversion below is the
    standard Calibri-11 metric XlsxWriter itself documents (7px per character
    plus 5px of cell padding), and 96px == 1 inch.
    """
    px = 0.0
    for w in widths:
        px += (w * 7 + 5) if w >= 1 else (w * 12 + 0.5)
    return px / 96.0


def _fit_scale(widths: List[int]) -> int:
    """
    Print scale (%) that pulls the table onto ONE page wide.

    `fit_to_pages()` is deliberately NOT used here. XlsxWriter documents that the
    fit-to-page option overrides ALL manual page breaks, which would silently undo
    the one-section-per-page layout. `set_print_scale()` is Excel's "Adjust to N%"
    option, which is honoured alongside manual page breaks.
    """
    table_in = _table_width_inches(widths)
    printable = _A4_LANDSCAPE_W - (2 * _MARGIN_LR)
    if table_in <= printable:
        return 100
    return max(int(printable / table_in * 100), 10)   # Excel's floor is 10%


def _rows_per_page(scale: int) -> int:
    """
    Approx. data rows that fit below the repeated header on one A4 landscape page.

    Used only to warn when a section will overflow its page — a section longer
    than this still starts on a fresh page, but will continue onto further pages,
    so the SMB-number == page-number mapping no longer holds one-to-one.
    """
    printable_pt = (_A4_LANDSCAPE_H - 2 * _MARGIN_TB) * _PT_PER_INCH
    printable_pt -= 43                      # default header/footer allowance
    printable_pt -= 24 + 15 + 15 + (6 * 14) + 15   # repeated title/meta block
    printable_pt -= 32                      # column header row (no section band)
    row_pt = _DEFAULT_ROW_PT * (scale / 100.0)
    return max(int(printable_pt / row_pt), 1) if row_pt > 0 else 1


def write_sectioned_sheet(wb, fmt, sheet_name: str, header: dict,
                          sections: List[Tuple[str, List[str], List[dict]]],
                          label_fn: Callable[[str], str]) -> None:
    """
    Render ONE worksheet holding several titled tables stacked vertically.

    Used by the String Combiner export: one sheet per inverter, carrying SMB1…SMB21
    as sections separated by blank rows. Each section starts directly with its own
    column-header row — there is no "SMB1"/"SMB2" title band. Rows are written
    strictly top to bottom, so this stays compatible with XlsxWriter's
    constant_memory mode — memory stays flat no matter how many sections a sheet
    holds.

    The only title line is COMPANY_TITLE in A1; the per-sheet subtitle is not
    rendered.

    PRINT LAYOUT: every section begins on a new printed page (SMB1 → page 1,
    SMB2 → page 2, …) via a manual page break before each section after the first.
    The report title and equipment metadata are set as Excel print titles, so they
    reprint at the top of every page without being duplicated in the sheet data.
    """
    ws = wb.add_worksheet(sheet_name)

    n = max((len(cols) for _, cols, _ in sections), default=1) or 1
    last_col = n - 1

    # Auto-fit widths across EVERY section (constant_memory needs set_column up front).
    widths = [12] * n
    for _title, cols, rows in sections:
        for ci, col in enumerate(cols):
            w = max((len(x) for x in label_fn(col).split()), default=8) + 2
            for row in rows:
                v = row.get(col)
                if col == "timestamp":
                    s = fmt_ts(v) if v else ""
                elif isinstance(v, float):
                    s = f"{v:.3f}"
                elif v is not None:
                    s = str(v)
                else:
                    s = ""
                w = max(w, len(s))
            widths[ci] = max(widths[ci], min(max(w + 1, 12), 40))
    for ci, w in enumerate(widths):
        ws.set_column(ci, ci, w)

    # Report header block — no merged cells. The company title is the ONLY title
    # line: the per-sheet subtitle ("String Combiner — INV1 — 21 SMB sections") is
    # deliberately not written. Row 1 is left empty rather than shifting the block
    # up, so the metadata rows, the print-titles range and the section start row all
    # keep their positions.
    ws.write(0, 0, COMPANY_TITLE, fmt["title"]); ws.set_row(0, 24)

    for i, (k, v) in enumerate(_meta_fields(header)):
        r = 3 + i
        ws.write(r, 0, k, fmt["meta_lbl"])
        ws.write(r, 1, str(v), fmt["meta"])
        ws.set_row(r, 14)

    # Print scale is derived from the final column widths, so it must be computed
    # after auto-fit but before the sections are written (rows_per_page warns on
    # sections that will spill past their own page).
    scale = _fit_scale(widths)
    rows_per_page = _rows_per_page(scale)

    cap = _section_row_cap(sections)
    truncated = False
    r = _SECTION_START_ROW
    first_hdr_row = None
    section_starts: List[int] = []      # first row of each section → page breaks
    overflow: List[str] = []            # sections too tall for a single page

    for title, cols, rows in sections:
        # A section begins directly with its COLUMN HEADERS. The bold "SMB1" band
        # that used to sit above them is intentionally not written — each block is
        # identified by its own column names (SCB1 …, SCB2 …) and by starting on a
        # fresh printed page. `title` is still carried for logging and page-break
        # diagnostics.
        section_starts.append(r)

        # Column headers for this section.
        for ci, col in enumerate(cols):
            ws.write(r, ci, label_fn(col), fmt["hdr"])
        ws.set_row(r, 32)
        if first_hdr_row is None:
            first_hdr_row = r
        hdr_row = r
        r += 1

        if not rows:
            ws.write(r, 0, "No Data Available", fmt["nodata"])
            r += 1 + _BLANK_ROWS_BETWEEN
            continue

        body = rows
        if len(body) > cap:
            body = body[:cap]
            truncated = True

        for i, row in enumerate(body):
            even = (i % 2 == 0)
            for ci, col in enumerate(cols):
                v = row.get(col)
                if col == "timestamp":
                    ws.write(r, ci, fmt_ts(v) if v else "—", fmt["ts_even" if even else "ts_odd"])
                elif v is None:
                    ws.write(r, ci, "—", fmt["na_even" if even else "na_odd"])
                elif isinstance(v, bool):
                    ws.write(r, ci, int(v), fmt["int_even" if even else "int_odd"])
                elif isinstance(v, (int, float)):
                    ws.write_number(r, ci, float(v), fmt["num_even" if even else "num_odd"])
                else:
                    ws.write(r, ci, str(v), fmt["txt_even" if even else "txt_odd"])
            r += 1

        # Per-section footer, then the readability gap before the next section.
        ws.write(r, 0, f"{len(body):,} records", fmt["foot"])
        r += 1 + _BLANK_ROWS_BETWEEN
        if len(body) > rows_per_page:
            overflow.append(f"{title} ({len(body)} rows)")

        # Excel permits exactly ONE autofilter per worksheet — apply it to the first
        # section's table so the columns stay filterable.
        if hdr_row == first_hdr_row:
            ws.autofilter(hdr_row, 0, hdr_row + len(body), last_col)

    if first_hdr_row is not None:
        ws.freeze_panes(first_hdr_row + 1, 0)

    # ── Print layout: one section per printed page ───────────────────────────
    # A4 landscape with narrow margins; the table is scaled (never fit_to_pages,
    # which would discard the manual breaks) so all columns land on one page wide.
    ws.set_landscape()
    ws.set_paper(_PAPER_A4)
    ws.set_margins(left=_MARGIN_LR, right=_MARGIN_LR, top=_MARGIN_TB, bottom=_MARGIN_TB)
    ws.set_print_scale(scale)
    ws.center_horizontally()
    ws.print_area(0, 0, max(r - 1, _SECTION_START_ROW), last_col)

    # Report title + equipment metadata reprint at the top of every page. Excel
    # prints these once on page 1 (they are the sheet's own first rows) and repeats
    # them on every page thereafter — no duplicated block in the data itself.
    ws.repeat_rows(0, _HDR_BLOCK_LAST_ROW)

    # A break BEFORE every section except the first: SMB1 → page 1, SMB2 → page 2 …
    if len(section_starts) > 1:
        ws.set_h_pagebreaks(section_starts[1:])

    ws.set_footer(f"&L&\"Calibri,Regular\"&8{sheet_name}"
                  f"&R&\"Calibri,Regular\"&8Page &P of &N")

    if overflow:
        logger.warning(
            "Sheet %s: %d section(s) hold more than ~%d rows and will continue onto "
            "additional pages, so section number no longer equals page number: %s",
            sheet_name, len(overflow), rows_per_page, ", ".join(overflow[:5]),
        )

    if truncated:
        logger.error(
            "Sheet %s: sections truncated to %d rows each — %d sections would exceed "
            "Excel's %d-row limit. Narrow the date range or widen the interval.",
            sheet_name, cap, len(sections), EXCEL_MAX_ROWS,
        )


# ── Workbook builder (streaming) ─────────────────────────────────────────────
SheetSpec = Tuple[str, dict, List[str], List[dict], Callable[[str], str]]
# (sheet_name, header, [(section_title, columns, rows), …], label_fn)
SectionedSheetSpec = Tuple[str, dict, List[Tuple[str, List[str], List[dict]]], Callable[[str], str]]


def build_sectioned_workbook(specs: Iterable[SectionedSheetSpec],
                             progress: Optional[Callable[[int, str], None]] = None,
                             total: int = 0, prog_lo: int = 0, prog_hi: int = 100) -> bytes:
    """One constant-memory workbook where each spec becomes ONE multi-section sheet."""
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"constant_memory": True, "in_memory": True})
    fmt = make_formats(wb)
    used: set = set()
    i = 0
    for name, header, sections, label_fn in specs:
        write_sectioned_sheet(wb, fmt, safe_sheet_name(name, used), header, sections, label_fn)
        i += 1
        if progress and total:
            pct = prog_lo + int(i / total * (prog_hi - prog_lo))
            progress(min(pct, prog_hi), f"Writing {name}… ({i}/{total})")
    if i == 0:
        write_report_sheet(wb, fmt, "Report", {"subtitle": "No data"}, ["timestamp"], [], lambda c: "Timestamp")
    wb.close()
    return buf.getvalue()


def build_workbook(specs: Iterable[SheetSpec], progress: Optional[Callable[[int, str], None]] = None,
                   total: int = 0, prog_lo: int = 0, prog_hi: int = 100,
                   wide_print_layout: bool = False) -> bytes:
    """
    Write every sheet-spec into one constant-memory workbook. Returns bytes.

    `wide_print_layout` is forwarded to every sheet; it defaults off so the existing
    callers (equipment export, T1 isolation, single-sheet preview) are unaffected.
    """
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"constant_memory": True, "in_memory": True})
    fmt = make_formats(wb)
    used: set = set()
    i = 0
    for name, header, columns, rows, label_fn in specs:
        write_report_sheet(wb, fmt, safe_sheet_name(name, used), header, columns, rows, label_fn,
                           wide_print_layout=wide_print_layout)
        i += 1
        if progress and total:
            pct = prog_lo + int(i / total * (prog_hi - prog_lo))
            progress(min(pct, prog_hi), f"Writing {name}… ({i}/{total})")
    if i == 0:
        # Never emit a zero-sheet workbook (invalid). Add an empty placeholder.
        write_report_sheet(wb, fmt, "Report", {"subtitle": "No data"}, ["timestamp"], [], lambda c: "Timestamp")
    wb.close()
    return buf.getvalue()


# ── Producer: one worksheet per equipment (Inverter_01, Inverter_02, …) ───────
def _equipment_label_fn():
    from app.routers.reports_v2 import col_header
    return col_header


def build_multi_equipment_workbook(req, equipment_ids, progress: Optional[Callable] = None):
    """
    Generic multi-equipment export: one worksheet per equipment, order preserved.

    Each worksheet is built from THAT equipment's OWN complete tag set — discovered
    per equipment from its live schema — never the frontend-selected `req.tags`
    array (which is shared across the whole request and would make every sheet show
    the same small subset). Fetches each equipment concurrently (bounded, own
    session) but writes sheets in the SELECTED order. Returns (bytes, filename).

    Opt-out: when `req.use_selected_tags` is set, the worksheet carries exactly
    `req.tags` in the order given instead. Only the Preconfigured Reports page sets
    it, where the chosen columns and their order are the point of the report.
    """
    ids = [e for e in (equipment_ids or [req.equipment_id]) if e and str(e).strip()]
    from_dt = req.from_datetime.strftime("%Y-%m-%d %H:%M:%S")
    to_dt   = req.to_datetime.strftime("%Y-%m-%d %H:%M:%S")
    interval_label = intervals.interval_label(req.interval)
    label_fn = _equipment_label_fn()
    t0 = time.perf_counter()

    def _fetch(eid):
        db = SessionLocal()
        try:
            # Complete tag set for THIS equipment (registry tags present in its
            # table, or auto-discovered) — resolved fresh every iteration so each
            # worksheet reflects its own columns, not a shared selection. When the
            # caller opted into its own selection, use it verbatim (order matters).
            tag_cols = (list(req.tags) if getattr(req, "use_selected_tags", False) and req.tags
                        else [t["column_name"]
                              for t in ReportsRepository.get_tags(db, req.equipment_type, eid)])
            r = ReportsRepository.get_report_data(
                db=db, equipment_type=req.equipment_type, equipment_id=eid,
                tags=tag_cols, from_datetime=from_dt, to_datetime=to_dt,
                interval=req.interval.value, agg_function=req.agg_function.value,
                page=1, page_size=_MAX_ROWS, compute_total=False,
            )
            return eid, r, None
        except Exception as e:  # noqa: BLE001
            logger.error("Excel export fetch failed for %s: %s", eid, e, exc_info=True)
            return eid, None, str(e)
        finally:
            db.close()

    total = len(ids)
    if progress:
        progress(2, f"Fetching {total} equipment ({_FETCH_WORKERS} in parallel)…")
    results: Dict[str, dict] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=_FETCH_WORKERS) as ex:
        futures = {ex.submit(_fetch, eid): eid for eid in ids}
        for fut in as_completed(futures):
            eid, r, _err = fut.result()
            results[eid] = r
            done += 1
            if progress:
                progress(2 + int(done / total * 55), f"Fetched {done}/{total} equipment")

    def _specs():
        for eid in ids:                       # SELECTED ORDER preserved
            r = results.get(eid) or {}
            # Columns come from THIS equipment's own result (reset per sheet).
            columns = r.get("columns") or ["timestamp"]
            rows = r.get("rows", [])
            header = {
                "subtitle": f"{req.equipment_type}  —  {eid}  —  Data Report",
                "equipment_type": req.equipment_type, "equipment_id": eid,
                "from": _d(req.from_datetime), "to": _d(req.to_datetime),
                "interval": interval_label,
                "agg": intervals.agg_label(req.interval, req.agg_function),
            }
            yield (eid, header, columns, rows, label_fn)

    data = build_workbook(_specs(), progress, total=total, prog_lo=58, prog_hi=99)
    filename = (f"{ids[0]}_Report_{time.strftime('%d-%m-%Y')}.xlsx" if total == 1 else
                f"{_safe(req.equipment_type)}_{total}_Equipment_Report_{time.strftime('%d-%m-%Y')}.xlsx")
    empty = sum(1 for eid in ids if not (results.get(eid) or {}).get("rows"))
    logger.info("Equipment Excel | type=%s | sheets=%d | empty=%d | %.0fms | %d bytes",
                req.equipment_type, total, empty, (time.perf_counter() - t0) * 1000, len(data))
    if progress:
        progress(100, f"Workbook ready — {total} sheet(s).")
    return data, filename


def _d(dt) -> str:
    try:
        return dt.strftime("%d/%m/%Y")
    except AttributeError:
        return str(dt)


def _safe(s: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in str(s))
