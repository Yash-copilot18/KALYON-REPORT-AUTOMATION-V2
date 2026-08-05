# app/services/t1_isolation_excel.py
"""
T1 Isolation export — one workbook per ISO device, selected tags split 40 per sheet.

Layout (per selected device ISO1…ISO12):

    ISO{n}_Report.xlsx
     ├── Tags_1_40      (Timestamp + selected tags 1–40)
     ├── Tags_41_80     (Timestamp + selected tags 41–80)
     ├── Tags_81_120
     └── …              (continues until every SELECTED tag is written)

Only the tags selected in the UI (`req.tags`) are exported — in the order the UI
sent them — so the Excel column set always matches the selection 1:1.

Production-grade PARALLEL STREAMING export
------------------------------------------
Devices are built **concurrently** by a thread pool (`_MAX_WORKERS`), each worker on
its own pooled DB connection, and the result is **streamed to the client as each
device finishes**:

  * One selected device  → its `.xlsx` is streamed in chunks.
  * Several selected      → a ZIP is streamed entry-by-entry — as each device
                            workbook COMPLETES it is written into the archive and its
                            compressed bytes are flushed to the HTTP response
                            immediately, then discarded (ZIP-as-completed).

Per device the selected tags are read in ONE query (server-side-cursor batched at
10k rows, only the requested columns — never SELECT *), then split into 40-tag
sheets. Rendering is delegated to the shared `report_excel` service (company header,
dark theme, unit-labelled bold headers, borders, frozen header row, auto-filter,
auto-fit columns, DD/MM/YYYY HH:MM:SS timestamps, 3-decimal numbers) so the report
format/layout is byte-identical to before. Up to `_MAX_WORKERS` devices are in
flight at once (memory is bounded to that many device row-sets); their SQL reads
overlap. Per-device and total benchmarks (SQL time, Excel time, rows/s) are logged.
"""

import os
import re
import time
import zipfile
import logging
import threading
from datetime import datetime
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from typing import List, Tuple, Optional, Callable, Iterator

from sqlalchemy import text

from app.database.session import SessionLocal
from app.services import schema_cache, intervals
from app.services.smb_excel import _TEXT_TYPES                 # shared text-column set
from app.repositories.reports_repository import _safe_name, _build_interval_expr

logger = logging.getLogger(__name__)

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
ZIP_MIME  = "application/zip"

TAGS_PER_SHEET = 40         # NOT a tag limit — the per-worksheet split size
_STREAM_CHUNK  = 64 * 1024  # single-device .xlsx is streamed in 64 KB chunks
_READ_BATCH    = 10_000     # DB rows fetched per round-trip (server-side cursor batch)

# Devices are built CONCURRENTLY (ProcessPoolExecutor for >1 device — each worker on
# its OWN pooled DB connection and CPU core, so the CPU-bound xlsxwriter work and the
# per-device SQL reads truly overlap).
#
# Worker count is the dominant tuning knob here because the real bottleneck is the
# per-device SQL aggregation (750 bigint→FLOAT AVGs over a 3-month hourly range ≈ 11s
# each, single-core on SQL Express — MAXDOP has no effect, and the CAST is required
# for exact averages so it cannot be removed). Overlapping more of those reads is what
# cuts wall-time. Measured, 12 devices · 750 tags · hourly · 3 months (SQL Express,
# 12-core host, pool=10+20):
#     workers=4 → ~101–111s   workers=5 → ~83s
#     workers=6 → ~73–85s     workers=8 → ~89s   workers=12 → ~85s
# 6 is the sweet spot (≈80s avg, reliably < 90s); beyond it the DB oversubscribes and
# process-spawn overhead dominates. Bounded to the device count by the caller, and
# well within the connection pool. Each aggregated device holds only ~1.5k rows, so
# memory stays low even at 6 in flight.
_MAX_WORKERS = 6


