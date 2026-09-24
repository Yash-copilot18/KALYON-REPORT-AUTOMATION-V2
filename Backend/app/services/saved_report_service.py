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
import os
from typing import Any, Dict, List, Optional

from sqlalchemy import func as sa_func, text
from sqlalchemy.orm import Session

from app.models.saved_reports import SavedReport

logger = logging.getLogger(__name__)

_FMT = "%d/%m/%Y %H:%M:%S"


# ── Saved-template capacity ──────────────────────────────────────────────────
# THE single place the maximum lives. Change this one value (or set the
# MAX_SAVED_TEMPLATES environment variable) to raise/lower the cap — no other
# logic, message or UI text has to be touched.
def _read_max() -> int:
    raw = (os.getenv("MAX_SAVED_TEMPLATES") or "").strip()
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
            logger.warning("MAX_SAVED_TEMPLATES=%s is not positive — using default.", raw)
        except ValueError:
            logger.warning("MAX_SAVED_TEMPLATES=%s is not an integer — using default.", raw)
    return 20


MAX_SAVED_TEMPLATES = _read_max()

# The one wording shown by the API and the UI when the cap is hit.
LIMIT_MESSAGE = (
    f"Maximum limit of {MAX_SAVED_TEMPLATES} saved templates reached. "
    "Please delete an existing template before creating a new one."
)


class SavedReportLimitError(Exception):
    """Raised by create_saved_report when the saved-template cap is reached.

    A domain error rather than an HTTPException so the service stays free of web
    concerns; the router maps it to 409 Conflict.
    """

    def __init__(self, count: int, limit: int):
        self.count = count
        self.limit = limit
        super().__init__(LIMIT_MESSAGE)


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


def count_saved_reports(db: Session) -> int:
    """How many saved templates exist right now — always read from the database."""
    return int(db.query(sa_func.count(SavedReport.id)).scalar() or 0)


def _count_for_insert(db: Session) -> int:
    """
    Count saved templates while holding a lock that blocks a concurrent insert
    until this transaction ends, so two simultaneous creates can never both see
    19 and both succeed.

    On SQL Server the table hints take an exclusive table lock that is HELD to
    the end of the transaction (the create commits immediately after, so it is
    held for microseconds). Any other backend — or a server that rejects the
    hint — falls back to a plain count, which still enforces the cap for the
    normal single-request case.
    """
    if db.bind is not None and db.bind.dialect.name == "mssql":
        try:
            return int(db.execute(
                text("SELECT COUNT_BIG(*) FROM saved_reports WITH (TABLOCKX, HOLDLOCK)")
            ).scalar() or 0)
        except Exception as exc:                       # pragma: no cover - server/permission dependent
            logger.warning("Locked count unavailable (%s) — falling back to a plain count.", exc)
            db.rollback()
    return count_saved_reports(db)


def capacity(db: Session) -> Dict[str, Any]:
    """Current usage of the saved-template allowance, for the API and the UI."""
    count = count_saved_reports(db)
    return {
        "count":         count,
        "max":           MAX_SAVED_TEMPLATES,
        "remaining":     max(MAX_SAVED_TEMPLATES - count, 0),
        "limit_reached": count >= MAX_SAVED_TEMPLATES,
        "message":       LIMIT_MESSAGE if count >= MAX_SAVED_TEMPLATES else "",
    }


def create_saved_report(db: Session, data: Dict) -> SavedReport:
    # Enforce the cap here, in the ONE function that inserts a row, so every
    # caller (API, future importer, script) is covered. Updates/renames/deletes
    # go through their own functions and are deliberately unaffected.
    existing = _count_for_insert(db)
    if existing >= MAX_SAVED_TEMPLATES:
        logger.info("Saved report creation refused -> at limit (%s/%s)",
                    existing, MAX_SAVED_TEMPLATES)
        raise SavedReportLimitError(existing, MAX_SAVED_TEMPLATES)

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
