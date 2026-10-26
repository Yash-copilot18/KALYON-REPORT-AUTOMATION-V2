# app/models/forecasts.py
"""
Forecast persistence — two tables, created automatically at startup via
Base.metadata.create_all, exactly like `saved_reports` and `report_schedules`.
No migration tool is needed and no existing table is touched.

Why anything is stored at all: a forecast is only a forecast if it was recorded
BEFORE the day it describes. `forecast_points.made_on` is that proof. Without
it, "we predicted 700 MWh" is an unverifiable claim made after the fact, and
every accuracy figure derived from it is worthless.

`forecast_runs` holds one row per training run: which model won, on what window,
and the measured error on the held-out test block. Runs accumulate, so model
performance over time is itself auditable.
"""

from sqlalchemy import (Boolean, Column, DateTime, Float, ForeignKey, Integer,
                        String, Text)
from sqlalchemy.sql import func

from app.database.base import Base


class ForecastRun(Base):
    """One training + selection pass. Maps to [Kalyan].[dbo].[forecast_runs]."""
    __tablename__ = "forecast_runs"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    # What was predicted, and by what.
    target = Column(String(40), nullable=False)          # energy_net_mwh | energy_gross_mwh
    model_name = Column(String(80), nullable=False)
    selection_basis = Column(String(120), nullable=True)

    # The chronological windows, so a score can never be read without its dates.
    train_start = Column(DateTime, nullable=True)
    train_end   = Column(DateTime, nullable=True)
    test_start  = Column(DateTime, nullable=True)
    test_end    = Column(DateTime, nullable=True)
    n_train = Column(Integer, nullable=True)
    n_test  = Column(Integer, nullable=True)
    folds   = Column(Integer, nullable=True)

    # Measured on the held-out test block — never on anything used for selection.
    wape_pct  = Column(Float, nullable=True)
    score_pct = Column(Float, nullable=True)
    mae_mwh   = Column(Float, nullable=True)
    rmse_mwh  = Column(Float, nullable=True)
    bias_mwh  = Column(Float, nullable=True)
    r2        = Column(Float, nullable=True)

    # Honesty flags, surfaced directly in the UI.
    weather_forecast_used = Column(Boolean, nullable=False, default=False)
    target_score_pct = Column(Float, nullable=True)       # the agreed goal, e.g. 85
    target_met = Column(Boolean, nullable=False, default=False)

    # Full leaderboard / per-horizon / per-condition breakdown as JSON text.
    detail_json = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)


class WeatherForecastCache(Base):
    """
    One archived weather forecast, as issued. Maps to
    [Kalyan].[dbo].[weather_forecast_cache].

    Two timestamps, and the distinction between them is the whole point:

      * `issued_at`  - when the forecast was produced by the weather model.
      * `valid_date` - the day it describes.

    A prediction for day d may only use a row whose `issued_at` is on or before
    d-1. Storing both makes that rule checkable after the fact instead of taken
    on trust, which is what separates a forecast archive from reanalysis.

    Rows are immutable once written: re-fetching the same (provider, lat, lon,
    valid_date, issued_at) is a no-op rather than an overwrite, so a later model
    revision can never silently rewrite the history an old score was measured on.
    """
    __tablename__ = "weather_forecast_cache"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    provider = Column(String(40), nullable=False)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)

    valid_date = Column(DateTime, nullable=False, index=True)
    issued_at = Column(DateTime, nullable=False)
    lead_days = Column(Integer, nullable=False, default=1)

    ghi_kwh_m2 = Column(Float, nullable=True)
    cloud_pct = Column(Float, nullable=True)
    temp_c = Column(Float, nullable=True)

    fetched_at = Column(DateTime, server_default=func.now(), nullable=False)
    source_note = Column(String(200), nullable=True)


class ForecastPoint(Base):
    """
    One predicted day. Maps to [Kalyan].[dbo].[forecast_points].

    `actual_mwh` is null until the day completes and the reconciliation job
    fills it in. That is the moment a prediction becomes a measured deviation.
    """
    __tablename__ = "forecast_points"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    run_id = Column(Integer, ForeignKey("forecast_runs.id"), nullable=True, index=True)

    target_date = Column(DateTime, nullable=False, index=True)
    made_on     = Column(DateTime, server_default=func.now(), nullable=False)

    predicted_mwh = Column(Float, nullable=False)
    lower_mwh     = Column(Float, nullable=True)
    upper_mwh     = Column(Float, nullable=True)

    # Filled once the day is complete.
    actual_mwh    = Column(Float, nullable=True)
    deviation_mwh = Column(Float, nullable=True)
    deviation_pct = Column(Float, nullable=True)

    # OK | UNDER | OVER | UNRELIABLE — see service.classify_deviation.
    status = Column(String(20), nullable=True, index=True)

    # True for a point produced by walking the held-out test block: a genuine
    # out-of-sample day-ahead prediction, made without the model having seen
    # that day, but reconstructed after the fact rather than issued at the time.
    # The distinction matters — a backtest point proves the method, a forward
    # point proves the service — so the two are never silently mixed.
    is_backtest = Column(Boolean, nullable=False, default=False)

    # Measured evidence captured at reconciliation time (curtailment, irradiance,
    # inverter availability, alarm count) as JSON text. Hypotheses only.
    evidence_json = Column(Text, nullable=True)
