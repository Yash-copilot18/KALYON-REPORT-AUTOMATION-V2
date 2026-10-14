"""
Prototype AI forensic / assurance report for the Kalyon plant.

Standalone: it touches no production table, no router and no page. It reads the
Kalyon database, performs the deterministic analysis, runs the forecasting
experiment, and writes a PDF.

    cd Backend
    venv\\Scripts\\python.exe prototypes\\build_report.py

Every figure printed is either measured from the database, derived by a documented
formula, or produced by a model whose validation scores are printed alongside it.
Section 16 classifies each one. Where the data cannot support a claim the report
says so instead of estimating.
"""

from __future__ import annotations

import os
import statistics
import sys
from datetime import datetime
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (BaseDocTemplate, CondPageBreak, Frame,
                                KeepTogether, NextPageTemplate, PageTemplate,
                                Paragraph, Table, TableStyle)
from reportlab.graphics.shapes import Drawing, Line, String, Rect, Circle
from reportlab.graphics.charts.lineplots import LinePlot
from reportlab.graphics.charts.barcharts import VerticalBarChart

import extract as ex
import forecast as fc
from sklearn.linear_model import LinearRegression

# ── palette ──────────────────────────────────────────────────────────────────
INK      = colors.HexColor("#1a2332")
MUTED    = colors.HexColor("#5b6b7f")
RULE     = colors.HexColor("#d4dae2")
ACCENT   = colors.HexColor("#0b63a8")
GOOD     = colors.HexColor("#1b7f4d")
WARN     = colors.HexColor("#b26a00")
BAD      = colors.HexColor("#b3261e")
BAND     = colors.HexColor("#cfe0f0")
PANEL    = colors.HexColor("#f4f6f9")

INSTALLED_CAPACITY_MW = 130.0   # plant nameplate; every PR denominator uses it

PAGE_W, PAGE_H = A4
MARGIN = 16 * mm
CONTENT_W = PAGE_W - 2 * MARGIN

ss = getSampleStyleSheet()
S_TITLE = ParagraphStyle("t", parent=ss["Title"], fontName="Helvetica-Bold",
                         fontSize=20, leading=24, textColor=INK, spaceAfter=2)
S_SUB = ParagraphStyle("st", parent=ss["Normal"], fontName="Helvetica",
                       fontSize=9.5, leading=13, textColor=MUTED)
S_H1 = ParagraphStyle("h1", parent=ss["Heading1"], fontName="Helvetica-Bold",
                      fontSize=11.5, leading=14, textColor=INK,
                      spaceBefore=7, spaceAfter=3, keepWithNext=True)
S_H2 = ParagraphStyle("h2", parent=ss["Heading2"], fontName="Helvetica-Bold",
                      fontSize=10, leading=13, textColor=ACCENT,
                      spaceBefore=5, spaceAfter=2, keepWithNext=True)
S_BODY = ParagraphStyle("b", parent=ss["Normal"], fontName="Helvetica",
                        fontSize=8.8, leading=11.6, textColor=INK, spaceAfter=2.5)
S_NOTE = ParagraphStyle("n", parent=S_BODY, fontSize=7.8, leading=10.2,
                        textColor=MUTED, spaceBefore=1.5, spaceAfter=4)
S_CELL = ParagraphStyle("c", parent=S_BODY, fontSize=7.6, leading=9.8, spaceAfter=0)
S_CELLB = ParagraphStyle("cb", parent=S_CELL, fontName="Helvetica-Bold")

# Column-header text for the dark table-header bar. A TableStyle TEXTCOLOR does
# not reach a Paragraph — the paragraph carries its own colour — so header cells
# must use this style or they render dark-on-dark and the bar looks blank.
S_HEAD = ParagraphStyle("th", parent=ss["Normal"], fontName="Helvetica-Bold",
                        fontSize=7.0, leading=8.6, textColor=colors.white,
                        spaceAfter=0, spaceBefore=0)
# Section-label bar text. One style for all sixteen sections, so the typography
# cannot drift between them.
S_BAR = ParagraphStyle("sb", parent=ss["Normal"], fontName="Helvetica-Bold",
                       fontSize=10.5, leading=12.5, textColor=colors.white,
                       spaceAfter=0, spaceBefore=0)

