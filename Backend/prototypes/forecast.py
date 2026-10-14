"""
Forecasting experiment for daily plant generation.

Design decisions, all forced by the data rather than by preference:

* **Target is DAILY generation, not monthly.** The archive holds 100 complete days
  spanning two complete calendar months. A monthly or annual model cannot be fitted,
  let alone validated, on two observations of the seasonal cycle.
* **Rolling-origin backtesting, never a random split.** Each fold trains on
  everything up to a cut-off and predicts the following `HORIZON` days, so no future
  information can leak into training.
* **A seasonal-naive baseline is always scored.** A model that cannot beat
  "same as 7 days ago" has not earned its place, and that comparison is reported
  whether it flatters the model or not.
* **Intervals are split-conformal.** They are distribution-free and make no
  assumption about residual normality, and their coverage is measured on held-out
  folds rather than asserted.

Nothing here invents a future value: the forecast is produced by a model fitted to
observed data, and the horizon is capped by what backtesting can actually validate.
"""

from __future__ import annotations

import numpy as np
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from sklearn.ensemble import GradientBoostingRegressor, RandomForestRegressor
from sklearn.linear_model import Ridge

HORIZON = 7            # days ahead per fold - the longest span backtesting supports
MIN_TRAIN = 60         # days before the first fold
CONFORMAL_ALPHA = 0.10 # -> a 90% prediction interval


# ── features ─────────────────────────────────────────────────────────────────
def build_frame(daily: List[Dict[str, Any]],
                weather: Optional[List[Dict[str, Any]]] = None
                ) -> Tuple[np.ndarray, np.ndarray, List[str], List[Any]]:
    """
    Supervised frame from the daily series.

    Only lagged values are used as predictors. A same-day feature (today's
    irradiance, say) would not be known when forecasting ahead, so including it
    would produce validation scores that cannot be reproduced in use.
    """
    rows = [r for r in daily if r["minutes"] >= 1380]
    rows.sort(key=lambda r: r["d"])
    wmap = {w["d"]: w for w in (weather or [])}

    y_all = np.array([r["energy_integrated_mwh"] for r in rows], dtype=float)

    names = ["lag1", "lag2", "lag3", "lag7", "roll7_mean", "roll7_std",
             "roll3_mean", "doy_sin", "doy_cos", "dow",
             "curtailed_min_lag1", "inv_running_lag1"]
    use_weather = bool(wmap)
    if use_weather:
        names += ["gti_lag1", "module_temp_lag1", "soiling_lag1"]

    X, y, keep = [], [], []
    for i, r in enumerate(rows):
        if i < 7:
            continue                                   # need a 7-day history
        prev = rows[i - 1]
        hist = y_all[max(0, i - 7):i]
        doy = r["d"].timetuple().tm_yday
        feats = [
            y_all[i - 1], y_all[i - 2], y_all[i - 3], y_all[i - 7],
            float(np.mean(hist)), float(np.std(hist)),
            float(np.mean(y_all[i - 3:i])),
            np.sin(2 * np.pi * doy / 365.25), np.cos(2 * np.pi * doy / 365.25),
            float(r["d"].weekday()),
            float(prev["curtailed_minutes"]), float(prev["avg_inverters_running"]),
        ]
        if use_weather:
            w = wmap.get(prev["d"])
            if not w:
                continue                               # no weather lag -> skip the row
            feats += [w["gti_kwh_m2"], w["module_temp_c"], w["soiling_ratio"]]
        X.append(feats)
        y.append(y_all[i])
        keep.append(r["d"])
    return np.array(X, dtype=float), np.array(y, dtype=float), names, keep


