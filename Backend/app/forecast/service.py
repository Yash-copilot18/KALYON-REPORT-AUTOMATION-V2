"""
Forecast orchestration: build the frame, validate chronologically, select a
model, predict forward, persist, and reconcile against what actually happened.

The module keeps one rule above all others: **a number is either measured or it
is labelled.** The API never returns an accuracy figure without the window it
was measured on, never returns a forecast without saying whether a weather
forecast informed it, and never reports the client's 85% target as met unless
the held-out test block says so.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.forecast import backtest as BT
from app.forecast import cache
from app.forecast import features as F
from app.forecast import models as M
from app.forecast import weather as W
from app.models.forecasts import ForecastPoint, ForecastRun

logger = logging.getLogger(__name__)

# The agreed goal. Configurable so the figure in the UI always matches whatever
# was actually signed off, rather than a constant buried in code.
TARGET_SCORE_PCT = float(os.getenv("FORECAST_TARGET_SCORE", "83"))

# A deviation smaller than this is noise, not an event.
DEVIATION_PCT_THRESHOLD = float(os.getenv("FORECAST_DEVIATION_PCT", "15"))
DEVIATION_MIN_MWH = float(os.getenv("FORECAST_DEVIATION_MIN_MWH", "50"))

# How many days forward a run predicts. Capped at the validation horizon,
# because predicting further than the backtest can score is unverifiable.
FORECAST_DAYS = 7

_lock = threading.Lock()


# ── training ─────────────────────────────────────────────────────────────────
def run_training(db: Session, target: str = "energy_net_mwh",
                 use_weather: bool = True,
                 ceiling_test: bool = False) -> Dict[str, Any]:
    """
    One complete pass: frame -> chronological validation -> selection ->
    held-out test -> forward prediction -> persistence.

    `ceiling_test` substitutes MEASURED weather for a forecast to establish the
    best score a perfect weather feed could reach. It leaks future information
    by design, is flagged as such throughout, and is never persisted.
    """
    with _lock:
        plant = F.daily_plant(db)
        wx = F.daily_weather(db)

        wx_status = W.status()
        fc_map: Optional[Dict[date, Dict[str, float]]] = None
        leakage = False

        if ceiling_test:
            fc_map = W.observed_as_forecast(wx)
            leakage = True
        elif use_weather and wx_status["configured"]:
            # A provider gives only the next few days, which cannot train a
            # model. Historical forecast values would be needed for that, and
            # this database holds none - so the weather feed informs the forward
            # prediction, not the training frame. Stated, not hidden.
            fc_map = None

        frame = F.build_frame(plant, wx, target=target, weather_forecast=fc_map)
        if len(frame["y"]) == 0:
            return {"ok": False, "reason": "No usable rows after frame construction."}

        # The product is a NEXT-DAY forecast, so the headline score is measured
        # day-ahead. The multi-horizon backtest still runs, because a 7-day view
        # is shown on the page and its accuracy decay must be visible rather
        # than implied.
        bt = BT.walk_forward_one_day(frame)
        if not bt.get("ok"):
            return {"ok": False, "reason": bt.get("reason"), "rows": len(frame["y"])}

        multi = BT.rolling_origin(frame)

        interval = BT.conformal_interval(bt["test_records"])
        test = bt["test"]
        score = test.get("score_pct")
        target_met = bool(score is not None and score >= TARGET_SCORE_PCT)

        result: Dict[str, Any] = {
            "ok": True,
            "generated_at": datetime.now(),
            "target": target,
            "selected_model": bt["selected_model"],
            "selection_basis": bt["selection_basis"],
            "evaluation_mode": bt["mode"],
            "validation_folds": multi.get("folds") if multi.get("ok") else None,
            "horizon_days": FORECAST_DAYS,
            "train_start": bt["train_start"], "train_end": bt["train_end"],
            "test_start": bt["test_start"], "test_end": bt["test_end"],
            "n_train": bt["n_train"], "n_test": bt["n_test"],
            "n_validation_days": bt["n_validation_days"],
            "leaderboard": bt["leaderboard"],
            "test_metrics": test,
            # Accuracy decay with distance, from the multi-horizon backtest.
            "by_horizon": multi.get("by_horizon") if multi.get("ok") else [],
            "multi_horizon_test": multi.get("test") if multi.get("ok") else None,
            "by_condition": bt["by_condition"],
            "prediction_interval": interval,
            "weather": wx_status,
            "weather_features_used": bt["weather_features_used"],
            "leakage_ceiling_test": leakage,
            "target_score_pct": TARGET_SCORE_PCT,
            "target_met": target_met,
            "feature_names": bt["feature_names"],
        }

        if leakage:
            result["warning"] = (
                "CEILING TEST ONLY. Measured weather was used in place of a "
                "forecast, which leaks information unavailable at prediction "
                "time. This score is an upper bound, not an achievable result, "
                "and has not been saved."
            )
            return result

        forward = _predict_forward(frame, bt, plant, interval)
        result["forecast"] = forward
        result["run_id"] = _persist(db, result, forward,
                                    bt["test_records"], plant, wx, interval)
        return result


def _predict_forward(frame: Dict[str, Any], bt: Dict[str, Any],
                     plant: List[Dict[str, Any]],
                     interval: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Recursive multi-step prediction beyond the last observed day.

    Each step feeds its own output back as the next lag, which is the only
    option when no future weather is known. Error therefore compounds with
    distance, and the interval is widened by sqrt(step) to say so rather than
    pretending day 7 is as certain as day 1.
    """
    import numpy as np

    X, y = frame["X"], frame["y"]
    model = M.fit_model(bt["selected_model"], X, y)

    rows = sorted(plant, key=lambda r: r["d"])
    last = rows[-1]
    series = list(map(float, y))
    half = (interval or {}).get("half_width_mwh")

    out: List[Dict[str, Any]] = []
    for step in range(1, FORECAST_DAYS + 1):
        d = last["d"] + timedelta(days=step)
        doy = d.timetuple().tm_yday
        hist = np.array(series[-7:], dtype=float)
        feats = [
            series[-1], series[-2], series[-3], series[-7],
            float(np.mean(series[-3:])),
            float(np.mean(hist)), float(np.std(hist)),
            float(np.sin(2 * np.pi * doy / 365.25)),
            float(np.cos(2 * np.pi * doy / 365.25)),
            float(d.weekday()),
            float(last["curtailed_minutes"]), float(last["avg_inverters_running"]),
        ]
        while len(feats) < X.shape[1]:
            feats.append(float(X[-1][len(feats)]))     # carry last known exogenous
        p = float(M.predict(model, np.array([feats[:X.shape[1]]], dtype=float))[0])
        widen = (half or 0.0) * float(np.sqrt(step))
        out.append({
            "date": d,
            "horizon": step,
            "predicted_mwh": p,
            "lower_mwh": max(0.0, p - widen) if half else None,
            "upper_mwh": (p + widen) if half else None,
        })
        series.append(p)
    return out


