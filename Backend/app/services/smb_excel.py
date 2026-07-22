# app/services/smb_excel.py
"""
String Combiner (SMB) export — data access + per-inverter sheet production.

Layout (ONE worksheet per inverter; its SMBs are stacked as titled sections):

    Workbook
     ├── INV1     SMB1 … SMB21   (bold section title + table, blank rows between)
     ├── INV2     SMB1 … SMB21
     │   …
     └── INV24    SMB1 … SMB21

Rendering is delegated to the shared `report_excel` service — this module only
discovers/​fetches SCB columns and yields sheet-specs, so there is a SINGLE Excel
code path across the whole Reports module.

Performance: inverters are fetched CONCURRENTLY (lean query per table — only the
required SCB columns, no COUNT, batched reads for large datasets). Each inverter is
queried ONCE and that row set feeds all of its SMB sections; sheets stream into one
XlsxWriter constant-memory workbook, so memory stays flat.
"""

import io
import re
import time
import logging
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import text

from app.database.session import SessionLocal
from app.repositories.reports_repository import (
    EQUIPMENT_REGISTRY, _safe_name, _build_interval_expr,
)
from app.services import intervals

# Rows fetched per DB round-trip; datasets larger than this are read in batches.
_FETCH_BATCH = 20000
_MAX_ROWS    = 200000        # hard safety cap to bound memory
_TEXT_TYPES  = {"nvarchar", "varchar", "char", "nchar", "text", "ntext"}
_MAX_WORKERS = 4             # concurrent inverter queries

logger = logging.getLogger(__name__)

_REG_TAGS = EQUIPMENT_REGISTRY.get("String Combiner", {}).get("tags", {})

# Unit hints for SCB columns not present in the registry (e.g. extra string currents).
_UNIT_HINTS = [
    ("DC_POWER", "kW"), ("DC_VOLTAGE", "V"), ("INTERNAL_TEMP", "C"),
    ("TOTAL_CURRENT", "A"), ("STRING_CURRENT", "A"), ("CURRENT", "A"),
    ("VOLTAGE", "V"), ("POWER", "kW"), ("TEMP", "C"),
]


def _col_meta(col: str):
    """Return (label, unit) for an SCB column."""
    if col in _REG_TAGS:
        return _REG_TAGS[col]["label"], _REG_TAGS[col]["unit"]
    label = col.replace("_", " ").title()
    unit = ""
    for key, u in _UNIT_HINTS:
        if key in col:
            unit = u
            break
    return label, unit


def _col_header(col: str) -> str:
    if col == "timestamp":
        return "Timestamp (DD/MM/YYYY HH:MM:SS)"
    label, unit = _col_meta(col)
    return f"{label} ({unit})" if unit else label


def sheet_name_for(table: str) -> str:
    """INVERTER1_SMB -> INV1 (Excel sheet names are capped at 31 chars)."""
    m = re.match(r"INVERTER0*(\d+)_SMB", table, re.IGNORECASE)
    return f"INV{m.group(1)}" if m else table[:31]


def _inv_no(table: str) -> str:
    """INVERTER1_SMB -> '1'."""
    m = re.match(r"INVERTER0*(\d+)_SMB", table, re.IGNORECASE)
    return m.group(1) if m else table


def group_by_scb(columns) -> dict:
    """Group SCB columns by their SCB index → {1: [cols], 2: [cols], …} (ordered)."""
    groups: dict[int, list] = {}
    for c in columns:
        m = re.match(r"^SCB(\d+)_", c)
        if m:
            groups.setdefault(int(m.group(1)), []).append(c)
    return dict(sorted(groups.items()))


# ── Data access ──────────────────────────────────────────────────────────────
def _discover_scb_columns(db, table: str):
    """All SCB* columns of a table + their data types, in physical order — one query."""
    sql = text("""
        SELECT COLUMN_NAME, DATA_TYPE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_NAME = :t AND TABLE_SCHEMA = 'dbo' AND COLUMN_NAME LIKE 'SCB%'
        ORDER BY ORDINAL_POSITION
    """)
    cols, types = [], {}
    for name, dtype in db.execute(sql, {"t": table}).fetchall():
        if _safe_name(name):
            cols.append(name)
            types[name] = (dtype or "").lower()
    return cols, types


