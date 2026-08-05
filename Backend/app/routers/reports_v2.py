# app/routers/reports_v2.py

from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from typing import Optional, List
import io
import re
import json
import time
import asyncio
import logging
import threading
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from app.database.session import get_db
from app.schemas.reports_schema import ReportDataRequest, MultiReportRequest
from app.services.reports_service import ReportsService
from app.services.report_files import build_report_csv, PROJECT_NAME
from app.services import mgr_service, ygr_service, intervals

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/reports-v2", tags=["Reports v2"])

# ── Unit map ──────────────────────────────────────────────────────────────────
UNIT_MAP = {
    "DC_VOLTAGE": "V", "DC_CURRENT": "A", "DC_POWER": "kW",
    "ACTIVE_POWER": "kW", "REACTIVE_POWER": "kVAR", "APPARENT_POWER": "kVA",
    "DAILY_ENERGY": "kWh", "MONTHLY_ENERGY": "kWh", "YEARLY_ENERGY": "kWh",
    "LIFETIME_ENERGY": "kWh", "DAILY_REACT_ENERGY": "kVARh",
    "GRID_CURRENT_PHASE1": "A", "GRID_CURRENT_PHASE2": "A",
    "GRID_CURRENT_PHASE3": "A", "GRID_LINE_VOLT_UV": "V",
    "GRID_LINE_VOLT_VW": "V", "GRID_LINE_VOLT_WU": "V",
    "GRID_FREQUENCY_MEASURED": "Hz", "GRID_PF_MEASURED": "",
    "GRID_ACTIVE_POWER_MEASURED": "kW", "GRID_REACTIVE_POWER_MEASURED": "kVAR",
    "GRID_VOLTAGE_L_L_MEASURED": "V", "INVERTER_TOTAL_ACTIVE_POWER": "kW",
    "INVERTER_TOTAL_REACTIVE_POWER": "kVAR", "PLANT_DAILY_PRODUCTION": "kWh",
    "PLANT_MONTHLY_PRODUCTION": "kWh", "PLANT_YEARLY_PRODUCTION": "kWh",
    "PLANT_LIFETIME_PRODUCTION": "kWh", "AVG_GHI_IRRADIATION": "W/m2",
    "AVG_GTI_IRRADIATION": "W/m2", "TOTAL_IRRADIANCE": "W/m2",
    "AVG_AIR_TEMP": "C", "AVG_WIND_SPEED": "m/s",
    "AVG_RELATIVE_HUMIDITY": "%", "AVG_IR_SOILING_RATIO1": "%",
    "AVG_IR_SOILING_RATIO2": "%", "ALL_WMS_AVG_MODULE_TEMP": "C",
    "AVG_AIR_PRESSURE": "hPa", "PRIMEPACK_IGBT_HEATSINK_TEMP": "C",
    "MV_TRAFO_TEMP": "C", "DAILY_OPERATING_TIME": "min",
    "INVERTER_RUNNING": "", "STRING_CURRENT1": "A", "STRING_CURRENT2": "A",
    "STRING_CURRENT3": "A", "STRING_CURRENT4": "A", "STRING_CURRENT5": "A",
    "STRING_CURRENT6": "A", "STRING_CURRENT7": "A", "STRING_CURRENT8": "A",
    "STRING_CURRENT9": "A", "STRING_CURRENT10": "A",
    "DAILY_RUN_MIN": "min", "DOWN_TIME_MIN": "min", "OK_TIME_MIN": "min",
    "SLEEP_TIME_MIN": "min", "GRID_OUTAGE_TIME_MIN": "min",
    "GRID_CURRENT_Ia": "A", "GRID_CURRENT_Ib": "A", "GRID_CURRENT_Ic": "A",
    "ACTIVE_POWER_SET_POINT": "kW", "VOLTAGE_SET_POINT": "V",
    "VAR_SET_POINT": "kVAR", "POWER_FACTOR_SET_POINT": "",
    "SCB1_DC_POWER": "kW", "SCB1_DC_VOLTAGE": "V",
    "SCB1_INTERNAL_TEMP": "C", "SCB1_TOTAL_CURRENT": "A",
    "SCB2_DC_POWER": "kW", "SCB2_DC_VOLTAGE": "V",
    "SCB3_DC_POWER": "kW", "SCB4_DC_POWER": "kW", "SCB5_DC_POWER": "kW",
    "DurCol": "s", "POWER_GENERATION": "kW",
    "AVG_GHI_CUMM_IRRADIATION": "Wh/m2", "AVG_GTI_CUMM_IRRADIATION": "Wh/m2",
    "AVG_ALBEDO_UP_IRRADIATION": "W/m2", "MONTHLY_RUN_HR": "hr",
    "YEARLY_RUN_HR": "hr", "LIFETIME_RUN_HR": "hr",
}