def _iter_table_rows(db, table, cols, col_types, from_dt, to_dt, interval, agg,
                     batch_size: int = _READ_BATCH, timings: Optional[dict] = None):
    """
    Stream one isolation device's rows for `cols` from a SINGLE query, pulling the
    result set in `batch_size` chunks with a server-side cursor (`fetchmany`), and
    yielding `{"timestamp": iso, col: value, …}` dicts one at a time. The whole
    dataset is never held in memory and there is no row cap (no rows are skipped).

    A single ordered query + `fetchmany` is cheaper than paging with OFFSET/FETCH
    (which re-scans from the top for every page). Value rounding and timestamp
    formatting match the shared reader exactly, so exported values are unchanged.
    Selects ONLY the requested columns (never SELECT *); relies on the clustered
    TimeCol index for the range scan / bucket grouping.
    """
    if not cols:
        return

    if intervals.is_instant(interval):
        select = ", ".join(f"[{c}]" for c in cols)
        base = (f"SELECT TimeCol, {select} FROM [{table}] "
                f"WHERE TimeCol BETWEEN :f AND :t ORDER BY TimeCol")
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
        base = (f"SELECT {grp} AS TimeCol, {', '.join(parts)} FROM [{table}] "
                f"WHERE TimeCol BETWEEN :f AND :t GROUP BY {grp} ORDER BY {grp}")

    # ONE query; stream it in batches straight off the cursor (never buffer it all).
    # When a `timings` dict is supplied, the DB-read time (waiting on `fetchmany`) and
    # the row-transform time (building the value dicts) are accumulated SEPARATELY so
    # the two phases can be reported independently — each batch's dicts are built up
    # front (timed), then yielded, so the timer never includes the downstream consumer.
    # The result set is still only ever one batch in memory (streaming preserved) and
    # the emitted rows are byte-for-byte the same either way.
    result = db.execute(text(base), {"f": from_dt, "t": to_dt})
    try:
        while True:
            t_db = time.perf_counter()
            batch = result.fetchmany(batch_size)
            if timings is not None:
                timings["db"] += time.perf_counter() - t_db
            if not batch:
                break
            t_x = time.perf_counter()
            built = []
            for r in batch:
                d = {"timestamp": r[0].isoformat() if hasattr(r[0], "isoformat") else str(r[0] or "")}
                for i, c in enumerate(cols):
                    v = r[i + 1]
                    d[c] = round(v, 4) if isinstance(v, float) else v
                built.append(d)
            if timings is not None:
                timings["xform"] += time.perf_counter() - t_x
            yield from built
    finally:
        result.close()


def iso_name(table: str) -> str:
    """T1_IS3 → ISO3, T2_IS13 → ISO13 (matches the UI's equipment display name)."""
    m = re.match(r"T\d+_IS0*(\d+)", table, re.IGNORECASE)
    return f"ISO{m.group(1)}" if m else table


def _disp(table: str, label: Optional[str]) -> str:
    """
    PRESENTATION-layer device name — the only thing a user should see for a device.

    For the operator-facing "Tracker" type the device is shown as Tracker{n}; the
    internal/legacy isolation types keep ISO{n}. The physical table (equipment_id) is
    NEVER changed by this — it exists purely so filenames, sheet names, headers and
    progress messages read "Tracker{n}" while SQL/routing/caching/logging keep using
    the real table name.
    """
    return tracker_name(table) if (label or "").strip() == "Tracker" else iso_name(table)


# Columns are named {FIELD}_ID{n} — e.g. ALARM_ID1, BATTERY_LEVEL_ID1.
_ID_COL_RE = re.compile(r"^(.*)_ID(\d+)$", re.IGNORECASE)


def _order_isolation_columns(cols: List[str]) -> List[str]:
    """
    Client-required column order: every field belonging to the same numeric ID is
    grouped together, IDs ascending (1, 2, 3, …), and within each ID the fields are
    ordered alphabetically by prefix — ALARM, BATTERY_LEVEL, ELEVATION_POSITION,
    ELEVATION_SETPOINT, MAX_MOTOR_CURRENT, OPERATION_MODE, … :

        ALARM_ID1, BATTERY_LEVEL_ID1, … OPERATION_MODE_ID1,
        ALARM_ID2, BATTERY_LEVEL_ID2, … OPERATION_MODE_ID2, …

    Only the columns actually present are ordered, so a field missing for some ID is
    simply absent — its slot is skipped and the ID's remaining fields still follow in
    order (no gaps, no placeholders). Any column that does not match {FIELD}_ID{n}
    keeps its original relative order and is placed after all ID-grouped columns.
    (Timestamp is added separately as the first column by the caller.)
    """
    def key(c: str):
        m = _ID_COL_RE.match(c)
        if m:
            return (0, int(m.group(2)), m.group(1).upper())
        return (1, 0, "")          # non-ID columns last; stable sort keeps their order
    return sorted(cols, key=key)


def _d(dt) -> str:
    try:
        return dt.strftime("%d/%m/%Y")
    except AttributeError:
        return str(dt)


# ── Streaming ZIP sink ───────────────────────────────────────────────────────
class _StreamingZipSink:
    """
    A minimal non-seekable file object for `zipfile.ZipFile`. `writestr` supplies
    each entry in full (size + CRC known up-front) so no seek-back is ever needed;
    after every entry we `drain()` the freshly written bytes and yield them to the
    HTTP response, so the archive is emitted incrementally with constant memory.
    """
    def __init__(self):
        self._buf = bytearray()
        self._pos = 0

    def write(self, data) -> int:
        self._buf += data
        self._pos += len(data)
        return len(data)

    def tell(self) -> int:        # absolute offset — used for the central directory
        return self._pos

    def flush(self) -> None:
        pass

    def seekable(self) -> bool:   # force ZipFile down the no-seek path
        return False

    def drain(self) -> bytes:
        chunk = bytes(self._buf)
        self._buf.clear()
        return chunk