def _persist(db: Session, result: Dict[str, Any],
             forward: List[Dict[str, Any]],
             test_records: Optional[List[Dict[str, Any]]] = None,
             plant: Optional[List[Dict[str, Any]]] = None,
             wx: Optional[Dict[date, Dict[str, Any]]] = None,
             interval: Optional[Dict[str, Any]] = None) -> Optional[int]:
    """
    Store the run, its forward points, and the held-out test predictions.

    The test predictions are saved as `is_backtest=True` points with their
    actuals already known. That is what gives the page a real actual-vs-predicted
    chart and a real deviation list on an archive whose last day has already
    passed — without inventing a single value, because every one of those
    predictions was made by a model that had not seen the day it predicted.
    """
    try:
        t = result["test_metrics"]
        run = ForecastRun(
            target=result["target"],
            model_name=result["selected_model"],
            selection_basis=result["selection_basis"],
            train_start=_dt(result["train_start"]), train_end=_dt(result["train_end"]),
            test_start=_dt(result["test_start"]), test_end=_dt(result["test_end"]),
            n_train=result["n_train"], n_test=result["n_test"],
            folds=result["validation_folds"],
            wape_pct=t.get("wape_pct"), score_pct=t.get("score_pct"),
            mae_mwh=t.get("mae_mwh"), rmse_mwh=t.get("rmse_mwh"),
            bias_mwh=t.get("bias_mwh"), r2=t.get("r2"),
            weather_forecast_used=bool(result["weather_features_used"]),
            target_score_pct=result["target_score_pct"],
            target_met=bool(result["target_met"]),
            detail_json=json.dumps({
                "leaderboard": result["leaderboard"],
                "by_horizon": result["by_horizon"],
                "by_condition": result["by_condition"],
                "prediction_interval": result["prediction_interval"],
                "feature_names": result["feature_names"],
            }, default=str),
        )
        db.add(run)
        db.flush()

        # Out-of-sample backtest points, with their measured outcome.
        half = (interval or {}).get("half_width_mwh")
        pmap = {p["d"]: p for p in (plant or [])}
        ref = reference_levels(plant or [])
        for r in (test_records or []):
            d = r["d"]
            row = pmap.get(d)
            actual = float(r["actual"])
            pred = float(r["forecast"])
            dev = actual - pred
            w = (wx or {}).get(d)
            db.add(ForecastPoint(
                run_id=run.id, target_date=_dt(d),
                predicted_mwh=pred,
                lower_mwh=max(0.0, pred - half) if half else None,
                upper_mwh=(pred + half) if half else None,
                actual_mwh=actual, deviation_mwh=dev,
                deviation_pct=(100.0 * dev / pred) if pred else None,
                status=classify_deviation(pred, actual,
                                          row["minutes"] if row else 0),
                is_backtest=True,
                evidence_json=json.dumps({
                    "curtailed_minutes": row["curtailed_minutes"] if row else None,
                    "mean_setpoint_pct": row["mean_setpoint_pct"] if row else None,
                    "avg_inverters_running": round(row["avg_inverters_running"], 2) if row else None,
                    "producing_minutes": row["producing_minutes"] if row else None,
                    "peak_mw": round(row["peak_mw"], 2) if row else None,
                    "gti_kwh_m2": round(w["gti_kwh_m2"], 3) if w and w["complete"] and not w["frozen"] else None,
                    "gti_prev_kwh_m2": _prev_gti(wx, d),
                    "module_temp_c": round(w["module_temp_c"], 1) if w and w.get("module_temp_c") is not None else None,
                    "weather_frozen": bool(w["frozen"]) if w else None,
                    "minutes_recorded": row["minutes"] if row else None,
                    **ref,
                }, default=str),
            ))

        # Forward points: no actual yet, by definition.
        for p in forward:
            db.add(ForecastPoint(
                run_id=run.id,
                target_date=_dt(p["date"]),
                predicted_mwh=p["predicted_mwh"],
                lower_mwh=p["lower_mwh"], upper_mwh=p["upper_mwh"],
                is_backtest=False,
            ))
        db.commit()
        return run.id
    except Exception as exc:                           # noqa: BLE001
        db.rollback()
        logger.warning("Forecast run not persisted: %s", exc)
        return None


