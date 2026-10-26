"""
The daily feature frame the forecast is built from.

Everything here reads the verified Kalyon schema and nothing else:

    PPC.GRID_ACTIVE_POWER_MEASURED   MW   (net export; negative at night)
    PPC.ACTIVE_POWER_SET_POINT       %    (<100 while exporting = curtailed)
    PPC.INVERTER_RUNNING             count
    WMS.AVG_GTI_IRRADIATION          W/m2 instantaneous
    WMS.AVG_GHI_IRRADIATION          W/m2 instantaneous
    WMS.ALL_WMS_AVG_MODULE_TEMP      degC

Three properties of this archive drive the SQL below, and each was measured
rather than assumed:

* **`TimeCol` is UTC and the site is UTC+3.** Proven by the daily production
  register resetting at 21:00 UTC and by mean output peaking at 08:00 UTC.
  Generation falls between roughly 02:00 and 16:00 UTC, entirely inside one UTC
  date, so grouping by the UTC date never splits a solar day. `LocalCol` is NOT
  used: it carries +05:30 on PPC and +03:00 on WMS, so it records the ingesting
  machine's clock, not site time.
* **Duplicate timestamps exist** (278 on PPC, 81 on WMS). Summing raw rows
  double-counts those minutes, so every query collapses to one row per minute
  slot *before* integrating.
* **Night-time import is real** - 81,921 minutes at about -0.5 MW. Net and gross
  energy are therefore both computed and the caller chooses; they differ by
  roughly 711 MWh across the archive.
"""

from __future__ import annotations

import logging
import os
from datetime import date
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# A day must carry at least this many of 1440 minutes to be trusted as complete.
# Below it the total is an artefact of the logger, not of the plant.
COMPLETE_DAY_MINUTES = 1380

# A WMS day needs the same completeness before its irradiance integral is usable.
COMPLETE_WMS_MINUTES = 1380

# ── Quality control on MEASURED irradiance (pre-registration section 5) ──────
#
# These thresholds are physical, not statistical: surface irradiance at 38 deg N
# cannot reach them. A day that trips one has a sensor or scaling fault, and a
# model trained on it learns the fault.
#
# Raw readings are NEVER modified or deleted. A failure sets a flag computed at
# read time; the day keeps its energy and its FORECAST weather, and so remains
# in training and scoring. Only its measured-weather comparison and its
# condition label are withheld.
# C3 - a single-minute peak. Set well above the cloud-enhancement range: a
# brief edge-of-cloud spike genuinely reaches 1,200-1,400 W/m2 and is NOT a
# fault, so only a reading beyond any physical mechanism is flagged.
QC_MAX_PEAK_GTI_WM2 = 1500.0
# C4 - the daily integral, against a clear-sky ceiling from solar geometry
# rather than a flat number. A day cannot collect more than this fraction of
# the extraterrestrial horizontal irradiation; 0.85 is generous (real clear-sky
# transmittance is ~0.7-0.78), so only genuinely impossible days trip it.
QC_CLEARSKY_FRACTION = 0.85
QC_MIN_DISTINCT_GTI = 50        # C5 - fewer distinct values = latched sensor
QC_LATCH_MIN_MINUTES = 600
# Latitude used ONLY to compute the clear-sky ceiling in QC rule C4.
#
# This is NOT a statement about where the plant is. The plant's coordinates are
# not settled - public sources disagree - and nothing here should be read as
# confirming them. A regional fallback is acceptable for this one purpose
# because the ceiling is insensitive to it: across the whole Nigde province the
# daily extraterrestrial irradiation varies by well under one percent, far
# smaller than the margin C4 allows. Set WEATHER_LAT to the real latitude when
# it is known, and this sharpens automatically.
QC_FALLBACK_LATITUDE_DEG = 38.0          # Nigde province, rounded; not the plant
SITE_LATITUDE_DEG = float(os.getenv("WEATHER_LAT", QC_FALLBACK_LATITUDE_DEG))