# ── One device workbook: selected tags chunked TAGS_PER_SHEET per worksheet ──
def _build_device_workbook(session, iso: str, meta: dict, table: str, present: List[str],
                           col_types: dict, from_str: str, to_str: str,
                           interval: str, agg: str
                           ) -> Tuple[bytes, int, float, float, float]:
    """
    Build ISO{n}_Report.xlsx and return
    (bytes, row_count, sql_secs, xform_secs, xlsx_secs).

    ALL selected tags go into a SINGLE worksheet (Timestamp + every selected tag) —
    Excel allows up to 16,384 columns, so there is no per-sheet split. The columns
    are read from the database in ONE query (only the selected columns, never
    SELECT *), server-side-cursor batched at 10k rows (`_iter_table_rows`, no row
    cap), then rendered by the shared `report_excel.build_workbook` (xlsxwriter
    constant_memory) — so headers, column names, styles and numeric formatting are
    exactly as before; only the sheet count changes (N sheets → 1).
    """
    from app.services import report_excel
    from app.routers.reports_v2 import col_header

    # Separate the DB-read phase from the row-transform phase (see `_iter_table_rows`).
    timings = {"db": 0.0, "xform": 0.0}
    rows = list(_iter_table_rows(session, table, present, col_types,
                                 from_str, to_str, interval, agg,
                                 timings=timings))   # ONE query
    sql_secs   = timings["db"]
    xform_secs = timings["xform"]

    n_tags = len(present)
    rng  = f"1-{n_tags}" if n_tags else "—"
    name = f"Tags_1_{n_tags}" if n_tags else "Data"
    label = meta.get("label", "T1 Isolation")
    header = {
        "subtitle": f"{iso}  —  {label}  —  Tags {rng}",
        "equipment_type": label, "equipment_id": iso,
        "from": meta["from"], "to": meta["to"],
        "interval": meta["interval"], "agg": meta["agg"],
    }
    # ONE spec → ONE worksheet holding Timestamp + every selected tag.
    spec = (name, header, ["timestamp"] + present, rows, col_header)

    t_xl = time.time()
    data = report_excel.build_workbook([spec])
    xlsx_secs = time.time() - t_xl
    return data, len(rows), sql_secs, xform_secs, xlsx_secs


def _build_one_device(table: str, req_tags: List[str], meta: dict,
                      from_str: str, to_str: str, interval: str, agg: str
                      ) -> Tuple[str, str, bytes, int, int, list, float, float, float]:
    """
    Pool worker: build ONE device's workbook on its OWN pooled DB session (each
    process/thread gets its own SQLAlchemy engine+connection — never reconnect per
    device). Kept module-level and returning only picklable values so it runs under
    ProcessPoolExecutor. Benchmark/warning logging happens in the PARENT (child
    process logs may not reach the server log), so this returns the timings.

    Returns (display_name, filename, xlsx_bytes, row_count, tag_count, missing_tags,
             sql_secs, xform_secs, xlsx_secs, internal_name).
    `display_name`/`filename` are what the USER sees (Tracker{n}); `internal_name`
    (ISO{n}) is for server-side logging only.
    """
    session = SessionLocal()
    try:
        iso  = iso_name(table)                       # internal — logging only
        disp = _disp(table, meta.get("label"))       # presentation — user-facing
        all_cols = schema_cache.get_columns(session, table)          # cached metadata
        present  = [t for t in req_tags if t in all_cols]            # keep UI order
        missing  = [t for t in req_tags if t not in all_cols]
        col_types = {c: all_cols[c] for c in present}
        data, nrows, sql_s, xform_s, xl_s = _build_device_workbook(
            session, disp, meta, table, present, col_types, from_str, to_str, interval, agg)
        return (disp, f"{disp}_Report.xlsx", data, nrows, len(present), missing,
                sql_s, xform_s, xl_s, iso)
    finally:
        session.close()


def _resolve(req, equipment_ids) -> Tuple[List[str], List[str], dict, str, str, str, str]:
    """Validate/normalise the request → (devices, req_tags, meta, from, to, interval, agg)."""
    interval = req.interval.value if hasattr(req.interval, "value") else str(req.interval)
    agg      = req.agg_function.value if hasattr(req.agg_function, "value") else str(req.agg_function)
    from_str = req.from_datetime.strftime("%Y-%m-%d %H:%M:%S")
    to_str   = req.to_datetime.strftime("%Y-%m-%d %H:%M:%S")
    meta = {
        "from": _d(req.from_datetime), "to": _d(req.to_datetime),
        "interval": interval if interval != "raw" else "Raw (all records)",
        "agg": agg.upper(),
        "label": req.equipment_type,   # "T1 Isolation" / "T2 Isolation" — for headers
    }
    # Selected tags, UI order preserved, injection-guarded — the ONLY source of
    # columns (no rediscovery, no fixed template, no slicing/limit).
    req_tags = [t for t in (req.tags or []) if _safe_name(t)]
    devices  = [t for t in equipment_ids
                if _safe_name(t) and re.match(r"^T\d+_IS\d+$", t, re.IGNORECASE)]
    if not devices:
        raise RuntimeError("No valid isolation devices selected")
    if not req_tags:
        raise RuntimeError("No tags selected for export")
    return devices, req_tags, meta, from_str, to_str, interval, agg