# One geometry for every section bar: same height, same padding, same colour.
BAR_PAD_V = 4.0          # pt above and below the bar text
BAR_PAD_H = 6.0          # pt inside the left/right bar edge
BAR_SPACE_BEFORE = 6.0   # pt of air above the bar
BAR_SPACE_AFTER = 3.5    # pt of air below the bar
BAR_KEEP = 46 * mm       # a bar must have at least this much page left below it


def fmt(v, d=0, dash="—"):
    if v is None:
        return dash
    return f"{v:,.{d}f}"


def para(t, s=S_CELL):
    return Paragraph(str(t), s)


_HEAD_CACHE = {}


def _head_style(size):
    """Header type tracks the table's own body size, so a dense table's labels
    stay on one line instead of breaking mid-word."""
    fs = round(min(7.0, size - 0.4), 2)
    if fs not in _HEAD_CACHE:
        # Leading is held constant so a single-line header bar is exactly the
        # same height in every table, whatever the table's body font size.
        _HEAD_CACHE[fs] = ParagraphStyle(f"th{fs}", parent=S_HEAD, fontSize=fs,
                                         leading=8.6)
    return _HEAD_CACHE[fs]


def _head_cell(c, size):
    """
    Re-style a column label for the dark header bar: white, bold, uppercase.

    Accepts whatever the call site passed — a Paragraph or a bare string — and
    reads its text back, so no label has to be restated at the call site and none
    can be lost. Labels containing markup are left cased as written.
    """
    txt = c.text if isinstance(c, Paragraph) else str(c)
    return Paragraph(txt if "<" in txt else txt.upper(), _head_style(size))


