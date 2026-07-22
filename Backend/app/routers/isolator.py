# app/routers/isolator.py
"""
Isolator data controller (thin) — dynamic, ID-scoped querying for dbo.T1_IS*.

Endpoints:
  GET  /isolator/{table}/meta     → available isolator IDs + parameter metadata
  POST /isolator/data             → paginated telemetry for the selected IDs only
  GET  /isolator/{table}/status   → live per-tracker status + KPIs (latest row)
  GET  /isolator/{table}/kpis     → aggregate KPIs only (latest row, cached)
  GET  /isolator/{table}/trend    → historical fleet trend for charts
  WS   /isolator/{table}/ws        → live snapshot push (on new TimeCol) + heartbeat
  POST /isolator/export/excel/async → one worksheet per selected tracker

Only TimeCol + the required parameter columns are queried (never SELECT *).
Business logic lives in isolator_service; this stays a thin controller.
Existing report/tracker/export APIs are untouched.
"""

import time
import asyncio
import logging
import threading
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, Path, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.database.session import get_db
from app.services import isolator_service, isolator_excel, export_jobs

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/isolator", tags=["Isolator"])

# WebSocket poll cadence — how often the server re-checks for a newer TimeCol.
_WS_POLL_SECONDS = 5


class IsolatorDataRequest(BaseModel):
    table:         str = Field(..., description="Isolator table, e.g. T1_IS4")
    isolator_ids:  List[int] = Field(..., min_length=1, description="Selected IDs, e.g. [1,2,3]")
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


class IsolatorExportRequest(BaseModel):
    table:         str = Field(..., description="Isolator table, e.g. T1_IS6")
    isolator_ids:  List[int] = Field(..., min_length=1, description="Selected tracker IDs")
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


@router.get("/{table}/meta", summary="Available isolator IDs + parameter metadata")
def isolator_meta(table: str = Path(..., description="e.g. T1_IS4"),
                  db: Session = Depends(get_db)):
    return isolator_service.get_metadata(db, table)


@router.post("/data", summary="Paginated telemetry for the selected isolator IDs (dynamic columns)")
def isolator_data(req: IsolatorDataRequest, db: Session = Depends(get_db)):
    return isolator_service.get_data(
        db, req.table, req.isolator_ids, _fmt(req.from_datetime), _fmt(req.to_datetime),
        req.interval, req.agg_function, req.page, req.page_size,
    )


# ── Live status / KPIs / trend ───────────────────────────────────────────────
@router.get("/{table}/status", summary="Live per-tracker status + KPIs (latest TimeCol row)")
def isolator_status(table: str = Path(..., description="e.g. T1_IS8"),
                    db: Session = Depends(get_db)):
    return isolator_service.get_snapshot(db, table)


@router.get("/{table}/kpis", summary="Aggregate tracker KPIs (latest TimeCol row, cached)")
def isolator_kpis(table: str = Path(..., description="e.g. T1_IS8"),
                  db: Session = Depends(get_db)):
    return isolator_service.get_kpis(db, table)


@router.get("/{table}/trend", summary="Historical fleet trend (alarms / battery / tracking error)")
def isolator_trend(
    table: str = Path(..., description="e.g. T1_IS8"),
    from_datetime: Optional[datetime] = Query(None),
    to_datetime:   Optional[datetime] = Query(None),
    interval:      str = Query("hourly"),
    db: Session = Depends(get_db),
):
    # Default to the last 3 days of available data when no range is supplied.
    if to_datetime is None or from_datetime is None:
        latest = isolator_service.get_snapshot(db, table).get("timestamp")
        end = datetime.fromisoformat(latest) if latest else datetime.utcnow()
        to_datetime = to_datetime or end
        from_datetime = from_datetime or (end - timedelta(days=3))
    return isolator_service.get_trend(
        db, table, _fmt(from_datetime), _fmt(to_datetime), interval,
    )


@router.websocket("/{table}/ws")
async def isolator_ws(websocket: WebSocket, table: str):
    """
    Push a fresh snapshot on connect, then re-check every few seconds and push a
    new snapshot only when TimeCol advances (heartbeat otherwise). The client
    handles reconnection. DB access runs in a worker thread so the event loop
    is never blocked.
    """
    await websocket.accept()
    last_ts = None
    try:
        while True:
            try:
                snap = await run_in_threadpool(isolator_service.snapshot_standalone, table)
            except Exception as e:  # noqa: BLE001 — report and close cleanly
                await websocket.send_json({"type": "error", "message": str(e)})
                break
            ts = snap.get("timestamp")
            if ts != last_ts:
                await websocket.send_json({"type": "snapshot", **snap})
                last_ts = ts
            else:
                await websocket.send_json({"type": "heartbeat", "timestamp": ts})
            await asyncio.sleep(_WS_POLL_SECONDS)
    except WebSocketDisconnect:
        return
    except Exception as e:  # noqa: BLE001
        logger.warning("Isolator WS (%s) closed: %s", table, e)
        try:
            await websocket.close()
        except Exception:
            pass


@router.post("/export/excel/async", summary="Background Excel export — one worksheet per selected tracker")
def isolator_export_excel_async(req: IsolatorExportRequest):
    """
    Start the export in the background; watch progress over the shared SSE
    endpoint (/reports-v2/export/progress/{job_id}) and download via
    /reports-v2/export/download/{job_id}. One workbook, one worksheet per tracker.
    """
    job_id = export_jobs.create_job()
    table, ids = req.table, list(req.isolator_ids)
    from_dt, to_dt = _fmt(req.from_datetime), _fmt(req.to_datetime)
    interval, agg = req.interval, req.agg_function

    def run():
        try:
            def prog(pct, msg):
                export_jobs.update(job_id, status="running", progress=pct, message=msg)
            data, valid_ids = isolator_excel.build_workbook(
                table, ids, from_dt, to_dt, interval, agg,
                ts_cols=("TimeCol",), sheet_title="Tracker", progress=prog,
            )
            total = len(valid_ids)
            filename = (f"{table}_IS{valid_ids[0]:02d}_Report_{time.strftime('%d-%m-%Y')}.xlsx"
                        if total == 1 else
                        f"{table}_{total}_Trackers_Report_{time.strftime('%d-%m-%Y')}.xlsx")
            export_jobs.set_result(job_id, data, filename, isolator_excel.XLSX_MIME)
        except Exception as e:  # noqa: BLE001 — surface failure to the client
            logger.error("Isolator export job %s failed: %s", job_id, e, exc_info=True)
            export_jobs.set_error(job_id, f"{type(e).__name__}: {e}")

    threading.Thread(target=run, name=f"isolator-export-{job_id}", daemon=True).start()
    logger.info("Isolator export job %s created -> table=%s trackers=%d", job_id, table, len(ids))
    return {"job_id": job_id, "count": len(ids)}