def extraterrestrial_daily_kwh_m2(d: date, lat_deg: float = None) -> float:
    """
    Daily solar irradiation on a horizontal plane at the top of the atmosphere.

    Standard astronomy, no site data needed beyond latitude. It gives the hard
    physical ceiling: no pyranometer on the ground can read more than this, and
    in practice not more than ~78% of it.
    """
    import math
    lat = math.radians(SITE_LATITUDE_DEG if lat_deg is None else lat_deg)
    n = d.timetuple().tm_yday
    # Solar constant 1361 W/m2, with Earth-Sun distance correction.
    e0 = 1 + 0.033 * math.cos(math.radians(360 * n / 365.0))
    decl = math.radians(23.45 * math.sin(math.radians(360 * (284 + n) / 365.0)))
    x = -math.tan(lat) * math.tan(decl)
    x = max(-1.0, min(1.0, x))
    ws = math.acos(x)                                   # sunset hour angle
    h0 = (24 / math.pi) * 1.361 * e0 * (
        ws * math.sin(lat) * math.sin(decl)
        + math.cos(lat) * math.cos(decl) * math.sin(ws))
    return max(0.0, h0)                                 # kWh/m2/day


# ── plant generation ─────────────────────────────────────────────────────────
_DAILY_PLANT_SQL = text(f"""
    WITH minute_slot AS (
        -- One row per minute: duplicates averaged, never summed twice.
        SELECT DATEADD(minute, DATEDIFF(minute, 0, TimeCol), 0)   AS ts,
               AVG(GRID_ACTIVE_POWER_MEASURED)                     AS mw,
               AVG(ACTIVE_POWER_SET_POINT)                         AS setpoint,
               AVG(CAST(INVERTER_RUNNING AS float))                AS inv_running
        FROM PPC
        GROUP BY DATEADD(minute, DATEDIFF(minute, 0, TimeCol), 0)
    )
    SELECT CAST(ts AS DATE)                                        AS d,
           COUNT(*)                                                AS minutes,
           SUM(mw) / 60.0                                          AS energy_net_mwh,
           SUM(CASE WHEN mw > 0 THEN mw ELSE 0 END) / 60.0         AS energy_gross_mwh,
           MAX(mw)                                                 AS peak_mw,
           AVG(inv_running)                                        AS avg_inverters_running,
           SUM(CASE WHEN setpoint > 0 AND setpoint < 100 AND mw > 1
                    THEN 1 ELSE 0 END)                             AS curtailed_minutes,
           SUM(CASE WHEN mw > 1 THEN 1 ELSE 0 END)                 AS producing_minutes,
           AVG(CASE WHEN setpoint > 0 AND setpoint < 100 AND mw > 1
                    THEN setpoint END)                             AS mean_setpoint_pct
    FROM minute_slot
    GROUP BY CAST(ts AS DATE)
    HAVING COUNT(*) >= {COMPLETE_DAY_MINUTES}
    ORDER BY d
""")


# ── weather stations ─────────────────────────────────────────────────────────
# Irradiance is instantaneous W/m2 sampled once a minute, so the daily
# integral is sum(W/m2) / 60 / 1000 -> kWh/m2.
_DAILY_WMS_SQL = text("""
    WITH minute_slot AS (
        SELECT DATEADD(minute, DATEDIFF(minute, 0, TimeCol), 0)   AS ts,
               AVG(AVG_GTI_IRRADIATION)                            AS gti,
               AVG(AVG_GHI_IRRADIATION)                            AS ghi,
               AVG(ALL_WMS_AVG_MODULE_TEMP)                        AS module_temp,
               AVG(AVG_AIR_TEMP)                                   AS air_temp,
               AVG(AVG_WIND_SPEED)                                 AS wind,
               AVG(AVG_RELATIVE_HUMIDITY)                          AS humidity,
               AVG(AVG_IR_SOILING_RATIO1)                          AS soiling
        FROM WMS
        GROUP BY DATEADD(minute, DATEDIFF(minute, 0, TimeCol), 0)
    )
    SELECT CAST(ts AS DATE)              AS d,
           COUNT(*)                      AS minutes,
           SUM(gti) / 60000.0            AS gti_kwh_m2,
           SUM(ghi) / 60000.0            AS ghi_kwh_m2,
           AVG(module_temp)              AS module_temp_c,
           AVG(air_temp)                 AS air_temp_c,
           AVG(wind)                     AS wind_ms,
           AVG(humidity)                 AS humidity_pct,
           AVG(soiling)                  AS soiling_ratio,
           MAX(gti)                      AS peak_gti_wm2,
           COUNT(DISTINCT CAST(gti AS decimal(18,4))) AS distinct_gti
    FROM minute_slot
    GROUP BY CAST(ts AS DATE)
    ORDER BY d
""")