def table(data, widths, header=True, align=None, size=7.6):
    if header and data:
        data = [[_head_cell(c, size) for c in data[0]]] + list(data[1:])
    t = Table(data, colWidths=widths, repeatRows=1 if header else 0, hAlign="LEFT")
    style = [
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), size),
        ("TEXTCOLOR", (0, 0), (-1, -1), INK),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, RULE),
    ]
    # Column headers repeat ONLY when this same table genuinely continues
    # onto the next page (repeatRows above); they are never a page header.
    if header:
        style += [("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                  ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                  ("BACKGROUND", (0, 0), (-1, 0), INK),
                  ("FONTSIZE", (0, 0), (-1, 0), size - 0.4),
                  ("VALIGN", (0, 0), (-1, 0), "MIDDLE"),
                  ("TOPPADDING", (0, 0), (-1, 0), 4),
                  ("BOTTOMPADDING", (0, 0), (-1, 0), 4)]
        style += [("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, PANEL])]
    for col, a in (align or {}).items():
        style.append(("ALIGN", (col, 0), (col, -1), a))
    t.setStyle(TableStyle(style))
    return t


def kpi_row(items):
    """A row of headline figures with a label and an optional colour."""
    cells, styles = [], [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("BACKGROUND", (0, 0), (-1, -1), PANEL),
        ("BOX", (0, 0), (-1, -1), 0.4, RULE),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, RULE),
    ]
    for value, label, tone in items:
        cells.append(Paragraph(
            f'<font size="13" color="#{tone.hexval()[2:]}"><b>{value}</b></font><br/>'
            f'<font size="7" color="#{MUTED.hexval()[2:]}">{label}</font>',
            ParagraphStyle("k", parent=S_BODY, leading=15)))
    w = CONTENT_W / len(cells)
    t = Table([cells], colWidths=[w] * len(cells), hAlign="LEFT")
    t.setStyle(TableStyle(styles))
    return t


def tag(text, tone):
    return Paragraph(
        f'<font color="#{tone.hexval()[2:]}"><b>{text}</b></font>', S_CELL)


# ── charts ───────────────────────────────────────────────────────────────────
def line_chart(series, width=CONTENT_W, height=50 * mm, y_label="",
               legend=None, title="", x_label=""):
    """series: list of (name, [(x, y)...], colour, dashed)."""
    d = Drawing(width, height)
    lp = LinePlot()
    lp.x, lp.y = 40, 30
    lp.width, lp.height = width - 56, height - (62 if title else 50)
    data = []
    for i, (name, pts, col, dashed) in enumerate(series):
        data.append(pts)
    lp.data = data
    for i, (name, pts, col, dashed) in enumerate(series):
        lp.lines[i].strokeColor = col
        lp.lines[i].strokeWidth = 1.4 if not dashed else 1.0
        if dashed:
            lp.lines[i].strokeDashArray = (2.5, 2.5)
    allx = [p[0] for s in data for p in s] or [0, 1]
    ally = [p[1] for s in data for p in s] or [0, 1]
    lp.xValueAxis.valueMin, lp.xValueAxis.valueMax = min(allx), max(allx)
    lp.yValueAxis.valueMin = 0
    lp.yValueAxis.valueMax = max(ally) * 1.12 if max(ally) > 0 else 1
    lp.xValueAxis.labels.fontSize = 6
    lp.yValueAxis.labels.fontSize = 6
    lp.xValueAxis.strokeColor = RULE
    lp.yValueAxis.strokeColor = RULE
    lp.yValueAxis.gridStrokeColor = colors.HexColor("#eef1f5")
    lp.yValueAxis.visibleGrid = True
    d.add(lp)
    if legend:
        x = 40
        for name, col in legend:
            d.add(Rect(x, 6, 9, 4, fillColor=col, strokeColor=None))
            d.add(String(x + 12, 6, name, fontSize=6.3, fillColor=MUTED))
            x += 16 + len(name) * 3.5
    return chart_frame(d, width, height, title, x_label if not legend else "", y_label)


def bar_chart(labels, values, width=CONTENT_W, height=46 * mm, colour=ACCENT,
              y_label="", highlight=None, title="", x_label=""):
    d = Drawing(width, height)
    bc = VerticalBarChart()
    bc.x, bc.y = 40, 34
    bc.width, bc.height = width - 56, height - (64 if title else 52)
    bc.data = [values]
    bc.categoryAxis.categoryNames = labels
    bc.categoryAxis.labels.fontSize = 5.6
    bc.categoryAxis.labels.angle = 90
    bc.categoryAxis.labels.dy = -12
    bc.categoryAxis.labels.boxAnchor = "e"
    bc.valueAxis.valueMin = 0
    bc.valueAxis.valueMax = max(values) * 1.15 if values and max(values) > 0 else 1
    bc.valueAxis.labels.fontSize = 6
    bc.bars[0].fillColor = colour
    bc.bars[0].strokeColor = None
    bc.barWidth = 4
    bc.groupSpacing = 2
    d.add(bc)
    return chart_frame(d, width, height, title, x_label, y_label)


def chart_frame(d, width, height, title, x_label, y_label):
    """Title, axis labels and units, applied identically to every chart."""
    if title:
        d.add(String(0, height - 8, title.upper(), fontSize=7,
                     fillColor=INK, fontName="Helvetica-Bold"))
    if y_label:
        d.add(String(0, height - 19 if title else height - 9, y_label,
                     fontSize=6.3, fillColor=MUTED))
    if x_label:
        d.add(String(width / 2 - len(x_label) * 1.6, 1, x_label,
                     fontSize=6.3, fillColor=MUTED))
    return d


def month_bar(labels, values, title, y_label, x_label="Month", colour=ACCENT,
              partial=None, decimals=0, width=CONTENT_W, height=44 * mm):
    """
    One bar per month, with its value printed above it.

    Months that are not complete are drawn in a lighter tone and marked in the
    label, because a partial month's TOTAL is not comparable with a whole one —
    the eye would otherwise read a short February as a weak February.
    """
    d = Drawing(width, height)
    bc = VerticalBarChart()
    bc.x, bc.y = 40, 30
    bc.width, bc.height = width - 56, height - 56
    bc.data = [list(values)]
    bc.categoryAxis.categoryNames = [
        f"{l} (partial)" if partial and partial[i] else l for i, l in enumerate(labels)]
    bc.categoryAxis.labels.fontSize = 6.2
    bc.categoryAxis.labels.dy = -4
    bc.valueAxis.valueMin = 0
    bc.valueAxis.valueMax = max(values) * 1.22 if values and max(values) > 0 else 1
    bc.valueAxis.labels.fontSize = 6
    bc.valueAxis.strokeColor = RULE
    bc.valueAxis.gridStrokeColor = colors.HexColor("#eef1f5")
    bc.valueAxis.visibleGrid = True
    bc.bars[0].fillColor = colour
    bc.bars[0].strokeColor = None
    for i in range(len(values)):
        if partial and partial[i]:
            bc.bars[(0, i)].fillColor = BAND
    bc.barLabelFormat = lambda v, d=decimals: ("" if v is None else f"{v:,.{d}f}")
    bc.barLabels.fontSize = 6.2
    bc.barLabels.fillColor = INK
    bc.barLabels.dy = 4
    bc.barSpacing = 2
    bc.groupSpacing = 18
    d.add(bc)
    return chart_frame(d, width, height, title, x_label, y_label)


def grouped_bar(labels, series, title, y_label, x_label="Month",
                decimals=0, width=CONTENT_W, height=46 * mm):
    """Several measured series over the same months. series: [(name, values, colour)]."""
    d = Drawing(width, height)
    bc = VerticalBarChart()
    bc.x, bc.y = 40, 34
    bc.width, bc.height = width - 56, height - 60
    bc.data = [list(v) for _, v, _ in series]
    bc.categoryAxis.categoryNames = list(labels)
    # A None entry draws no bar at all, which is how a source that did not yet
    # exist in a month is shown - never as a zero, which would read as 0% uptime.
    bc.categoryAxis.labels.fontSize = 6.2
    bc.categoryAxis.labels.dy = -4
    allv = [v for _, vals, _ in series for v in vals if v is not None]
    bc.valueAxis.valueMin = 0
    bc.valueAxis.valueMax = max(allv) * 1.2 if allv and max(allv) > 0 else 1
    bc.valueAxis.labels.fontSize = 6
    bc.valueAxis.strokeColor = RULE
    bc.valueAxis.gridStrokeColor = colors.HexColor("#eef1f5")
    bc.valueAxis.visibleGrid = True
    for i, (_, _, col) in enumerate(series):
        bc.bars[i].fillColor = col
        bc.bars[i].strokeColor = None
    bc.barLabelFormat = lambda v, d=decimals: ("" if v is None else f"{v:,.{d}f}")
    bc.barLabels.fontSize = 5.6
    bc.barLabels.fillColor = INK
    bc.barLabels.dy = 3
    bc.groupSpacing = 14
    d.add(bc)
    x = 40
    for name, _, col in series:
        d.add(Rect(x, 6, 9, 4, fillColor=col, strokeColor=None))
        d.add(String(x + 12, 6, name, fontSize=6.2, fillColor=MUTED))
        x += 18 + len(name) * 3.4
    return chart_frame(d, width, height, title, "", y_label)


def scatter_chart(xs, ys, fit, width=CONTENT_W, height=50 * mm,
                  x_label="", y_label="", title=""):
    d = Drawing(width, height)
    x0, y0 = 40, 30
    w, h = width - 56, height - (62 if title else 50)
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = 0, max(ys) * 1.1
    def px(v): return x0 + (v - xmin) / (xmax - xmin or 1) * w
    def py(v): return y0 + (v - ymin) / (ymax - ymin or 1) * h
    d.add(Line(x0, y0, x0 + w, y0, strokeColor=RULE))
    d.add(Line(x0, y0, x0, y0 + h, strokeColor=RULE))
    for xv, yv in zip(xs, ys):
        d.add(Circle(px(xv), py(yv), 1.6, fillColor=ACCENT, strokeColor=None,
                     fillOpacity=0.55))
    m, c = fit
    d.add(Line(px(xmin), py(m * xmin + c), px(xmax), py(m * xmax + c),
               strokeColor=BAD, strokeWidth=1.3))
    d.add(String(x0, 6, x_label, fontSize=6.3, fillColor=MUTED))
    for i in range(5):
        v = ymin + (ymax - ymin) * i / 4
        d.add(String(4, py(v) - 2, f"{v:,.0f}", fontSize=5.6, fillColor=MUTED))
    for i in range(5):
        v = xmin + (xmax - xmin) * i / 4
        d.add(String(px(v) - 6, y0 - 9, f"{v:,.1f}", fontSize=5.6, fillColor=MUTED))
    return chart_frame(d, width, height, title, "", y_label)


def forecast_chart(hist, fcst, width=CONTENT_W, height=56 * mm, title=""):
    """Historical actual, central forecast and the prediction interval."""
    d = Drawing(width, height)
    x0, y0 = 40, 32
    w, h = width - 56, height - (64 if title else 52)
    n_h, n_f = len(hist), len(fcst)
    total = n_h + n_f
    ally = [v for _, v in hist] + [p["upper_mwh"] for p in fcst]
    ymax = max(ally) * 1.12 if ally else 1
    def px(i): return x0 + i / max(1, total - 1) * w
    def py(v): return y0 + v / (ymax or 1) * h

    d.add(Line(x0, y0, x0 + w, y0, strokeColor=RULE))
    d.add(Line(x0, y0, x0, y0 + h, strokeColor=RULE))
    for i in range(5):
        v = ymax * i / 4
        d.add(Line(x0, py(v), x0 + w, py(v), strokeColor=colors.HexColor("#eef1f5")))
        d.add(String(3, py(v) - 2, f"{v:,.0f}", fontSize=5.6, fillColor=MUTED))

    # prediction interval as stacked thin rectangles
    for k, p in enumerate(fcst):
        i = n_h + k
        xl = px(i) - (w / max(1, total - 1)) / 2
        wid = w / max(1, total - 1)
        d.add(Rect(xl, py(p["lower_mwh"]), wid,
                   max(0.5, py(p["upper_mwh"]) - py(p["lower_mwh"])),
                   fillColor=BAND, strokeColor=None, fillOpacity=0.85))

    prev = None
    for i, (_, v) in enumerate(hist):
        if prev is not None:
            d.add(Line(px(i - 1), py(prev), px(i), py(v),
                       strokeColor=ACCENT, strokeWidth=1.2))
        prev = v
    last = hist[-1][1] if hist else 0
    prev = last
    for k, p in enumerate(fcst):
        i = n_h + k
        d.add(Line(px(i - 1), py(prev), px(i), py(p["forecast_mwh"]),
                   strokeColor=BAD, strokeWidth=1.3, strokeDashArray=(3, 2)))
        prev = p["forecast_mwh"]
    d.add(Line(px(n_h - 1), y0, px(n_h - 1), y0 + h,
               strokeColor=MUTED, strokeWidth=0.6, strokeDashArray=(1.5, 2)))
    d.add(String(px(n_h - 1) + 3, y0 + h - 6, "forecast starts", fontSize=5.8,
                 fillColor=MUTED))
    x = x0
    for name, col in (("Historical actual", ACCENT), ("Central forecast", BAD),
                      ("90% prediction interval", BAND)):
        d.add(Rect(x, 6, 9, 4, fillColor=col, strokeColor=None))
        d.add(String(x + 12, 6, name, fontSize=6.3, fillColor=MUTED))
        x += 16 + len(name) * 3.4
    return chart_frame(d, width, height, title, "", "Energy, MWh / day")


# ── page furniture ───────────────────────────────────────────────────────────
class Doc(BaseDocTemplate):
    """
    Two page templates, so the report header appears on the cover page only.

      'first' — draws the running header in the top margin, frame starts below it.
      'later' — no header; the frame extends upward into the space the header
                occupied, so body content on pages 2+ starts higher rather than
                leaving a blank strip where the header used to be.

    The footer and the page number are drawn by both templates, so they remain on
    every page. Neither is ever inserted as a content flowable: both are painted on
    the canvas in the page margins, which is what keeps them to exactly one per page.
    """

    HEADER_SPACE = 6 * mm          # vertical room the header occupies on page 1

    def __init__(self, path, **kw):
        super().__init__(path, pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN,
                         topMargin=MARGIN, bottomMargin=MARGIN + 6 * mm, **kw)
        bottom = MARGIN + 6 * mm
        first = Frame(MARGIN, bottom, CONTENT_W, PAGE_H - MARGIN - bottom, id="first")
        later = Frame(MARGIN, bottom, CONTENT_W,
                      PAGE_H - MARGIN - bottom + self.HEADER_SPACE, id="later")
        self.addPageTemplates([
            PageTemplate(id="first", frames=[first], onPage=self._cover_chrome),
            PageTemplate(id="later", frames=[later], onPage=self._plain_chrome),
        ])

    # ── shared footer: present on EVERY page, drawn once ─────────────────────
    def _footer(self, canv, doc, note=False):
        """
        Footer rule and page number on every page; the classification sentence on
        page 1 only. The page number uses `doc.page` throughout, so numbering is
        continuous and is never reset by the template switch.
        """
        canv.setStrokeColor(RULE)
        canv.setLineWidth(0.5)
        canv.line(MARGIN, MARGIN + 4 * mm, PAGE_W - MARGIN, MARGIN + 4 * mm)
        canv.setFont("Helvetica", 7)
        canv.setFillColor(MUTED)
        if note:
            canv.drawString(MARGIN, MARGIN,
                            "Every figure is measured, derived or model-predicted "
                            "— see §16 classification.")
        canv.drawRightString(PAGE_W - MARGIN, MARGIN, f"Page {doc.page}")

    def _cover_chrome(self, canv, doc):
        """Page 1 only: the full report header, plus the standard footer."""
        canv.saveState()
        canv.setStrokeColor(RULE)
        canv.setLineWidth(0.5)
        canv.line(MARGIN, PAGE_H - MARGIN + 3 * mm, PAGE_W - MARGIN,
                  PAGE_H - MARGIN + 3 * mm)
        canv.setFont("Helvetica", 7)
        canv.setFillColor(MUTED)
        canv.drawString(MARGIN, PAGE_H - MARGIN + 5 * mm,
                        "Kalyon Nigde 130 MW  ·  AI Performance Assurance  ·  PROTOTYPE")
        canv.drawRightString(PAGE_W - MARGIN, PAGE_H - MARGIN + 5 * mm,
                             "Generated from plant telemetry — not for external circulation")
        self._footer(canv, doc, note=True)
        canv.restoreState()

    def _plain_chrome(self, canv, doc):
        """Pages 2+: page number only - no report header, no classification note."""
        canv.saveState()
        self._footer(canv, doc, note=False)
        canv.restoreState()


def h1(n, t):
    """
    A numbered section-label bar: white uppercase text on the dark navy panel.

    Every section is built from this one function, so the colour, the height, the
    padding, the width (always the full content width, flush with the page
    margins) and the typography are identical throughout the report by
    construction rather than by discipline.

    It is a body flowable, deliberately not page chrome: it scrolls with its
    section and is never painted into the repeating page template. `keepWithNext`
    binds it to the content below so reportlab never strands a bar at the foot of
    a page; main() additionally refuses to place one with less than BAR_KEEP of
    page remaining.
    """
    bar = Table([[Paragraph(f"{n}&nbsp;&nbsp;&nbsp;{str(t).upper()}", S_BAR)]],
                colWidths=[CONTENT_W], hAlign="LEFT")
    bar.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), INK),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), BAR_PAD_H),
        ("RIGHTPADDING", (0, 0), (-1, -1), BAR_PAD_H),
        ("TOPPADDING", (0, 0), (-1, -1), BAR_PAD_V),
        ("BOTTOMPADDING", (0, 0), (-1, -1), BAR_PAD_V),
    ]))
    bar.spaceBefore = BAR_SPACE_BEFORE
    bar.spaceAfter = BAR_SPACE_AFTER
    bar._section_bar = True
    return bar


