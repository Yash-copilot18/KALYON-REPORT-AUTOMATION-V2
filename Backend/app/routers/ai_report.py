"""
AI assurance report API.

Endpoints:
  GET  /ai-report/preflight              what the archive supports, before running
  POST /ai-report/start                  begin a run in the background -> job id
  GET  /ai-report/status/{job_id}        progress; the package once complete

A forensic pass reads 1-minute telemetry across the whole fleet, so it runs on a
worker thread through the SAME export_jobs registry the Excel exports and Generate &
Send already use - the HTTP request never waits for it.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.ai import orchestrator
from app.database.session import SessionLocal, get_db
from app.services import export_jobs

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ai-report", tags=["AI Report"])


class AIReportRequest(BaseModel):
    """Window to analyse. Both ends are clamped to the archive by the orchestrator."""
    from_datetime: Optional[str] = Field(default=None, description="ISO 'YYYY-MM-DD HH:MM'")
    to_datetime:   Optional[str] = None
    modules: Optional[List[str]] = None      # default: every implemented module


@router.get("/preflight")
def ai_report_preflight(db: Session = Depends(get_db)) -> Dict[str, Any]:
    """Coverage per source group, plus the analyses that are blocked and why."""
    return orchestrator.preflight(db)


def _normalise(value: Optional[str], fallback: str) -> str:
    if not value:
        return fallback
    return str(value).replace("T", " ").strip()


@router.post("/start")
def ai_report_start(payload: AIReportRequest,
                    db: Session = Depends(get_db)) -> Dict[str, Any]:
    """
    Queue a report. Returns a job id immediately; poll /status/{job_id}.

    When no window is given the engine defaults to the last 30 days the archive
    actually holds, rather than "the last 30 days" of wall-clock time, which for a
    historical archive would be empty.
    """
    pre = orchestrator.preflight(db)
    latest, earliest = pre.get("plant_latest"), pre.get("plant_earliest")
    if not latest or not earliest:
        raise HTTPException(400, detail="No telemetry is available to analyse.")

    default_end = latest
    try:
        default_start = (datetime.fromisoformat(latest) - timedelta(days=30)).isoformat(" ")
    except ValueError:
        default_start = earliest
    default_start = max(default_start, earliest)

    start = _normalise(payload.from_datetime, default_start)
    end = _normalise(payload.to_datetime, default_end)
    if start >= end:
        raise HTTPException(400, detail="The start of the window must precede its end.")

    modules = payload.modules or ["data_integrity"]
    job_id = export_jobs.create_job()
    export_jobs.update(job_id, status="running", progress=1, message="Queued…")

    def _run() -> None:
        # The request's session closes when this endpoint returns, so the worker
        # opens its own.
        worker_db = SessionLocal()
        try:
            def on_progress(pct: int, msg: str) -> None:
                export_jobs.update(job_id, status="running",
                                   progress=max(1, min(99, int(pct))), message=str(msg))

            package = orchestrator.run_report(worker_db, start, end,
                                              progress=on_progress, modules=modules)
            export_jobs.update(job_id, status="done", progress=100,
                               message="Report ready.", json_result=package.to_dict())
        except Exception as exc:                      # noqa: BLE001 - never die silently
            logger.error("AI report job %s failed: %s", job_id, exc, exc_info=True)
            export_jobs.set_error(job_id, f"{type(exc).__name__}: {exc}")
        finally:
            worker_db.close()

    threading.Thread(target=_run, name=f"ai-report-{job_id[:8]}", daemon=True).start()
    logger.info("AI report job %s queued | window %s .. %s | modules=%s",
                job_id, start, end, modules)
    return {"job_id": job_id, "status": "running",
            "window": {"from": start, "to": end}, "modules": modules}


@router.get("/status/{job_id}")
def ai_report_status(job_id: str) -> Dict[str, Any]:
    """Progress while running; the full evidence package once complete."""
    snap = export_jobs.status(job_id)
    if snap is None:
        raise HTTPException(404, detail="Job not found or expired")
    out: Dict[str, Any] = dict(snap)
    if snap["status"] == "done":
        out["report"] = export_jobs.get_json_result(job_id)
    return out
