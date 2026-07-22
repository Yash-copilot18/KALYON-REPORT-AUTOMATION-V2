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
    """Ordered (label, value) metadata pairs shared by both sheet renderers."""
    return [
        ("Equipment Type", header.get("equipment_type", "")),
        ("Equipment ID",   header.get("equipment_id", "")),
        ("From",           header.get("from", "")),
        ("To",             header.get("to", "")),
        ("Interval",       header.get("interval", "")),
        ("Aggregation",    header.get("agg", "")),
    ]


# ── The one professional sheet renderer ──────────────────────────────────────
def write_report_sheet(wb, fmt, sheet_name: str, header: dict,
                       columns: List[str], rows: List[dict],
                       label_fn: Callable[[str], str]) -> None:
    """Render one worksheet. Empty rows → 'No Data Available' (never skipped)."""
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


# ── Sectioned sheet renderer (many tables stacked on ONE worksheet) ──────────
# Excel's hard limit. A sectioned sheet stacks N tables, so the row budget is
# shared — we cap rows per section rather than let XlsxWriter overflow.
EXCEL_MAX_ROWS = 1_048_576
_BLANK_ROWS_BETWEEN = 3        # readability gap between sections
_SECTION_OVERHEAD = 2 + _BLANK_ROWS_BETWEEN   # title row + header row + gap


def _section_row_cap(sections: List[Tuple[str, List[str], List[dict]]]) -> int:
    """Max data rows each section may write so the sheet stays inside Excel's limit."""
    n = max(len(sections), 1)
    budget = EXCEL_MAX_ROWS - 12 - (_SECTION_OVERHEAD * n)  # 12 = report header block
    return max(budget // n, 1)


def write_sectioned_sheet(wb, fmt, sheet_name: str, header: dict,
                          sections: List[Tuple[str, List[str], List[dict]]],
                          label_fn: Callable[[str], str]) -> None:
    """
    Render ONE worksheet holding several titled tables stacked vertically.

    Used by the String Combiner export: one sheet per inverter, carrying SMB1…SMB21
    as bold-titled sections separated by blank rows. Rows are written strictly top
    to bottom, so this stays compatible with XlsxWriter's constant_memory mode —
    memory stays flat no matter how many sections a sheet holds.
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

    # Report header block — identical to the single-table sheet (no merged cells).
    ws.write(0, 0, COMPANY_TITLE, fmt["title"]); ws.set_row(0, 24)
    ws.write(1, 0, header.get("subtitle", ""), fmt["subtitle"]); ws.set_row(1, 18)

    for i, (k, v) in enumerate(_meta_fields(header)):
        r = 3 + i
        ws.write(r, 0, k, fmt["meta_lbl"])
        ws.write(r, 1, str(v), fmt["meta"])
        ws.set_row(r, 14)

    cap = _section_row_cap(sections)
    truncated = False
    r = 10
    first_hdr_row = None

    for title, cols, rows in sections:
        # Bold section title (e.g. "SMB1") — the gray band is painted cell-by-cell
        # rather than merged, so no merged cells are introduced.
        ws.write(r, 0, title, fmt["section"])
        for ci in range(1, n):
            ws.write_blank(r, ci, None, fmt["section"])
        ws.set_row(r, 20)
        r += 1

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

        # Excel permits exactly ONE autofilter per worksheet — apply it to the first
        # section's table so the columns stay filterable.
        if hdr_row == first_hdr_row:
            ws.autofilter(hdr_row, 0, hdr_row + len(body), last_col)

    if first_hdr_row is not None:
        ws.freeze_panes(first_hdr_row + 1, 0)

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
                   total: int = 0, prog_lo: int = 0, prog_hi: int = 100) -> bytes:
    """Write every sheet-spec into one constant-memory workbook. Returns bytes."""
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"constant_memory": True, "in_memory": True})
    fmt = make_formats(wb)
    used: set = set()
    i = 0
    for name, header, columns, rows, label_fn in specs:
        write_report_sheet(wb, fmt, safe_sheet_name(name, used), header, columns, rows, label_fn)
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
