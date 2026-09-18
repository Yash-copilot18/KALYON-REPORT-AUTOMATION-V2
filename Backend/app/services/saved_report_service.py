# app/services/saved_report_service.py
"""
Business logic for saved (a.k.a. Preconfigured) reports.

One place owns CRUD over `saved_reports` plus the JSON (de)serialization of the
multi-value fields (equipment identifiers + tags). The Reports page saves a
configuration here; the Preconfigured Reports page lists them and restores one
back into the Reports page to be re-run.

This module is intentionally standalone: it does NOT touch report generation,
SQL queries, or export logic — it only persists the configuration that those
existing paths already consume.
"""

import json
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.models.saved_reports import SavedReport

logger = logging.getLogger(__name__)

_FMT = "%d/%m/%Y %H:%M:%S"


# ── JSON helpers ─────────────────────────────────────────────────────────────
def _dump_list(value: Any) -> Optional[str]:
    """Serialize a list of strings to JSON text; None/empty → None."""
    if not value:
        return None
    if isinstance(value, str):          # already serialized / single value
        return json.dumps([value])
    return json.dumps([str(v) for v in value])


def _load_list(raw: Optional[str]) -> List[str]:
    """Parse JSON-text back to a list; tolerant of NULL / legacy plain strings."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [str(v) for v in data]
        return [str(data)]
    except (ValueError, TypeError):
        # Legacy / non-JSON value — treat as a single entry so nothing is lost.
        return [raw]


def _display_date(value: Optional[str]) -> str:
    """
    'YYYY-MM-DDTHH:MM' (datetime-local) → 'DD/MM/YYYY HH:MM' for the list view.
    Falls back to the raw stored string on anything unexpected.
    """
    if not value:
        return ""
    s = str(value).replace("T", " ")
    date_part, _, time_part = s.partition(" ")
    try:
        y, mo, d = date_part.split("-")
        return f"{d}/{mo}/{y}" + (f" {time_part[:5]}" if time_part else "")
    except ValueError:
        return str(value)


# ── Serialization for the API ────────────────────────────────────────────────
def to_row(s: SavedReport) -> Dict:
    """
    Shape a saved report for the frontend. Carries BOTH the raw values needed to
    restore the Reports page exactly AND pre-formatted fields for the list view.
    """
    eq_ids = _load_list(s.equipment_ids)
    tags   = _load_list(s.tags)
    return {
        "id":             s.id,
        "name":           s.name,
        "equipment_type": s.equipment_type,
        "equipment_ids":  eq_ids,
        "tags":           tags,
        "from_date":      s.from_date or "",
        "to_date":        s.to_date or "",
        "interval":       s.interval,
        "agg_function":   s.agg_function,
        "page_size":      s.page_size,
        # Convenience fields for the Preconfigured Reports table (no client math).
        "equipment_label": ", ".join(eq_ids) if eq_ids else "—",
        "equipment_count": len(eq_ids),
        "tag_count":       len(tags),
        "date_range":      f"{_display_date(s.from_date)} — {_display_date(s.to_date)}".strip(" —"),
        "created":         s.created_at.strftime(_FMT) if s.created_at else "",
    }


# ── CRUD ─────────────────────────────────────────────────────────────────────
def list_saved_reports(db: Session) -> List[SavedReport]:
    return db.query(SavedReport).order_by(SavedReport.id.desc()).all()


def get_saved_report(db: Session, report_id: int) -> Optional[SavedReport]:
    return db.query(SavedReport).filter(SavedReport.id == report_id).first()


def create_saved_report(db: Session, data: Dict) -> SavedReport:
    s = SavedReport(
        name=(data.get("name") or "").strip() or "Untitled Report",
        equipment_type=data.get("equipment_type") or "",
        equipment_ids=_dump_list(data.get("equipment_ids")),
        tags=_dump_list(data.get("tags")),
        from_date=(data.get("from_date") or None),
        to_date=(data.get("to_date") or None),
        interval=data.get("interval") or "hourly",
        agg_function=data.get("agg_function") or "avg",
        page_size=data.get("page_size"),
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    logger.info("Saved report created -> id=%s | %s | %s", s.id, s.name, s.equipment_type)
    return s


def update_saved_report(db: Session, report_id: int, data: Dict) -> Optional[SavedReport]:
    s = get_saved_report(db, report_id)
    if not s:
        return None
    if "name" in data:
        s.name = (data.get("name") or "").strip() or s.name
    if "equipment_type" in data:
        s.equipment_type = data.get("equipment_type") or s.equipment_type
    if "equipment_ids" in data:
        s.equipment_ids = _dump_list(data.get("equipment_ids"))
    if "tags" in data:
        s.tags = _dump_list(data.get("tags"))
    if "from_date" in data:
        s.from_date = data.get("from_date") or None
    if "to_date" in data:
        s.to_date = data.get("to_date") or None
    if "interval" in data:
        s.interval = data.get("interval") or s.interval
    if "agg_function" in data:
        s.agg_function = data.get("agg_function") or s.agg_function
    if "page_size" in data:
        s.page_size = data.get("page_size")
    db.commit()
    db.refresh(s)
    logger.info("Saved report updated -> id=%s", s.id)
    return s


def delete_saved_report(db: Session, report_id: int) -> bool:
    s = get_saved_report(db, report_id)
    if not s:
        return False
    db.delete(s)
    db.commit()
    logger.info("Saved report deleted -> id=%s", report_id)
    return True
