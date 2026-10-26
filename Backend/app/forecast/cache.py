"""
Persistence for archived weather forecasts.

The cache exists for three reasons, in order of importance:

1. **Auditability.** Every row carries the timestamp the forecast was issued and
   the date it describes. The no-leakage rule — a prediction for day *d* may use
   only a forecast issued on or before *d−1* — becomes something that can be
   checked in SQL rather than trusted.
2. **Reproducibility.** A score measured today must be reproducible next month.
   Rows are append-only: re-fetching the same forecast is a no-op, so a later
   model run cannot silently rewrite the history an earlier score rested on.
3. **Courtesy.** One bulk read of the provider, then everything runs offline.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.models.forecasts import WeatherForecastCache

logger = logging.getLogger(__name__)

PROVIDER = "open-meteo"
# Coordinates are matched to this tolerance (~1 km), so a caller passing
# 37.9700 and 37.97001 reads the same cached rows instead of refetching.
COORD_TOLERANCE = 0.01


def store(db: Session, lat: float, lon: float,
          records: List[Dict[str, Any]], provider: str = PROVIDER) -> Dict[str, int]:
    """
    Append forecasts that are not already held. Never updates an existing row.
    """
    inserted = skipped = 0
    for r in records:
        valid = r["valid_date"]
        valid_dt = datetime.combine(valid, datetime.min.time()) \
            if not isinstance(valid, datetime) else valid
        exists = (db.query(WeatherForecastCache.id)
                    .filter(WeatherForecastCache.provider == provider)
                    .filter(WeatherForecastCache.valid_date == valid_dt)
                    .filter(WeatherForecastCache.issued_at == r["issued_at"])
                    .filter(and_(
                        WeatherForecastCache.latitude.between(lat - COORD_TOLERANCE,
                                                              lat + COORD_TOLERANCE),
                        WeatherForecastCache.longitude.between(lon - COORD_TOLERANCE,
                                                               lon + COORD_TOLERANCE)))
                    .first())
        if exists:
            skipped += 1
            continue
        db.add(WeatherForecastCache(
            provider=provider, latitude=lat, longitude=lon,
            valid_date=valid_dt, issued_at=r["issued_at"],
            lead_days=r.get("lead_days", 1),
            ghi_kwh_m2=r.get("ghi_kwh_m2"), cloud_pct=r.get("cloud_pct"),
            temp_c=r.get("temp_c"), source_note=r.get("source_note"),
        ))
        inserted += 1
    if inserted:
        db.commit()
    return {"inserted": inserted, "already_held": skipped}


def load_day_ahead(db: Session, lat: Optional[float] = None,
                   lon: Optional[float] = None,
                   provider: str = PROVIDER) -> Dict[date, Dict[str, float]]:
    """
    Every cached day-ahead forecast, keyed by the date it describes.

    **The leakage guard lives here.** A row is returned only when it was issued
    strictly before the day it predicts. A row failing that test is dropped and
    counted, never silently used.
    """
    q = db.query(WeatherForecastCache).filter(
        WeatherForecastCache.provider == provider)
    if lat is not None and lon is not None:
        q = q.filter(and_(
            WeatherForecastCache.latitude.between(lat - COORD_TOLERANCE,
                                                  lat + COORD_TOLERANCE),
            WeatherForecastCache.longitude.between(lon - COORD_TOLERANCE,
                                                   lon + COORD_TOLERANCE)))

    out: Dict[date, Dict[str, float]] = {}
    rejected = 0
    for row in q.order_by(WeatherForecastCache.valid_date.asc()).all():
        valid = row.valid_date.date() if isinstance(row.valid_date, datetime) \
            else row.valid_date
        issued = row.issued_at.date() if isinstance(row.issued_at, datetime) \
            else row.issued_at
        if issued >= valid:
            rejected += 1          # would be same-day or later knowledge
            continue
        out[valid] = {
            "gti_kwh_m2": float(row.ghi_kwh_m2 or 0.0),   # site GTI ~= GHI
            "ghi_kwh_m2": float(row.ghi_kwh_m2 or 0.0),
            "cloud_pct": float(row.cloud_pct or 0.0),
            "air_temp_c": float(row.temp_c or 0.0),
        }
    if rejected:
        logger.warning("Dropped %d cached forecast(s) not issued before their "
                       "valid date - these would have leaked.", rejected)
    return out


def coverage(db: Session, provider: str = PROVIDER) -> Dict[str, Any]:
    """What the cache holds, for the status endpoint."""
    rows = (db.query(WeatherForecastCache)
              .filter(WeatherForecastCache.provider == provider).all())
    if not rows:
        return {"rows": 0, "first": None, "last": None, "coordinates": [],
                "leakage_rows": 0}
    valid = [r.valid_date for r in rows]
    coords = sorted({(round(r.latitude, 4), round(r.longitude, 4)) for r in rows})
    leak = sum(1 for r in rows
               if (r.issued_at.date() if isinstance(r.issued_at, datetime) else r.issued_at)
               >= (r.valid_date.date() if isinstance(r.valid_date, datetime) else r.valid_date))
    return {
        "rows": len(rows), "first": min(valid), "last": max(valid),
        "coordinates": [{"lat": c[0], "lon": c[1]} for c in coords],
        "leakage_rows": leak,
        "provider": provider,
    }


def verify_no_leakage(db: Session, provider: str = PROVIDER) -> Tuple[bool, str]:
    """
    An explicit, reportable check that nothing in the cache could leak.

    Returns (ok, message). Called by the API and by the test suite, so the claim
    is verified rather than asserted in a comment.
    """
    cov = coverage(db, provider)
    if cov["rows"] == 0:
        return True, "Cache is empty; nothing can leak."
    if cov["leakage_rows"]:
        return False, (f"{cov['leakage_rows']} cached forecast(s) were issued on "
                       f"or after the day they describe and are excluded from use.")
    return True, (f"All {cov['rows']} cached forecasts were issued before the day "
                  f"they describe.")
