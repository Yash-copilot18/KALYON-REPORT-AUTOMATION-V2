# app/services/isolator_excel.py
"""
Generic per-tracker Excel export for the wide `dbo.T*_IS*` tables.

One workbook, ONE worksheet per selected tracker/isolator ID (IS01, IS02, …).
Each worksheet contains only that ID's columns — the leading timestamp column(s)
plus its six parameters — so a 125-tracker table never triggers a SELECT * or a
750-column read. Column names are generated dynamically from the shared
`isolator_columns` model, so it works for every ID and every T*_IS* table with
zero hardcoding.

Formatting: company/tracker title, date-range subtitle, bold unit-labelled
headers, banded (zebra) rows with borders, frozen header row, auto-filter (Excel
table styling), auto-fit columns, DD/MM/YYYY HH:MM:SS timestamps, 3-decimal
numbers, and a "No Data Available" placeholder for empty ranges.

Performance/reliability: a single dynamic parameterized query is STREAMED from
SQL Server in batches (server-side cursor), pivoted into compact per-tracker
buffers, then written with XlsxWriter constant-memory — bounded memory even for
all 125 trackers. Reused by tracker_service (dbo.T1_IS2) and the /isolator
export endpoint (any T*_IS* table).
"""

import io
import time
import logging
from typing import List, Sequence, Tuple, Optional, Callable

import xlsxwriter
from sqlalchemy import text

from app.database.session import SessionLocal
from app.services import schema_cache, isolator_columns as ic, intervals
from app.repositories.reports_repository import _build_interval_expr

logger = logging.getLogger(__name__)

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# Export tuning
_FETCH_BATCH = 10_000        # rows pulled per DB round-trip (streamed, batched)
_MAX_ROWS    = 50_000        # hard row cap to bound memory

# ── Palette (matches the rest of the app's Excel reports) ────────────────────
# Clean white/black corporate theme (printer-friendly) — matches report_excel.
BLACK, WHITE = "#000000", "#FFFFFF"
GRAY_HDR, ROW_ALT, BORDER = "#F2F2F2", "#FAFAFA", "#000000"

# Friendly labels for the system/timestamp columns that may lead a sheet.
_TS_LABELS = {"TimeCol": "Timestamp", "LocalCol": "Local Time", "MSecCol": "Millisecond"}
_PARAM_HEADERS = [f"{lbl} ({u})" if u else lbl for _, lbl, u in ic.PARAMS]


def _ts_header(col: str) -> str:
    return _TS_LABELS.get(col, col)


def _fmt_ts(val) -> str:
    if val is None:
        return ""
    try:
        return val.strftime("%d/%m/%Y %H:%M:%S")
    except AttributeError:
        return str(val)


def _fmt_date(dt_str: str) -> str:
    try:
        d = dt_str.split(" ")[0].split("-")
        return f"{d[2]}/{d[1]}/{d[0]}"
    except Exception:
        return str(dt_str)


def _make_formats(wb):
    """All cell formats created once and reused across every sheet/cell."""
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
        "sub":   wb.add_format({"font_name": "Calibri", "font_size": 10, "font_color": BLACK,
                                "align": "left", "valign": "vcenter"}),
        "hdr":   wb.add_format({"font_name": "Calibri", "bold": True, "font_color": BLACK,
                                "bg_color": GRAY_HDR, "align": "center", "valign": "vcenter",
                                "text_wrap": True, **base}),
        "nodata": wb.add_format({"font_name": "Calibri", "bold": True, "font_color": BLACK,
                                 "bg_color": WHITE, "align": "left", "valign": "vcenter", **base}),
        "ts_even": data("left", WHITE),  "ts_odd": data("left", ROW_ALT),
        "num_even": data("right", WHITE, num=True), "num_odd": data("right", ROW_ALT, num=True),
        "na_even": data("center", WHITE), "na_odd": data("center", ROW_ALT),
    }