def fmt_ts(val) -> str:
    """Convert any timestamp value to DD/MM/YYYY HH:MM:SS."""
    if not val:
        return ""
    if isinstance(val, datetime):
        return val.strftime("%d/%m/%Y %H:%M:%S")
    # Strip T separator, timezone suffix, and microseconds before parsing
    s = str(val).replace("T", " ").replace("Z", "").strip().split(".")[0]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%d/%m/%Y %H:%M:%S")
        except Exception:
            continue
    return s


def fmt_date(val) -> str:
    """Convert any date to DD/MM/YYYY."""
    raw = fmt_ts(val)
    return raw.split(" ")[0] if raw else ""


def fmt_num(val) -> str:
    """Format number to exactly 3 decimal places."""
    if val is None:
        return ""
    if isinstance(val, (int, float)):
        return f"{val:.3f}"
    return str(val)


def col_header(col: str) -> str:
    """Build column header: 'Grid Active Power Measured (kW)'."""
    if col == "timestamp":
        return "Timestamp (DD/MM/YYYY HH:MM:SS)"
    label = col.replace("_", " ").title()
    unit  = UNIT_MAP.get(col, "")
    return f"{label} ({unit})" if unit else label


def display_equipment_id(equipment_type: str, equipment_id) -> str:
    """
    PRESENTATION-only equipment name for user-facing text (CSV metadata, filenames).
    The merged "Tracker" type shows Tracker{n}; every other type is unchanged. The real
    equipment_id (the table name) is never modified — this only affects what is shown.
    """
    if equipment_type == "Tracker":
        m = re.match(r"^T\d+_IS0*(\d+)$", str(equipment_id or ""), re.IGNORECASE)
        if m:
            return f"Tracker{m.group(1)}"
    return equipment_id


def today_dmy() -> str:
    return datetime.now().strftime("%d-%m-%Y")


def now_dmy() -> str:
    return datetime.now().strftime("%d/%m/%Y %H:%M:%S")


def _export_meta(req: ReportDataRequest, result: dict) -> dict:
    """Requested / exported / skipped tag breakdown for an export."""
    exported = [c for c in result.get("columns", []) if c != "timestamp"]
    return {
        "equipment":     req.equipment_id,
        "requested":     list(req.tags or []),
        "exported":      exported,
        "skipped":       list(result.get("skipped_tags", []) or []),
    }


def _csv_metadata(req: ReportDataRequest):
    """Standard CSV metadata block (ordered label/value pairs) for any report type.

    Instant intervals report no aggregation — the export must not claim the data was
    averaged when it is raw telemetry.
    """
    return [
        ("Project Name",          PROJECT_NAME),
        ("Report Name",           f"{req.equipment_type} Report"),
        ("Equipment",             display_equipment_id(req.equipment_type, req.equipment_id) or ""),
        ("Generated Date & Time", now_dmy()),
        ("From Date",             fmt_ts(req.from_datetime)),
        ("To Date",               fmt_ts(req.to_datetime)),
        ("Time Interval",         intervals.interval_label(req.interval)),
        ("Aggregation",           intervals.agg_label(req.interval, req.agg_function)),
    ]


def _log_export(kind: str, meta: dict, query_ms: float, gen_ms: float, total_ms: float) -> None:
    """Structured, single-line log for every export."""
    logger.info(
        "Export[%s] | equipment=%s | requested=%d %s | valid=%d %s | skipped=%d %s | "
        "query=%.0fms | generate=%.0fms | total=%.0fms",
        kind, meta["equipment"],
        len(meta["requested"]), meta["requested"],
        len(meta["exported"]), meta["exported"],
        len(meta["skipped"]), meta["skipped"],
        query_ms, gen_ms, total_ms,
    )


