"""
Device-wise forecasting and combiner analysis endpoints.

Separate from the plant forecast router on purpose: the plant endpoints keep
their shapes and behaviour exactly as they were, and nothing here can affect
them.

    GET  /api/v1/devices/inventory        what the database holds, fast
    POST /api/v1/devices/forecast         run every inverter (button-driven)
    GET  /api/v1/devices/forecast         the last run's results
    GET  /api/v1/devices/{id}/history     one device's series, day/week/month
    GET  /api/v1/devices/combiners        peer ranking within each inverter
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.forecast import aggregate as AGG
from app.forecast import device_service as DS
from app.forecast import devices as D

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/devices", tags=["Device forecasting"])


@router.get("/inventory")
def inventory(db: Session = Depends(get_db)) -> Dict[str, Any]:
    """
    Every discovered device, with freshness.

    Counts come from the database, never from a constant: this plant has 24
    inverters and 504 combiner boxes today, and if that changes the inventory
    follows without a code change.
    """
    disc = D.discover(db)
    tables = sorted({d["table"] for group in disc.values() for d in group})
    fresh = D.last_seen(db, tables)
    for group in disc.values():
        for d in group:
            d["last_seen"] = fresh.get(d["table"])
    return {
        **disc,
        "counts": {k: len(v) for k, v in disc.items()},
        "note": (
            "Inverters are forecast individually. Combiner boxes are ranked "
            "against their peers rather than forecast - they carry no energy "
            "register and too few complete days. This database holds no meter "
            "or feeder tables, and the tracker tables carry no generation "
            "telemetry, so neither category appears here."),
    }


@router.post("/forecast")
def run_devices(db: Session = Depends(get_db)) -> Dict[str, Any]:
    """Backtest and forecast every inverter. Minutes, so it is button-driven."""
    result = DS.run_device_forecasts(db)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("reason"))
    return result


@router.get("/forecast")
def device_forecasts() -> Dict[str, Any]:
    """
    The last run's results, including one still in progress.

    Devices are published as they finish, so a caller polling this sees real
    progress rather than a spinner with nothing behind it.
    """
    return DS.cached_forecasts()


@router.get("/{device_id}/history")
def device_history(device_id: str,
                   bucket: str = Query("day", pattern="^(day|week|month)$")
                   ) -> Dict[str, Any]:
    """
    One device's Actual vs Predicted series.

    Served from the same cache the table reads, so the chart and the row can
    never disagree, and the device must have been run first. `bucket` groups
    the daily series into calendar weeks or months through exactly the code
    the plant chart uses - a week is the sum of its days, and a partial period
    says so rather than being compared with a whole prediction.
    """
    cached = DS.cached_forecasts()
    match = next((d for d in cached.get("devices") or []
                  if d.get("device") == device_id), None)
    if match is None:
        raise HTTPException(
            status_code=404,
            detail=(f"No forecast held for {device_id}. Run the device "
                    f"forecast first, or check the device id against "
                    f"/devices/inventory."))

    series = match.get("series") or []
    if bucket == "day":
        points = series
        note = ("One point per day. Points marked is_backtest were predicted "
                "by a model that had not seen that day.")
    else:
        points = AGG.aggregate_points(series, bucket)
        note = " ".join([AGG.AGGREGATION_NOTE[bucket], AGG.INTERVAL_CAVEAT])

    return {
        "device": device_id,
        "bucket": bucket,
        "points": points,
        "n_points": len(points),
        "n_daily_points": len(series),
        "model": match.get("model"),
        "status": match.get("status"),
        "validation": match.get("validation"),
        "score_pct": match.get("score_pct"),
        "wape_pct": match.get("wape_pct"),
        "mae_mwh": match.get("mae_mwh"),
        "r2": match.get("r2"),
        "n_test": match.get("n_test"),
        "complete_days": match.get("complete_days"),
        "latest_actual_mwh": match.get("latest_actual_mwh"),
        "latest_actual_date": match.get("latest_actual_date"),
        "last_seen": match.get("last_seen"),
        "generated_at": cached.get("generated_at"),
        "note": note,
    }


@router.get("/combiners")
def combiners(db: Session = Depends(get_db)) -> Dict[str, Any]:
    """Peer ranking of every combiner box within its own inverter."""
    return DS.combiner_analysis(db)