def _iter_device_workbooks(
    devices: List[str], req_tags: List[str], meta: dict,
    from_str: str, to_str: str, interval: str, agg: str,
    progress: Optional[Callable] = None,
) -> Iterator[Tuple[str, bytes]]:
    """
    Build device workbooks CONCURRENTLY (ThreadPoolExecutor, `_MAX_WORKERS`) and
    yield (filename, xlsx_bytes) in COMPLETION order — so the caller can add each to
    the ZIP the instant it is ready (ZIP-as-completed), discarding the bytes right
    after. Up to `_MAX_WORKERS` devices are in flight at once; their per-device SQL
    reads overlap. Progress is aggregated thread-safely and reported with the current
    equipment, rows processed, throughput, and ETA.
    """
    def emit(pct, msg):
        if progress:
            try:
                progress(int(pct), msg)
            except Exception:
                pass

    n = len(devices)
    t0 = time.time()
    state = {"done": 0, "rows": 0, "sql": 0.0, "xform": 0.0, "excel": 0.0}

    # Log the benchmark + progress for a completed device, and return the ZIP entry.
    def _report(res) -> Tuple[str, bytes]:
        # `disp`/`fname` are user-facing (Tracker{n}); `internal` (ISO{n}) is for logs.
        disp, fname, data, nrows, ntags, missing, sql_s, xform_s, xl_s, internal = res
        if missing:
            logger.warning("T1 Isolation | %s missing %d selected tag(s) (not in table): %s",
                           internal, len(missing), missing[:10])
        # Per-device phase timings: SQL fetch, data transformation, Excel writing.
        logger.info("BENCH %s | rows=%d | tags=%d | sheet=1 | sql_fetch=%.2fs | "
                    "transform=%.2fs | excel=%.2fs | %.0f rows/s | bytes=%d",
                    internal, nrows, ntags, sql_s, xform_s, xl_s,
                    nrows / xl_s if xl_s > 0 else 0, len(data))
        state["done"] += 1
        state["rows"] += nrows
        state["sql"] += sql_s
        state["xform"] += xform_s
        state["excel"] += xl_s
        done, rows = state["done"], state["rows"]
        elapsed = max(time.time() - t0, 1e-6)
        remaining = (n - done) * (elapsed / done) if done else 0
        emit(5 + done * 90 / n,
             f"{disp} done ({done}/{n}) · {rows:,} rows · "
             f"{rows / elapsed:,.0f} rows/s · ETA {remaining:.0f}s")
        return fname, data

    def _log_phase_summary():
        """Aggregate per-phase timings across all devices (client-requested profile)."""
        wall   = max(time.time() - t0, 1e-6)
        serial = state["sql"] + state["xform"] + state["excel"]
        logger.info(
            "BENCH PHASE SUMMARY | devices=%d | rows=%d | SQL_fetch(sum)=%.1fs | "
            "transform(sum)=%.1fs | excel_write(sum)=%.1fs | serial_work=%.1fs | "
            "parallel_wall=%.1fs | speedup=%.2fx | workers=%d",
            state["done"], state["rows"], state["sql"], state["xform"], state["excel"],
            serial, wall, serial / wall, min(_MAX_WORKERS, n))

    emit(3, f"Processing {n} device(s) — {len(req_tags)} tag(s) each…")

    # Single device: build inline (nothing to parallelise; avoids process-spawn cost).
    if n == 1:
        yield _report(_build_one_device(devices[0], req_tags, meta,
                                        from_str, to_str, interval, agg))
        _log_phase_summary()
        return

    # Multiple devices: build them on separate CPU cores with a process pool. Each
    # worker process has its OWN SQLAlchemy engine/pool (no shared connections), so
    # the CPU-bound xlsxwriter work runs truly in parallel — unlike threads, which
    # the GIL serialises. Workbooks are collected AS EACH COMPLETES and zipped
    # immediately, then their bytes are discarded (no temp files are ever created).
    workers = min(_MAX_WORKERS, n)
    logger.info("T1 Isolation | fan-out start -> devices=%d | tags=%d | ProcessPool workers=%d",
                n, len(req_tags), workers)
    ex = ProcessPoolExecutor(max_workers=workers)
    try:
        futures = {
            ex.submit(_build_one_device, table, req_tags, meta,
                      from_str, to_str, interval, agg): table
            for table in devices
        }
        for fut in as_completed(futures):
            fname, data = _report(fut.result())
            yield fname, data
            del data   # release this device before the next completion
    finally:
        ex.shutdown(wait=True)
    _log_phase_summary()