def _add_export_summary_sheet(wb, meta: dict, report_range: str, interval: str,
                              record_count: int, palette: dict) -> None:
    """Append an 'Export Summary' worksheet documenting the export."""
    P = palette
    ws = wb.create_sheet("Export Summary")
    ws.sheet_view.showGridLines = False
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 90

    title = ws.cell(row=1, column=1, value="Export Summary")
    title.font = Font(color=P["ACCENT"], size=14, bold=True, name="Calibri")
    title.fill = PatternFill("solid", fgColor=P["NAVY"])
    ws.merge_cells("A1:B1")
    ws.cell(row=1, column=2).fill = PatternFill("solid", fgColor=P["NAVY"])
    ws.row_dimensions[1].height = 26

    fields = [
        ("Equipment",       meta["equipment"]),
        ("Export time",     now_dmy()),
        ("Report range",    report_range),
        ("Interval",        interval),
        ("Records",         f"{record_count:,}"),
        ("Selected tags",   ", ".join(meta["requested"]) or "(none)"),
        ("Exported tags",   ", ".join(meta["exported"]) or "(none)"),
        ("Skipped tags",    ", ".join(meta["skipped"]) or "(none)"),
    ]
    label_font = Font(color=P["TEXT2"], size=10, bold=True, name="Calibri")
    value_font = Font(color=P["TEXT1"], size=10, name="Calibri")
    skip_font  = Font(color="FFB020", size=10, bold=True, name="Calibri")
    border     = Border(bottom=Side(style="thin", color=P["BORDER"]))

    for i, (label, value) in enumerate(fields, start=2):
        lc = ws.cell(row=i, column=1, value=label)
        vc = ws.cell(row=i, column=2, value=value)
        lc.font = label_font
        vc.font = skip_font if (label == "Skipped tags" and meta["skipped"]) else value_font
        lc.alignment = Alignment(horizontal="left", vertical="top")
        vc.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
        lc.border = vc.border = border
        ws.row_dimensions[i].height = 20
    ws.sheet_properties.tabColor = P["TEXT3"]


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/equipment-types")
def get_equipment_types(db: Session = Depends(get_db)):
    return ReportsService.get_equipment_types(db)


@router.get("/equipment-list")
def get_equipment_list(type: str = Query(...), db: Session = Depends(get_db)):
    return ReportsService.get_equipment_list(db, type)


@router.get("/mgr/periods", summary="Year/month combinations that carry generation data")
def get_mgr_periods(db: Session = Depends(get_db)):
    """Drives the MGR Month/Year dropdowns — derived from [dbo].[INVERTER_DAILY_GEN]."""
    return mgr_service.get_available_periods(db)


@router.get("/mgr/monthly-generation", summary="Per-inverter monthly generation from INVERTER_DAILY_GEN")
def get_mgr_monthly_generation(
    month: int = Query(..., ge=1, le=12),
    year:  int = Query(..., ge=2000, le=2100),
    db: Session = Depends(get_db),
):
    """Monthly Generation Report: one row per inverter, plus the plant's per-day totals.

    Both are aggregated in SQL from [dbo].[INVERTER_DAILY_GEN] in a single query.
    """
    return mgr_service.get_monthly_generation(db, month, year)


# ── Multi-equipment report (Preconfigured Reports) ────────────────────────────
@router.post("/multi/data", summary="One timestamp-aligned page across several equipment")
def get_multi_report_data(req: MultiReportRequest, db: Session = Depends(get_db)):
    """Preview page for a report spanning several equipment types at once."""
    from app.services import multi_report_service
    return multi_report_service.get_page(db, req)


@router.post("/multi/export/excel", summary="Multi-equipment report as one worksheet")
def export_multi_report_excel(req: MultiReportRequest, db: Session = Depends(get_db)):
    """
    The whole merged dataset as a single worksheet — Timestamp first, then every
    selected column in the order the user picked them. Excel only, by design.
    """
    from app.services import multi_report_service, report_excel

    data    = multi_report_service.build_dataset(db, req)
    labels  = data["labels"]
    ids     = [s["equipment_id"] for s in data["sources"]]
    header  = {
        "subtitle":       "Preconfigured Multi-Equipment Report",
        "equipment_type": ", ".join(sorted({s["equipment_type"] for s in data["sources"]})),
        "equipment_id":   ", ".join(ids),
        "from":           req.from_datetime.strftime("%d/%m/%Y %H:%M"),
        "to":             req.to_datetime.strftime("%d/%m/%Y %H:%M"),
        "interval":       intervals.interval_label(req.interval),
        "agg":            intervals.agg_label(req.interval, req.agg_function),
    }

    def label_fn(col: str) -> str:
        return "Timestamp (DD/MM/YYYY HH:MM:SS)" if col == "timestamp" else labels.get(col, col)

    spec = ("Multi-Equipment Report", header, data["columns"], data["rows"], label_fn)
    payload = report_excel.build_workbook([spec])

    filename = f"Multi_Equipment_Report_{datetime.now().strftime('%d-%m-%Y')}.xlsx"
    logger.info("Multi-report Excel | sources=%d | columns=%d | rows=%d | %d bytes",
                len(ids), len(data["columns"]), len(data["rows"]), len(payload))
    return StreamingResponse(
        io.BytesIO(payload),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f"attachment; filename={filename}",
            "Access-Control-Expose-Headers": "Content-Disposition",
        },
    )


