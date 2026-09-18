# app/models/saved_reports.py
"""
Saved-report (a.k.a. "Preconfigured Report") persistence.

A saved report is the COMPLETE configuration a user builds on the Reports page —
equipment type, the selected equipment identifiers, the selected tags, the
date range, the time interval and the aggregation — captured with one click of
the new Save button so it can be re-opened and re-run later.

It is persisted in `saved_reports` (created automatically at startup via
Base.metadata.create_all, exactly like `report_schedules` — no migration tool
needed), so a saved report survives a page refresh / server restart.

The multi-value fields (equipment identifiers + tags) are stored as JSON text so
one row holds an arbitrary-length selection without an extra child table; the
service layer owns the (de)serialization.
"""

from sqlalchemy import Column, Integer, String, DateTime, Text
from sqlalchemy.sql import func

from app.database.base import Base


class SavedReport(Base):
    """A saved Reports-page configuration. Maps to [Kalyan].[dbo].[saved_reports]."""
    __tablename__ = "saved_reports"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)

    # User-supplied name shown in the Preconfigured Reports list.
    name = Column(String(200), nullable=False)

    # Report configuration — mirrors the Reports-module report request.
    equipment_type = Column(String(100), nullable=False)
    # JSON arrays (Text so the selection is not length-limited).
    equipment_ids  = Column(Text, nullable=True)          # e.g. ["INVERTER_01", ...]
    tags           = Column(Text, nullable=True)          # e.g. ["ACTIVE_POWER", ...]

    # Stored as the raw <input type="datetime-local"> strings (e.g. "2024-02-08T00:00")
    # so the exact from/to — including time-of-day — round-trips back into the page.
    from_date = Column(String(40), nullable=True)
    to_date   = Column(String(40), nullable=True)

    interval     = Column(String(30), nullable=False, default="hourly")
    agg_function = Column(String(20), nullable=False, default="avg")
    page_size    = Column(Integer, nullable=True)

    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=True, onupdate=func.now())

    def __repr__(self) -> str:
        return (f"<SavedReport(id={self.id}, name='{self.name}', "
                f"type='{self.equipment_type}')>")
