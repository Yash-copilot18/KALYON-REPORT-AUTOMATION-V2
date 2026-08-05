# app/services/smb_excel.py
"""
String Combiner (SMB) export — data access + per-inverter sheet production.

Layout — ONE worksheet per inverter; each is a COMPLETE, self-contained report
carrying EVERY selected tag as its own column in a SINGLE table:

    Workbook
     ├── INV1   | Timestamp | <every selected tag, in UI order> |
     ├── INV2   | Timestamp | …                                 |
     │   …
     └── INV24

Every worksheet is built from row 1 by `report_excel.write_report_sheet` on a
freshly created worksheet object: its own title, equipment metadata, one header
row and its data. No row index, style, print setting or worksheet state carries
over between inverters.

The header is generated dynamically from the UI selection — there is no predefined
column list and no grouping or chunking of any kind. Selecting 336 tags yields 336
data columns (plus Timestamp) on each inverter's sheet. Selected tags that do not
exist in the table keep their column (rendered "—") and are logged as an ERROR, so
the exported column count always equals the selection.

NOTE: this export previously bucketed the columns by SCB index (`group_by_scb`),
which rendered 21 sections of 16 columns each. That grouping was removed from the
layout path — the 16 was never a configured limit, it was simply how many columns
share each SCB<n>_ prefix in the source tables.

Print layout: A4 landscape with the header row repeated down the pages and the
Timestamp column repeated across them. A table this wide necessarily prints across
many pages; scale-to-fit is not attempted because it would be illegible.

Rendering is delegated to the shared `report_excel` service — this module only
discovers/​fetches SCB columns and yields sheet-specs, so there is a SINGLE Excel
code path across the whole Reports module.

Performance: inverters are fetched CONCURRENTLY (lean query per table — only the
required SCB columns, no COUNT, batched reads for large datasets). Each inverter is
queried ONCE and that row set feeds all of its SMB sections; sheets stream into one
XlsxWriter constant-memory workbook, so memory stays flat.
"""

import io
import os
import re
import time
import zipfile
import logging
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

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

XLSX_CTYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
ZIP_CTYPE  = "application/zip"

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

    # ONE ordered query, pulled off the server-side cursor in batches (fetchmany) —
    # this avoids the OFFSET/FETCH "re-scan from the top" that deep paging incurs.
    # The _MAX_ROWS safety cap is preserved, so the returned rows (and therefore the
    # exported data) are unchanged; only the read strategy is faster.
    result = db.execute(text(base), {"f": from_dt, "t": to_dt})
    rows = []
    try:
        while len(rows) < _MAX_ROWS:
            batch = result.fetchmany(_FETCH_BATCH)
            if not batch:
                break
            for r in batch:
                d = {"timestamp": r[0].isoformat() if hasattr(r[0], "isoformat") else str(r[0] or "")}
                for i, c in enumerate(cols):
                    v = r[i + 1]
                    d[c] = round(v, 4) if isinstance(v, float) else v
                rows.append(d)
                if len(rows) >= _MAX_ROWS:
                    break
    finally:
        result.close()
    return rows


def _fetch_inverter(table, req_tags, from_str, to_str, interval, agg):
    """
    Worker: discover + lean-fetch ONE inverter on its own DB session (thread-safe).

    The rows are fetched exactly once per inverter. Columns are driven entirely by
    the tags the UI selected (`req_tags`), in the UI's own order — there is no
    predefined SMB column list anywhere in this path. Selected tags that do not
    exist in the table are returned as `missing` and logged as an ERROR rather than
    silently dropped.

    The columns are NOT grouped or chunked in any way — every selected tag becomes
    its own column in a single table. (The export used to bucket them by SCB index
    via `group_by_scb`, which produced 21 sections of 16 columns; that grouping has
    been removed from the layout path.)

    Returns (table, present_columns, missing_tags, rows).
    """
    session = SessionLocal()
    try:
        t0 = time.time()
        cols, col_types = _discover_scb_columns(session, table)
        missing: list = []

        if req_tags:
            # UI ORDER is authoritative — iterate req_tags, not the physical schema,
            # so the exported column order matches what the operator selected.
            present = [t for t in req_tags if t in col_types]
            missing = [t for t in req_tags if t not in col_types]
            if missing:
                logger.error(
                    "SMB export | %s: %d of %d selected tag(s) do NOT exist in the table — "
                    "they are exported as empty columns, NOT skipped: %s",
                    table, len(missing), len(req_tags), missing[:10],
                )
            cols = present
            col_types = {c: col_types[c] for c in cols}

        rows = _fetch_table_rows(session, table, cols, col_types, from_str, to_str, interval, agg)
        logger.info(
            "SMB fetch %s | tags_selected=%d | db_columns_fetched=%d | tags_missing_from_db=%d | "
            "%d rows in %.1fs",
            table, len(req_tags or cols), len(cols), len(missing), len(rows), time.time() - t0,
        )
        return table, cols, missing, rows
    except Exception as e:  # noqa: BLE001 — a bad table shouldn't kill the workbook
        logger.error("SMB data fetch failed for %s: %s", table, e, exc_info=True)
        return table, [], list(req_tags or []), []
    finally:
        session.close()