@router.get("/ygr/years", summary="Years that actually carry plant data")
def get_ygr_years(db: Session = Depends(get_db)):
    """Drives the YGR year dropdown — only years present in [dbo].[PPC]."""
    return {"years": ygr_service.get_available_years(db)}


@router.get("/ygr/yearly-generation", summary="Month-by-month yearly generation report")
def get_ygr_yearly_generation(
    year: int = Query(..., ge=2000, le=2100),
    db: Session = Depends(get_db),
):
    """Yearly Generation Report: one row per month with data, aggregated in SQL
    from [dbo].[PPC] (energy + peak power) and [dbo].[WMS] (insolation, for PR)."""
    return ygr_service.get_yearly_report(db, year)


@router.get("/tags")
def get_tags(
    equipment_type: str = Query(...),
    equipment_id: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
):
    return ReportsService.get_tags(db, equipment_type, equipment_id)


class TagAvailabilityRequest(BaseModel):
    equipment_type: str
    equipment_ids:  List[str] = Field(default_factory=list)


@router.post("/tags/availability")
def get_tag_availability(req: TagAvailabilityRequest, db: Session = Depends(get_db)):
    """Tag availability across the selected equipment (cache-backed).

    Each tag reports `available_in`/`total` and an `available` flag so the UI can
    offer common tags and clearly mark partially-available ones.
    """
    return ReportsService.get_tag_availability(db, req.equipment_type, req.equipment_ids)


@router.post("/schema/refresh", summary="Explicitly refresh the schema cache")
def refresh_schema(table: Optional[str] = Query(default=None), db: Session = Depends(get_db)):
    """Reload the in-memory column cache (whole DB, or a single table)."""
    from app.services import schema_cache
    count = schema_cache.refresh(db, table)
    return {"refreshed": table or "all", "count": count, "stats": schema_cache.stats()}


@router.post("/data")
def get_report_data(req: ReportDataRequest, db: Session = Depends(get_db)):
    return ReportsService.get_report_data(db, req)


# ── Batch multi-equipment data load (async job + SSE progress) ─────────────────
@router.post("/data/batch/async")
def get_report_data_batch_async(req: ReportDataRequest):
    """
    Load a paginated preview for many equipment concurrently in the background.
    Returns a job id immediately; the client watches /export/progress/{job_id}
    (SSE) and fetches the merged result from /data/batch/result/{job_id}.
    """
    from app.services import export_jobs

    ids = req.equipment_ids or [req.equipment_id]
    job_id = export_jobs.create_job()

    def run():
        try:
            def prog(pct, msg):
                export_jobs.update(job_id, status="running", progress=pct, message=msg)
            result = ReportsService.get_report_data_batch(req, ids, prog)
            export_jobs.set_json_result(job_id, result,
                                        message=f"Loaded {result['equipment_count']} equipment.")
        except Exception as e:  # noqa: BLE001
            logger.error("Batch data job %s failed: %s", job_id, e, exc_info=True)
            export_jobs.set_error(job_id, f"{type(e).__name__}: {e}")

    threading.Thread(target=run, name=f"batch-{job_id}", daemon=True).start()
    logger.info("Batch data job %s created -> %d equipment (%s)",
                job_id, len(ids), req.equipment_type)
    return {"job_id": job_id, "count": len(ids)}


@router.get("/data/batch/result/{job_id}")
def get_report_data_batch_result(job_id: str):
    """Fetch (and clean up) the merged result for a completed batch data job."""
    from app.services import export_jobs

    result = export_jobs.get_json_result(job_id)
    if result is None:
        raise HTTPException(404, detail="Result not ready or job expired")
    export_jobs.cleanup(job_id)
    return result


# ── Server-side paginated merged view (infinite scroll) ────────────────────────
@router.post("/data/merged/first")
def get_merged_first_page(req: ReportDataRequest):
    """
    Start loading the first page of the merged multi-equipment view in the
    background (so per-equipment count preparation can report progress over SSE).
    Returns a job id; the client watches /export/progress/{job_id} and fetches the
    page from /data/batch/result/{job_id}.
    """
    from app.services import export_jobs

    ids = req.equipment_ids or [req.equipment_id]
    page_size = req.page_size or 200
    job_id = export_jobs.create_job()

    def run():
        try:
            def prog(pct, msg):
                export_jobs.update(job_id, status="running", progress=pct, message=msg)
            result = ReportsService.get_merged_page(req, ids, page=1, page_size=page_size, progress=prog)
            export_jobs.set_json_result(job_id, result,
                                        message=f"Loaded {result['total_records']} rows.")
        except Exception as e:  # noqa: BLE001
            logger.error("Merged first-page job %s failed: %s", job_id, e, exc_info=True)
            export_jobs.set_error(job_id, f"{type(e).__name__}: {e}")

    threading.Thread(target=run, name=f"merged-{job_id}", daemon=True).start()
    logger.info("Merged first-page job %s created -> %d equipment (%s)",
                job_id, len(ids), req.equipment_type)
    return {"job_id": job_id, "count": len(ids)}