def daily_plant(db: Session) -> List[Dict[str, Any]]:
    """One row per COMPLETE day of plant generation."""
    rows = db.execute(_DAILY_PLANT_SQL).fetchall()
    return [
        {
            "d": r[0],
            "minutes": int(r[1]),
            "energy_net_mwh": float(r[2] or 0.0),
            "energy_gross_mwh": float(r[3] or 0.0),
            "peak_mw": float(r[4] or 0.0),
            "avg_inverters_running": float(r[5] or 0.0),
            "curtailed_minutes": int(r[6] or 0),
            "producing_minutes": int(r[7] or 0),
            "mean_setpoint_pct": float(r[8]) if r[8] is not None else None,
        }
        for r in rows
    ]


def daily_weather(db: Session) -> Dict[date, Dict[str, Any]]:
    """
    Measured weather per day, keyed by date.

    `frozen` marks a day whose irradiance channel barely moved. A pyranometer
    stuck at one value produces a plausible-looking daily total that is pure
    fiction, and a model trained on it learns the fault. Such days keep their
    plant data and lose their weather.
    """
    out: Dict[date, Dict[str, Any]] = {}
    for r in db.execute(_DAILY_WMS_SQL).fetchall():
        minutes = int(r[1])
        distinct_gti = int(r[10] or 0)
        ghi = float(r[3] or 0.0)
        peak = float(r[9] or 0.0)
        # C5 - a latched sensor, not a uniform sky.
        frozen = minutes >= QC_LATCH_MIN_MINUTES and distinct_gti < QC_MIN_DISTINCT_GTI
        # C3 / C4 - physically impossible readings.
        qc_fail = []
        if peak > QC_MAX_PEAK_GTI_WM2:
            qc_fail.append(f"C3 peak GTI {peak:.0f} W/m2 > {QC_MAX_PEAK_GTI_WM2:.0f}")
        ceiling = QC_CLEARSKY_FRACTION * extraterrestrial_daily_kwh_m2(r[0])
        if ghi > ceiling:
            qc_fail.append(f"C4 daily GHI {ghi:.2f} > clear-sky ceiling "
                           f"{ceiling:.2f} kWh/m2")
        if frozen:
            qc_fail.append(f"C5 only {distinct_gti} distinct GTI values")
        out[r[0]] = {
            "d": r[0],
            "minutes": minutes,
            "complete": minutes >= COMPLETE_WMS_MINUTES,
            "frozen": frozen,
            "qc_failures": qc_fail,
            "qc_ok": not qc_fail,
            "gti_kwh_m2": float(r[2] or 0.0),
            "ghi_kwh_m2": float(r[3] or 0.0),
            "module_temp_c": float(r[4]) if r[4] is not None else None,
            "air_temp_c": float(r[5]) if r[5] is not None else None,
            "wind_ms": float(r[6]) if r[6] is not None else None,
            "humidity_pct": float(r[7]) if r[7] is not None else None,
            "soiling_ratio": float(r[8]) if r[8] is not None else None,
            "peak_gti_wm2": float(r[9] or 0.0),
        }
    return out


# ── the supervised frame ─────────────────────────────────────────────────────
#
# Only values knowable BEFORE the target day may appear as a feature. Every
# plant feature is therefore lagged by at least one day, and the only same-day
# inputs permitted are weather FORECAST values, which are supplied separately by
# the caller and are by definition available at prediction time.
LAG_FEATURES = [
    "lag1", "lag2", "lag3", "lag7",
    "roll3_mean", "roll7_mean", "roll7_std",
    "doy_sin", "doy_cos", "dow",
    "curtailed_min_lag1", "inv_running_lag1",
]
WEATHER_FEATURES = ["fc_gti_kwh_m2", "fc_air_temp_c", "fc_cloud_pct"]

MIN_HISTORY_DAYS = 7     # lag-7 needs a week behind every row


