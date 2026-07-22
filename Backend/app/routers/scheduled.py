# app/routers/scheduled.py
"""
Scheduled-report e-mail delivery (testing phase).

Generates the selected report (CSV / Excel), attaches it, and e-mails it to the
single pre-configured recipient. Every execution is logged with the time,
recipient, report, status, and any error.

No scheduler/persistence is introduced here — these endpoints are invoked on
demand (Run Now / Test Email). Multi-recipient + cron come in a later phase.
"""

import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.services import email_service, intervals
from app.services.report_files import (
    build_csv_bytes,
    build_excel_bytes,
    sample_report,
    fmt_timestamp,
    PROJECT_NAME,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/scheduled", tags=["Scheduled Reports"])

XLSX_MIME = ("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet")
CSV_MIME = ("text", "csv")


def _now_dmy() -> str:
    return datetime.now().strftime("%d/%m/%Y %H:%M:%S")


def _normalize_format(fmt: Optional[str]) -> str:
    return "CSV" if (fmt or "").strip().upper() == "CSV" else "Excel"


# ── Request models ──────────────────────────────────────────────────────────
class ScheduleSendRequest(BaseModel):
    equipment_type: str = Field(default="Inverter")
    equipment_id:   str = Field(default="INVERTER_01")
    format:         str = Field(default="Excel", description="CSV or Excel")
    # Optional real-data parameters. When tags + dates are absent we fall back to
    # a clearly-labelled sample report (schedules don't persist tags yet).
    tags:          List[str] = Field(default_factory=list)
    from_datetime: Optional[datetime] = None
    to_datetime:   Optional[datetime] = None
    interval:      str = "hourly"
    agg_function:  str = "avg"


class TestEmailRequest(BaseModel):
    format:         str = Field(default="Excel", description="CSV or Excel")
    equipment_type: str = "Inverter"
    equipment_id:   str = "INVERTER_01"


# ── Data + file generation ──────────────────────────────────────────────────
def _fetch_real_data(db: Session, req: ScheduleSendRequest):
    """Return (columns, rows) from the database, or None if not possible."""
    if not (req.tags and req.from_datetime and req.to_datetime):
        return None
    try:
        from app.schemas.reports_schema import ReportDataRequest
        from app.services.reports_service import ReportsService

        data_req = ReportDataRequest(
            equipment_type=req.equipment_type,
            equipment_id=req.equipment_id,
            tags=req.tags,
            from_datetime=req.from_datetime,
            to_datetime=req.to_datetime,
            interval=req.interval,
            agg_function=req.agg_function,
            page=1,
            page_size=10000,
        )
        result = ReportsService.get_report_data(db, data_req)
        return result.get("columns", []), result.get("rows", [])
    except Exception as e:  # noqa: BLE001 — fall back to sample, but log why
        logger.warning("Real report data unavailable, using sample. Reason: %s", e)
        return None


def _build_attachment(db: Session, req: ScheduleSendRequest):
    """Build the report file. Returns (bytes, filename, mime_main, mime_sub, source)."""
    fmt = _normalize_format(req.format)

    fetched = _fetch_real_data(db, req)
    if fetched is not None:
        columns, rows = fetched
        source = "database"
    else:
        columns, rows = sample_report(req.equipment_type, req.equipment_id)
        source = "sample"

    stamp = datetime.now().strftime("%d-%m-%Y")
    base = f"{req.equipment_id or 'Report'}_Report_{stamp}"
    title = f"Kalyon Solar Monitoring — {req.equipment_type} / {req.equipment_id}"

    if fmt == "CSV":
        metadata = [
            ("Project Name",          PROJECT_NAME),
            ("Report Name",           f"{req.equipment_type} Report"),
            ("Equipment",             req.equipment_id or ""),
            ("Generated Date & Time", _now_dmy()),
            ("From Date",             fmt_timestamp(req.from_datetime) if req.from_datetime else ""),
            ("To Date",               fmt_timestamp(req.to_datetime) if req.to_datetime else ""),
            ("Time Interval",         intervals.interval_label(req.interval)),
            ("Aggregation",           intervals.agg_label(req.interval, req.agg_function)),
        ]
        data = build_csv_bytes(columns, rows, metadata=metadata)
        return data, f"{base}.csv", CSV_MIME[0], CSV_MIME[1], source

    data = build_excel_bytes(columns, rows, sheet_title=req.equipment_id, title=title)
    return data, f"{base}.xlsx", XLSX_MIME[0], XLSX_MIME[1], source


def _execute(db: Session, req: ScheduleSendRequest, *, kind: str) -> dict:
    """Generate the report, e-mail it, log the outcome, and return a status record."""
    exec_time = _now_dmy()
    recipient = email_service.get_recipient_email()
    fmt = _normalize_format(req.format)

    logger.info(
        "[%s] Schedule execution started -> time=%s | recipient=%s | equipment=%s/%s | format=%s",
        kind, exec_time, recipient, req.equipment_type, req.equipment_id, fmt,
    )

    log = {
        "execution_time": exec_time,
        "recipient":      recipient,
        "equipment":      f"{req.equipment_type} / {req.equipment_id}",
        "format":         fmt,
        "report":         None,
        "data_source":    None,
        "status":         "Failed",
        "error":          None,
    }

    # Fail fast with a meaningful message if SMTP isn't configured.
    missing = email_service.get_missing_config()
    if missing:
        log["error"] = (
            "SMTP is not configured. Missing environment variable(s): "
            + ", ".join(missing)
            + ". Set them in Backend/.env (see .env.example) to enable e-mail delivery."
        )
        logger.error("[%s] Execution aborted -> %s", kind, log["error"])
        return log

    try:
        data, filename, mime_main, mime_sub, source = _build_attachment(db, req)
        log["report"] = filename
        log["data_source"] = source
        logger.info("[%s] Report generated -> file=%s | source=%s | size=%d bytes",
                    kind, filename, source, len(data))
    except Exception as e:  # noqa: BLE001
        log["error"] = f"Report generation failed: {type(e).__name__}: {e}"
        logger.error("[%s] %s", kind, log["error"], exc_info=True)
        return log

    subject = f"Kalyon Scheduled Report - {req.equipment_type}/{req.equipment_id} - {exec_time}"
    body = (
        "Kalyon Solar Monitoring — Automated Report\n\n"
        f"Equipment : {req.equipment_type} / {req.equipment_id}\n"
        f"Format    : {fmt}\n"
        f"Generated : {exec_time}\n"
        f"Source    : {source} data\n\n"
        "The requested report is attached.\n"
    )

    result = email_service.send_email_with_attachment(
        to_email=recipient,
        subject=subject,
        body=body,
        attachment_bytes=data,
        attachment_filename=filename,
        mime_main=mime_main,
        mime_sub=mime_sub,
    )

    if result["success"]:
        log["status"] = "Success"
        logger.info("[%s] Email status -> SUCCESS | recipient=%s | report=%s",
                    kind, recipient, filename)
    else:
        log["error"] = result["error"]
        logger.error("[%s] Email status -> FAILED | recipient=%s | error=%s",
                     kind, recipient, result["error"])

    return log


# ── Endpoints ───────────────────────────────────────────────────────────────
@router.get("/config")
def get_email_config():
    """Expose non-sensitive e-mail config so the UI can show status + setup help."""
    cfg = email_service.get_smtp_config()
    return {
        "recipient":       email_service.get_recipient_email(),
        "smtp_configured": email_service.is_configured(),
        "missing_vars":    email_service.get_missing_config(),
        "required_vars":   list(email_service.REQUIRED_VARS),
        "smtp_host":       cfg["host"] or None,
        "smtp_port":       cfg["port"],
    }


@router.post("/send")
def send_scheduled_report(req: ScheduleSendRequest, db: Session = Depends(get_db)):
    """Execute a schedule on demand: generate the report, attach it, and e-mail it."""
    return _execute(db, req, kind="RUN")


@router.post("/test-email")
def send_test_email(req: TestEmailRequest, db: Session = Depends(get_db)):
    """Generate a sample report and e-mail it to the configured recipient."""
    send_req = ScheduleSendRequest(
        equipment_type=req.equipment_type,
        equipment_id=req.equipment_id,
        format=req.format,
        # No tags/dates -> always uses the labelled sample report.
    )
    return _execute(db, send_req, kind="TEST")
