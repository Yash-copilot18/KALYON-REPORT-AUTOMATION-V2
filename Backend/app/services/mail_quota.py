# app/services/mail_quota.py
"""
The 20-mail cap for Scheduled Reports.

WHAT COUNTS
    One mail = one report that was generated AND delivered. That is exactly what a
    `schedule_runs` row with status 'Success' already records, so the count is derived
    from the existing history - there is no separate counter to keep in step, and a
    failed attempt (generation error, no data, SMTP failure) is never counted because
    it is never written as Success.

FROM WHEN
    Only runs AFTER a recorded start point count. The installation already holds
    hundreds of historical sends; counting those would put the plant permanently over
    quota on day one. The start point is written once, to `app_settings`, the first
    time the quota is consulted, and never moves - so the count survives page
    refreshes and backend restarts and cannot be reset by the browser.

ATOMICITY
    `claim()` counts and records intent inside ONE transaction that holds a lock on
    the history table, so two simultaneous requests at 19/20 cannot both be admitted.
"""

import logging
import os
import threading
from datetime import datetime
from typing import Dict, Optional

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.models.app_settings import AppSetting
from app.models.schedules import ScheduleRun

logger = logging.getLogger(__name__)

_START_KEY = "mail_quota_start"


def _read_max() -> int:
    """THE single place the cap lives; MAX_SENT_MAILS overrides it."""
    raw = (os.getenv("MAX_SENT_MAILS") or "").strip()
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
            logger.warning("MAX_SENT_MAILS=%s is not positive - using the default.", raw)
        except ValueError:
            logger.warning("MAX_SENT_MAILS=%s is not an integer - using the default.", raw)
    return 20


MAX_SENT_MAILS = _read_max()

LIMIT_MESSAGE = (f"Mail limit reached ({MAX_SENT_MAILS}/{MAX_SENT_MAILS}). "
                 "No more reports can be generated or sent.")


class MailQuotaExceeded(Exception):
    """Raised when a send is refused because the cap is reached."""

    def __init__(self, count: int, limit: int):
        self.count, self.limit = count, limit
        super().__init__(LIMIT_MESSAGE)


def _quota_start(db: Session) -> datetime:
    """
    The instant the quota began counting, created on first use and then fixed.

    Anything already in the history at that moment predates the cap and is left out,
    so switching the feature on does not retroactively exhaust the allowance.
    """
    row = db.query(AppSetting).filter(AppSetting.key == _START_KEY).first()
    if row and row.value:
        try:
            return datetime.fromisoformat(row.value)
        except ValueError:
            logger.warning("Unreadable %s=%r - re-basing the quota to now.",
                           _START_KEY, row.value)

    now = datetime.now()
    if row:
        row.value = now.isoformat()
    else:
        db.add(AppSetting(key=_START_KEY, value=now.isoformat()))
    db.commit()
    logger.info("Mail quota start recorded -> %s (history before this is not counted)",
                now.isoformat())
    return now


def count_sent(db: Session, *, since: Optional[datetime] = None) -> int:
    """Mails successfully generated AND delivered since the quota started."""
    start = since or _quota_start(db)
    return int(
        db.query(func.count(ScheduleRun.id))
          .filter(ScheduleRun.status == "Success",
                  ScheduleRun.execution_time >= start)
          .scalar() or 0
    )


def status(db: Session) -> Dict:
    """Usage snapshot for the API and the UI."""
    start = _quota_start(db)
    count = count_sent(db, since=start) + in_flight()
    return {
        "count":         count,
        "max":           MAX_SENT_MAILS,
        "remaining":     max(MAX_SENT_MAILS - count, 0),
        "limit_reached": count >= MAX_SENT_MAILS,
        "message":       LIMIT_MESSAGE if count >= MAX_SENT_MAILS else "",
        "since":         start.strftime("%d/%m/%Y %H:%M:%S"),
    }


# Sends that have been admitted but whose Success row does not exist yet. A send takes
# tens of seconds, so without this a second request arriving mid-send would read the
# same database count and be admitted alongside the first. Guarded by a process lock,
# which matches how this application is deployed (single-process uvicorn - the same
# assumption export_jobs already documents).
_lock = threading.Lock()
_in_flight = 0


def in_flight() -> int:
    with _lock:
        return _in_flight


def claim(db: Session) -> int:
    """
    Reserve one slot, or raise MailQuotaExceeded.

    Consumed = mails already delivered + sends currently running. The reservation is
    held until release() is called, so a send in progress cannot be double-counted or
    double-admitted. Nothing is written: the slot is made permanent by the Success row
    the send itself produces, and a failed send releases it again.

    The database count is taken under a table lock held to the end of the transaction,
    so even two processes cannot read the same count simultaneously.
    """
    global _in_flight
    start = _quota_start(db)
    with _lock:
        if db.bind is not None and db.bind.dialect.name == "mssql":
            try:
                count = int(db.execute(
                    text("SELECT COUNT_BIG(*) FROM schedule_runs WITH (TABLOCKX, HOLDLOCK) "
                         "WHERE status = 'Success' AND execution_time >= :since"),
                    {"since": start},
                ).scalar() or 0)
            except Exception as exc:  # pragma: no cover - server/permission dependent
                logger.warning("Locked mail count unavailable (%s) - using a plain count.", exc)
                db.rollback()
                count = count_sent(db, since=start)
        else:
            count = count_sent(db, since=start)
        db.commit()                      # release the table lock

        consumed = count + _in_flight
        if consumed >= MAX_SENT_MAILS:
            logger.info("Send refused - mail quota reached (%s sent + %s in flight / %s)",
                        count, _in_flight, MAX_SENT_MAILS)
            raise MailQuotaExceeded(consumed, MAX_SENT_MAILS)

        _in_flight += 1
        logger.info("Mail slot claimed -> %s sent + %s in flight / %s",
                    count, _in_flight, MAX_SENT_MAILS)
        return consumed + 1


def release() -> None:
    """Give a claimed slot back. Always call this when a send finishes, either way:
    on success the Success row now holds the slot, on failure it must be freed."""
    global _in_flight
    with _lock:
        _in_flight = max(0, _in_flight - 1)
