# app/routers/saved_reports.py
"""
Saved-report (Preconfigured Report) API — database-backed.

A saved report is the full Reports-page configuration (equipment type, selected
equipment, selected tags, date range, interval, aggregation). It is persisted in
`saved_reports` so it survives a refresh / restart and can be re-opened and
re-run from the Preconfigured Reports page.

This router only stores/serves configuration — it never runs a query, changes a
calculation, or exports a file. Generating a saved report is done by restoring it
into the existing Reports page and using its existing Load Data / Export paths.

Endpoints:
  GET    /saved-reports          list all saved reports (newest first)
  POST   /saved-reports          create a saved report
  GET    /saved-reports/{id}     fetch one
  PUT    /saved-reports/{id}     update one
  DELETE /saved-reports/{id}     delete one
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.services import saved_report_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/saved-reports", tags=["Saved Reports"])


# ── Request model ────────────────────────────────────────────────────────────
class SavedReportPayload(BaseModel):
    """Create/edit body — mirrors the Reports-page report configuration."""
    name:           str = Field(default="")
    equipment_type: str = Field(default="")
    equipment_ids:  List[str] = Field(default_factory=list)
    tags:           List[str] = Field(default_factory=list)
    from_date:      Optional[str] = None
    to_date:        Optional[str] = None
    interval:       str = "hourly"
    agg_function:   str = "avg"
    page_size:      Optional[int] = None

    def as_dict(self) -> Dict[str, Any]:
        return self.model_dump()


def _validate(payload: SavedReportPayload) -> None:
    """The config must be complete enough to restore and re-run (client req 9)."""
    if not payload.name.strip():
        raise HTTPException(400, detail="Report name is required")
    if not payload.equipment_type.strip():
        raise HTTPException(400, detail="Equipment type is required")
    if not payload.equipment_ids:
        raise HTTPException(400, detail="At least one equipment identifier is required")
    if not payload.tags:
        raise HTTPException(400, detail="At least one tag is required")


# ── CRUD ─────────────────────────────────────────────────────────────────────
@router.get("")
def list_saved_reports(db: Session = Depends(get_db)) -> List[Dict]:
    return [saved_report_service.to_row(s)
            for s in saved_report_service.list_saved_reports(db)]


@router.post("")
def create_saved_report(payload: SavedReportPayload,
                        db: Session = Depends(get_db)) -> Dict:
    _validate(payload)
    s = saved_report_service.create_saved_report(db, payload.as_dict())
    return saved_report_service.to_row(s)


@router.get("/{report_id}")
def get_saved_report(report_id: int, db: Session = Depends(get_db)) -> Dict:
    s = saved_report_service.get_saved_report(db, report_id)
    if not s:
        raise HTTPException(404, detail="Saved report not found")
    return saved_report_service.to_row(s)


@router.put("/{report_id}")
def update_saved_report(report_id: int, payload: SavedReportPayload,
                        db: Session = Depends(get_db)) -> Dict:
    _validate(payload)
    s = saved_report_service.update_saved_report(db, report_id, payload.as_dict())
    if not s:
        raise HTTPException(404, detail="Saved report not found")
    return saved_report_service.to_row(s)


@router.delete("/{report_id}")
def delete_saved_report(report_id: int, db: Session = Depends(get_db)) -> Dict:
    if not saved_report_service.delete_saved_report(db, report_id):
        raise HTTPException(404, detail="Saved report not found")
    return {"deleted": report_id}
