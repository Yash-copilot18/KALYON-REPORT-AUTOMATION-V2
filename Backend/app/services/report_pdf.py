# app/services/report_pdf.py
"""
Reusable PDF file builder for report e-mail attachments.

Self-contained, exactly like report_files (CSV/Excel): building a PDF here never
touches or depends on the Reports export endpoints. Used by the scheduled-report
flow when the selected Report Format is "PDF".

Design goals
------------
• Same data contract as the CSV/Excel builders — `columns` (list of keys) plus
  `rows` (list of dicts), with an optional `header_fn`/`metadata`. Cell values are
  formatted identically to the CSV builder (`_fmt_cell`): timestamps as
  DD/MM/YYYY HH:MM:SS, numbers to 3 decimals — so the three formats agree.
• Wide tables (DGR/MGR carry a timestamp + up to 24 inverter columns) are split
  into column "panels" that each fit a landscape A4 page, with the first (key)
  column repeated on every panel so rows stay identifiable.
• Printer-friendly monochrome theme matching the Excel export: gray header fill,
  thin black grid, alternating near-white row bands.

reportlab is the only dependency (already installed in the backend venv).
"""

import io
import logging
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as _canvas
from reportlab.platypus import (
    Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

from app.services.report_files import _fmt_cell, column_header

logger = logging.getLogger(__name__)

PDF_MIME = ("application", "pdf")

# Landscape A4 usable area after margins (see _MARGIN below).
_MARGIN = 14 * mm
_PAGE_W, _PAGE_H = landscape(A4)
_USABLE_W = _PAGE_W - 2 * _MARGIN

# Table typography.
_HDR_FONT      = "Helvetica-Bold"
_BODY_FONT     = "Helvetica"
_HDR_SIZE      = 7.5
_BODY_SIZE     = 7.0
_CHAR_W        = 4.35     # approx point width of one body character at _BODY_SIZE
_MIN_COL_W     = 38.0
_MAX_COL_W     = 150.0
_MAX_DATA_COLS = 10       # data columns per panel (the key column is extra + repeated)

# Guard rail: a report with an unexpectedly huge row count is truncated with a
# visible note rather than producing an unusable multi-thousand-page PDF. DGR/MGR/
# YGR are day/month/year summaries, so this is a safety net, not a normal path.
_MAX_ROWS = 5000

_GRAY_HDR = colors.HexColor("#F2F2F2")
_ODD_BG   = colors.HexColor("#FAFAFA")
_BLACK    = colors.HexColor("#000000")


_FOOT_RULE = colors.HexColor("#D2D9E4")
_FOOT_INK  = colors.HexColor("#6B7A99")


class _NumberedCanvas(_canvas.Canvas):
    """
    Stamps a footer — generated time · company · "Page X of Y" — on EVERY page (req 5),
    while the report header stays on page 1 only (the title/metadata are ordinary
    flowables and are never repeated). The total page count is only known once the whole
    document is laid out, so pages are buffered and the footer is drawn in a second pass.
    The report's flowable content (tables/data) is completely untouched.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved = []

    def showPage(self):
        self._saved.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved)
        for state in self._saved:
            self.__dict__.update(state)
            self._draw_footer(total)
            super().showPage()
        super().save()

    def _draw_footer(self, total):
        w, _h = landscape(A4)
        self.saveState()
        self.setStrokeColor(_FOOT_RULE)
        self.setLineWidth(0.6)
        self.line(_MARGIN, 10 * mm, w - _MARGIN, 10 * mm)
        self.setFont("Helvetica", 8)
        self.setFillColor(_FOOT_INK)
        y = 10 * mm - 9
        self.drawString(_MARGIN, y, f"Generated {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
        self.drawCentredString(w / 2, y, "Kalyon Solar Monitoring — Report Automation")
        self.drawRightString(w - _MARGIN, y, f"Page {self._pageNumber} of {total}")
        self.restoreState()


def _cell_text(col, val) -> str:
    t = _fmt_cell(col, val)
    return t if t != "" else "—"


def _column_groups(columns):
    """
    Split `columns` into panels that each fit the page width. The first column is
    treated as the row key and repeated at the front of every panel so rows remain
    identifiable when the table spans multiple horizontal pages.
    """
    if len(columns) <= 1:
        return [list(columns)]
    key, data = columns[0], columns[1:]
    groups = []
    for i in range(0, len(data), _MAX_DATA_COLS):
        groups.append([key] + data[i:i + _MAX_DATA_COLS])
    return groups


def _col_widths(group, rows, hf):
    """Content-fit widths for a panel, clamped and scaled to never exceed the page."""
    widths = []
    for col in group:
        header_len = max((len(w) for w in hf(col).split()), default=4)
        data_len = max((len(_cell_text(col, r.get(col))) for r in rows), default=0)
        w = max(header_len, data_len) * _CHAR_W + 8.0
        widths.append(min(max(w, _MIN_COL_W), _MAX_COL_W))
    total = sum(widths)
    if total > _USABLE_W:                      # scale down to fit the page exactly
        scale = _USABLE_W / total
        widths = [w * scale for w in widths]
    return widths


def _panel_table(group, rows, hf):
    header = [Paragraph(f"<b>{_escape(hf(c))}</b>", _HDR_PARA) for c in group]
    body = [[_cell_text(c, r.get(c)) for c in group] for r in rows]
    data = [header] + body

    table = Table(data, colWidths=_col_widths(group, rows, hf), repeatRows=1)
    style = [
        ("FONT",        (0, 0), (-1, 0), _HDR_FONT, _HDR_SIZE),
        ("FONT",        (0, 1), (-1, -1), _BODY_FONT, _BODY_SIZE),
        ("TEXTCOLOR",   (0, 0), (-1, -1), _BLACK),
        ("BACKGROUND",  (0, 0), (-1, 0), _GRAY_HDR),
        ("VALIGN",      (0, 0), (-1, -1), "MIDDLE"),
        # Alignment applied to header + data TOGETHER per column, so each header sits
        # directly above its values: key (first) column left, numeric columns centred.
        ("ALIGN",       (0, 0), (0, -1), "LEFT"),      # key column: header + data left
        ("ALIGN",       (1, 0), (-1, -1), "CENTER"),   # numeric columns: header + data centred (req 1 & 2)
        ("GRID",        (0, 0), (-1, -1), 0.4, _BLACK),
        ("TOPPADDING",  (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
    ]
    for ri in range(1, len(data)):                     # alternating row bands
        if ri % 2 == 0:
            style.append(("BACKGROUND", (0, ri), (-1, ri), _ODD_BG))
    table.setStyle(TableStyle(style))
    return table


def _escape(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


_TITLE_PARA = ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=14,
                             textColor=_BLACK, alignment=TA_LEFT, spaceAfter=4)
_META_PARA  = ParagraphStyle("m", fontName="Helvetica", fontSize=8.5,
                             textColor=_BLACK, alignment=TA_LEFT, leading=12)
_SUB_PARA   = ParagraphStyle("s", fontName="Helvetica-Bold", fontSize=10,
                             textColor=_BLACK, alignment=TA_LEFT, spaceBefore=6, spaceAfter=3)
_HDR_PARA   = ParagraphStyle("h", fontName=_HDR_FONT, fontSize=_HDR_SIZE,
                             textColor=_BLACK, alignment=1, leading=_HDR_SIZE + 1.5)
_NOTE_PARA  = ParagraphStyle("n", fontName="Helvetica-Oblique", fontSize=8,
                             textColor=_BLACK, alignment=TA_LEFT, spaceBefore=4)


def _append_table(story, columns, rows, hf):
    """Render one logical table (split into column panels) into `story`."""
    columns = list(columns or [])
    rows = list(rows or [])
    if not columns or not rows:
        story.append(Paragraph("No data available for the selected period.", _META_PARA))
        return
    truncated = len(rows) > _MAX_ROWS
    if truncated:
        rows = rows[:_MAX_ROWS]
    groups = _column_groups(columns)
    for gi, group in enumerate(groups):
        if gi > 0:
            story.append(Spacer(1, 10))
            story.append(Paragraph(
                f"Columns continued ({gi + 1} of {len(groups)})", _META_PARA))
            story.append(Spacer(1, 4))
        story.append(_panel_table(group, rows, hf))
    if truncated:
        story.append(Paragraph(
            f"Note: output limited to the first {_MAX_ROWS:,} rows.", _NOTE_PARA))


def build_pdf_report(title, sections, *, metadata=None) -> bytes:
    """
    Build a landscape-A4 PDF from one or more titled `sections`, so a single report
    can stack several tables (e.g. MGR's per-inverter table + daily-totals table),
    matching the manual report's multi-table layout.

    title    — bold heading line.
    metadata — ordered list of (label, value) pairs shown under the title.
    sections — list of (subtitle, columns, rows, header_fn); header_fn may be None
               (defaults to `column_header`). subtitle may be None to omit it.
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=landscape(A4),
        leftMargin=_MARGIN, rightMargin=_MARGIN,
        topMargin=_MARGIN, bottomMargin=_MARGIN,
        title=(title or "Kalyon Solar Monitoring — Report"),
    )

    story = [Paragraph(_escape(title or "Kalyon Solar Monitoring — Report"), _TITLE_PARA)]

    if metadata:
        meta_lines = "<br/>".join(
            f"<b>{_escape(label)}:</b> {_escape(value)}"
            for label, value in metadata if value not in (None, "")
        )
        if meta_lines:
            story.append(Paragraph(meta_lines, _META_PARA))
    story.append(Spacer(1, 8))

    for i, (subtitle, columns, rows, header_fn) in enumerate(sections):
        if i > 0:
            story.append(Spacer(1, 12))
        if subtitle:
            story.append(Paragraph(_escape(subtitle), _SUB_PARA))
        _append_table(story, columns, rows, header_fn or column_header)

    # Header/metadata are flowables → page 1 only (never repeated). The numbered
    # canvas adds the footer + "Page X of Y" to every page (req 5).
    doc.build(story, canvasmaker=_NumberedCanvas)
    buf.seek(0)
    return buf.getvalue()


def build_pdf_bytes(columns, rows, *, title=None, metadata=None,
                    header_fn=None) -> bytes:
    """
    Single-table convenience wrapper over `build_pdf_report`.

    title      — bold heading line.
    metadata   — ordered list of (label, value) pairs shown under the title.
    header_fn  — column-key → display label (default `column_header`).
    """
    return build_pdf_report(
        title, [(None, columns, rows, header_fn)], metadata=metadata)