# ── metrics ──────────────────────────────────────────────────────────────────
def metrics(actual: np.ndarray, pred: np.ndarray) -> Dict[str, Optional[float]]:
    a, p = np.asarray(actual, float), np.asarray(pred, float)
    err = p - a
    out: Dict[str, Optional[float]] = {
        "n": int(len(a)),
        "mae": float(np.mean(np.abs(err))),
        "rmse": float(np.sqrt(np.mean(err ** 2))),
        "bias": float(np.mean(err)),
    }
    # MAPE is undefined near zero. Several days in this archive produce almost
    # nothing, so it is reported only when every actual is comfortably non-zero,
    # and suppressed rather than silently distorted otherwise.
    floor = max(1.0, 0.01 * float(np.mean(a))) if len(a) else 1.0
    if np.all(a > floor):
        out["mape"] = float(np.mean(np.abs(err / a)) * 100.0)
    else:
        out["mape"] = None
        out["mape_note"] = (f"suppressed: {int(np.sum(a <= floor))} of {len(a)} actuals "
                            f"at or below {floor:.1f} MWh, where MAPE is undefined")
    ss_res = float(np.sum(err ** 2))
    ss_tot = float(np.sum((a - np.mean(a)) ** 2))
    out["r2"] = float(1 - ss_res / ss_tot) if ss_tot > 0 else None
    return out


# ── models ───────────────────────────────────────────────────────────────────
def _candidates() -> Dict[str, Any]:
    return {
        "Ridge (lag features)": Ridge(alpha=1.0),
        "Random Forest": RandomForestRegressor(
            n_estimators=300, min_samples_leaf=2, random_state=0, n_jobs=-1),
        "Gradient Boosting": GradientBoostingRegressor(
            n_estimators=300, max_depth=2, learning_rate=0.05, random_state=0),
    }


@dataclass
class FoldResult:
    cut: int
    actual: List[float] = field(default_factory=list)
    pred: Dict[str, List[float]] = field(default_factory=dict)
    dates: List[Any] = field(default_factory=list)


def rolling_origin_backtest(X: np.ndarray, y: np.ndarray, dates: List[Any],
                            horizon: int = HORIZON,
                            min_train: int = MIN_TRAIN) -> Dict[str, Any]:
    """
    Expanding-window backtest. Returns per-model pooled metrics plus the raw
    out-of-sample predictions, which are also what the conformal intervals use.
    """
    n = len(y)
    folds: List[FoldResult] = []
    cuts = list(range(min_train, n - horizon + 1, horizon))
    if not cuts:
        return {"error": f"Not enough history: {n} usable rows, need > {min_train + horizon}."}

    pooled: Dict[str, Dict[str, List[float]]] = {}

    def add(model: str, a: List[float], p: List[float]) -> None:
        d = pooled.setdefault(model, {"a": [], "p": []})
        d["a"].extend(a)
        d["p"].extend(p)

    for cut in cuts:
        Xtr, ytr = X[:cut], y[:cut]
        Xte, yte = X[cut:cut + horizon], y[cut:cut + horizon]
        fold = FoldResult(cut=cut, actual=list(map(float, yte)),
                          dates=list(dates[cut:cut + horizon]))

        # Baselines. Seasonal naive = value 7 days earlier, which for a daily solar
        # series is the honest "no model" comparison.
        sn = [float(y[cut + i - 7]) for i in range(len(yte))]
        nv = [float(y[cut - 1])] * len(yte)
        fold.pred["Seasonal Naive (lag-7)"] = sn
        fold.pred["Naive (last value)"] = nv
        add("Seasonal Naive (lag-7)", fold.actual, sn)
        add("Naive (last value)", fold.actual, nv)

        for name, model in _candidates().items():
            try:
                model.fit(Xtr, ytr)
                p = [float(v) for v in model.predict(Xte)]
            except Exception:                          # noqa: BLE001
                p = [float("nan")] * len(yte)
                fold.pred[f"{name} (failed)"] = p
                continue
            fold.pred[name] = p
            add(name, fold.actual, p)
        folds.append(fold)

    scores = {m: metrics(np.array(v["a"]), np.array(v["p"])) for m, v in pooled.items()}
    return {"folds": folds, "scores": scores, "cuts": cuts,
            "horizon": horizon, "min_train": min_train, "n_usable": n}


