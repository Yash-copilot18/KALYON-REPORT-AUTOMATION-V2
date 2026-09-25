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
  GET    /scheduled/schedules/{id}/runs          execution history
  GET    /scheduled/data-window                  first/last stored timestamp for a report
  POST   /scheduled/generate-send                ONE-OFF generate + e-mail (synchronous)
  POST   /scheduled/schedules/{id}/run/start     Generate & Send a SAVED schedule -> job id
  POST   /scheduled/generate-send/start          ad-hoc generate + e-mail -> job id
  GET    /scheduled/generate-send/status/{job}   progress + result of that job
  GET    /scheduled/mail-quota                   mails sent against the 20-mail cap
"""

import logging
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database.session import get_db, SessionLocal
from app.services import email_service, export_jobs, mail_quota, schedule_service
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


def _validate_runnable(s) -> None:
    """
    Gate a manual send of a SAVED schedule.

    The automatic scheduler is untouched by this: it keeps running every schedule it
    owns exactly as before. This only refuses to start a manual send that cannot
    succeed, so the caller gets a precise reason instead of a failure logged minutes
    later.
    """
    if not (s.equipment_type or "").strip():
        raise HTTPException(400, detail="This schedule has no report type.")
    if not schedule_service.is_supported_type(s.equipment_type):
        raise HTTPException(400, detail=f"Unsupported report type: {s.equipment_type}.")
    if not schedule_service.is_plant_level(s.equipment_type) and not (s.equipment_id or "").strip():
        raise HTTPException(400, detail="This schedule has no equipment identifier.")
    if not (s.frequency or "").strip():
        raise HTTPException(400, detail="This schedule has no frequency configured.")

    recipients = schedule_service.recipients_for(s)
    if not recipients:
        raise HTTPException(400, detail="This schedule has no recipient e-mail address.")
    bad = schedule_service.invalid_recipients(recipients)
    if bad:
        raise HTTPException(400, detail="Invalid recipient e-mail address: " + ", ".join(bad))
    _require_email_config()


@router.post("/schedules/{schedule_id}/run")
def run_schedule(schedule_id: int, db: Session = Depends(get_db)) -> Dict:
    """
    Generate a SAVED schedule's report now and e-mail it to all of its recipients.

    Runs the same schedule_service.execute_schedule the background scheduler calls,
    so there is no second, manual-only report implementation.
    """
    s = schedule_service.get_schedule(db, schedule_id)
    if not s:
        raise HTTPException(404, detail="Schedule not found")
    _validate_runnable(s)

    logger.info("Manual run requested for schedule id=%s (%s/%s)",
                s.id, s.equipment_type, s.equipment_id)
    res = schedule_service.execute_schedule(db, s, kind="RUN")
    logger.info("Manual run finished for schedule id=%s -> %s", s.id, res.get("status"))
    return res


def _validate_adhoc(payload: SchedulePayload) -> List[str]:
    """
    Validate a ONE-OFF Generate & Send described by the Create-Schedule form, and
    return the resolved recipient list.

    Every field the report needs is checked here, not only in the browser, so the
    endpoint cannot be driven into generating a report from an incomplete request.
    Each message names the single field to fix, so the UI can show it inline.
    """
    _validate_type(payload.eq_type)
    if (not schedule_service.is_plant_level(payload.eq_type)
            and not (payload.eq_id or "").strip()):
        raise HTTPException(400, detail="Equipment identifier is required")
    if not (payload.interval or "").strip():
        raise HTTPException(400, detail="Time interval is required")
    if not (payload.agg or "").strip():
        raise HTTPException(400, detail="Aggregation is required")
    if not (payload.format or "").strip():
        raise HTTPException(400, detail="Report format is required")
    # No report date is required: Scheduled Reports no longer collects one. When the
    # payload carries none, the report builders generate for the current day / month /
    # year, which is what a schedule run is expected to cover.

    _validate_recipients(payload)                     # rejects malformed addresses
    recipients = schedule_service.normalize_recipients(payload.recipients)
    if not recipients:
        raise HTTPException(400, detail="At least one recipient e-mail address is required")

    _require_email_config()
    return recipients


@router.get("/data-window")
def report_data_window(eq_type: str, eq_id: str = "",
                       db: Session = Depends(get_db)) -> Dict:
    """
    The first and last timestamp this report type currently has data for.

    The form uses it to resolve "Till Now" for display, so the confirmation shows the
    range that will actually be generated. It is read live from the database on every
    call, so newly-ingested data moves the window with no code or config change.
    """
    probe = schedule_service.ReportSchedule(equipment_type=eq_type, equipment_id=eq_id)
    first, last = schedule_service.data_window(db, probe)
    return {
        "table":      schedule_service.source_table(probe),
        "first":      first.strftime("%Y-%m-%d") if first else "",
        "last":       last.strftime("%Y-%m-%d") if last else "",
        "first_label": first.strftime("%d/%m/%Y") if first else "",
        "last_label":  last.strftime("%d/%m/%Y") if last else "",
        "has_data":   bool(first and last),
    }


@router.get("/mail-quota")
def mail_quota_status(db: Session = Depends(get_db)) -> Dict:
    """Mails sent against the cap: {count, max, remaining, limit_reached, message}."""
    return mail_quota.status(db)


def _require_mail_quota(db: Session) -> None:
    """
    Refuse to even START a send once the cap is reached.

    This is a courtesy check so the user gets the message immediately instead of a job
    that fails a second later; the binding enforcement is the claim inside
    _generate_and_email, which also covers the background scheduler.
    """
    q = mail_quota.status(db)
    if q["limit_reached"]:
        raise HTTPException(409, detail=q["message"])


def _start_job(runner, *, label: str) -> Dict:
    """
    Run `runner(db, on_progress)` on a worker thread and return its job id at once.

    Shared by both Generate & Send entry points so a saved-schedule run and an ad-hoc
    send behave identically: same registry, same progress shape, same status endpoint,
    and neither holds the HTTP request open while a large export is built and mailed.
    """
    job_id = export_jobs.create_job()
    export_jobs.update(job_id, status="running", progress=1, message="Queued...")

    def _run():
        # A worker thread needs its OWN session: the request's session is already
        # closed by the time this runs.
        db = SessionLocal()
        try:
            def on_progress(pct, msg):
                export_jobs.update(job_id, status="running",
                                   progress=max(1, min(99, int(pct))), message=str(msg))
            res = runner(db, on_progress)
            if res.get("status") == "Success":
                export_jobs.update(job_id, status="done", progress=100,
                                   message="Report generated and sent successfully.",
                                   json_result=res, filename=res.get("report"))
            else:
                export_jobs.update(job_id, status="error", progress=100,
                                   message=res.get("error") or "Generate & Send failed.",
                                   error=res.get("error"), json_result=res)
        except Exception as exc:  # noqa: BLE001 - a worker thread must never die silently
            logger.error("%s job %s crashed: %s", label, job_id, exc, exc_info=True)
            export_jobs.set_error(job_id, f"{type(exc).__name__}: {exc}")
        finally:
            db.close()

    threading.Thread(target=_run, name=f"{label}-{job_id[:8]}", daemon=True).start()
    return {"job_id": job_id, "status": "running"}


@router.post("/schedules/{schedule_id}/run/start")
def run_schedule_start(schedule_id: int, db: Session = Depends(get_db)) -> Dict:
    """
    Generate & Send an EXISTING schedule, in the background.

    This is what updates that row's Generated Time: on success execute_schedule writes
    the timestamp to the schedule itself, so the row the user clicked goes from "-" to
    a real time. Nothing new is created. Validated before the job exists, so a
    misconfigured schedule fails fast with a 400.
    """
    s = schedule_service.get_schedule(db, schedule_id)
    if not s:
        raise HTTPException(404, detail="Schedule not found")
    _validate_runnable(s)
    _require_mail_quota(db)
    logger.info("Generate & Send (saved schedule id=%s) queued -> %s/%s",
                s.id, s.equipment_type, s.equipment_id)

    def _runner(worker_db, on_progress):
        # Re-read inside the worker's own session; the outer one is gone.
        row = schedule_service.get_schedule(worker_db, schedule_id)
        if not row:
            return {"status": "Failed", "error": "Schedule not found"}
        return schedule_service.execute_schedule(worker_db, row, kind="RUN",
                                                 progress=on_progress)

    return _start_job(_runner, label="run-schedule")


@router.post("/generate-send/start")
def generate_and_send_start(payload: SchedulePayload,
                            db: Session = Depends(get_db)) -> Dict:
    """
    Start a ONE-OFF Generate & Send in the background and return a job id at once.

    Used by the Create New Schedule form for a configuration that is not saved yet; on
    success the configuration is recorded so the report appears in the table with its
    Generated Time. Validation runs BEFORE the job is created.
    """
    _validate_adhoc(payload)
    _require_mail_quota(db)
    data = payload.as_dict()
    logger.info("Generate & Send (ad-hoc) queued -> %s/%s", payload.eq_type, payload.eq_id)
    return _start_job(
        lambda db, on_progress: schedule_service.generate_and_send_adhoc(
            db, data, progress=on_progress),
        label="gen-send")


@router.get("/generate-send/status/{job_id}")
def generate_and_send_status(job_id: str) -> Dict:
    """Progress snapshot for a Generate & Send job, plus its result once finished."""
    snap = export_jobs.status(job_id)
    if snap is None:
        raise HTTPException(404, detail="Job not found or expired")
    result = export_jobs.get_json_result(job_id) or {}
    out = {**snap, **{k: result.get(k) for k in
                      ("generated", "from", "to", "schedule_id", "recipient",
                       "report", "stage", "build_seconds", "email_seconds")}}
    if snap["status"] in ("done", "error"):
        export_jobs.cleanup(job_id)          # one-shot: the client has the outcome
    return out


@router.post("/generate-send")
def generate_and_send(payload: SchedulePayload, db: Session = Depends(get_db)) -> Dict:
    """
    ONE-OFF manual report: generate what the form describes and e-mail it now.

    This is NOT a scheduling action - no ReportSchedule is created, no ScheduleRun is
    written and no next_run is set, so nothing appears in the Report Schedules list.
    Generation and delivery go through the SAME service the scheduler uses.
    """
    _validate_adhoc(payload)
    logger.info("Ad-hoc Generate & Send requested -> %s/%s | fmt=%s",
                payload.eq_type, payload.eq_id, payload.format)
    res = schedule_service.generate_and_send_adhoc(db, payload.as_dict())
    logger.info("Ad-hoc Generate & Send finished -> %s", res.get("status"))
    return res


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