@router.post("/data/merged/page")
def get_merged_page(req: ReportDataRequest):
    """
    Return one page of the merged view synchronously (counts are cached from the
    first-page load, so this is a single windowed query — fast). Used for
    infinite-scroll page fetches as the user scrolls the virtualized table.
    """
    ids = req.equipment_ids or [req.equipment_id]
    return ReportsService.get_merged_page(req, ids, page=req.page, page_size=req.page_size or 200)


# ── Export CSV ────────────────────────────────────────────────────────────────
@router.post("/export/csv")
def export_csv(req: ReportDataRequest, db: Session = Depends(get_db)):
    t0 = time.perf_counter()
    req.page      = 1
    req.page_size = 10000

    tq = time.perf_counter()
    result = ReportsService.get_report_data(db, req)   # never fails on missing tags
    query_ms = (time.perf_counter() - tq) * 1000

    rows   = result.get("rows", [])
    cols   = result.get("columns", [])
    meta   = _export_meta(req, result)

    tg = time.perf_counter()
    csv_bytes = build_report_csv(
        cols, rows,
        metadata=_csv_metadata(req),   # standard 8-field metadata block + blank row
        header_fn=col_header,          # user-friendly, unit-labelled headers
    )
    gen_ms   = (time.perf_counter() - tg) * 1000
    filename = f"{display_equipment_id(req.equipment_type, req.equipment_id)}_Report_{today_dmy()}.csv"
    _log_export("CSV", meta, query_ms, gen_ms, (time.perf_counter() - t0) * 1000)

    return StreamingResponse(
        iter([csv_bytes]),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename={filename}",
            "Access-Control-Expose-Headers": "Content-Disposition",
        }
    )


# ── Export Excel ──────────────────────────────────────────────────────────────
@router.post("/export/excel")
def export_excel(req: ReportDataRequest, db: Session = Depends(get_db)):
    """Synchronous Excel export — funnels through the shared report_excel service.

      * String Combiner  -> one worksheet per inverter (INV1 … INV24), SMBs stacked
      * all other types  -> one worksheet per equipment (INVERTER_01, INVERTER_02, ...)
    """
    ids = req.equipment_ids or [req.equipment_id]
    logger.info("Excel export request | type=%s | equipment=%d | tags_received=%d",
                req.equipment_type, len(ids), len(req.tags or []))
    xlsx_ctype = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if req.equipment_type == "String Combiner":
        from app.services.smb_excel import build_smb_workbook
        # One SMB{k} sheet per SCB; single inverter -> INV{n}.xlsx, several -> a .zip.
        output, filename, ctype = build_smb_workbook(db, req, ids)
    else:
        from app.services import report_excel
        data, filename = report_excel.build_multi_equipment_workbook(req, ids)
        output, ctype = io.BytesIO(data), xlsx_ctype
    return StreamingResponse(
        output,
        media_type=ctype,
        headers={
            "Content-Disposition": f"attachment; filename={filename}",
            "Access-Control-Expose-Headers": "Content-Disposition",
        },
    )