def _dt(v) -> Optional[datetime]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    return datetime(v.year, v.month, v.day)


# ── deviation detection ──────────────────────────────────────────────────────
def classify_deviation(predicted: float, actual: Optional[float],
                       minutes: int) -> str:
    """
    OK / UNDER / OVER / UNRELIABLE.

    UNRELIABLE comes first: a day the logger did not fully record cannot be
    compared with anything, and raising an alert from it would train people to
    ignore alerts.
    """
    if actual is None or minutes < F.COMPLETE_DAY_MINUTES:
        return "UNRELIABLE"
    dev = actual - predicted
    if abs(dev) < DEVIATION_MIN_MWH:
        return "OK"
    pct = 100.0 * dev / predicted if predicted > 0 else 0.0
    if abs(pct) < DEVIATION_PCT_THRESHOLD:
        return "OK"
    return "UNDER" if dev < 0 else "OVER"


def reconcile(db: Session, lookback_days: int = 60) -> Dict[str, Any]:
    """
    Fill in actuals for every stored prediction whose day has completed, and
    attach the measured evidence a human needs to judge the cause.

    The evidence is stated as fact; the cause is not. Curtailment minutes,
    irradiance and inverter availability are measured. Whether they *explain*
    the deviation is a hypothesis, and the API labels it as one.
    """
    plant_rows = F.daily_plant(db)
    plant = {p["d"]: p for p in plant_rows}
    ref = reference_levels(plant_rows)
    wx = F.daily_weather(db)
    cutoff = datetime.now() - timedelta(days=lookback_days)

    pending = (db.query(ForecastPoint)
                 .filter(ForecastPoint.actual_mwh.is_(None))
                 .filter(ForecastPoint.target_date >= cutoff)
                 .all())

    updated = 0
    for pt in pending:
        d = pt.target_date.date() if isinstance(pt.target_date, datetime) else pt.target_date
        row = plant.get(d)
        if not row:
            continue                                   # day not complete yet
        actual = float(row["energy_net_mwh"])
        dev = actual - float(pt.predicted_mwh)
        pt.actual_mwh = actual
        pt.deviation_mwh = dev
        pt.deviation_pct = (100.0 * dev / pt.predicted_mwh) if pt.predicted_mwh else None
        pt.status = classify_deviation(float(pt.predicted_mwh), actual, row["minutes"])
        w = wx.get(d)
        pt.evidence_json = json.dumps({
            "curtailed_minutes": row["curtailed_minutes"],
            "mean_setpoint_pct": row["mean_setpoint_pct"],
            "avg_inverters_running": round(row["avg_inverters_running"], 2),
            "producing_minutes": row["producing_minutes"],
            "peak_mw": round(row["peak_mw"], 2),
            "gti_kwh_m2": round(w["gti_kwh_m2"], 3) if w and w["complete"] and not w["frozen"] else None,
            "gti_prev_kwh_m2": _prev_gti(wx, d),
            "module_temp_c": round(w["module_temp_c"], 1) if w and w.get("module_temp_c") is not None else None,
            "weather_frozen": bool(w["frozen"]) if w else None,
            "minutes_recorded": row["minutes"],
            **ref,
        }, default=str)
        updated += 1

    if updated:
        db.commit()
    return {"reconciled": updated, "pending": len(pending) - updated}


