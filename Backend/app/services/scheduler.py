# app/services/scheduler.py
"""
Background scheduler for automatic report delivery.

A single APScheduler BackgroundScheduler runs ONE interval job that, every 60
seconds, executes every Active schedule whose next_run has passed
(`schedule_service.run_due_schedules`). A single polling job — rather than one
cron job per schedule — means create/edit/delete/pause/resume need no job
reprogramming: the next tick simply reads the current table.

Duplicate-instance prevention (e.g. uvicorn --reload, or two workers): before
starting, we try to bind a socket to a fixed loopback port. Exactly one process
in the machine can hold it; any other process fails the bind and skips starting
its scheduler. The socket is released automatically when the owning process
exits, so there is no stale lock to clean up after a crash.
"""

import socket
import logging

from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger(__name__)

# Loopback port used purely as a cross-process mutex for "who owns the scheduler".
_LOCK_HOST = "127.0.0.1"
_LOCK_PORT = 5599

_TICK_SECONDS = 60

# Module-level singletons so a second start() in the same process is a no-op.
_scheduler: BackgroundScheduler | None = None
_lock_socket: socket.socket | None = None


def _acquire_singleton_lock() -> bool:
    """Bind the mutex port. True if THIS process now owns the scheduler."""
    global _lock_socket
    if _lock_socket is not None:
        return True
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((_LOCK_HOST, _LOCK_PORT))
        # Keep it bound (but not listening) for the process lifetime.
        _lock_socket = sock
        return True
    except OSError:
        sock.close()
        return False


def _tick():
    """One scheduler tick — run everything that is due."""
    try:
        from app.services.schedule_service import run_due_schedules
        run_due_schedules()
    except Exception as e:  # noqa: BLE001 — a tick must never kill the scheduler
        logger.error("Scheduler tick error: %s", e, exc_info=True)


def start_scheduler() -> bool:
    """
    Start the background scheduler if this process owns the singleton lock and one
    isn't already running here. Returns True when a scheduler is (now) running in
    this process. Safe to call more than once.
    """
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        return True

    if not _acquire_singleton_lock():
        logger.info("Scheduler NOT started in this process — another instance owns "
                    "the lock (port %d). Duplicate prevented.", _LOCK_PORT)
        return False

    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(
        _tick,
        trigger="interval",
        seconds=_TICK_SECONDS,
        id="scheduled_reports_tick",
        replace_existing=True,
        coalesce=True,          # collapse missed ticks into one
        max_instances=1,        # never overlap ticks
        misfire_grace_time=300,
    )
    _scheduler.start()
    logger.info("Scheduled-reports scheduler started (tick every %ds, lock port %d).",
                _TICK_SECONDS, _LOCK_PORT)
    return True


def stop_scheduler() -> None:
    """Shut the scheduler down and release the singleton lock."""
    global _scheduler, _lock_socket
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:  # noqa: BLE001
            pass
        _scheduler = None
        logger.info("Scheduled-reports scheduler stopped.")
    if _lock_socket is not None:
        try:
            _lock_socket.close()
        except Exception:  # noqa: BLE001
            pass
        _lock_socket = None