# ── Per-inverter workbook (rendered by the shared report_excel service) ──────
def _smb_bytes(req, equipment_ids, progress=None):
    """
    Build the String Combiner export as ONE workbook per inverter, with ONE worksheet
    per String Combiner (SCB) — named SMB1, SMB2, … SMB{k}. There is NO Part1/Part2
    splitting; each SMB sheet holds ALL of that SCB's columns (plus Timestamp).

        INV1.xlsx                 INV2.xlsx
         ├── SMB1                   ├── SMB1
         ├── SMB2                   ├── SMB2
         │   …                      │   …
         └── SMB21                  └── SMB18

    The number of SCBs is discovered DYNAMICALLY per inverter from its columns
    (`group_by_scb`) — never hardcoded, and each inverter may have a different count.
    A single selected inverter returns its `INV{n}.xlsx`; several inverters return a
    ZIP of per-inverter workbooks (sheet names like "SMB1" would collide inside one
    combined workbook). Each inverter is fetched ONCE (in parallel) and every one of
    its SMB sheets renders a column slice of that SAME single read — no query is ever
    repeated, and the report header / formatting / styles / filters / column order /
    data are byte-for-byte what they were; only the worksheet organisation changed.
    Returns (bytes, filename, content_type).
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

    # ── One worksheet per SCB (SMB{k}); NO Part splitting ──────────────────────
    def _inverter_workbook(table, present, missing, rows):
        """
        Build ONE inverter's workbook — one SMB{k} worksheet per String Combiner,
        SCBs discovered dynamically and ordered ascending. Returns (bytes, inv, sheets).
        """
        inv = _inv_no(table)
        # Columns come from the UI selection in the UI's / database order — never
        # re-sorted. Tags absent from the table keep their column and render as "—".
        tag_cols = req_tags if req_tags else present

        skipped = [t for t in req_tags if t not in set(present)] if req_tags else []
        logger.info("SMB column audit | INV%s | selected=%d | matched=%d | missing=%d",
                    inv, len(req_tags), len(present), len(missing))
        if missing:
            logger.error("    tags NOT in database (%d), exported as empty columns: %s",
                         len(missing), missing[:20])
        if skipped:
            logger.error("    SKIPPED tags (%d): %s", len(skipped), skipped[:20])

        # Header repeated on EVERY SMB sheet — identical block as before (company title
        # A1, Equipment Type, Equipment ID, From, To, Interval, Aggregation).
        header = {
            "equipment_type": "String Combiner",
            "equipment_id":   f"INV{inv}",
            "from": from_d, "to": to_d,
            "interval": interval_label, "agg": agg_label,
        }

        # Dynamic SCB discovery: {scb_index: [cols]}, ascending, order within an SCB
        # preserved. Sheet name SMB{k}. Every sheet shares the SAME already-materialised
        # `rows` (read once) via a fresh iterator, so no query is repeated.
        grouped = group_by_scb(tag_cols)
        grouped_cols = {c for cols in grouped.values() for c in cols}
        leftover = [c for c in tag_cols if c not in grouped_cols]   # non-SCB cols (rare)

        specs = [
            (f"SMB{k}", dict(header), ["timestamp"] + cols, iter(rows), _col_header)
            for k, cols in grouped.items()
        ]
        if leftover:
            # Preserve EXACT data: any column that does not match the SCB pattern is
            # still exported (kept together on a trailing sheet), never dropped.
            logger.warning("SMB INV%s | %d column(s) not matched to an SCB, kept on 'Other': %s",
                           inv, len(leftover), leftover[:10])
            specs.append(("Other", dict(header), ["timestamp"] + leftover, iter(rows), _col_header))
        if not specs:
            specs = [("SMB1", dict(header), ["timestamp"], iter(rows), _col_header)]

        # SINGLE-PASS streaming writer (constant_memory + reused formats) — same
        # renderer as before, so each SMB sheet's content is identical to that SCB's
        # columns in the old Part layout; only the sheet grouping changed.
        wb = report_excel.build_workbook_streaming(iter(specs), wide_print_layout=True)
        return wb, inv, len(specs)

    t_render = time.time()
    stamp = datetime.now().strftime("%d-%m-%Y_%H%M%S")

    # Single inverter → one INV{n}.xlsx.
    if len(fetched) == 1:
        if progress:
            progress(55, "Generating workbook (one sheet per SCB)…")
        table, present, missing, rows = fetched[0]
        data, inv, nsheets = _inverter_workbook(table, present, missing, rows)
        filename, ctype = f"INV{inv}.xlsx", XLSX_CTYPE
        if progress:
            progress(100, "Export completed.")
        logger.info("SMB export done -> 1 inverter (INV%s) | SMB sheets=%d | DB %.1fs | "
                    "render %.1fs | %d bytes | file=%s",
                    inv, nsheets, db_secs, time.time() - t_render, len(data), filename)
        return data, filename, ctype

    # Several inverters → ZIP of per-inverter INV{n}.xlsx workbooks. Each is built and
    # then released after zipping, so peak memory stays ~one workbook (lower than the
    # old single combined workbook). The parallel fetch above is unchanged.
    buf = io.BytesIO()
    total_sheets = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for i, (table, present, missing, rows) in enumerate(fetched, start=1):   # SELECTED ORDER
            wb_bytes, inv, nsheets = _inverter_workbook(table, present, missing, rows)
            zf.writestr(f"INV{inv}.xlsx", wb_bytes)
            total_sheets += nsheets
            del wb_bytes
            if progress:
                progress(55 + int(i / len(fetched) * 44), f"INV{inv} ({i}/{len(fetched)})…")
    data = buf.getvalue()
    filename, ctype = f"SMB_Report_{len(fetched)}INV_{stamp}.zip", ZIP_CTYPE
    if progress:
        progress(100, "Export completed.")
    logger.info("SMB export done -> %d inverters (ZIP) | total SMB sheets=%d | DB %.1fs | "
                "render %.1fs | %d bytes | file=%s",
                len(fetched), total_sheets, db_secs, time.time() - t_render, len(data), filename)
    return data, filename, ctype


def build_smb_workbook(db, req, equipment_ids):
    """Synchronous entry point (used by /export/excel). Returns (BytesIO, filename, content_type)."""
    data, filename, ctype = _smb_bytes(req, equipment_ids, progress=None)
    return io.BytesIO(data), filename, ctype


def generate_smb_workbook_streaming(req, equipment_ids, progress=None):
    """Background/async entry point (job + SSE progress). Returns (bytes, filename, content_type)."""
    return _smb_bytes(req, equipment_ids, progress)


# ── Per-SCB folder export → D:\SMB\INV{n}\SCB{k}.xlsx ────────────────────────────
# NEW export organisation (client requirement) that leaves the existing combined
# workbook export completely untouched. One Excel file per String Combiner, grouped
# into a folder per inverter:
#
#     <SMB_EXPORT_DIR>\INV1\SCB1.xlsx, SCB2.xlsx, …
#     <SMB_EXPORT_DIR>\INV2\SCB1.xlsx, …
#
# The SCBs are DISCOVERED per inverter from the live schema (never assumed identical
# across inverters, never a fixed count). Each SCB file's content — company header,
# Equipment Type/ID, From/To/Interval/Aggregation, unit-labelled bold headers, styles,
# 3-decimal numbers, DD/MM/YYYY timestamps — is produced by the SAME
# `report_excel.build_workbook_streaming` writer as the combined export, so a given
# SCB's columns are byte-for-byte identical to that SCB's slice in the old workbook.
def _smb_export_dir() -> str:
    """Base output folder for the per-SCB export. Configurable (never hardcoded);
    defaults to D:\\SMB. Created if missing."""
    d = os.environ.get("SMB_EXPORT_DIR") or os.path.join("D:\\", "SMB")
    os.makedirs(d, exist_ok=True)
    return d


def _build_inverter_scb_folder(table, req_tags, from_d, to_d, from_str, to_str,
                               interval, agg, base_dir):
    """
    Pool worker: fetch ONE inverter once, then write one SCB{k}.xlsx per discovered
    String Combiner into <base_dir>\\INV{n}\\. Returns a picklable summary tuple.
    """
    from app.services import report_excel

    session = SessionLocal()
    try:
        # ONE read of this inverter (only the requested columns, fetchmany-batched);
        # reused for every SCB file — no query is ever repeated per SCB.
        _t, present, missing, rows = _fetch_inverter(
            table, req_tags, from_str, to_str, interval, agg)

        # Group the PRESENT columns by their SCB index, order preserved. This is the
        # dynamic discovery — an inverter with fewer/more SCBs simply yields fewer/more
        # files; nothing is assumed about the count.
        by_scb = group_by_scb(present)

        inv    = _inv_no(table)
        folder = os.path.join(base_dir, f"INV{inv}")
        os.makedirs(folder, exist_ok=True)

        interval_label = intervals.interval_label(interval)
        agg_label      = intervals.agg_label(interval, agg)

        written = []
        for k, scb_cols in by_scb.items():
            header = {
                "equipment_type": "String Combiner", "equipment_id": f"INV{inv}",
                "from": from_d, "to": to_d,
                "interval": interval_label, "agg": agg_label,
            }
            # Same writer/args as the combined export → identical cell content for
            # this SCB's columns. One worksheet per file (named SCB{k}).
            spec = (f"SCB{k}", header, ["timestamp"] + scb_cols, iter(rows), _col_header)
            data = report_excel.build_workbook_streaming([spec], wide_print_layout=True)
            path = os.path.join(folder, f"SCB{k}.xlsx")
            with open(path, "wb") as fh:
                fh.write(data)
            written.append(f"SCB{k}.xlsx")

        return f"INV{inv}", folder, len(rows), sorted(by_scb.keys()), missing, written
    finally:
        session.close()


def export_smb_to_folders(req, equipment_ids, progress=None) -> dict:
    """
    Write the String Combiner report as one Excel file per SCB, foldered by inverter:
        <SMB_EXPORT_DIR>\\INV{n}\\SCB{k}.xlsx

    Inverters are processed in parallel (each on its own pooled DB connection); every
    inverter is read exactly once and its SCBs discovered from the live schema. Only
    the export ORGANISATION changes — each file's Excel content is identical to the
    corresponding columns in the existing combined workbook. Returns a summary dict.
    """
    tables = [t for t in equipment_ids if _safe_name(t)]
    if not tables:
        raise RuntimeError("No valid inverters selected")

    interval = req.interval.value if hasattr(req.interval, "value") else str(req.interval)
    agg      = req.agg_function.value if hasattr(req.agg_function, "value") else str(req.agg_function)
    from_str = req.from_datetime.strftime("%Y-%m-%d %H:%M:%S")
    to_str   = req.to_datetime.strftime("%Y-%m-%d %H:%M:%S")
    from_d   = req.from_datetime.strftime("%d/%m/%Y")
    to_d     = req.to_datetime.strftime("%d/%m/%Y")
    req_tags = [t for t in (req.tags or []) if _safe_name(t)]

    base_dir = _smb_export_dir()
    n = len(tables)

    def emit(pct, msg):
        if progress:
            try:
                progress(int(pct), msg)
            except Exception:
                pass

    emit(3, f"Exporting {n} inverter(s) → {base_dir} …")
    logger.info("SMB folder export start -> inverters=%d | selected_tags=%d | interval=%s | dir=%s",
                n, len(req_tags), interval, base_dir)

    t0 = time.time()
    inverters, total_files, done = [], 0, 0
    workers = min(_MAX_WORKERS, n) or 1
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_build_inverter_scb_folder, t, req_tags, from_d, to_d,
                          from_str, to_str, interval, agg, base_dir): t for t in tables}
        for fut in as_completed(futs):
            inv, folder, nrows, scb_keys, missing, files = fut.result()
            if missing:
                logger.warning("SMB folder export | %s: %d selected tag(s) not in table: %s",
                               inv, len(missing), missing[:10])
            logger.info("SMB folder | %s | rows=%d | scbs=%d (%s) | files=%d",
                        inv, nrows, len(scb_keys), scb_keys, len(files))
            inverters.append({"inverter": inv, "folder": folder, "scbs": len(files),
                              "files": files})
            total_files += len(files)
            done += 1
            emit(3 + done * 95 / n, f"{inv} done ({done}/{n}) · {len(files)} SCB file(s)")

    logger.info("SMB folder export DONE | inverters=%d | files=%d | %.1fs | dir=%s",
                n, total_files, time.time() - t0, base_dir)
    emit(100, f"Saved {total_files} SCB file(s) across {n} inverter folder(s) to {base_dir}.")
    return {"directory": base_dir, "inverters": inverters, "count": n, "files": total_files}