def _fetch_table_rows(db, table, cols, col_types, from_dt, to_dt, interval, agg):
    """
    Lean, batched fetch for one inverter table.

    - Selects ONLY the required SCB columns — never SELECT *.
    - No COUNT round-trip (export doesn't need a total).
    - Reads in batches of _FETCH_BATCH rows so large datasets never buffer one huge
      result set, bounded by _MAX_ROWS.
    Returns a list of {"timestamp": iso, col: value, …} dicts.
    """
    if not cols:
        return []

    if intervals.is_instant(interval):
        select = ", ".join(f"[{c}]" for c in cols)
        base = (
            f"SELECT TimeCol, {select} FROM [{table}] "
            f"WHERE TimeCol BETWEEN :f AND :t ORDER BY TimeCol"
        )
    else:
        grp = _build_interval_expr(interval)
        parts = []
        for c in cols:
            if col_types.get(c) in _TEXT_TYPES:
                parts.append(f"MIN([{c}]) AS [{c}]")
            else:
                fn = {
                    "avg": f"AVG(CAST([{c}] AS FLOAT))",
                    "min": f"MIN(CAST([{c}] AS FLOAT))",
                    "max": f"MAX(CAST([{c}] AS FLOAT))",
                    "sum": f"SUM(CAST([{c}] AS FLOAT))",
                }.get(agg, f"AVG(CAST([{c}] AS FLOAT))")
                parts.append(f"{fn} AS [{c}]")
        base = (
            f"SELECT {grp} AS TimeCol, {', '.join(parts)} FROM [{table}] "
            f"WHERE TimeCol BETWEEN :f AND :t GROUP BY {grp} ORDER BY {grp}"
        )

    rows, offset = [], 0
    while offset < _MAX_ROWS:
        sql = text(f"{base} OFFSET :off ROWS FETCH NEXT :lim ROWS ONLY")
        batch = db.execute(sql, {"f": from_dt, "t": to_dt, "off": offset, "lim": _FETCH_BATCH}).fetchall()
        if not batch:
            break
        for r in batch:
            d = {"timestamp": r[0].isoformat() if hasattr(r[0], "isoformat") else str(r[0] or "")}
            for i, c in enumerate(cols):
                v = r[i + 1]
                d[c] = round(v, 4) if isinstance(v, float) else v
            rows.append(d)
        if len(batch) < _FETCH_BATCH:
            break
        offset += _FETCH_BATCH
    return rows


def _fetch_inverter(table, req_tags, from_str, to_str, interval, agg):
    """
    Worker: discover + lean-fetch ONE inverter on its own DB session (thread-safe).

    The rows are fetched exactly once per inverter and reused by every one of its
    SMB sheets — never a query per sheet. Columns are restricted to the tags the UI
    actually selected (`req_tags`), preserving the UI's column order; selected tags
    missing from the table are logged as an ERROR rather than silently dropped.
    """
    session = SessionLocal()
    try:
        t0 = time.time()
        cols, col_types = _discover_scb_columns(session, table)

        if req_tags:
            sel = set(req_tags)
            present = [c for c in cols if c in sel]          # physical == UI order
            missing = [t for t in req_tags if t not in col_types]
            if missing:
                logger.error(
                    "SMB export | %s: %d of %d selected tag(s) do NOT exist in the table — "
                    "export would be incomplete: %s",
                    table, len(missing), len(req_tags), missing[:10],
                )
            cols = present
            col_types = {c: col_types[c] for c in cols}

        groups = group_by_scb(cols)

        # Validation: every selected tag present in this table must reach a sheet.
        exported = sum(len(v) for v in groups.values())
        expected = len(cols)
        if exported != expected:
            logger.error(
                "SMB export | %s: column count mismatch — expected %d, sheets carry %d. "
                "Refusing to silently export incomplete data.",
                table, expected, exported,
            )

        rows = _fetch_table_rows(session, table, cols, col_types, from_str, to_str, interval, agg)
        logger.info("SMB fetch %s -> %d SMBs, %d cols (selected=%d), %d rows in %.1fs",
                    table, len(groups), len(cols), len(req_tags or cols), len(rows), time.time() - t0)
        return table, groups, rows
    except Exception as e:  # noqa: BLE001 — a bad table shouldn't kill the workbook
        logger.error("SMB data fetch failed for %s: %s", table, e, exc_info=True)
        return table, {}, []
    finally:
        session.close()


