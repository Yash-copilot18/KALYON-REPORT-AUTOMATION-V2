# app/services/report_files.py
"""
Reusable CSV / Excel file builders for report e-mail attachments.

Intentionally self-contained: generating an attachment here never modifies or
depends on the Reports export endpoints (app/routers/reports_v2.py). Both the
scheduled-email flow and the Test-Email flow share these builders.
"""

import io
import csv
import logging
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

logger = logging.getLogger(__name__)


# Shown as the first line of every CSV's metadata block.
PROJECT_NAME = "Kalyon Solar Power Plant"


def column_header(col: str) -> str:
    """Human-friendly header label for a column key."""
    if col in ("timestamp", "TimeCol"):
        return "Timestamp (DD/MM/YYYY HH:MM:SS)"
    if col == "_equipment":
        return "Equipment"
    return col.replace("_", " ").title()


def fmt_timestamp(val) -> str:
    """Any timestamp value → DD/MM/YYYY HH:MM:SS (blank if empty)."""
    if val is None or val == "":
        return ""
    if isinstance(val, datetime):
        return val.strftime("%d/%m/%Y %H:%M:%S")
    s = str(val).replace("T", " ").replace("Z", "").strip().split(".")[0]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%d/%m/%Y %H:%M:%S")
        except ValueError:
            continue
    return s


def _fmt_cell(col, val) -> str:
    """Cell value for CSV: timestamps DD/MM/YYYY HH:MM:SS, numbers 3 decimals."""
    if val is None:
        return ""
    if col in ("timestamp", "TimeCol"):
        return fmt_timestamp(val)
    if isinstance(val, bool):
        return str(val)
    if isinstance(val, (int, float)):
        return f"{float(val):.3f}"
    return str(val)


def build_report_csv(columns, rows, *, metadata=None, header_fn=None) -> bytes:
    """
    THE standard CSV builder for the whole application. Produces a file Excel opens
    with correctly aligned columns and no manual adjustment:

      • UTF-8 **BOM** so Excel detects the encoding.
      • An optional **metadata block** — `metadata` is an ordered list of
        (label, value) pairs, each written as a SINGLE cell `Label: Value`,
        followed by exactly **one blank row**. A single cell is used (rather than
        two columns) because a CSV cannot carry column widths: Excel overflows the
        text into the empty cells beside it, so the whole line stays readable
        without any manual column resizing, and the `Label: ` prefix stops Excel
        from misreading dates as numbers (no `#######`).
      • A **header row** of user-friendly names (`header_fn`, default `column_header`).
      • **Data rows** — `timestamp` as DD/MM/YYYY HH:MM:SS, numerics to exactly
        3 decimals, in the SAME column order supplied.

    `csv.writer` (QUOTE_MINIMAL) escapes commas, quotes and newlines, so no value
    can ever break the column layout. No '#' prefixes or debug text are emitted.
    """
    hf = header_fn or column_header
    buf = io.StringIO()
    buf.write("﻿")  # UTF-8 BOM
    writer = csv.writer(buf, delimiter=",", quoting=csv.QUOTE_MINIMAL,
                        lineterminator="\r\n")   # CRLF — most robust for Excel

    if metadata:
        for label, value in metadata:
            text = f"{label}: {value}" if value not in (None, "") else f"{label}:"
            writer.writerow([text])    # single cell → overflows into empty cells in Excel
        writer.writerow([])            # one blank row between metadata and the table

    writer.writerow([hf(c) for c in columns])
    for row in rows:
        writer.writerow([_fmt_cell(c, row.get(c)) for c in columns])
    return buf.getvalue().encode("utf-8")


def build_csv_bytes(columns, rows, metadata=None, header_fn=None) -> bytes:
    """Backwards-compatible wrapper — delegates to the standard builder."""
    return build_report_csv(columns, rows, metadata=metadata, header_fn=header_fn)