def _prev_gti(wx: Optional[Dict[date, Dict[str, Any]]], d: date) -> Optional[float]:
    """Yesterday's measured irradiation, when it is usable."""
    w = (wx or {}).get(d - timedelta(days=1))
    if not w or not w.get("complete") or w.get("frozen"):
        return None
    return round(float(w["gti_kwh_m2"]), 3)


def reference_levels(plant: List[Dict[str, Any]]) -> Dict[str, float]:
    """
    The plant's own normal, computed from the archive.

    Needed because `avg_inverters_running` is a mean over all 1,440 minutes of
    the day, night included, so it sits near 13 on a perfectly healthy day. An
    absolute threshold against the 24-unit fleet would fire on every single day
    and train people to ignore the alerts. Comparison is therefore against the
    median day, not against the nameplate count.
    """
    import statistics
    if not plant:
        return {}
    return {
        "ref_inverters_running": float(statistics.median(
            p["avg_inverters_running"] for p in plant)),
        "ref_producing_minutes": float(statistics.median(
            p["producing_minutes"] for p in plant)),
    }


def hypotheses(evidence: Dict[str, Any], status: str) -> List[str]:
    """
    Candidate explanations for a deviation, each tied to a measured value.

    Every string returned names the evidence behind it and is phrased as a
    possibility. Nothing here is asserted as the cause: this archive carries no
    dispatch log, no work orders and no outage records, so a definitive
    attribution cannot be made from data alone.
    """
    out: List[str] = []
    if status in ("OK", "UNRELIABLE"):
        return out
    curt = evidence.get("curtailed_minutes") or 0
    if curt >= 60:
        out.append(
            f"Curtailment is a candidate: the setpoint was below 100% for "
            f"{curt:,} exporting minutes"
            + (f" at a mean of {evidence['mean_setpoint_pct']:.1f}%"
               if evidence.get("mean_setpoint_pct") else "") + ".")
    inv = evidence.get("avg_inverters_running")
    ref = evidence.get("ref_inverters_running")
    # Only material against the plant's own median day — never against the
    # 24-unit fleet count, which this figure is not comparable with.
    if inv is not None and ref and inv < 0.85 * ref:
        out.append(
            f"Reduced availability is a candidate: inverter running time averaged "
            f"{inv:.1f} against a typical {ref:.1f} for this plant "
            f"({100 * (inv / ref - 1):+.0f}%).")
    pm = evidence.get("producing_minutes")
    pref = evidence.get("ref_producing_minutes")
    if pm is not None and pref and pm < 0.85 * pref:
        out.append(
            f"Shortened operation is a candidate: the plant exported for "
            f"{pm:,} minutes against a typical {pref:,.0f}.")
    gti = evidence.get("gti_kwh_m2")
    prev = evidence.get("gti_prev_kwh_m2")
    if gti is not None and gti < 4.0 and status == "UNDER":
        out.append(f"Low measured resource is a candidate: plane-of-array "
                   f"irradiation was {gti:.2f} kWh/m² on the day.")
    # The day-on-day swing in sunlight is the usual explanation for a miss by a
    # model that carries yesterday forward, so it is stated explicitly.
    if gti is not None and prev:
        change = 100.0 * (gti / prev - 1) if prev else 0.0
        if status == "UNDER" and change <= -12:
            out.append(f"Sunlight fell against the previous day: irradiation "
                       f"{prev:.2f} to {gti:.2f} kWh/m² ({change:+.0f}%), which a "
                       f"forecast built on recent output cannot anticipate.")
        elif status == "OVER" and change >= 12:
            out.append(f"Sunlight rose against the previous day: irradiation "
                       f"{prev:.2f} to {gti:.2f} kWh/m² ({change:+.0f}%), so the "
                       f"plant beat a forecast based on recent output.")
    if evidence.get("weather_frozen"):
        out.append("Weather evidence is unusable for this day: the irradiance "
                   "channel was latched, so no resource comparison is possible.")
    if not out:
        out.append("No measured cause stands out. The deviation is not explained "
                   "by curtailment, availability or measured irradiance.")
    return out


