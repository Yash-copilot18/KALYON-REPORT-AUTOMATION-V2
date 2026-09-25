# app/models/app_settings.py
"""
Tiny key/value store for a handful of persistent application markers.

It exists because some values must outlive a restart but belong to no report and to
no schedule - currently just the point in time the mail quota starts counting from.
It is deliberately NOT a counter: the number of mails sent is always derived from
`schedule_runs`, the real record of what was generated and delivered. Storing a
second copy of that number would be a duplicate that could drift out of step.

Created automatically at startup via Base.metadata.create_all, exactly like
`report_schedules` and `saved_reports` - no migration tool needed.
"""

from sqlalchemy import Column, String, DateTime
from sqlalchemy.sql import func

from app.database.base import Base


class AppSetting(Base):
    """One persistent setting. Maps to [Kalyan].[dbo].[app_settings]."""
    __tablename__ = "app_settings"

    key        = Column(String(100), primary_key=True)
    value      = Column(String(400), nullable=True)
    updated_at = Column(DateTime, nullable=False,
                        server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<AppSetting({self.key}={self.value!r})>"