# ── Dynamic, parameterized SQL (never SELECT *) ──────────────────────────────
def _build_sql(table: str, param_cols: List[str], ts_cols: Sequence[str],
               interval: str, agg: str) -> str:
    """
    Projection is always: <ts_cols…> then the selected trackers' six params.
    ts_cols[0] must be TimeCol. Row shape is fixed so the pivot can slice params
    from a constant offset. Dates are bound parameters.
    """
    if intervals.is_instant(interval):
        select = ", ".join([f"[{c}]" for c in ts_cols] + [f"[{c}]" for c in param_cols])
        return (f"SELECT {select} FROM [{table}] "
                f"WHERE TimeCol BETWEEN :from_dt AND :to_dt "
                f"ORDER BY TimeCol OFFSET 0 ROWS FETCH NEXT :cap ROWS ONLY")

    grp = _build_interval_expr(interval)
    fn = {"avg": "AVG", "min": "MIN", "max": "MAX", "sum": "SUM"}.get(agg, "AVG")
    # First ts col is the bucket key; any others (e.g. LocalCol) carry MIN per bucket.
    ts_select = [f"{grp} AS TimeCol"] + [f"MIN([{c}]) AS [{c}]" for c in ts_cols[1:]]
    aggs = [f"{fn}(CAST([{c}] AS FLOAT)) AS [{c}]" for c in param_cols]
    return (f"SELECT {', '.join(ts_select + aggs)} FROM [{table}] "
            f"WHERE TimeCol BETWEEN :from_dt AND :to_dt "
            f"GROUP BY {grp} ORDER BY {grp} "
            f"OFFSET 0 ROWS FETCH NEXT :cap ROWS ONLY")


def _write_sheet(wb, fmt, sheet_name, tid, table, ts_cols, ts_values, values, date_range, title):
    """One worksheet: the leading timestamp column(s) + the six params for a tracker."""
    ws = wb.add_worksheet(sheet_name)

    n_ts = len(ts_cols)
    headers = [_ts_header(c) for c in ts_cols] + _PARAM_HEADERS
    ncols = len(headers)
    nrows = len(ts_values[0]) if ts_values else 0

    # Auto-fit widths up front (constant_memory needs set_column before writes).
    ts_w = len("Timestamp (DD/MM/YYYY HH:MM:SS)")
    widths = [ts_w] * n_ts + [max(len(h), 12) for h in headers[n_ts:]]
    for row in values:
        for ci, v in enumerate(row, start=n_ts):
            if v is not None:
                widths[ci] = max(widths[ci], len(f"{v:.3f}" if isinstance(v, float) else str(v)))
    for ci, w in enumerate(widths):
        ws.set_column(ci, ci, min(max(w + 1, 12), 40))

    # Title/subtitle written to column A — left-aligned text overflows the empty
    # cells beside it, so no merged cells are needed.
    ws.write(0, 0, title, fmt["title"]); ws.set_row(0, 24)
    ws.write(1, 0, f"{table}  ·  {date_range}", fmt["sub"]); ws.set_row(1, 18)

    HDR = 2
    for ci, h in enumerate(headers):
        ws.write(HDR, ci, h, fmt["hdr"])
    ws.set_row(HDR, 30)

    if nrows == 0:
        ws.write(HDR + 1, 0, "No Data Available", fmt["nodata"])
        ws.freeze_panes(HDR + 1, 0)
        return

    r = HDR
    for i in range(nrows):
        r += 1
        even = (i % 2 == 0)
        ts_fmt = fmt["ts_even" if even else "ts_odd"]
        for j in range(n_ts):
            ws.write(r, j, _fmt_ts(ts_values[j][i]), ts_fmt)
        row_vals = values[i]
        for ci in range(n_ts, ncols):
            v = row_vals[ci - n_ts]
            if v is None:
                ws.write(r, ci, "—", fmt["na_even" if even else "na_odd"])
            else:
                ws.write_number(r, ci, float(v), fmt["num_even" if even else "num_odd"])

    ws.freeze_panes(HDR + 1, 0)          # freeze header row
    ws.autofilter(HDR, 0, r, ncols - 1)  # Excel table filtering