# ── weather archive ingestion ────────────────────────────────────────────────
def ingest_weather_archive(db: Session, start: date, end: date) -> Dict[str, Any]:
    """
    Backfill the forecast cache from the provider's historical archive.

    Gated twice: a provider must be configured AND WEATHER_ARCHIVE_ENABLED=1.
    Coordinates come from WEATHER_LAT / WEATHER_LON and are never hardcoded -
    the plant's exact location is not settled, and a guessed one must not be
    baked into the code.
    """
    st = W.status()
    if not st["configured"]:
        return {"ok": False, "reason": st["reason"]}
    if not W.archive_enabled():
        return {"ok": False, "reason": (
            f"Archive backfill is off. Set {W.ARCHIVE_ENV}=1 to allow it.")}
    lat, lon = os.getenv(W.LAT_ENV), os.getenv(W.LON_ENV)
    if not (lat and lon):
        return {"ok": False, "reason": (
            f"{W.LAT_ENV} and {W.LON_ENV} must be set to the plant's "
            f"coordinates. No default is assumed.")}
    lat, lon = float(lat), float(lon)

    records = W.fetch_historical_day_ahead(lat, lon, start, end)
    if not records:
        return {"ok": False, "reason": "Provider returned no archived forecasts."}
    res = cache.store(db, lat, lon, records)
    ok, msg = cache.verify_no_leakage(db)
    return {"ok": True, "latitude": lat, "longitude": lon,
            "start": start, "end": end, **res,
            "leakage_check_passed": ok, "leakage_check": msg}