# ── Async batch export (job + SSE progress + download) ─────────────────────────
@router.post("/export/excel/async")
def export_excel_async(req: ReportDataRequest):
    """
    Start a background Excel export and return a job id immediately.
    Used for large batch exports (String Combiner multi-inverter, T1 Isolation) so
    the HTTP request never blocks / times out. Single-equipment exports for other
    types keep using the synchronous /export/excel endpoint.
    """
    from app.services import export_jobs

    ids = req.equipment_ids or [req.equipment_id]
    job_id = export_jobs.create_job()

    # Diagnostic: log EXACTLY what the client sent so a "only one file exported"
    # report can be traced to whether the browser sent one id or all of them.
    logger.info(
        "Async export request | type=%s | equipment_ids(%d)=%s | equipment_id=%r | tags=%d",
        req.equipment_type, len(ids), ids, req.equipment_id, len(req.tags or []),
    )

    def run():
        try:
            def prog(pct, msg):
                export_jobs.update(job_id, status="running", progress=pct, message=msg)

            ctype = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            if req.equipment_type in ("T1 Isolation", "T2 Isolation", "Tracker"):
                from app.services.t1_isolation_excel import generate_t1_isolation_export
                data, filename, ctype = generate_t1_isolation_export(req, ids, prog)
            elif req.equipment_type == "String Combiner":
                from app.services.smb_excel import generate_smb_workbook_streaming
                # One SMB{k} sheet per SCB; several inverters come back as a .zip.
                data, filename, ctype = generate_smb_workbook_streaming(req, ids, prog)
            else:
                # All other equipment types → one worksheet per equipment.
                from app.services import report_excel
                data, filename = report_excel.build_multi_equipment_workbook(req, ids, prog)

            export_jobs.set_result(job_id, data, filename, ctype)
        except Exception as e:  # noqa: BLE001 — surface failure to the client
            logger.error("Async export job %s failed: %s", job_id, e, exc_info=True)
            export_jobs.set_error(job_id, f"{type(e).__name__}: {e}")

    threading.Thread(target=run, name=f"export-{job_id}", daemon=True).start()
    logger.info("Async export job %s created -> %d equipment (%s)", job_id, len(ids), req.equipment_type)
    return {"job_id": job_id, "count": len(ids)}


@router.get("/export/progress/{job_id}")
async def export_progress(job_id: str):
    """Server-Sent Events stream of a job's progress until done/error."""
    from app.services import export_jobs

    async def event_gen():
        last = None
        while True:
            snap = export_jobs.status(job_id)
            if snap is None:
                yield f"data: {json.dumps({'status': 'error', 'message': 'Job not found', 'progress': 0})}\n\n"
                return
            key = (snap["progress"], snap["message"], snap["status"])
            if key != last:
                yield f"data: {json.dumps(snap)}\n\n"
                last = key
            if snap["status"] in ("done", "error"):
                return
            await asyncio.sleep(0.4)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/export/download/{job_id}")
def export_download(job_id: str):
    """Download the finished workbook for a completed job."""
    from app.services import export_jobs

    data, filename, content_type = export_jobs.get_result(job_id)
    if data is None:
        raise HTTPException(404, detail="Export not ready or job expired")

    def stream_and_cleanup():
        yield data
        export_jobs.cleanup(job_id)

    return StreamingResponse(
        stream_and_cleanup(),
        media_type=content_type or "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f"attachment; filename={filename}",
            "Access-Control-Expose-Headers": "Content-Disposition",
        }
    )


# ── Direct-to-Downloads export (T1/T2 Isolation) — no ZIP ──────────────────────
# Each device report is built in parallel (ProcessPool) and written straight to the
# user's Downloads folder the moment it finishes. Progress is reported over the same
# SSE endpoint (/export/progress/{job_id}); there is no file to download afterwards.
@router.post("/export/excel/to-downloads")
def export_excel_to_downloads(req: ReportDataRequest):
    from app.services import export_jobs

    if req.equipment_type not in ("T1 Isolation", "T2 Isolation", "Tracker", "String Combiner"):
        raise HTTPException(400, detail="Direct-to-Downloads export is only for Tracker / String Combiner")

    ids = req.equipment_ids or [req.equipment_id]
    job_id = export_jobs.create_job()
    export_jobs.update(job_id, status="running", progress=0, message="Queued…")
    logger.info("To-Downloads export queued | job=%s | type=%s | equipment(%d)=%s | tags=%d",
                job_id, req.equipment_type, len(ids), ids, len(req.tags or []))

    def run():
        try:
            def prog(pct, msg):
                export_jobs.update(job_id, status="running", progress=pct, message=msg)

            # Each export writes its files into a single timestamped parent folder in
            # Downloads (Tracker_Reports_<ts>\Tracker{n}.xlsx / SMB_Reports_<ts>\INV{n}.xlsx).
            if req.equipment_type == "String Combiner":
                from app.services.smb_excel import export_smb_to_downloads
                result = export_smb_to_downloads(req, ids, prog)
            else:
                from app.services.t1_isolation_excel import export_to_downloads
                result = export_to_downloads(req, ids, prog)
            # Absolute path on disk (the ZIP for Tracker, the folder for SMB).
            saved_path = result.get("path") or result.get("directory") or "Downloads"
            count = result.get("count", 0)
            logger.info("To-Downloads done | job=%s | saved=%s | files=%d | excel=%.1fs | zip=%.2fs | total=%.1fs",
                        job_id, saved_path, count, result.get("excel_seconds", 0),
                        result.get("zip_seconds", 0), result.get("total_seconds", 0))
            export_jobs.update(job_id, status="done", progress=100,
                               message=f"Saved {count} report(s) to {saved_path}",
                               json_result=result)
        except Exception as e:  # noqa: BLE001 — surface failure to the client
            logger.error("To-Downloads export job %s failed: %s", job_id, e, exc_info=True)
            export_jobs.set_error(job_id, f"{type(e).__name__}: {e}")

    threading.Thread(target=run, name=f"dl-export-{job_id}", daemon=True).start()
    return {"job_id": job_id, "count": len(ids)}


