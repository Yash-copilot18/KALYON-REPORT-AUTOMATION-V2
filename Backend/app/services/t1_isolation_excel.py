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

Production-grade SINGLE-REQUEST STREAMING export
------------------------------------------------
Devices are processed **sequentially** (no threads, no batching, no per-device
requests) and the result is **streamed straight to the client as it is built**:

  * One selected device  → its `.xlsx` is streamed in chunks.
  * Several selected      → a ZIP is streamed entry-by-entry — each device
                            workbook is built, written into the archive, and its
                            compressed bytes are flushed to the HTTP response
                            immediately, then discarded.

Only ONE device's rows + workbook are ever in memory at a time (constant memory),
and the browser never holds the whole file — it writes the stream straight to
disk. Rendering is delegated to the shared `report_excel` service (company header,
dark theme, unit-labelled bold headers, borders, frozen header row, auto-filter,
auto-fit columns, DD/MM/YYYY HH:MM:SS timestamps, 3-decimal numbers) so the report
format/layout is identical to before. Rows are read with the shared batched,
columns-scoped reader (never SELECT *).
"""

import re
import time
import zipfile
import logging
from datetime import datetime
from typing import List, Tuple, Optional, Callable, Iterator

from app.database.session import SessionLocal
from app.services import schema_cache
from app.services.smb_excel import _fetch_table_rows          # shared batched, columns-scoped fetch
from app.repositories.reports_repository import _safe_name

logger = logging.getLogger(__name__)

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
ZIP_MIME  = "application/zip"

TAGS_PER_SHEET = 40         # NOT a tag limit — the per-worksheet split size
_STREAM_CHUNK  = 64 * 1024  # single-device .xlsx is streamed in 64 KB chunks


def iso_name(table: str) -> str:
    """T1_IS3 → ISO3, T2_IS13 → ISO13 (matches the UI's equipment display name)."""
    m = re.match(r"T\d+_IS0*(\d+)", table, re.IGNORECASE)
    return f"ISO{m.group(1)}" if m else table


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
def _build_device_workbook(iso: str, meta: dict, cols: List[str], rows: List[dict]) -> bytes:
    """
    Build ISO{n}_Report.xlsx: the selected columns split into worksheets of
    TAGS_PER_SHEET, rendered by the shared report_excel service. The chunk
    generator is consumed lazily so only one sheet is materialised at a time.
    """
    from app.services import report_excel
    from app.routers.reports_v2 import col_header

    chunks = [cols[i:i + TAGS_PER_SHEET] for i in range(0, len(cols), TAGS_PER_SHEET)] or [[]]

    def _specs():
        for ci, chunk in enumerate(chunks):
            start = ci * TAGS_PER_SHEET + 1
            end = start + len(chunk) - 1
            rng = f"{start}-{end}" if chunk else "—"
            name = f"Tags_{start}_{end}" if chunk else "Data"
            label = meta.get("label", "T1 Isolation")
            header = {
                "subtitle": f"{iso}  —  {label}  —  Tags {rng}",
                "equipment_type": label, "equipment_id": iso,
                "from": meta["from"], "to": meta["to"],
                "interval": meta["interval"], "agg": meta["agg"],
            }
            # Same `rows` reference across chunks; each sheet renders only its
            # own columns (report_excel reads row[col] per column), so no copy.
            yield (name, header, ["timestamp"] + chunk, rows, col_header)

    return report_excel.build_workbook(_specs())


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
    Yield (filename, xlsx_bytes) for each selected device, built ONE AT A TIME on
    a single DB session. Each device's rows and workbook are released before the
    next begins, so memory stays flat regardless of how many devices are selected.
    """
    def emit(pct, msg):
        if progress:
            try:
                progress(int(pct), msg)
            except Exception:
                pass

    n = len(devices)
    session = SessionLocal()
    try:
        for idx, table in enumerate(devices):
            iso = iso_name(table)
            emit(5 + idx * 90 / n, f"Processing {iso} ({idx + 1}/{n})…")
            logger.info("T1 Isolation | processing %s (%s) [%d/%d]…", iso, table, idx + 1, n)

            all_cols = schema_cache.get_columns(session, table)          # {col: dtype}
            present  = [t for t in req_tags if t in all_cols]            # keep UI order
            missing  = [t for t in req_tags if t not in all_cols]
            if missing:
                logger.warning("T1 Isolation | %s missing %d selected tag(s) (not in table): %s",
                               iso, len(missing), missing[:10])
            col_types = {c: all_cols[c] for c in present}
            rows = _fetch_table_rows(session, table, present, col_types,
                                     from_str, to_str, interval, agg)
            logger.info("T1 Isolation | %s fetched %d record(s) for %d tag(s)",
                        iso, len(rows), len(present))

            data = _build_device_workbook(iso, meta, present, rows)
            sheets = (len(present) + TAGS_PER_SHEET - 1) // TAGS_PER_SHEET or 1
            logger.info("T1 Isolation | %s workbook OK -> selected=%d exported=%d sheets=%d rows=%d bytes=%d",
                        iso, len(req_tags), len(present), sheets, len(rows), len(data))
            emit(5 + (idx + 1) * 90 / n, f"Streaming {iso} ({idx + 1}/{n})…")
            yield f"{iso}_Report.xlsx", data
            del rows, data   # release before the next device
    finally:
        session.close()


# ── Public streaming entry point ─────────────────────────────────────────────
def stream_t1_isolation_export(req, equipment_ids, progress: Optional[Callable] = None
                               ) -> Tuple[Iterator[bytes], str, str]:
    """
    Build the export SEQUENTIALLY and STREAM it to the caller as a byte iterator.

    Returns `(byte_iterator, filename, content_type)`:
      * single device  → the `.xlsx`, streamed in 64 KB chunks
      * many devices    → a `.zip`, streamed entry-by-entry as each device is built

    The iterator does the work lazily (nothing is generated until it is consumed),
    so an HTTP StreamingResponse emits bytes to the client while the workbook is
    still being produced. Only one device is ever in memory at a time.
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
        # Single workbook — stream the one .xlsx in chunks.
        iso = iso_name(devices[0])
        filename = f"{iso}_Report.xlsx"

        def _one() -> Iterator[bytes]:
            t0 = time.time()
            emit(2, f"Processing {iso}…")
            _, data = next(iter(pairs))
            for i in range(0, len(data), _STREAM_CHUNK):
                yield data[i:i + _STREAM_CHUNK]
            emit(100, "Export completed.")
            logger.info("T1 Isolation STREAM done -> 1 device | %.1fs | %s",
                        time.time() - t0, filename)

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
        try:
            for entry_name, data in pairs:
                zf.writestr(entry_name, data)      # size+CRC known → no seek-back
                exported += 1
                chunk = sink.drain()
                if chunk:
                    yield chunk                    # flush this entry to the client now
        finally:
            zf.close()
        tail = sink.drain()                        # central directory
        if tail:
            yield tail
        if exported != n:
            logger.warning("T1 Isolation STREAM | only %d/%d device workbooks written", exported, n)
        else:
            logger.info("T1 Isolation STREAM | all %d device(s) written", n)
        emit(100, "Export completed.")
        logger.info("T1 Isolation STREAM done -> %d devices zipped | %.1fs | %s",
                    exported, time.time() - t0, filename)

    return _zip(), filename, ZIP_MIME


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