def build_excel_bytes(columns, rows, sheet_title: str = "Report", title: str | None = None) -> bytes:
    """Build a styled .xlsx file as bytes from columns + list-of-dict rows."""
    wb = Workbook()
    ws = wb.active
    ws.title = (sheet_title or "Report")[:30]
    ws.sheet_view.showGridLines = False

    n = max(len(columns), 1)

    # Clean white/black corporate theme (printer-friendly) — matches report_excel.
    BLACK    = "000000"    # every title, header and data value
    GRAY_HDR = "F2F2F2"    # table header fill
    EVEN     = "FFFFFF"    # alternating data rows: white …
    ODD      = "FAFAFA"    # … and near-white
    BORDER   = "000000"    # thin black borders

    thin = Side(style="thin", color=BORDER)
    cell_border = Border(left=thin, right=thin, top=thin, bottom=thin)

    # ── Title row ──────────────────────────────────────────────────────────
    # Bold 16pt black, left-aligned, no fill and no merged cells — the text simply
    # overflows the empty cells beside it.
    tcell = ws.cell(row=1, column=1, value=title or "Kalyon Solar Monitoring — Report")
    tcell.font = Font(color=BLACK, size=16, bold=True, name="Calibri")
    tcell.alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[1].height = 24

    # ── Header row ─────────────────────────────────────────────────────────
    HDR = 2
    for ci, col in enumerate(columns, start=1):
        c = ws.cell(row=HDR, column=ci, value=column_header(col))
        c.font = Font(color=BLACK, size=10, bold=True, name="Calibri")
        c.fill = PatternFill("solid", fgColor=GRAY_HDR)
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = cell_border
    ws.row_dimensions[HDR].height = 30

    # ── Data rows ──────────────────────────────────────────────────────────
    for ri, row in enumerate(rows, start=HDR + 1):
        bg = EVEN if (ri - HDR) % 2 == 0 else ODD
        for ci, col in enumerate(columns, start=1):
            val = row.get(col)
            c = ws.cell(row=ri, column=ci)
            c.fill = PatternFill("solid", fgColor=bg)
            c.border = cell_border
            # Black text throughout; numbers right-aligned, text left-aligned.
            if isinstance(val, float):
                c.value = round(val, 3)
                c.font = Font(color=BLACK, size=9, name="Calibri")
                c.alignment = Alignment(horizontal="right", vertical="center")
                c.number_format = "#,##0.000"
            elif isinstance(val, int):
                c.value = val
                c.font = Font(color=BLACK, size=9, name="Calibri")
                c.alignment = Alignment(horizontal="right", vertical="center")
            elif val is None:
                c.value = "—"
                c.font = Font(color=BLACK, size=9, name="Calibri")
                c.alignment = Alignment(horizontal="center", vertical="center")
            else:
                c.value = str(val)
                c.font = Font(color=BLACK, size=9, name="Calibri")
                c.alignment = Alignment(horizontal="left", vertical="center")

    # ── Auto-fit column widths ─────────────────────────────────────────────
    for ci, col in enumerate(columns, start=1):
        max_data = max(
            [len(_fmt_cell(col, r.get(col))) for r in rows] + [0]
        )
        hdr_min = max((len(w) for w in column_header(col).split()), default=4) + 2
        width = max(max_data * 1.23 + 1, hdr_min, 12)
        ws.column_dimensions[get_column_letter(ci)].width = min(width, 40)

    if rows:
        ws.freeze_panes = f"A{HDR + 1}"

    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    return out.getvalue()


def sample_report(equipment_type: str = "Inverter", equipment_id: str = "INVERTER_01"):
    """A small synthetic dataset used by the Test-Email feature.

    Lets the SMTP / attachment pipeline be verified without depending on the
    database or on a fully-configured schedule (which has no tags yet).
    """
    columns = ["timestamp", "ACTIVE_POWER", "DC_VOLTAGE", "DAILY_ENERGY"]
    now = datetime.now()
    rows = []
    for i in range(6):
        ts = now.replace(minute=0, second=0, microsecond=0)
        rows.append({
            "timestamp":    ts.strftime("%d/%m/%Y %H:%M:%S"),
            "ACTIVE_POWER": 1500.0 + i * 12.5,
            "DC_VOLTAGE":   720.0 + i * 1.2,
            "DAILY_ENERGY": 8200.0 + i * 30.0,
        })
    return columns, rows