# ── Folder exports (NEW, additive): Tracker + per-SCB, one file per unit ────────
# These write an organised folder tree to the server's disk (single-machine deploy),
# reusing the SAME byte-identical renderers as the existing exports. They run as
# background jobs and report over the same SSE endpoint (/export/progress/{job_id}).
# The existing ZIP / to-Downloads exports are left untouched.
@router.post("/export/trackers/to-folders")
def export_trackers_to_folders(req: ReportDataRequest):
    """Tracker (T1/T2 Isolation) → <TRACKER_EXPORT_DIR>\\Tracker{n}\\Tags_{a}_{b}.xlsx."""
    from app.services import export_jobs

    ids = req.equipment_ids or [req.equipment_id]
    job_id = export_jobs.create_job()
    export_jobs.update(job_id, status="running", progress=0, message="Queued…")
    logger.info("Tracker folder export queued | job=%s | equipment(%d)=%s | tags=%d",
                job_id, len(ids), ids, len(req.tags or []))

    def run():
        try:
            from app.services.t1_isolation_excel import export_trackers_to_folders as _run

            def prog(pct, msg):
                export_jobs.update(job_id, status="running", progress=pct, message=msg)

            result = _run(req, ids, prog)
            export_jobs.update(job_id, status="done", progress=100,
                               message=f"Saved {result.get('files', 0)} file(s) across "
                                       f"{result.get('count', 0)} tracker folder(s).",
                               json_result=result)
        except Exception as e:  # noqa: BLE001 — surface failure to the client
            logger.error("Tracker folder export job %s failed: %s", job_id, e, exc_info=True)
            export_jobs.set_error(job_id, f"{type(e).__name__}: {e}")

    threading.Thread(target=run, name=f"tracker-export-{job_id}", daemon=True).start()
    return {"job_id": job_id, "count": len(ids)}


@router.post("/export/smb/to-folders")
def export_smb_to_folders(req: ReportDataRequest):
    """String Combiner → <SMB_EXPORT_DIR>\\INV{n}\\SCB{k}.xlsx (one file per SCB)."""
    from app.services import export_jobs

    ids = req.equipment_ids or [req.equipment_id]
    job_id = export_jobs.create_job()
    export_jobs.update(job_id, status="running", progress=0, message="Queued…")
    logger.info("SMB folder export queued | job=%s | inverters(%d)=%s | tags=%d",
                job_id, len(ids), ids, len(req.tags or []))

    def run():
        try:
            from app.services.smb_excel import export_smb_to_folders as _run

            def prog(pct, msg):
                export_jobs.update(job_id, status="running", progress=pct, message=msg)

            result = _run(req, ids, prog)
            export_jobs.update(job_id, status="done", progress=100,
                               message=f"Saved {result.get('files', 0)} SCB file(s) across "
                                       f"{result.get('count', 0)} inverter folder(s).",
                               json_result=result)
        except Exception as e:  # noqa: BLE001 — surface failure to the client
            logger.error("SMB folder export job %s failed: %s", job_id, e, exc_info=True)
            export_jobs.set_error(job_id, f"{type(e).__name__}: {e}")

    threading.Thread(target=run, name=f"smb-export-{job_id}", daemon=True).start()
    return {"job_id": job_id, "count": len(ids)}


# ── Single-request STREAMING export (T1 Isolation) ─────────────────────────────
# One prepare call stashes the request; the browser then downloads the streaming
# GET, which builds the workbook/ZIP sequentially and streams it straight to disk
# while pushing progress into the same job the SSE endpoint reports. Nothing is
# held in browser memory and no parallel/batch/per-device requests are involved.
@router.post("/export/excel/stream/prepare")
def export_excel_stream_prepare(req: ReportDataRequest):
    from app.services import export_jobs

    if req.equipment_type not in ("T1 Isolation", "T2 Isolation", "Tracker"):
        raise HTTPException(400, detail="Streaming export is only available for T1/T2 Isolation")

    ids = req.equipment_ids or [req.equipment_id]
    job_id = export_jobs.create_job()
    export_jobs.set_request(job_id, req, ids)
    export_jobs.update(job_id, status="running", progress=0, message="Preparing export…")
    logger.info("Stream export prepared | job=%s | type=%s | equipment(%d)=%s | tags=%d",
                job_id, req.equipment_type, len(ids), ids, len(req.tags or []))
    return {"job_id": job_id, "count": len(ids)}