# ── Public streaming entry point ─────────────────────────────────────────────
def stream_t1_isolation_export(req, equipment_ids, progress: Optional[Callable] = None
                               ) -> Tuple[Iterator[bytes], str, str]:
    """
    Build the export (devices in PARALLEL) and STREAM it to the caller as a byte
    iterator.

    Returns `(byte_iterator, filename, content_type)`:
      * single device  → the `.xlsx`, streamed in 64 KB chunks
      * many devices    → a `.zip`, streamed entry-by-entry as each device completes

    The iterator does the work lazily (nothing is generated until it is consumed),
    so an HTTP StreamingResponse emits bytes to the client while later workbooks are
    still being produced by the thread pool.
    """
    devices, req_tags, meta, from_str, to_str, interval, agg = _resolve(req, equipment_ids)
    n = len(devices)
    logger.info("T1 Isolation STREAM export start -> devices=%d | selected_tags=%d | interval=%s",
                n, len(req_tags), interval)

    def emit(pct, msg):
        if progress:
            try:
                progress(int(pct), msg)
            except Exception:
                pass

    pairs = _iter_device_workbooks(devices, req_tags, meta,
                                   from_str, to_str, interval, agg, progress)

    if n == 1:
        # Single workbook — stream the one .xlsx in chunks. User sees Tracker{n}; the
        # internal name is used only for the server log.
        internal = iso_name(devices[0])
        disp     = _disp(devices[0], meta.get("label"))
        filename = f"{disp}_Report.xlsx"

        def _one() -> Iterator[bytes]:
            t0 = time.time()
            emit(2, f"Processing {disp}…")
            _, data = next(iter(pairs))
            for i in range(0, len(data), _STREAM_CHUNK):
                yield data[i:i + _STREAM_CHUNK]
            emit(100, "Export completed.")
            logger.info("T1 Isolation STREAM done -> 1 device (%s) | %.1fs | %s",
                        internal, time.time() - t0, filename)

        return _one(), filename, XLSX_MIME

    # Multiple devices — stream a ZIP archive, one entry at a time.
    zip_prefix = re.sub(r"[^A-Za-z0-9]+", "_", meta.get("label", "T1 Isolation")).strip("_")
    filename = f"{zip_prefix}_Reports_{datetime.now().strftime('%d-%m-%Y')}.zip"

    def _zip() -> Iterator[bytes]:
        t0 = time.time()
        emit(2, f"Processing {n} isolation device(s) — {len(req_tags)} tag(s) each…")
        sink = _StreamingZipSink()
        zf = zipfile.ZipFile(sink, "w", zipfile.ZIP_DEFLATED, allowZip64=True)
        exported = 0
        total_bytes = 0
        try:
            # `pairs` yields device workbooks in COMPLETION order (built by the thread
            # pool); each is written into the ZIP the moment it is ready and its bytes
            # are flushed to the client, then discarded — the ZIP is never fully held.
            for entry_name, data in pairs:
                zf.writestr(entry_name, data)      # size+CRC known → no seek-back
                exported += 1
                total_bytes += len(data)
                emit(95 + exported * 4 / n, f"Zipping {entry_name} ({exported}/{n})…")
                chunk = sink.drain()
                if chunk:
                    yield chunk                    # flush this entry to the client now
        finally:
            zf.close()
        tail = sink.drain()                        # central directory (after ALL files)
        if tail:
            yield tail
        total_s = time.time() - t0
        if exported != n:
            logger.warning("T1 Isolation STREAM | only %d/%d device workbooks written", exported, n)
        emit(100, "Export completed.")
        logger.info("BENCH TOTAL | devices=%d/%d | %.1fs | %d bytes zipped | %.1f devices/s | %s",
                    exported, n, total_s, total_bytes,
                    exported / total_s if total_s > 0 else 0, filename)

    return _zip(), filename, ZIP_MIME


# ── Direct-to-Downloads export (NO ZIP) ──────────────────────────────────────
def _resolve_downloads_dir() -> str:
    """
    The user's Downloads folder on this (single-machine) deployment. Override with
    the EXPORT_DOWNLOADS_DIR env var if the reports should land elsewhere. Created
    if missing.
    """
    d = os.environ.get("EXPORT_DOWNLOADS_DIR") or os.path.join(os.path.expanduser("~"), "Downloads")
    os.makedirs(d, exist_ok=True)
    return d


def _unique_path(directory: str, filename: str) -> str:
    """A non-colliding path, appending ' (1)', ' (2)'… like a browser download."""
    base, ext = os.path.splitext(filename)
    path = os.path.join(directory, filename)
    i = 1
    while os.path.exists(path):
        path = os.path.join(directory, f"{base} ({i}){ext}")
        i += 1
    return path


