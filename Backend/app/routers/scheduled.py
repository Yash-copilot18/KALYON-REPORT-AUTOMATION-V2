# app/routers/scheduled.py
"""
Scheduled-report API — database-backed.

Schedules are persisted in `report_schedules` and executed automatically by the
background scheduler (see app/services/scheduler.py) on their configured
frequency. Every execution (automatic or Run Now) is logged in `schedule_runs`.

Report generation is reused from the Reports module via schedule_service — there
is no duplicated query/formatting code here.

Endpoints:
  GET    /scheduled/config                      e-mail/SMTP status for the UI
  POST   /scheduled/test-email                  send a labelled SAMPLE report
  GET    /scheduled/schedules                    list all schedules
  POST   /scheduled/schedules                    create a schedule
  PUT    /scheduled/schedules/{id}               edit a schedule
  DELETE /scheduled/schedules/{id}               delete a schedule
  POST   /scheduled/schedules/{id}/run           Run Now (generate + e-mail + log)
  POST   /scheduled/schedules/{id}/pause         pause (skip automatic runs)
  POST   /scheduled/schedules/{id}/resume        resume
  GET    /scheduled/schedules/{id}/runs          execution history
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.services import email_service, schedule_service
from app.services.report_files import (
    build_excel_bytes, sample_report, PROJECT_NAME,
)
from app.services.report_pdf import build_pdf_bytes, PDF_MIME

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/scheduled", tags=["Scheduled Reports"])

XLSX_MIME = ("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def _now_dmy() -> str:
    return datetime.now().strftime("%d/%m/%Y %H:%M:%S")


def _normalize_format(fmt: Optional[str]) -> str:
    """Scheduled Reports supports ONLY Excel and PDF (CSV removed)."""
    return "PDF" if (fmt or "").strip().upper() == "PDF" else "Excel"


# ── Request models ──────────────────────────────────────────────────────────
class SchedulePayload(BaseModel):
    """Create/edit body — accepts the existing form's field names."""
    eq_type:    str = Field(default="")
    eq_id:      str = Field(default="")
    from_:      Optional[str] = Field(default=None, alias="from")
    to:         Optional[str] = None
    interval:   str = "hourly"
    agg:        str = "avg"
    format:     str = "Excel"
    freq:       str = "Daily"
    time:       Optional[str] = None       # HH:MM scheduled execution time (default 20:30)
    status:     Optional[str] = None
    # One or more destination addresses. Accepts a list (the multi-recipient UI) or a
    # comma/semicolon-separated string (the original single-recipient form and any
    # existing API client), so both callers keep working.
    recipients: Optional[Union[str, List[str]]] = None

    model_config = {"populate_by_name": True}

    def as_dict(self) -> Dict[str, Any]:
        return {
            "eq_type": self.eq_type, "eq_id": self.eq_id,
            "from": self.from_, "to": self.to,
            "interval": self.interval, "agg": self.agg,
            "format": self.format, "freq": self.freq, "time": self.time,
            "status": self.status, "recipients": self.recipients,
        }


class TestEmailRequest(BaseModel):
    format:         str = Field(default="Excel", description="CSV or Excel")
    equipment_type: str = "Inverter"
    equipment_id:   str = "INVERTER_01"


# ── Config + Test Email ─────────────────────────────────────────────────────
@router.get("/config")
def get_email_config():
    """Non-sensitive e-mail config so the UI can show status + setup help."""
    cfg = email_service.get_smtp_config()
    return {
        "recipient":       email_service.get_recipient_email(),
        "smtp_configured": email_service.is_configured(),
        "missing_vars":    email_service.get_missing_config(),
        "required_vars":   list(email_service.REQUIRED_VARS),
        "smtp_host":       cfg["host"] or None,
        "smtp_port":       cfg["port"],
    }


@router.post("/test-email")
def send_test_email(req: TestEmailRequest):
    """Generate a labelled SAMPLE report and e-mail it to the configured recipient."""
    exec_time = _now_dmy()
    recipient = email_service.get_recipient_email()
    fmt = _normalize_format(req.format)

    missing = email_service.get_missing_config()
    if missing:
        return {"status": "Failed", "execution_time": exec_time, "recipient": recipient,
                "error": "SMTP is not configured. Missing: " + ", ".join(missing)}

    columns, rows = sample_report(req.equipment_type, req.equipment_id)
    stamp = datetime.now().strftime("%d-%m-%Y")
    base  = f"{req.equipment_id or 'Report'}_Sample_{stamp}"
    if fmt == "PDF":
        data = build_pdf_bytes(columns, rows,
                               title="Kalyon Solar Monitoring — Sample",
                               metadata=[("Project Name", PROJECT_NAME)])
        filename, mm, ms = f"{base}.pdf", PDF_MIME[0], PDF_MIME[1]
    else:
        data = build_excel_bytes(columns, rows, sheet_title=req.equipment_id,
                                 title=f"Kalyon Solar Monitoring — Sample")
        filename, mm, ms = f"{base}.xlsx", XLSX_MIME[0], XLSX_MIME[1]

    result = email_service.send_email_with_attachment(
        to_email=recipient, subject=f"Kalyon Test Email - {exec_time}",
        body="Kalyon Solar Monitoring — sample report attached.\n",
        attachment_bytes=data, attachment_filename=filename, mime_main=mm, mime_sub=ms,
    )
    if result.get("success"):
        return {"status": "Success", "execution_time": exec_time, "recipient": recipient,
                "report": filename}
    return {"status": "Failed", "execution_time": exec_time, "recipient": recipient,
            "error": result.get("error")}