@router.get("/export/excel/stream/{job_id}")
def export_excel_stream(job_id: str):
    """Generate the export SEQUENTIALLY and stream it to the client as it is built."""
    from app.services import export_jobs
    from app.services.t1_isolation_excel import stream_t1_isolation_export

    stashed = export_jobs.get_request(job_id)
    if not stashed:
        raise HTTPException(404, detail="Export job not found or expired")
    req, ids = stashed

    def prog(pct, msg):
        export_jobs.update(job_id, status="running", progress=pct, message=msg)

    try:
        byte_iter, filename, ctype = stream_t1_isolation_export(req, ids, prog)
    except Exception as e:  # noqa: BLE001 — invalid selection etc.
        export_jobs.set_error(job_id, f"{type(e).__name__}: {e}")
        raise HTTPException(400, detail=str(e))

    def body():
        try:
            for chunk in byte_iter:
                yield chunk
            # Release the stashed request but keep a small 'done' status so the
            # SSE progress stream can observe completion (TTL purges it later).
            export_jobs.update(job_id, status="done", progress=100,
                               message="Export completed.", filename=filename, request=None)
        except Exception as e:  # noqa: BLE001 — a mid-stream failure truncates the download
            logger.error("Stream export job %s failed mid-stream: %s", job_id, e, exc_info=True)
            export_jobs.set_error(job_id, f"{type(e).__name__}: {e}")
            raise

    return StreamingResponse(
        body(),
        media_type=ctype,
        headers={
            "Content-Disposition": f"attachment; filename={filename}",
            "Access-Control-Expose-Headers": "Content-Disposition",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── Summary ───────────────────────────────────────────────────────────────────
@router.post("/summary")
def get_summary(req: ReportDataRequest, db: Session = Depends(get_db)):
    from app.repositories.reports_repository import EQUIPMENT_REGISTRY, _safe_name
    from sqlalchemy import text

    config   = EQUIPMENT_REGISTRY.get(req.equipment_type, {})
    tag_meta = config.get("tags", {})

    column_names = []
    for tag in req.tags:
        if tag in tag_meta:
            column_names.append(tag)
        else:
            found = next(
                (col for col, m in tag_meta.items() if m["label"] == tag),
                tag
            )
            column_names.append(found)

    for col in column_names:
        if not _safe_name(col):
            raise HTTPException(400, detail=f"Invalid tag: {col}")

    table     = req.equipment_id
    agg_parts = []
    for col in column_names:
        agg_parts.extend([
            f"AVG(CAST([{col}] AS FLOAT)) AS [{col}_avg]",
            f"MIN(CAST([{col}] AS FLOAT)) AS [{col}_min]",
            f"MAX(CAST([{col}] AS FLOAT)) AS [{col}_max]",
            f"SUM(CAST([{col}] AS FLOAT)) AS [{col}_sum]",
        ])

    sql = text(f"""
        SELECT COUNT(*) AS record_count, {', '.join(agg_parts)}
        FROM [{table}]
        WHERE TimeCol BETWEEN :from_dt AND :to_dt
    """)
    try:
        row = db.execute(sql, {
            "from_dt": req.from_datetime.strftime("%Y-%m-%d %H:%M:%S"),
            "to_dt":   req.to_datetime.strftime("%Y-%m-%d %H:%M:%S"),
        }).fetchone()
        if not row:
            return {"record_count": 0, "stats": {}}
        stats = {"record_count": row[0]}
        idx   = 1
        for col in column_names:
            label = tag_meta.get(col, {}).get("label", col)
            stats[label] = {
                "avg": round(float(row[idx]),   3) if row[idx]   is not None else None,
                "min": round(float(row[idx+1]), 3) if row[idx+1] is not None else None,
                "max": round(float(row[idx+2]), 3) if row[idx+2] is not None else None,
                "sum": round(float(row[idx+3]), 3) if row[idx+3] is not None else None,
            }
            idx += 4
        return stats
    except Exception as e:
        logger.error(f"Summary error: {e}", exc_info=True)
        raise HTTPException(500, detail=str(e))