def _build_tracker_workbook_dl(table: str, req_tags: List[str], meta: dict, from_str: str,
                               to_str: str, interval: str, agg: str, downloads: str):
    """
    Pool worker: build ONE tracker's workbook — the selected tags split every
    TAGS_PER_SHEET into worksheets named Tracker{n}_{start}-{end} (the tab suffix is the
    1-based tag range on that sheet) — and stream it straight to Downloads\\Tracker{n}.xlsx.

    Each tracker's rows are read from the database EXACTLY ONCE (one server-side-cursor
    query via `_iter_table_rows` → fetchmany, only the selected columns, ID-grouped
    order) and reused across all of that tracker's range sheets — no query is repeated.
    Module-level + returns only picklable values so it runs under ProcessPoolExecutor.
    Returns (disp, fname, rows, tags, sheets, internal_iso, sql_s, xform_s, xlsx_s).
    """
    import xlsxwriter
    from app.services.report_excel import make_formats, write_report_sheet_streaming, safe_sheet_name
    from app.routers.reports_v2 import col_header

    session = SessionLocal()
    try:
        iso  = iso_name(table)                       # internal — logging only
        disp = _disp(table, meta.get("label"))       # presentation — file/sheet/header (Tracker{n})
        all_cols = schema_cache.get_columns(session, table)
        present  = [t for t in req_tags if t in all_cols]
        # Client-required ID-grouped order — IDENTICAL to before; only sheet names change.
        present   = _order_isolation_columns(present)
        col_types = {c: all_cols[c] for c in present}

        timings = {"db": 0.0, "xform": 0.0}
        rows = list(_iter_table_rows(session, table, present, col_types,
                                     from_str, to_str, interval, agg, timings=timings))

        agg_label = meta.get("agg", "")
        # Header: Report Title (A1, from the writer) + Tracker Name (subtitle), then
        # From / To / Interval / Aggregation. NO Equipment Type / Equipment ID rows.
        def _header():
            return {
                "subtitle": disp,
                "from": meta["from"], "to": meta["to"],
                "interval": meta["interval"], "agg": agg_label,
                "meta_fields": [
                    ("Tracker Name", disp),
                    ("From",         meta["from"]),
                    ("To",           meta["to"]),
                    ("Interval",     meta["interval"]),
                    ("Aggregation",  agg_label),
                ],
            }

        path  = _unique_path(downloads, f"{disp}.xlsx")
        fname = os.path.basename(path)
        t_xl  = time.time()
        wb  = xlsxwriter.Workbook(path, {"constant_memory": True})   # streamed to disk
        fmt = make_formats(wb)                                       # formats built once
        used: set = set()
        # Dynamic 40-tag split; tab = Tracker{n}_{start}-{end}. Every sheet renders its
        # own slice of the SAME already-read rows (no extra query, no reorder).
        chunks = [present[i:i + TAGS_PER_SHEET]
                  for i in range(0, len(present), TAGS_PER_SHEET)] or [[]]
        for i, chunk in enumerate(chunks):
            start = i * TAGS_PER_SHEET + 1
            end   = start + len(chunk) - 1 if chunk else start
            sheet = safe_sheet_name(f"{disp}_{start}-{end}", used)
            write_report_sheet_streaming(
                wb, fmt, sheet, _header(), ["timestamp"] + chunk, iter(rows), col_header)
        wb.close()
        xlsx_s = time.time() - t_xl
        return (disp, fname, len(rows), len(present), len(chunks), iso,
                timings["db"], timings["xform"], xlsx_s)
    finally:
        session.close()


def export_to_downloads(req, equipment_ids, progress: Optional[Callable] = None) -> dict:
    """
    Build ONE workbook PER tracker and save each to the user's Downloads folder:

        Tracker1.xlsx, Tracker2.xlsx, … Tracker24.xlsx

    Inside each workbook the selected tags are split every TAGS_PER_SHEET into worksheets
    named  Tracker{n}_{start}-{end}  (e.g. Tracker1_1-40, Tracker1_41-80, Tracker1_81-120)
    — the tab suffix is the tag range on that sheet. Header keeps only the Report Title
    (A1), Tracker Name, From, To, Interval and Aggregation (no Equipment Type / ID).

    Report DATA, SQL, column order (`_order_isolation_columns`), formatting, filters,
    widths, fonts and styles are UNCHANGED — only the worksheet tab names changed.

    Performance: trackers are built CONCURRENTLY (ProcessPoolExecutor, `_MAX_WORKERS`),
    each on its own pooled DB connection and CPU core; each tracker is read ONCE and its
    range sheets reuse that single read; workbooks stream to disk (constant_memory) with
    formats built once. Per-tracker SQL/transform/Excel timings are logged.

    Returns {"saved": [Tracker1.xlsx, …], "directory": <downloads>, "count": N,
             "sheets": <total sheets>}.
    """
    devices, req_tags, meta, from_str, to_str, interval, agg = _resolve(req, equipment_ids)
    meta = dict(meta)
    meta.setdefault("label", "Tracker")
    n = len(devices)
    downloads = _resolve_downloads_dir()

    def emit(pct, msg):
        if progress:
            try:
                progress(int(pct), msg)
            except Exception:
                pass

    emit(2, f"Processing {n} tracker(s) → Downloads…")
    logger.info("Tracker per-workbook export start -> trackers=%d | tags=%d | dir=%s | workers=%d",
                n, len(req_tags), downloads, min(_MAX_WORKERS, n) or 1)

    t0 = time.time()
    results = []
    args = (req_tags, meta, from_str, to_str, interval, agg, downloads)
    if n == 1:
        results.append(_build_tracker_workbook_dl(devices[0], *args))
    else:
        # Independent per-tracker workbooks → true CPU-parallel builds (their SQL reads
        # overlap too). Each worker has its OWN engine/connection; no shared state.
        workers = min(_MAX_WORKERS, n)
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_build_tracker_workbook_dl, t, *args): t for t in devices}
            done = 0
            for fut in as_completed(futs):
                res = fut.result()
                results.append(res)
                done += 1
                emit(5 + done * 92 / n, f"{res[0]} done ({done}/{n})…")

    saved, total_sheets = [], 0
    agg_sql = agg_xform = agg_excel = 0.0
    for disp, fname, nrows, ntags, nsheets, iso, sql_s, xform_s, xl_s in results:
        saved.append(fname); total_sheets += nsheets
        agg_sql += sql_s; agg_xform += xform_s; agg_excel += xl_s
        logger.info("BENCH %s | rows=%d | tags=%d | sheets=%d | sql_fetch=%.2fs | "
                    "transform=%.2fs | excel=%.2fs", iso, nrows, ntags, nsheets, sql_s, xform_s, xl_s)

    wall = max(time.time() - t0, 1e-6)
    logger.info("BENCH TOTAL | per-tracker workbooks (parallel) | files=%d | sheets=%d | "
                "SQL_fetch(sum)=%.1fs | transform(sum)=%.1fs | excel(sum)=%.1fs | "
                "wall=%.1fs | speedup=%.2fx | workers=%d",
                len(saved), total_sheets, agg_sql, agg_xform, agg_excel, wall,
                (agg_sql + agg_xform + agg_excel) / wall, min(_MAX_WORKERS, n) or 1)
    emit(100, f"Saved {len(saved)} tracker report(s) to Downloads.")
    return {"saved": saved, "directory": downloads, "count": len(saved), "sheets": total_sheets}


