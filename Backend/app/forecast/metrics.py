"""
Forecast accuracy metrics.

The headline number this module exists to produce is the **forecast score**,
defined by the client as `100 - WAPE`. WAPE is used rather than MAPE because
solar generation legitimately approaches zero on a curtailed or heavily overcast
day, and a percentage error against a near-zero actual explodes without telling
anyone anything. WAPE divides one summed error by one summed actual, so a quiet
day contributes in proportion to its size instead of dominating the average.

Every function here is pure arithmetic over two aligned sequences. Nothing is
clipped, smoothed or winsorised: a metric that flatters the model by discarding
its worst days is not a metric.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

import numpy as np

# Below this daily total a percentage error carries no information - the plant
# was curtailed, shut down or overcast to a standstill. Such days are still
# scored by WAPE, MAE and RMSE; only MAPE is suppressed.
NEAR_ZERO_MWH = 10.0


def _arrays(actual: Sequence[float], forecast: Sequence[float]):
    a = np.asarray(actual, dtype=float)
    f = np.asarray(forecast, dtype=float)
    if a.shape != f.shape:
        raise ValueError(f"actual {a.shape} and forecast {f.shape} differ in length")
    return a, f


def wape(actual: Sequence[float], forecast: Sequence[float]) -> Optional[float]:
    """
    Weighted absolute percentage error, in percent.

        WAPE = sum(|actual - forecast|) / sum(actual) * 100

    Returns None when the actuals sum to zero or less, which happens only if the
    whole scored window was a shutdown; a score computed on that would be
    meaningless rather than merely poor.
    """
    a, f = _arrays(actual, forecast)
    denom = float(np.sum(a))
    if denom <= 0:
        return None
    return 100.0 * float(np.sum(np.abs(f - a))) / denom


def forecast_score(actual: Sequence[float], forecast: Sequence[float]) -> Optional[float]:
    """The client's headline figure: 100 - WAPE, floored at zero.

    A model worse than 100% WAPE scores 0 rather than a negative number, because
    "minus 40% accurate" invites misreading. The underlying WAPE is always
    reported alongside, so nothing is hidden by the floor.
    """
    w = wape(actual, forecast)
    if w is None:
        return None
    return max(0.0, 100.0 - w)


def evaluate(actual: Sequence[float], forecast: Sequence[float]) -> Dict[str, Any]:
    """Every metric for one aligned set of predictions."""
    a, f = _arrays(actual, forecast)
    if len(a) == 0:
        return {"n": 0}

    err = f - a
    w = wape(a, f)
    out: Dict[str, Any] = {
        "n": int(len(a)),
        "mae_mwh": float(np.mean(np.abs(err))),
        "rmse_mwh": float(np.sqrt(np.mean(err ** 2))),
        "bias_mwh": float(np.mean(err)),          # positive = over-forecasting
        "wape_pct": w,
        "score_pct": None if w is None else max(0.0, 100.0 - w),
        "mean_actual_mwh": float(np.mean(a)),
        "sum_actual_mwh": float(np.sum(a)),
    }

    # MAPE only where every actual is comfortably away from zero, and said so
    # explicitly when it is withheld.
    near_zero = int(np.sum(a <= NEAR_ZERO_MWH))
    if near_zero == 0:
        out["mape_pct"] = float(np.mean(np.abs(err / a)) * 100.0)
        out["mape_note"] = None
    else:
        out["mape_pct"] = None
        out["mape_note"] = (
            f"suppressed: {near_zero} of {len(a)} days at or below "
            f"{NEAR_ZERO_MWH:.0f} MWh, where a percentage error is undefined"
        )

    ss_res = float(np.sum(err ** 2))
    ss_tot = float(np.sum((a - np.mean(a)) ** 2))
    out["r2"] = float(1 - ss_res / ss_tot) if ss_tot > 0 else None
    out["near_zero_days"] = near_zero
    return out


def evaluate_by_horizon(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Metrics split by how far ahead each prediction was made.

    `records` are dicts carrying `horizon` (1 = next day), `actual` and
    `forecast`. Accuracy decays with distance, and a single pooled number hides
    that, so the API reports both.
    """
    by: Dict[int, Dict[str, List[float]]] = {}
    for r in records:
        h = int(r["horizon"])
        d = by.setdefault(h, {"a": [], "f": []})
        d["a"].append(float(r["actual"]))
        d["f"].append(float(r["forecast"]))
    out = []
    for h in sorted(by):
        m = evaluate(by[h]["a"], by[h]["f"])
        m["horizon_days"] = h
        out.append(m)
    return out


def evaluate_by_condition(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Metrics split by the day's measured conditions.

    A model can look adequate overall while failing completely on the days that
    matter. Buckets are assigned from measured data only - curtailment minutes
    from the PPC setpoint, and irradiance from the weather stations - never from
    anything the model itself produced.
    """
    buckets: Dict[str, Dict[str, List[float]]] = {}
    for r in records:
        label = r.get("condition") or "unclassified"
        d = buckets.setdefault(label, {"a": [], "f": []})
        d["a"].append(float(r["actual"]))
        d["f"].append(float(r["forecast"]))
    out = []
    for label in sorted(buckets):
        m = evaluate(buckets[label]["a"], buckets[label]["f"])
        m["condition"] = label
        out.append(m)
    return out


def classify_condition(curtailed_minutes: int, gti_kwh_m2: Optional[float]) -> str:
    """
    The measured condition of one day, used only to group metrics.

    Thresholds are descriptive labels for reporting, not physics. Curtailment is
    checked first because a curtailed day's output is set by an instruction, not
    by the weather, and grouping it with cloudy days would misattribute the error.
    """
    if curtailed_minutes >= 300:
        return "curtailed"
    if gti_kwh_m2 is None:
        return "no irradiance data"
    if gti_kwh_m2 >= 6.5:
        return "high irradiance"
    if gti_kwh_m2 >= 4.0:
        return "moderate irradiance"
    return "low irradiance"