# ── Schedule CRUD ───────────────────────────────────────────────────────────
def _row(db: Session, s) -> Dict:
    return schedule_service.to_row(s, fallback_recipient=email_service.get_recipient_email())


@router.get("/schedules")
def list_schedules(db: Session = Depends(get_db)) -> List[Dict]:
    return [_row(db, s) for s in schedule_service.list_schedules(db)]


def _validate_type(eq_type: str) -> None:
    """A report type is required; every supported equipment/report type is schedulable."""
    if not (eq_type or "").strip():
        raise HTTPException(400, detail="Report type is required")
    if not schedule_service.is_supported_type(eq_type):
        raise HTTPException(400, detail="Unsupported report type.")


def _validate_recipients(payload: SchedulePayload) -> None:
    """
    Reject the request if ANY supplied address is malformed, naming the offenders so
    the UI can show exactly which one to fix. An omitted/empty list is fine — the
    schedule then falls back to the configured recipient. Duplicates are not an error
    here: they are collapsed case-insensitively when normalised for storage.
    """
    if payload.recipients is None:
        return
    bad = schedule_service.invalid_recipients(payload.recipients)
    if bad:
        raise HTTPException(400, detail="Invalid e-mail address: " + ", ".join(bad))


def _require_email_config() -> None:
    """
    Refuse to put a schedule into the ACTIVE state while SMTP is unconfigured, so the
    problem surfaces the moment the user creates/enables it instead of silently
    failing at the scheduled hour. Only the missing VARIABLE NAMES are returned —
    never a value. Creating a Paused schedule is still allowed.
    """
    missing = email_service.get_missing_config()
    if missing:
        raise HTTPException(400, detail=(
            "E-mail is not configured, so this schedule could not send its report. "
            "Set " + ", ".join(missing) + " in Backend/.env (see EMAIL_SETUP.md) and "
            "restart the backend."))


@router.post("/schedules")
def create_schedule(payload: SchedulePayload, db: Session = Depends(get_db)) -> Dict:
    _validate_type(payload.eq_type)
    _validate_recipients(payload)
    if (payload.status or "Active").strip().lower() != "paused":
        _require_email_config()
    s = schedule_service.create_schedule(db, payload.as_dict())
    return _row(db, s)


@router.put("/schedules/{schedule_id}")
def update_schedule(schedule_id: int, payload: SchedulePayload,
                    db: Session = Depends(get_db)) -> Dict:
    if payload.eq_type:
        _validate_type(payload.eq_type)
    _validate_recipients(payload)
    s = schedule_service.update_schedule(db, schedule_id, payload.as_dict())
    if not s:
        raise HTTPException(404, detail="Schedule not found")
    return _row(db, s)


@router.delete("/schedules/{schedule_id}")
def delete_schedule(schedule_id: int, db: Session = Depends(get_db)) -> Dict:
    if not schedule_service.delete_schedule(db, schedule_id):
        raise HTTPException(404, detail="Schedule not found")
    return {"deleted": schedule_id}


@router.post("/schedules/{schedule_id}/run")
def run_schedule(schedule_id: int, db: Session = Depends(get_db)) -> Dict:
    res = schedule_service.run_schedule_now(db, schedule_id)
    if res is None:
        raise HTTPException(404, detail="Schedule not found")
    return res


@router.post("/schedules/{schedule_id}/pause")
def pause_schedule(schedule_id: int, db: Session = Depends(get_db)) -> Dict:
    s = schedule_service.set_status(db, schedule_id, "Paused")
    if not s:
        raise HTTPException(404, detail="Schedule not found")
    return _row(db, s)


@router.post("/schedules/{schedule_id}/resume")
def resume_schedule(schedule_id: int, db: Session = Depends(get_db)) -> Dict:
    _require_email_config()          # enabling is the other moment to surface this
    s = schedule_service.set_status(db, schedule_id, "Active")
    if not s:
        raise HTTPException(404, detail="Schedule not found")
    return _row(db, s)


@router.get("/schedules/{schedule_id}/runs")
def schedule_runs(schedule_id: int, db: Session = Depends(get_db)) -> List[Dict]:
    runs = schedule_service.list_runs(db, schedule_id)
    return [{
        "time":     r.execution_time.strftime("%d/%m/%Y %H:%M:%S") if r.execution_time else "",
        "status":   r.status,
        "size":     f"{r.file_size / 1024:.0f} KB" if r.file_size else "—",
        "duration": f"{r.duration_ms / 1000:.0f}s" if r.duration_ms else "—",
        "report":   r.report or "—",
        "error":    r.error,
    } for r in runs]
