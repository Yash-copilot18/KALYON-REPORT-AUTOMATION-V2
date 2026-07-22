# app/routers/tracker.py
"""
Tracker module controller (thin) — dbo.T1_IS2 (tracker IDs 1–125).

Endpoints:
  GET  /tracker/ids                 → available tracker IDs + parameter metadata
  POST /tracker/data                → paginated preview for the selected trackers
  POST /tracker/export/excel/async  → background Excel export (one sheet/tracker)

Progress and download reuse the shared job infrastructure:
  GET  /reports-v2/export/progress/{job_id}   (SSE)
  GET  /reports-v2/export/download/{job_id}
"""

import logging
import threading
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session
from typing import List, Optional

from app.database.session import get_db
from app.services import tracker_service, export_jobs

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tracker", tags=["Tracker"])


# ── Request models ───────────────────────────────────────────────────────────
class TrackerDataRequest(BaseModel):
    tracker_ids:   List[int] = Field(..., min_length=1)
    from_datetime: datetime
    to_datetime:   datetime
    interval:      str = "raw"
    agg_function:  str = "avg"
    page:          int = Field(default=1, ge=1)
    page_size:     int = Field(default=100, ge=1, le=10000)

    @field_validator("to_datetime")
    @classmethod
    def _order(cls, v, info):
        if "from_datetime" in info.data and v <= info.data["from_datetime"]:
            raise ValueError("to_datetime must be after from_datetime")
        return v


class TrackerExportRequest(BaseModel):
    tracker_ids:   List[int] = Field(..., min_length=1)
    from_datetime: datetime
    to_datetime:   datetime
    interval:      str = "raw"
    agg_function:  str = "avg"

    @field_validator("to_datetime")
    @classmethod
    def _order(cls, v, info):
        if "from_datetime" in info.data and v <= info.data["from_datetime"]:
            raise ValueError("to_datetime must be after from_datetime")
        return v


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ── Endpoints ────────────────────────────────────────────────────────────────
@router.get("/ids", summary="Available tracker IDs + parameter metadata")
def get_tracker_ids(db: Session = Depends(get_db)):
    return tracker_service.get_available_trackers(db)


@router.post("/data", summary="Paginated preview for selected trackers")
def get_tracker_data(req: TrackerDataRequest, db: Session = Depends(get_db)):
    return tracker_service.get_tracker_data(
        db, req.tracker_ids, _fmt(req.from_datetime), _fmt(req.to_datetime),
        req.interval, req.agg_function, req.page, req.page_size,
    )


@router.post("/export/excel/async", summary="Background Excel export (one sheet per tracker)")
def export_tracker_excel_async(req: TrackerExportRequest):
    """Start the export in the background; watch progress over the shared SSE
    endpoint and download via the shared download endpoint."""
    job_id = export_jobs.create_job()
    from_dt, to_dt = _fmt(req.from_datetime), _fmt(req.to_datetime)
    ids, interval, agg = list(req.tracker_ids), req.interval, req.agg_function

    def run():
        try:
            def prog(pct, msg):
                export_jobs.update(job_id, status="running", progress=pct, message=msg)
            data, filename = tracker_service.build_tracker_workbook(
                ids, from_dt, to_dt, interval, agg, prog)
            export_jobs.set_result(job_id, data, filename, tracker_service.XLSX_MIME)
        except Exception as e:  # noqa: BLE001
            logger.error("Tracker export job %s failed: %s", job_id, e, exc_info=True)
            export_jobs.set_error(job_id, f"{type(e).__name__}: {e}")

    threading.Thread(target=run, name=f"tracker-export-{job_id}", daemon=True).start()
    logger.info("Tracker export job %s created -> %d trackers", job_id, len(ids))
    return {"job_id": job_id, "count": len(ids)}