def h2(n, t):
    """A numbered subsection heading inside a section - no bar, accent rule only."""
    return Paragraph(f"{n}&nbsp;&nbsp;{str(t).upper()}", S_H2)


# ── analysis ─────────────────────────────────────────────────────────────────
def build_monthly(full, wmap, curt, mon_sql, cal_days, mon_inv) -> List[Dict[str, Any]]:
    """
    Fold the daily series into calendar months.

    Two rules decide everything here:

    * Every total is summed over COMPLETE days only (>= 1380 recorded minutes), so
      a month is never penalised for a day the logger missed.
    * A month is COMPLETE only when the archive covers its whole calendar span.
      February and May are PARTIAL and are labelled as such everywhere, because
      their totals cannot be set beside March and April — only their per-day means
      can.
    """
    from calendar import monthrange

    by_month: Dict[str, List[Dict[str, Any]]] = {}
    for r in full:
        by_month.setdefault(f"{r['d']:%Y-%m}", []).append(r)
    curt_map = {m["month"]: m for m in curt["by_month"]}
    sql_map = {m["month"]: m for m in mon_sql}

    out = []
    for m, rs in sorted(by_month.items()):
        y, mo = int(m[:4]), int(m[5:7])
        days_in_month = monthrange(y, mo)[1]
        energy = sum(r["energy_integrated_mwh"] for r in rs)
        paired = [r for r in rs if r["d"] in wmap and wmap[r["d"]]["minutes"] >= 1380]
        gti = sum(wmap[r["d"]]["gti_kwh_m2"] for r in paired)
        e_paired = sum(r["energy_integrated_mwh"] for r in paired)
        # Implied PR is computed only where enough days carry BOTH a complete
        # plant record and a complete weather record; otherwise it is withheld.
        pr = (100 * e_paired / (gti * INSTALLED_CAPACITY_MW)) if len(paired) >= 5 and gti > 0 else None
        cm = curt_map.get(m, {})
        sq = sql_map.get(m, {})
        inv = mon_inv.get(m, {})
        energies = sorted(v["energy_mwh"] for v in inv.values()) if inv else []
        med = statistics.median(energies) if energies else None
        dev = sorted(((100 * (v["energy_mwh"] - med) / med, u) for u, v in inv.items()))             if med else []
        out.append({
            "month": m,
            "calendar_days_present": cal_days.get(m, 0),
            "days_in_month": days_in_month,
            "complete_month": cal_days.get(m, 0) >= days_in_month,
            "complete_days": len(rs),
            "energy_mwh": energy,
            "mean_mwh_day": energy / len(rs),
            "peak_mw": max(r["peak_mw"] for r in rs),
            "setpoint_minutes": sum(r["curtailed_minutes"] for r in rs),
            "producing_minutes": sum(r["producing_minutes"] for r in rs),
            "limited_minutes": int(cm.get("minutes", 0)),
            "limited_days": int(cm.get("days", 0)),
            "mean_setpoint_pct": cm.get("avg_setpoint_pct"),
            "wms_days": len(paired),
            "gti_sum": gti,
            "gti_mean_day": (gti / len(paired)) if paired else None,
            "energy_paired": e_paired,
            "pr_pct": pr,
            "resource_coverage_pct": 100 * len(paired) / len(rs),
            "inv_median_mwh": med,
            "inv_low": dev[0] if dev else None,
            "inv_high": dev[-1] if dev else None,
            "inv_spread_pp": (dev[-1][0] - dev[0][0]) if dev else None,
            "inv_alarms": sum(v["alarm_rows"] for v in inv.values()),
            "sql_energy_mwh": sq.get("energy_mwh"),
            "sql_complete_days": sq.get("complete_days"),
        })
    return out


