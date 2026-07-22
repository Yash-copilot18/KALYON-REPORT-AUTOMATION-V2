# app/services/export_jobs.py
"""
In-memory async export-job registry.

A batch Excel export is generated in a background thread; the frontend polls
progress over SSE and downloads the finished file by job id. Suitable for a
single-process uvicorn deployment (the intended setup here).
"""

import time
import uuid
import threading
import logging

logger = logging.getLogger(__name__)

_jobs: dict[str, dict] = {}
_lock = threading.Lock()

# Finished jobs are dropped after this many seconds to bound memory.
_TTL_SECONDS = 1800


def create_job() -> str:
    jid = uuid.uuid4().hex
    with _lock:
        _jobs[jid] = {
            "status":   "pending",   # pending | running | done | error
            "progress": 0,
            "message":  "Queued…",
            "filename": None,
            "error":    None,
            "data":     None,        # bytes of the finished workbook/zip
            "json_result": None,     # dict result for JSON (batch data) jobs
            "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "created":  time.time(),
            "updated":  time.time(),
        }
    _purge_expired()
    return jid


def update(jid: str, **fields) -> None:
    with _lock:
        job = _jobs.get(jid)
        if job:
            job.update(fields)
            job["updated"] = time.time()


def set_result(jid: str, data: bytes, filename: str,
               content_type: str = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet") -> None:
    update(jid, status="done", progress=100, message="Export completed.",
           data=data, filename=filename, content_type=content_type)


def set_json_result(jid: str, obj: dict, message: str = "Load completed.") -> None:
    """Store a JSON/dict result (batch data-load jobs) and mark the job done."""
    update(jid, status="done", progress=100, message=message, json_result=obj)


def get_json_result(jid: str):
    with _lock:
        job = _jobs.get(jid)
        if not job:
            return None
        return job.get("json_result")


def set_request(jid: str, req, equipment_ids: list) -> None:
    """Stash the export request so a later streaming GET can generate it live."""
    update(jid, request=(req, list(equipment_ids)))


def get_request(jid: str):
    """Return the stashed (req, equipment_ids) for a prepared streaming job, or None."""
    with _lock:
        job = _jobs.get(jid)
        if not job:
            return None
        return job.get("request")


def set_error(jid: str, message: str) -> None:
    update(jid, status="error", message=message, error=message)


def status(jid: str) -> dict | None:
    """Progress snapshot WITHOUT the file bytes (safe to serialize)."""
    with _lock:
        job = _jobs.get(jid)
        if not job:
            return None
        return {k: job[k] for k in ("status", "progress", "message", "filename", "error")}


def get_result(jid: str):
    with _lock:
        job = _jobs.get(jid)
        if not job or not job.get("data"):
            return None, None, None
        return job["data"], job["filename"], job["content_type"]


def cleanup(jid: str) -> None:
    with _lock:
        _jobs.pop(jid, None)


def _purge_expired() -> None:
    now = time.time()
    with _lock:
        stale = [j for j, v in _jobs.items()
                 if now - v["updated"] > _TTL_SECONDS]
        for j in stale:
            _jobs.pop(j, None)
    if stale:
        logger.info("Purged %d expired export job(s)", len(stale))