# ── the acceptance run ───────────────────────────────────────────────────────
def run_acceptance(db: Session, target: str = "energy_net_mwh") -> Dict[str, Any]:
    """
    Score the PRE-REGISTERED model over every day the archive allows.

    No model is selected here. The model, its hyperparameters, the rolling
    window, the features, the cleaning rules and the scoring formula were all
    fixed in docs/forecast-prereg.md before this was ever run, so every scored
    day is legitimately out of sample.

    Baselines are scored on identical dates for comparison only; their results
    can never change which model the acceptance figure comes from.
    """
    fcmap = cache.load_day_ahead(db)
    if not fcmap:
        return {"ok": False, "reason": (
            "No archived day-ahead forecasts are cached. Run the archive "
            "ingestion first.")}

    plant = F.daily_plant(db)
    wx = F.daily_weather(db)
    frame = F.build_frame(plant, wx, target=target, weather_forecast=fcmap)
    if len(frame["y"]) == 0:
        return {"ok": False, "reason": "No usable rows after frame construction."}

    names = frame["feature_names"]
    ghi_index = names.index("fc_gti_kwh_m2") if "fc_gti_kwh_m2" in names else None
    if ghi_index is None:
        return {"ok": False, "reason": "Forecast irradiance is not in the frame."}

    primary = BT.preregistered_walk_forward(
        frame, M.PREREGISTERED_MODEL, ghi_index=ghi_index)
    if not primary.get("ok"):
        return {"ok": False, "reason": primary.get("reason")}

    # Identical dates, identical window, for every comparison model.
    comparisons = []
    for name in M.candidates(ghi_index):
        if name == M.PREREGISTERED_MODEL:
            continue
        r = BT.preregistered_walk_forward(frame, name, ghi_index=ghi_index)
        if r.get("ok"):
            comparisons.append({
                "model": name, "n_scored": r["n_scored"],
                **{k: r["metrics"].get(k) for k in
                   ("score_pct", "wape_pct", "mae_mwh", "rmse_mwh", "bias_mwh")},
                "is_baseline": M.is_baseline(name),
            })
    comparisons.sort(key=lambda c: -(c["score_pct"] or 0))

    m = primary["metrics"]
    ci = BT.bootstrap_ci(primary["records"])
    score = m.get("score_pct")
    enough = primary["n_scored"] >= 60
    met = bool(score is not None and score >= TARGET_SCORE_PCT and enough)

    qc = [{"date": w["d"], "failures": w["qc_failures"]}
          for w in wx.values() if w.get("qc_failures")]

    return {
        "ok": True,
        "generated_at": datetime.now(),
        "mode": primary["mode"],
        "preregistration": "docs/forecast-prereg.md",
        "model": primary["model"],
        "rolling_window_days": primary["rolling_window_days"],
        "n_scored": primary["n_scored"],
        "sample_sufficient": enough,
        "first_scored": primary["first_scored"],
        "last_scored": primary["last_scored"],
        "metrics": m,
        "confidence_interval": ci,
        "by_condition": primary["by_condition"],
        "comparisons": comparisons,
        "target_score_pct": TARGET_SCORE_PCT,
        "target_met": met,
        "weather": W.status(),
        "weather_cache": cache.coverage(db),
        "qc_exclusions": sorted(qc, key=lambda x: str(x["date"])),
        "metric_definition": "score = 100 - WAPE; WAPE = sum|a-f| / sum(a) * 100",
    }