def build_workbook(
    table: str,
    tracker_ids: List[int],
    from_dt: str,
    to_dt: str,
    interval: str,
    agg: str,
    ts_cols: Sequence[str] = ("TimeCol",),
    sheet_title: str = "Tracker",
    progress: Optional[Callable[[int, str], None]] = None,
) -> Tuple[bytes, List[int]]:
    """
    Build one workbook with one worksheet per selected tracker of `table`.
    Returns (xlsx_bytes, validated_ids). Opens its own DB session (background-safe).
    """
    if not ic.is_isolator_table(table):
        raise ValueError(f"Invalid isolator table: {table!r}")

    db = SessionLocal()
    try:
        available, valid_cols = ic.discover_ids(db, table)
        if not schema_cache.table_exists(db, table):
            raise ValueError(f"Table '{table}' not found")
        ids = ic.validate_ids(tracker_ids, available)
        if not ids:
            raise ValueError("No valid trackers selected")

        param_cols = ic.columns_for_ids(ids, valid_cols)   # 6 per tracker, in order
        per = ic.PER_UNIT
        n_ts = len(ts_cols)
        sql = _build_sql(table, param_cols, ts_cols, interval, agg)

        if progress:
            progress(2, f"Querying {len(ids)} tracker(s) from {table}…")

        t0 = time.perf_counter()
        ts_values: List[List] = [[] for _ in ts_cols]       # ts_values[j] = column j
        buffers: List[List] = [[] for _ in ids]             # buffers[t] = list of 6-tuples
        result = db.execute(text(sql), {"from_dt": from_dt, "to_dt": to_dt, "cap": _MAX_ROWS})
        fetched = 0
        while True:
            batch = result.fetchmany(_FETCH_BATCH)
            if not batch:
                break
            for row in batch:
                for j in range(n_ts):
                    ts_values[j].append(row[j])
                vals = row[n_ts:]
                for ti in range(len(ids)):
                    base = ti * per
                    buffers[ti].append(vals[base:base + per])
            fetched += len(batch)
            if progress:
                progress(min(55, 5 + int(fetched / max(_MAX_ROWS, 1) * 50)), f"Fetched {fetched:,} rows")
            if fetched >= _MAX_ROWS:
                break
        query_ms = (time.perf_counter() - t0) * 1000

        date_range = f"{_fmt_date(from_dt)} — {_fmt_date(to_dt)}"

        buf = io.BytesIO()
        wb = xlsxwriter.Workbook(buf, {"constant_memory": True, "in_memory": True})
        fmt = _make_formats(wb)

        tg = time.perf_counter()
        used: set = set()
        total = len(ids)
        for i, tid in enumerate(ids):
            name = _sheet_name(tid, used)
            _write_sheet(wb, fmt, name, tid, table, ts_cols, ts_values, buffers[i],
                         date_range, f"{sheet_title} {tid}")
            if progress:
                progress(58 + int((i + 1) / total * 40), f"Writing sheet {i + 1}/{total}: {name}")

        wb.close()
        data = buf.getvalue()
        logger.info(
            "Isolator export | table=%s | trackers=%d | rows=%d | cols=%d | query=%.0fms | write=%.0fms | %d bytes",
            table, total, len(ts_values[0]) if ts_values[0] else 0, len(param_cols),
            query_ms, (time.perf_counter() - tg) * 1000, len(data),
        )
        if progress:
            progress(100, f"Workbook ready — {total} tracker sheet(s).")
        return data, ids
    finally:
        db.close()


def _sheet_name(tid: int, used: set) -> str:
    """One worksheet name per tracker — IS01, IS02, … IS125."""
    name = ic.id_label(tid)[:31]
    base, i = name, 1
    while name.lower() in used:
        suffix = f"_{i}"
        name = base[:31 - len(suffix)] + suffix
        i += 1
    used.add(name.lower())
    return name