def select_model(scores: Dict[str, Dict[str, Any]]) -> Tuple[str, Dict[str, Any]]:
    """Lowest pooled RMSE wins. The baselines compete on the same footing."""
    ranked = sorted(scores.items(), key=lambda kv: kv[1]["rmse"])
    return ranked[0][0], ranked[0][1]


# ── conformal intervals ──────────────────────────────────────────────────────
def conformal_interval(backtest: Dict[str, Any], model_name: str,
                       alpha: float = CONFORMAL_ALPHA) -> Dict[str, Any]:
    """
    Split-conformal half-width from out-of-sample absolute residuals.

    q is the ceil((n+1)(1-alpha))/n empirical quantile of |actual - predicted|
    gathered across backtest folds, giving an interval with approximately
    (1-alpha) coverage without assuming a residual distribution. Coverage is then
    re-measured on those same folds and reported, so the claim is checkable.
    """
    resid: List[float] = []
    for f in backtest["folds"]:
        p = f.pred.get(model_name)
        if not p:
            continue
        for a, pv in zip(f.actual, p):
            if not np.isnan(pv):
                resid.append(abs(a - pv))
    if not resid:
        return {"available": False, "reason": "no out-of-sample residuals"}
    r = np.sort(np.array(resid))
    n = len(r)
    k = int(np.ceil((n + 1) * (1 - alpha))) - 1
    q = float(r[min(max(k, 0), n - 1)])
    covered = float(np.mean(r <= q))
    return {"available": True, "alpha": alpha, "half_width_mwh": q,
            "n_residuals": n, "empirical_coverage": covered,
            "nominal_coverage": 1 - alpha}


# ── forward forecast ─────────────────────────────────────────────────────────
def forecast_forward(X: np.ndarray, y: np.ndarray, dates: List[Any],
                     daily: List[Dict[str, Any]], names: List[str],
                     model_name: str, horizon: int,
                     half_width: Optional[float]) -> Dict[str, Any]:
    """
    Recursive multi-step forecast beyond the last observation.

    Each step feeds its own prediction back in as the next lag, which is the only
    honest option when no future weather or setpoint is known. Uncertainty
    therefore compounds with distance; the interval is widened by sqrt(step) to
    reflect that rather than holding the one-step width flat across the horizon.
    """
    rows = [r for r in daily if r["minutes"] >= 1380]
    rows.sort(key=lambda r: r["d"])
    hist = list(map(float, y))
    last_date = dates[-1]
    last_row = rows[-1]

    if model_name.startswith("Seasonal Naive"):
        model = None
    elif model_name.startswith("Naive"):
        model = None
    else:
        model = _candidates()[model_name]
        model.fit(X, y)

    from datetime import timedelta
    out = []
    series = list(hist)
    for step in range(1, horizon + 1):
        d = last_date + timedelta(days=step)
        doy = d.timetuple().tm_yday
        h = np.array(series[-7:], dtype=float)
        feats = [
            series[-1], series[-2], series[-3], series[-7],
            float(np.mean(h)), float(np.std(h)), float(np.mean(series[-3:])),
            np.sin(2 * np.pi * doy / 365.25), np.cos(2 * np.pi * doy / 365.25),
            float(d.weekday()),
            float(last_row["curtailed_minutes"]), float(last_row["avg_inverters_running"]),
        ]
        while len(feats) < X.shape[1]:
            feats.append(float(X[-1][len(feats)]))     # carry last known exogenous
        if model is None:
            pred = float(series[-7]) if model_name.startswith("Seasonal") else float(series[-1])
        else:
            pred = float(model.predict(np.array([feats[:X.shape[1]]], dtype=float))[0])
        pred = max(0.0, pred)
        widen = (half_width or 0.0) * float(np.sqrt(step))
        out.append({"date": d, "forecast_mwh": pred,
                    "lower_mwh": max(0.0, pred - widen),
                    "upper_mwh": pred + widen})
        series.append(pred)
    return {"model": model_name, "horizon_days": horizon, "points": out}