# ── Tracker folder export → D:\Trackers\Tracker{n}\Tags_1_40.xlsx ─────────────
# NEW export organisation (client requirement). T1/T2 Isolation are presented to the
# operator as "Tracker", and this export writes, per device, a folder of 40-tag Excel
# files:
#
#     <TRACKER_EXPORT_DIR>\Tracker1\Tags_1_40.xlsx, Tags_41_80.xlsx, …
#     <TRACKER_EXPORT_DIR>\Tracker2\…
#
# Internally the device is still queried by its ISO table (T1_IS{n}/T2_IS{n}); only
# the display name (Tracker{n}) changes. Tags are DISCOVERED dynamically (the UI
# selection intersected with the table's real columns) and split every TAGS_PER_SHEET
# — nothing about the tag count is hardcoded. Each file is written by the SAME
# `write_report_sheet_streaming` renderer the existing isolation export uses, so the
# 40-tag block's content is byte-for-byte identical; only its container (separate file
# per block, folder per device) changes. The existing ZIP / to-Downloads exports are
# left completely untouched.
def _tracker_export_dir() -> str:
    """Base output folder for the Tracker export. Configurable (never hardcoded);
    defaults to D:\\Trackers. Created if missing."""
    d = os.environ.get("TRACKER_EXPORT_DIR") or os.path.join("D:\\", "Trackers")
    os.makedirs(d, exist_ok=True)
    return d


def tracker_name(table: str) -> str:
    """T1_IS3 → Tracker3, T2_IS13 → Tracker13 (the display name; DB still uses ISO)."""
    m = re.match(r"T\d+_IS0*(\d+)", table, re.IGNORECASE)
    return f"Tracker{m.group(1)}" if m else table


def _build_tracker_folder(table: str, req_tags: List[str], meta: dict, from_str: str,
                          to_str: str, interval: str, agg: str, base_dir: str):
    """
    Worker: read ONE device once, then write one Tags_{a}_{b}.xlsx per 40-tag block
    into <base_dir>\\Tracker{n}\\. Returns a summary + per-phase timings.
    """
    import xlsxwriter
    from app.services.report_excel import make_formats, write_report_sheet_streaming, safe_sheet_name
    from app.routers.reports_v2 import col_header

    session = SessionLocal()
    try:
        tracker  = tracker_name(table)
        all_cols = schema_cache.get_columns(session, table)
        present  = [t for t in req_tags if t in all_cols]
        missing  = [t for t in req_tags if t not in all_cols]
        # Client-required column order (ID-grouped) — identical to the on-disk isolation
        # export, so a block's columns match the existing output exactly.
        present   = _order_isolation_columns(present)
        col_types = {c: all_cols[c] for c in present}

        timings = {"db": 0.0, "xform": 0.0}
        rows = list(_iter_table_rows(session, table, present, col_types,
                                     from_str, to_str, interval, agg, timings=timings))

        folder = os.path.join(base_dir, tracker)
        os.makedirs(folder, exist_ok=True)

        # Dynamic 40-tag split (never a fixed tag count).
        chunks = [present[i:i + TAGS_PER_SHEET]
                  for i in range(0, len(present), TAGS_PER_SHEET)] or [[]]
        label = meta.get("label", "Tracker")
        t_xl = time.time()
        files = []
        for i, chunk in enumerate(chunks):
            start = i * TAGS_PER_SHEET + 1
            end   = start + len(chunk) - 1 if chunk else start
            fname = f"Tags_{start}_{end}.xlsx"
            path  = os.path.join(folder, fname)

            # ONE constant_memory workbook per file, formats built once, single-pass.
            wb  = xlsxwriter.Workbook(path, {"constant_memory": True})
            fmt = make_formats(wb)
            header = {
                "subtitle": f"{tracker}  —  {label}",
                "equipment_type": label, "equipment_id": tracker,
                "from": meta["from"], "to": meta["to"], "interval": meta["interval"],
                "meta_fields": [
                    ("Equipment Type", label),
                    ("Equipment ID",   tracker),
                    ("From",           meta["from"]),
                    ("To",             meta["to"]),
                    ("Interval",       meta["interval"]),
                ],
            }
            used: set = set()
            write_report_sheet_streaming(
                wb, fmt, safe_sheet_name(f"Tags_{start}_{end}", used), header,
                ["timestamp"] + chunk, iter(rows), col_header)
            wb.close()
            files.append(fname)
        xl_secs = time.time() - t_xl

        return (tracker, folder, len(rows), len(present), missing, files,
                timings["db"], timings["xform"], xl_secs)
    finally:
        session.close()