def analyse() -> Dict[str, Any]:
    """Run every query and model once; everything the PDF prints comes from here."""
    db = ex.session()
    try:
        cov = ex.coverage(db)
        daily = ex.daily_plant(db)
        weather = ex.daily_weather(db)
        stations = ex.station_comparison(db)
        inverters = ex.inverter_summary(db)
        curt = ex.curtailment_events(db)
        clip = ex.clipping_candidates(db)
        alarms = ex.alarm_summary(db)
        integ = ex.integrity_summary(db)
        mon_sql = ex.monthly_plant_sql(db)
        cal_days = ex.calendar_days_by_month(db)
        mon_inv = ex.monthly_inverter(db)
        mon_q = ex.monthly_quality(db)
    finally:
        db.close()

    full = [r for r in daily if r["minutes"] >= 1380]
    wmap = {w["d"]: w for w in weather}
    paired = [r for r in full if r["d"] in wmap and wmap[r["d"]]["minutes"] >= 1380]

    tot_int = sum(r["energy_integrated_mwh"] for r in full)
    tot_reg = sum(r["energy_register_mwh"] for r in full)

    monthly = build_monthly(full, wmap, curt, mon_sql, cal_days, mon_inv)
    # Independent verification: the month-by-month roll-up of the daily series
    # must equal the month aggregate computed separately in SQL. A mismatch is
    # printed and carried into the report rather than quietly tolerated.
    checks = [(m["month"], m["energy_mwh"], m["sql_energy_mwh"],
               m["complete_days"], m["sql_complete_days"]) for m in monthly]
    mismatch = [c for c in checks
                if c[2] is None or abs(c[1] - c[2]) > 0.5 or c[3] != c[4]]
    for mth, a_e, s_e, a_d, s_d in checks:
        print(f"  check {mth}: daily roll-up {a_e:,.1f} MWh / {a_d} d  vs  "
              f"SQL {s_e:,.1f} MWh / {s_d} d  "
              f"{'MISMATCH' if (mth, a_e, s_e, a_d, s_d) in mismatch else 'ok'}")

    years: Dict[str, Dict[str, Any]] = {}
    for r in full:
        y = f"{r['d']:%Y}"
        v = years.setdefault(y, {"year": y, "days": 0, "energy_mwh": 0.0,
                                 "peak_mw": 0.0, "setpoint_minutes": 0,
                                 "first": r["d"], "last": r["d"]})
        v["days"] += 1
        v["energy_mwh"] += r["energy_integrated_mwh"]
        v["peak_mw"] = max(v["peak_mw"], r["peak_mw"])
        v["setpoint_minutes"] += r["curtailed_minutes"]
        v["first"] = min(v["first"], r["d"])
        v["last"] = max(v["last"], r["d"])
    for v in years.values():
        v["mean_mwh_day"] = v["energy_mwh"] / v["days"]
        v["months_complete"] = sum(1 for m in monthly
                                   if m["month"].startswith(v["year"]) and m["complete_month"])

    # Conditional performance model: generation explained by measured resource.
    model = None
    if len(paired) >= 20:
        y = np.array([r["energy_integrated_mwh"] for r in paired])
        g = np.array([wmap[r["d"]]["gti_kwh_m2"] for r in paired])
        lr = LinearRegression().fit(g.reshape(-1, 1), y)
        pred = lr.predict(g.reshape(-1, 1))
        r2 = 1 - ((y - pred) ** 2).sum() / ((y - y.mean()) ** 2).sum()
        model = {"slope": float(lr.coef_[0]), "intercept": float(lr.intercept_),
                 "r2": float(r2), "mae": float(np.abs(y - pred).mean()),
                 "n": len(y), "gti": g.tolist(), "actual": y.tolist(),
                 "mean_gti": float(g.mean()), "mean_energy": float(y.mean())}

    # Forecasting experiment.
    X, yv, names, dates = fc.build_frame(daily, None)
    bt = fc.rolling_origin_backtest(X, yv, dates)
    best, best_score, ci, fwd = None, None, None, None
    if "error" not in bt:
        best, best_score = fc.select_model(bt["scores"])
        ci = fc.conformal_interval(bt, best)
        fwd = fc.forecast_forward(X, yv, dates, daily, names, best,
                                  fc.HORIZON, ci.get("half_width_mwh"))
    return dict(monthly=monthly, monthly_mismatch=mismatch, monthly_quality=mon_q,
                monthly_inverter=mon_inv, yearly=sorted(years.values(), key=lambda v: v["year"]),
                coverage=cov, daily=daily, full=full, weather=weather, paired=paired,
                wmap=wmap, stations=stations, inverters=inverters, curt=curt,
                clip=clip, alarms=alarms, integ=integ, tot_int=tot_int,
                tot_reg=tot_reg, model=model, X=X, y=yv, dates=dates,
                backtest=bt, best=best, best_score=best_score, ci=ci, forward=fwd,
                feature_names=names)


