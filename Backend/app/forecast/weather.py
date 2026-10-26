"""
Weather-forecast provider — pluggable, and OFF unless explicitly configured.

Day-ahead solar generation is driven by cloud cover, which no amount of past
generation can anticipate. A weather forecast is therefore the single largest
accuracy lever available. This database contains none: only measured history.

Rather than pretend otherwise, this module:

* reports `configured = False` by default, so the UI can state plainly that the
  model is running on a historical baseline;
* never makes a network call unless `WEATHER_PROVIDER` is set;
* keeps any key in the environment, never in code, the database or a response.

**Measured weather must never be substituted for a forecast.** Scoring a model
on the irradiance that actually occurred inflates it by a wide margin - on this
archive, from a 57% score to about 76% - and that number could not be
reproduced in service. `observed_as_forecast()` exists for one purpose: to
measure the ceiling a perfect forecast would reach. It is clearly labelled and
its output is never written to a stored forecast run.
"""

from __future__ import annotations

import logging
import os
from datetime import date
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

PROVIDER_ENV = "WEATHER_PROVIDER"      # unset or "none" -> disabled
LAT_ENV = "WEATHER_LAT"
LON_ENV = "WEATHER_LON"
KEY_ENV = "WEATHER_API_KEY"
TIMEOUT_ENV = "WEATHER_TIMEOUT"

SUPPORTED = ("open-meteo",)


def _provider() -> str:
    return (os.getenv(PROVIDER_ENV) or "none").strip().lower()


def status() -> Dict[str, Any]:
    """
    Whether a forecast source is configured — without revealing the key.

    Only a boolean is returned for the key. The value is never logged, returned
    or stored.
    """
    p = _provider()
    configured = p in SUPPORTED
    lat, lon = os.getenv(LAT_ENV), os.getenv(LON_ENV)
    return {
        "configured": configured,
        "provider": p if configured else None,
        "supported_providers": list(SUPPORTED),
        "has_coordinates": bool(lat and lon),
        "api_key_present": bool(os.getenv(KEY_ENV)),
        "reason": None if configured else (
            f"No weather-forecast provider configured. Set {PROVIDER_ENV} "
            f"(one of {', '.join(SUPPORTED)}), {LAT_ENV} and {LON_ENV} to enable. "
            "Until then the model runs on a historical baseline and the forecast "
            "score is limited by the absence of cloud-cover information."
        ),
    }


def fetch_forecast(days: int = 7) -> Optional[Dict[date, Dict[str, float]]]:
    """
    Day-ahead weather for the next `days`, or None when not configured.

    Returns a mapping of date -> {gti_kwh_m2, air_temp_c, cloud_pct}. A failure
    to reach the provider returns None and is logged; it never raises into a
    request, because a missing weather forecast must degrade the forecast, not
    break the dashboard.
    """
    st = status()
    if not st["configured"]:
        return None

    lat, lon = os.getenv(LAT_ENV), os.getenv(LON_ENV)
    if not (lat and lon):
        logger.warning("Weather provider set but %s/%s are missing.", LAT_ENV, LON_ENV)
        return None

    try:
        timeout = float(os.getenv(TIMEOUT_ENV, "15"))
    except ValueError:
        timeout = 15.0

    if _provider() == "open-meteo":
        return _open_meteo(float(lat), float(lon), days, timeout)
    return None


def _open_meteo(lat: float, lon: float, days: int, timeout: float
                ) -> Optional[Dict[date, Dict[str, float]]]:
    """
    Open-Meteo adapter. Needs no API key, which is why it is the default choice
    when a provider is switched on.

    `shortwave_radiation_sum` is horizontal daily irradiation in MJ/m2; it is
    converted to kWh/m2 (1 kWh = 3.6 MJ). It is NOT plane-of-array, so it is an
    approximation of the GTI the model is trained against - a transposition
    model would be the next refinement, and that gap is reported rather than
    silently absorbed.
    """
    import json
    import urllib.parse
    import urllib.request

    q = urllib.parse.urlencode({
        "latitude": lat, "longitude": lon,
        "daily": "shortwave_radiation_sum,temperature_2m_mean,cloudcover_mean",
        "forecast_days": max(1, min(16, days)),
        "timezone": "UTC",
    })
    url = f"https://api.open-meteo.com/v1/forecast?{q}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:                       # noqa: BLE001 - never break a request
        logger.warning("Weather forecast unavailable: %s", exc)
        return None

    daily = payload.get("daily") or {}
    times = daily.get("time") or []
    rad = daily.get("shortwave_radiation_sum") or []
    temp = daily.get("temperature_2m_mean") or []
    cloud = daily.get("cloudcover_mean") or []

    out: Dict[date, Dict[str, float]] = {}
    for i, t in enumerate(times):
        try:
            d = date.fromisoformat(t)
        except ValueError:
            continue
        mj = rad[i] if i < len(rad) and rad[i] is not None else None
        out[d] = {
            "gti_kwh_m2": (float(mj) / 3.6) if mj is not None else 0.0,
            "air_temp_c": float(temp[i]) if i < len(temp) and temp[i] is not None else 0.0,
            "cloud_pct": float(cloud[i]) if i < len(cloud) and cloud[i] is not None else 0.0,
            "source": 1.0,       # marks a real provider value, not a stand-in
        }
    return out or None