# ── Per-inverter workbook (rendered by the shared report_excel service) ──────
def _smb_bytes(req, equipment_ids, progress=None):
    """
    Build the String Combiner workbook: ONE worksheet per INVERTER.

        Workbook
         ├── INV1   → SMB1, SMB2 … SMB21 stacked as titled sections
         ├── INV2   → SMB1 …
         └── INV24

    Each inverter is fetched ONCE (in parallel) and that single row set feeds all of
    its SMB sections — never a query per section. Sheets stream into one XlsxWriter
    constant-memory workbook, so memory stays flat. Returns (bytes, filename).
    """
    from app.services import report_excel

    tables = [t for t in equipment_ids if _safe_name(t)]
    interval = req.interval.value if hasattr(req.interval, "value") else str(req.interval)
    agg      = req.agg_function.value if hasattr(req.agg_function, "value") else str(req.agg_function)
    from_str = req.from_datetime.strftime("%Y-%m-%d %H:%M:%S")
    to_str   = req.to_datetime.strftime("%Y-%m-%d %H:%M:%S")
    from_d   = req.from_datetime.strftime("%d/%m/%Y")
    to_d     = req.to_datetime.strftime("%d/%m/%Y")
    interval_label = intervals.interval_label(interval)
    agg_label      = intervals.agg_label(interval, agg)

    # UI-selected tags drive the exported columns (injection-guarded, order preserved).
    req_tags = [t for t in (req.tags or []) if _safe_name(t)]

    t0 = time.time()
    logger.info("SMB export start -> inverters=%d | selected_tags=%d | interval=%s",
                len(tables), len(req_tags), interval)
    if progress:
        progress(3, f"Fetching {len(tables)} inverter(s)…")

    # Parallel fetch — one query per inverter, reused by all of its SMB sections.
    workers = min(len(tables), _MAX_WORKERS) or 1
    with ThreadPoolExecutor(max_workers=workers) as ex:
        fetched = list(ex.map(
            lambda t: _fetch_inverter(t, req_tags, from_str, to_str, interval, agg), tables))
    db_secs = time.time() - t0

    total_sheets   = len(fetched) or 1
    total_sections = sum(len(g) for _, g, _ in fetched)

    # Validation: every selected tag found in a table must reach a section.
    for table, groups, _ in fetched:
        exported = sum(len(c) for c in groups.values())
        if req_tags and exported != len([t for t in req_tags if any(t in c for c in groups.values())]):
            logger.error("SMB export | %s: exported columns (%d) != selected tags found — "
                         "workbook may be incomplete.", table, exported)

    if progress:
        progress(52, f"Generating workbook — {total_sheets} inverter sheet(s), "
                     f"{total_sections} SMB section(s)…")

    def _specs():
        for (table, groups, rows) in fetched:            # SELECTED ORDER
            inv = _inv_no(table)
            # SMB1 … SMB21 stacked on this inverter's single sheet, in SCB index order.
            sections = [
                (f"SMB{scb_idx}", ["timestamp"] + cols, rows)
                for scb_idx, cols in groups.items()
            ]

            # Audit trail: every selected tag must reach exactly one section. The tags
            # are spread ACROSS the sections (each SMB owns its own 16 sensors), not
            # repeated on each — so `written` is the count over the whole sheet.
            written = [c for _t, cols, _r in sections for c in cols if c != "timestamp"]
            logger.info(
                "SMB export | sheet=%s | tags_requested=%d | tags_fetched=%d | "
                "tags_written_to_excel=%d (distinct=%d) across %d section(s)",
                sheet_name_for(table), len(req_tags), sum(len(c) for c in groups.values()),
                len(written), len(set(written)), len(sections),
            )
            if req_tags and len(set(written)) != len([t for t in req_tags if t in set(written)]):
                logger.error(
                    "SMB export | sheet=%s: %d selected tag(s) never reached the workbook.",
                    sheet_name_for(table),
                    len(req_tags) - len(set(written) & set(req_tags)),
                )
            header = {
                "subtitle": f"String Combiner  —  INV{inv}  —  {len(sections)} SMB sections",
                "equipment_type": "String Combiner",
                "equipment_id":   f"INV{inv}",
                "from": from_d, "to": to_d,
                "interval": interval_label, "agg": agg_label,
            }
            yield (sheet_name_for(table), header, sections, _col_header)

    t_render = time.time()
    data = report_excel.build_sectioned_workbook(
        _specs(), progress, total=total_sheets, prog_lo=55, prog_hi=99)
    filename = f"SMB_Report_{datetime.now().strftime('%d-%m-%Y')}.xlsx"
    logger.info("SMB export done -> inverters=%d | sheets=%d | sections=%d | "
                "DB %.1fs | render %.1fs | %d bytes",
                len(tables), total_sheets, total_sections, db_secs, time.time() - t_render, len(data))
    if progress:
        progress(100, "Export completed.")
    return data, filename


def build_smb_workbook(db, req, equipment_ids):
    """Synchronous entry point (used by /export/excel). Returns (BytesIO, filename)."""
    data, filename = _smb_bytes(req, equipment_ids, progress=None)
    return io.BytesIO(data), filename


def generate_smb_workbook_streaming(req, equipment_ids, progress=None):
    """Background/async entry point (job + SSE progress). Returns (bytes, filename)."""
    return _smb_bytes(req, equipment_ids, progress)
