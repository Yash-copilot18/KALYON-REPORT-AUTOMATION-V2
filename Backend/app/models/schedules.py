# app/models/schedules.py
"""
Scheduled-report persistence.

Two tables, both created automatically at startup via Base.metadata.create_all
(no migration tool needed):

  · report_schedules — one row per user-created schedule (the full report config,
    frequency, status, and the computed Last Run / Next Run timestamps).
  · schedule_runs    — one row per execution (Run Now or automatic), so the UI's
    execution-history panel shows real runs instead of mock data.
"""

from sqlalchemy import Column, Integer, String, DateTime, Date, Text
from sqlalchemy.sql import func

from app.database.base import Base


class ReportSchedule(Base):
    """A saved report schedule. Maps to [Kalyan].[dbo].[report_schedules]."""
    __tablename__ = "report_schedules"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)

    # Report configuration — mirrors the Reports-module report request.
    equipment_type = Column(String(100), nullable=False)
    equipment_id   = Column(String(100), nullable=True)
    from_date      = Column(Date, nullable=True)
    to_date        = Column(Date, nullable=True)
    interval       = Column(String(30), nullable=False, default="hourly")
    agg_function   = Column(String(20), nullable=False, default="avg")
    report_format  = Column(String(20), nullable=False, default="Excel")   # CSV | Excel

    # Scheduling.
    frequency = Column(String(20), nullable=False, default="Daily")         # Daily|Weekly|Monthly
    status    = Column(String(20), nullable=False, default="Active")        # Active|Paused

    # Optional per-schedule recipients (comma-separated). Blank → backend .env
    # recipient, so the current single-recipient behaviour is preserved.
    recipients = Column(String(1000), nullable=True)

    last_run = Column(DateTime, nullable=True)
    next_run = Column(DateTime, nullable=True)

    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=True, onupdate=func.now())

    def __repr__(self) -> str:
        return (f"<ReportSchedule(id={self.id}, type='{self.equipment_type}', "
                f"eq='{self.equipment_id}', freq='{self.frequency}', status='{self.status}')>")


class ScheduleRun(Base):
    """One execution of a schedule. Maps to [Kalyan].[dbo].[schedule_runs]."""
    __tablename__ = "schedule_runs"

    id          = Column(Integer, primary_key=True, index=True, autoincrement=True)
    schedule_id = Column(Integer, nullable=False, index=True)

    execution_time = Column(DateTime, nullable=False, server_default=func.now())
    status         = Column(String(20), nullable=False, default="Failed")   # Success|Failed
    report         = Column(String(255), nullable=True)                     # filename
    data_source    = Column(String(30), nullable=True)                      # database|sample
    error          = Column(Text, nullable=True)
    file_size      = Column(Integer, nullable=True)                         # bytes
    duration_ms    = Column(Integer, nullable=True)

    def __repr__(self) -> str:
        return (f"<ScheduleRun(id={self.id}, schedule_id={self.schedule_id}, "
                f"status='{self.status}')>")