def observed_as_forecast(weather: Dict[date, Dict[str, Any]]) -> Dict[date, Dict[str, float]]:
    """
    CEILING TEST ONLY — measured weather dressed as a forecast.

    This leaks future information on purpose, to answer one question: what score
    would be reachable if the weather forecast were perfect? Any run using it is
    flagged `leakage=True` and is never persisted as a real forecast run.
    """
    out: Dict[date, Dict[str, float]] = {}
    for d, w in weather.items():
        if not w.get("complete") or w.get("frozen"):
            continue
        out[d] = {
            "gti_kwh_m2": float(w.get("gti_kwh_m2") or 0.0),
            "air_temp_c": float(w.get("air_temp_c") or 0.0),
            "cloud_pct": 0.0,
        }
    return out


# ── historical forecast archive ──────────────────────────────────────────────
#
# This is what makes an honest backtest possible: Open-Meteo retains what each
# model run PREDICTED, not only what happened. The `_previous_day1` series is
# the forecast issued one day before the day it describes, and it is verifiably
# different from the best-match series (751 vs 499 W/m2 at 2024-04-08 10:00),
# which is how we know it is a forecast and not reanalysis.
#
# Daily variables do not support the suffix, so hourly values are fetched and
# integrated here.

HISTORICAL_HOST = "https://historical-forecast-api.open-meteo.com/v1/forecast"
ARCHIVE_ENV = "WEATHER_ARCHIVE_ENABLED"      # must be "1" to allow the fetch


def archive_enabled() -> bool:
    """A second switch, independent of the live provider.

    Backfilling the archive is a bulk read of someone else's service, so it is
    off unless explicitly turned on, even when a provider is configured.
    """
    return (os.getenv(ARCHIVE_ENV) or "").strip() == "1"


def fetch_historical_day_ahead(lat: float, lon: float,
                               start: date, end: date,
                               timeout: float = 120.0
                               ) -> Optional[List[Dict[str, Any]]]:
    """
    Day-ahead forecasts for every date in [start, end], as they were issued.

    Returns one record per day carrying the integrated forecast and both
    timestamps the audit trail needs: `valid_date` and `issued_at`. Returns
    None on any failure - a weather archive that cannot be reached must degrade
    the forecast, never break the caller.
    """
    import json
    import urllib.parse
    import urllib.request
    from datetime import datetime, timedelta

    q = urllib.parse.urlencode({
        "latitude": lat, "longitude": lon,
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "hourly": ("shortwave_radiation_previous_day1,"
                   "cloud_cover_previous_day1,"
                   "temperature_2m_previous_day1"),
        "timezone": "UTC",
    })
    try:
        with urllib.request.urlopen(f"{HISTORICAL_HOST}?{q}", timeout=timeout) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except Exception as exc:                        # noqa: BLE001
        logger.warning("Historical forecast archive unavailable: %s", exc)
        return None

    h = payload.get("hourly") or {}
    times = h.get("time") or []
    if not times:
        logger.warning("Historical forecast archive returned no hourly data.")
        return None

    rad = h.get("shortwave_radiation_previous_day1") or []
    cld = h.get("cloud_cover_previous_day1") or []
    tmp = h.get("temperature_2m_previous_day1") or []

    acc: Dict[str, List[float]] = {}
    for i, t in enumerate(times):
        d = t[:10]
        a = acc.setdefault(d, [0.0, 0.0, 0.0, 0.0])
        if i < len(rad) and rad[i] is not None:
            a[0] += float(rad[i]) / 1000.0          # W/m2 hourly -> kWh/m2
        if i < len(cld) and cld[i] is not None:
            a[1] += float(cld[i])
        if i < len(tmp) and tmp[i] is not None:
            a[2] += float(tmp[i])
        a[3] += 1.0

    out: List[Dict[str, Any]] = []
    for d, (ghi, cloud, temp, n) in sorted(acc.items()):
        valid = date.fromisoformat(d)
        # The run that produced a "previous_day1" value was issued the day
        # before the valid date. Recorded explicitly so the no-leakage rule can
        # be verified rather than assumed.
        out.append({
            "valid_date": valid,
            "issued_at": datetime.combine(valid - timedelta(days=1),
                                          datetime.min.time()),
            "lead_days": 1,
            "ghi_kwh_m2": ghi,
            "cloud_pct": cloud / n if n else None,
            "temp_c": temp / n if n else None,
            "source_note": "open-meteo historical-forecast previous_day1, hourly->daily UTC",
        })
    return out