def _space_sections(flow: List[Any]) -> List[Any]:
    """
    Guarantee a section bar is never the last thing on a page, without buying
    that guarantee with a third of a blank page.

    Every bar is preceded by a CondPageBreak, so it is only ever placed where at
    least BAR_KEEP of the frame remains below it — a bar can therefore never end
    a page.

    What follows the bar decides the rest:

    * a **table** is left free to split. Its own header row repeats on the
      continuation (`repeatRows`), so the section reads correctly across the
      break, and the page before it fills up instead of being abandoned.
    * anything **else** — a chart, a KPI block, a paragraph — cannot split, so it
      is bound to the bar with KeepTogether and the pair travels together.
    """
    out: List[Any] = []
    i = 0
    while i < len(flow):
        f = flow[i]
        if getattr(f, "_section_bar", False):
            out.append(CondPageBreak(BAR_KEEP))
            nxt = flow[i + 1] if i + 1 < len(flow) else None
            if nxt is not None and not isinstance(nxt, Table):
                out.append(KeepTogether([f, nxt]))
                i += 2
                continue
        out.append(f)
        i += 1
    return out


def main() -> str:
    """Run the analysis, render the PDF, return its path."""
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir,
                        f"Kalyon_AI_Assurance_Prototype_{datetime.now():%Y%m%d_%H%M}.pdf")

    print("Analysing Kalyon database ...")
    a = analyse()
    print(f"  {len(a['full'])} complete days | {a['tot_int']:,.0f} MWh integrated")
    if a.get("best"):
        print(f"  best model: {a['best']} (RMSE {a['best_score']['rmse']:,.0f} MWh)")

    import report_body
    doc = Doc(path)
    # Page 1 uses 'first' (with header); everything after it uses 'later'.
    story = [NextPageTemplate("later")] + _space_sections(report_body.story(a))
    doc.build(story)
    print(f"PDF written: {path}")
    return path


if __name__ == "__main__":
    main()