def export_trackers_to_folders(req, equipment_ids, progress: Optional[Callable] = None) -> dict:
    """
    Write the Tracker (T1/T2 Isolation) report as a folder of 40-tag Excel files per
    device:  <TRACKER_EXPORT_DIR>\\Tracker{n}\\Tags_{a}_{b}.xlsx

    Devices are processed in parallel (each on its own pooled DB connection, read once,
    tags discovered dynamically and split every TAGS_PER_SHEET). Only the export
    organisation changes — each block's Excel content matches the existing isolation
    export. Returns a summary dict.
    """
    devices, req_tags, meta, from_str, to_str, interval, agg = _resolve(req, equipment_ids)
    meta = dict(meta)
    meta["label"] = "Tracker"                       # display rename (DB still uses ISO)
    base_dir = _tracker_export_dir()
    n = len(devices)

    def emit(pct, msg):
        if progress:
            try:
                progress(int(pct), msg)
            except Exception:
                pass

    emit(3, f"Exporting {n} tracker(s) → {base_dir} …")
    logger.info("Tracker folder export start -> devices=%d | tags=%d | interval=%s | dir=%s",
                n, len(req_tags), interval, base_dir)

    t0 = time.time()
    trackers, total_files, done = [], 0, 0
    agg_sql = agg_xform = agg_excel = 0.0
    workers = min(_MAX_WORKERS, n) or 1
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_build_tracker_folder, t, req_tags, meta, from_str, to_str,
                          interval, agg, base_dir): t for t in devices}
        for fut in as_completed(futs):
            tracker, folder, nrows, ntags, missing, files, sql_s, xform_s, xl_s = fut.result()
            if missing:
                logger.warning("Tracker folder export | %s missing %d selected tag(s): %s",
                               tracker, len(missing), missing[:10])
            logger.info("BENCH %s | rows=%d | tags=%d | files=%d | sql_fetch=%.2fs | "
                        "transform=%.2fs | excel=%.2fs", tracker, nrows, ntags, len(files),
                        sql_s, xform_s, xl_s)
            trackers.append({"tracker": tracker, "folder": folder, "files": files,
                             "tags": ntags, "rows": nrows})
            total_files += len(files)
            agg_sql += sql_s; agg_xform += xform_s; agg_excel += xl_s
            done += 1
            emit(3 + done * 95 / n, f"{tracker} done ({done}/{n}) · {len(files)} file(s)")

    wall = max(time.time() - t0, 1e-6)
    logger.info("BENCH PHASE SUMMARY (Tracker folder) | devices=%d | files=%d | "
                "SQL_fetch(sum)=%.1fs | transform(sum)=%.1fs | excel(sum)=%.1fs | "
                "parallel_wall=%.1fs | dir=%s",
                n, total_files, agg_sql, agg_xform, agg_excel, wall, base_dir)
    emit(100, f"Saved {total_files} file(s) across {n} tracker folder(s) to {base_dir}.")
    return {"directory": base_dir, "trackers": trackers, "count": n, "files": total_files}


# ── Buffered wrapper (kept for compatibility) ────────────────────────────────
def generate_t1_isolation_export(req, equipment_ids, progress: Optional[Callable] = None):
    """
    Sequential, buffered variant: consumes the streaming iterator into a single
    bytes object. Prefer `stream_t1_isolation_export` for the live-streaming HTTP
    path; this exists so any non-streaming caller keeps the same contract:
    returns (bytes, filename, content_type).
    """
    byte_iter, filename, ctype = stream_t1_isolation_export(req, equipment_ids, progress)
    data = b"".join(byte_iter)
    return data, filename, ctype