def build_frame(
    plant: List[Dict[str, Any]],
    weather: Optional[Dict[date, Dict[str, Any]]] = None,
    target: str = "energy_net_mwh",
    weather_forecast: Optional[Dict[date, Dict[str, float]]] = None,
) -> Dict[str, Any]:
    """
    Turn the daily series into X, y and the metadata the backtest needs.

    `weather_forecast` maps a date to the weather that WOULD have been forecast
    for it. It is deliberately a separate argument from `weather` (the measured
    record) so that observed weather can never leak in by accident: measured
    values are used for reporting and for condition labels, never as features.
    """
    import numpy as np

    rows = sorted(plant, key=lambda r: r["d"])
    use_weather = bool(weather_forecast)
    names = list(LAG_FEATURES) + (list(WEATHER_FEATURES) if use_weather else [])

    y_all = np.array([r[target] for r in rows], dtype=float)

    X: List[List[float]] = []
    y: List[float] = []
    dates: List[date] = []
    meta: List[Dict[str, Any]] = []

    for i, r in enumerate(rows):
        if i < MIN_HISTORY_DAYS:
            continue
        prev = rows[i - 1]
        hist = y_all[i - 7:i]
        doy = r["d"].timetuple().tm_yday
        feats = [
            float(y_all[i - 1]), float(y_all[i - 2]), float(y_all[i - 3]), float(y_all[i - 7]),
            float(np.mean(y_all[i - 3:i])),
            float(np.mean(hist)), float(np.std(hist)),
            float(np.sin(2 * np.pi * doy / 365.25)), float(np.cos(2 * np.pi * doy / 365.25)),
            float(r["d"].weekday()),
            float(prev["curtailed_minutes"]), float(prev["avg_inverters_running"]),
        ]
        if use_weather:
            fc = (weather_forecast or {}).get(r["d"])
            if not fc:
                continue                      # no forecast for that day -> no row
            feats += [float(fc.get("gti_kwh_m2", 0.0)),
                      float(fc.get("air_temp_c", 0.0)),
                      float(fc.get("cloud_pct", 0.0))]

        obs = (weather or {}).get(r["d"])
        gti = obs["gti_kwh_m2"] if obs and obs["complete"] and not obs["frozen"] else None

        X.append(feats)
        y.append(float(r[target]))
        dates.append(r["d"])
        meta.append({
            "d": r["d"],
            "curtailed_minutes": r["curtailed_minutes"],
            "gti_kwh_m2": gti,
            "peak_mw": r["peak_mw"],
            "avg_inverters_running": r["avg_inverters_running"],
        })

    return {
        "X": np.array(X, dtype=float) if X else np.empty((0, len(names))),
        "y": np.array(y, dtype=float),
        "dates": dates,
        "meta": meta,
        "feature_names": names,
        "target": target,
        "weather_features_used": use_weather,
    }


def coverage_summary(db: Session) -> Dict[str, Any]:
    """
    What the archive can actually support, computed live.

    Served by the API before anything is trained, so the limits are visible in
    the UI rather than buried in a report.
    """
    plant = daily_plant(db)
    wx = daily_weather(db)
    paired = [p for p in plant
              if (w := wx.get(p["d"])) and w["complete"] and not w["frozen"]]
    span = db.execute(text(
        "SELECT MIN(TimeCol), MAX(TimeCol) FROM PPC")).first()
    wspan = db.execute(text(
        "SELECT MIN(TimeCol), MAX(TimeCol) FROM WMS")).first()
    return {
        "plant_first": span[0], "plant_last": span[1],
        "weather_first": wspan[0], "weather_last": wspan[1],
        "complete_days": len(plant),
        "days_with_weather": len(paired),
        "supervised_rows": max(0, len(plant) - MIN_HISTORY_DAYS),
        "frozen_weather_days": sum(1 for w in wx.values() if w["frozen"]),
        "curtailed_days": sum(1 for p in plant if p["curtailed_minutes"] > 0),
        "heavily_curtailed_days": sum(1 for p in plant if p["curtailed_minutes"] >= 300),
        "complete_day_threshold_minutes": COMPLETE_DAY_MINUTES,
        "timezone_note": (
            "TimeCol is UTC; the site is UTC+3. Generation falls inside one UTC "
            "date, so daily grouping does not split a solar day. LocalCol is "
            "inconsistent between tables and is not used."
        ),
    }
