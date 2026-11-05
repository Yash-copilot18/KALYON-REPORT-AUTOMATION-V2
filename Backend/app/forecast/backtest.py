"""
Chronological validation. No random splits, anywhere, ever.

Shuffling a time series lets a model train on next week and be tested on last
week, which produces a flattering number that cannot be reproduced in service.
Everything here walks forward in time:

    |-------- train --------|-- validate --|   fold 1
    |------------ train ------------|-- validate --|   fold 2
    ...
    |---------------- train ----------------|---- TEST ----|

The final TEST block is held out of model selection entirely. Folds are used to
compare candidates and pick a winner; the winner is then scored once on the test
block, and that score is the one reported. A model chosen on the test set has
been tuned on it, and its score means nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Sequence, Tuple

import logging
import os

import numpy as np

from app.forecast import models as M
from app.forecast import pool as POOL
from app.forecast.metrics import (classify_condition, evaluate,
                                  evaluate_by_condition, evaluate_by_horizon)

logger = logging.getLogger(__name__)

HORIZON_DAYS = 7        # each fold predicts this many days forward

# Smallest training block that can carry a lag-7 model. 45 suits Kalyon, which
# has a hundred complete days; it is read from the environment so a site with a
# shorter record can still be evaluated deliberately, with the small sample
# reported rather than hidden.
MIN_TRAIN_DAYS = int(os.getenv("MIN_TRAIN_DAYS", "45"))

# A held-out block below this many days gives a score dominated by which
# particular days happened to land in it. Scores measured on fewer are reported,
# never suppressed, but always carry the caveat. The current Kalyon run rests on
# 19 test days, so this is live, not theoretical.
MIN_CREDIBLE_TEST_DAYS = int(os.getenv("MIN_CREDIBLE_TEST_DAYS", "30"))

TEST_FRACTION = 0.20    # final untouched block, as a share of all rows


# ── parallel fitting ─────────────────────────────────────────────────────────
#
# The walk-forward refits every candidate from scratch at every step: stand on
# day t, train on everything before it, predict t+1, move on. On this plant
# that is 29 steps for each of five candidates, and the two tree models carry
# 300 estimators apiece, so a run spent 14.1 of its 14.7 seconds inside
# `fit`. Measured here: Random Forest 10.25s, Gradient Boosting 3.82s, Ridge
# 0.06s, and the two baselines nothing at all, since a baseline reads a column
# and does not fit.
#
# Every one of those fits is independent - its own slice of history, its own
# model object, a fixed seed, and `n_jobs=1` on the forest - so they are spread
# across processes. Nothing about the arithmetic changes: the same step trains
# on the same rows and returns the same number, and the results are reassembled
# in step order before they are scored. Only the waiting is shorter.

# Below this much work the pool costs more than it saves: handing a short
# series to other processes costs more in message passing than it returns.
MIN_PARALLEL_FITS = 24

# Steps per task. Small enough that one slow chunk cannot hold up the run,
# large enough that per-task overhead stays in the noise.
CHUNK_STEPS = 3


def _fit_chunk(name: str, X: np.ndarray, y: np.ndarray,
               lo: int, hi: int) -> List[Tuple[int, float]]:
    """
    Predict each day in [lo, hi) from everything before it, for one candidate.

    Top-level and self-contained so a worker process can unpickle it. A fit
    that raises is skipped rather than failing the chunk, which is what the
    serial loop did.
    """
    out: List[Tuple[int, float]] = []
    for i in range(lo, hi):
        try:
            model = M.fit_model(name, X[:i], y[:i])
            out.append((i, float(M.predict(model, X[i:i + 1])[0])))
        except Exception:                              # noqa: BLE001
            continue
    return out


def _walk_many(names: Sequence[str], X: np.ndarray, y: np.ndarray,
               start: int, stop: int) -> Dict[str, Dict[int, float]]:
    """
    Walk every named candidate over [start, stop), in parallel where it pays.

    Returns {candidate: {step index: prediction}}. Falls back to a plain loop
    if the pool cannot be created - a locked-down host, a frozen build - so the
    forecast still completes, just slower.
    """
    steps = max(0, stop - start)
    if steps <= 0 or not names:
        return {n: {} for n in names}

    tasks = [(n, lo, min(lo + CHUNK_STEPS, stop))
             for n in names
             for lo in range(start, stop, CHUNK_STEPS)]

    out: Dict[str, Dict[int, float]] = {n: {} for n in names}

    if steps * len(names) >= MIN_PARALLEL_FITS:
        results = POOL.run_map(
            _fit_chunk,
            [t[0] for t in tasks],
            [X] * len(tasks), [y] * len(tasks),
            [t[1] for t in tasks], [t[2] for t in tasks])
        if results is not None:
            for (name, _, _), got in zip(tasks, results):
                out[name].update(got)
            logger.info("walk-forward fitted on the shared pool "
                        "(%d candidates x %d steps)", len(names), steps)
            return out

    for name, lo, hi in tasks:
        out[name].update(_fit_chunk(name, X, y, lo, hi))
    return out


@dataclass
class Prediction:
    d: date
    horizon: int
    actual: float
    forecast: float
    condition: str


@dataclass
class ModelResult:
    name: str
    validation: Dict[str, Any] = field(default_factory=dict)
    predictions: List[Prediction] = field(default_factory=list)


def _condition(meta: Dict[str, Any]) -> str:
    return classify_condition(meta.get("curtailed_minutes", 0), meta.get("gti_kwh_m2"))


def rolling_origin(frame: Dict[str, Any],
                   horizon: int = HORIZON_DAYS,
                   min_train: int = MIN_TRAIN_DAYS,
                   test_fraction: float = TEST_FRACTION) -> Dict[str, Any]:
    """
    Compare every candidate on expanding-window folds, then score the winner
    once on the held-out tail.
    """
    X, y = frame["X"], frame["y"]
    dates, meta = frame["dates"], frame["meta"]
    n = len(y)

    n_test = int(round(n * test_fraction))
    n_dev = n - n_test
    if n_dev < min_train + horizon or n_test < 3:
        return {
            "ok": False,
            "reason": (f"Not enough history: {n} usable rows. Need at least "
                       f"{min_train + horizon} for validation plus a test block."),
            "rows": n,
        }

    cuts = list(range(min_train, n_dev - horizon + 1, horizon))
    if not cuts:
        return {"ok": False, "reason": f"No validation folds fit in {n_dev} rows.", "rows": n}

    results: Dict[str, ModelResult] = {
        name: ModelResult(name=name) for name in M.candidates()
    }

    # ── validation folds: selection happens here and nowhere else ────────────
    for cut in cuts:
        Xtr, ytr = X[:cut], y[:cut]
        hi = min(cut + horizon, n_dev)
        Xte, yte = X[cut:hi], y[cut:hi]
        for name in results:
            try:
                model = M.fit_model(name, Xtr, ytr)
                preds = M.predict(model, Xte)
            except Exception:                          # noqa: BLE001
                continue                               # a failed candidate is simply not scored
            for k in range(len(yte)):
                i = cut + k
                results[name].predictions.append(Prediction(
                    d=dates[i], horizon=k + 1, actual=float(yte[k]),
                    forecast=float(preds[k]), condition=_condition(meta[i]),
                ))

    scored = []
    for name, res in results.items():
        if not res.predictions:
            continue
        a = [p.actual for p in res.predictions]
        f = [p.forecast for p in res.predictions]
        res.validation = evaluate(a, f)
        res.validation["is_baseline"] = M.is_baseline(name)
        scored.append((name, res))

    if not scored:
        return {"ok": False, "reason": "No candidate produced a prediction.", "rows": n}

    # Winner = lowest validation WAPE, because WAPE defines the client's score.
    def key(item):
        w = item[1].validation.get("wape_pct")
        return w if w is not None else float("inf")

    scored.sort(key=key)
    best_name = scored[0][0]

    # ── held-out test: scored once, after selection ──────────────────────────
    best = M.fit_model(best_name, X[:n_dev], y[:n_dev])
    test_pred = M.predict(best, X[n_dev:])
    test_records = [
        {"d": dates[n_dev + k], "horizon": k + 1,
         "actual": float(y[n_dev + k]), "forecast": float(test_pred[k]),
         "condition": _condition(meta[n_dev + k])}
        for k in range(len(test_pred))
    ]
    test_metrics = evaluate([r["actual"] for r in test_records],
                            [r["forecast"] for r in test_records])

    return {
        "ok": True,
        "rows": n,
        "selected_model": best_name,
        "selection_basis": "lowest WAPE on chronological validation folds",
        "folds": len(cuts),
        "horizon_days": horizon,
        "min_train_days": min_train,
        "train_start": dates[0], "train_end": dates[n_dev - 1],
        "test_start": dates[n_dev], "test_end": dates[-1],
        "n_train": n_dev, "n_test": len(test_records),
        "validation": {name: res.validation for name, res in scored},
        "leaderboard": [
            {"model": name,
             "wape_pct": res.validation.get("wape_pct"),
             "score_pct": res.validation.get("score_pct"),
             "mae_mwh": res.validation.get("mae_mwh"),
             "rmse_mwh": res.validation.get("rmse_mwh"),
             "bias_mwh": res.validation.get("bias_mwh"),
             "r2": res.validation.get("r2"),
             "is_baseline": res.validation.get("is_baseline")}
            for name, res in scored
        ],
        "test": test_metrics,
        "test_records": test_records,
        "by_horizon": evaluate_by_horizon([
            {"horizon": p.horizon, "actual": p.actual, "forecast": p.forecast}
            for p in results[best_name].predictions
        ]),
        "by_condition": evaluate_by_condition([
            {"condition": p.condition, "actual": p.actual, "forecast": p.forecast}
            for p in results[best_name].predictions
        ]),
        "feature_names": frame["feature_names"],
        "weather_features_used": frame["weather_features_used"],
        "target": frame["target"],
    }


def walk_forward_one_day(frame: Dict[str, Any],
                         min_train: int = MIN_TRAIN_DAYS,
                         test_fraction: float = TEST_FRACTION) -> Dict[str, Any]:
    """
    Day-ahead evaluation: the product, measured the way it will be used.

    The brief asks for NEXT-DAY generation. Pooling horizons 1 to 7 into one
    figure answers a different question and understates the thing being sold,
    because error grows with distance. So this walks one day at a time: stand on
    day *t*, train on everything up to and including *t*, predict *t+1*, step
    forward. Nothing from the future is ever in the training set.

    It also yields far more scored points than a 7-day-block backtest over the
    same archive, which makes the resulting score less of a lottery.
    """
    X, y = frame["X"], frame["y"]
    dates, meta = frame["dates"], frame["meta"]
    n = len(y)

    n_test = int(round(n * test_fraction))
    n_dev = n - n_test
    if n_dev < min_train + 1 or n_test < 3:
        return {"ok": False,
                "reason": f"Not enough history for day-ahead validation: {n} rows."}

    def _records(preds: Dict[int, float]) -> List[Dict[str, Any]]:
        """Turn {step: prediction} into scored records, in chronological order."""
        return [{"d": dates[i], "horizon": 1, "actual": float(y[i]),
                 "forecast": preds[i], "condition": _condition(meta[i])}
                for i in sorted(preds)]

    # Selection on the development block only. Every candidate walks the same
    # steps over the same rows; `_walk_many` spreads those fits across
    # processes and hands back the same numbers a serial loop produced.
    names = list(M.candidates())
    walked = _walk_many(names, X, y, min_train, n_dev)

    validation: Dict[str, Dict[str, Any]] = {}
    val_records: Dict[str, List[Dict[str, Any]]] = {}
    for name in names:
        recs = _records(walked.get(name, {}))
        if not recs:
            continue
        val_records[name] = recs
        m = evaluate([r["actual"] for r in recs], [r["forecast"] for r in recs])
        m["is_baseline"] = M.is_baseline(name)
        validation[name] = m

    if not validation:
        return {"ok": False, "reason": "No candidate produced a day-ahead prediction."}

    best_name = min(validation,
                    key=lambda k: validation[k].get("wape_pct") or float("inf"))

    # The held-out block, walked the same way, scored once.
    test_records = _records(_walk_many([best_name], X, y, n_dev, n)[best_name])
    test_metrics = evaluate([r["actual"] for r in test_records],
                            [r["forecast"] for r in test_records])

    return {
        "ok": True,
        "mode": "day-ahead walk-forward (horizon = 1)",
        "rows": n,
        "selected_model": best_name,
        "selection_basis": "lowest day-ahead WAPE on the validation block",
        "train_start": dates[0], "train_end": dates[n_dev - 1],
        "test_start": dates[n_dev], "test_end": dates[-1],
        "n_train": n_dev, "n_test": len(test_records),
        "n_validation_days": len(val_records[best_name]),
        "validation": validation,
        "leaderboard": [
            {"model": k,
             "wape_pct": v.get("wape_pct"), "score_pct": v.get("score_pct"),
             "mae_mwh": v.get("mae_mwh"), "rmse_mwh": v.get("rmse_mwh"),
             "bias_mwh": v.get("bias_mwh"), "r2": v.get("r2"),
             "is_baseline": v.get("is_baseline")}
            for k, v in sorted(validation.items(),
                               key=lambda kv: kv[1].get("wape_pct") or float("inf"))
        ],
        "test": test_metrics,
        "test_records": test_records,
        "by_condition": evaluate_by_condition(val_records[best_name]),
        "feature_names": frame["feature_names"],
        "weather_features_used": frame["weather_features_used"],
        "target": frame["target"],
    }


def conformal_interval(records: List[Dict[str, Any]], alpha: float = 0.10
                       ) -> Optional[Dict[str, Any]]:
    """
    A prediction interval built from measured out-of-sample error.

    Split-conformal: the half-width is the empirical quantile of absolute
    residuals seen on data the model never trained on. It assumes nothing about
    the error distribution, and its coverage is re-measured on the same
    residuals and reported, so the claim can be checked rather than trusted.
    """
    resid = [abs(r["actual"] - r["forecast"]) for r in records]
    if len(resid) < 5:
        return None
    r = np.sort(np.array(resid, dtype=float))
    n = len(r)
    k = int(np.ceil((n + 1) * (1 - alpha))) - 1
    q = float(r[min(max(k, 0), n - 1)])
    return {
        "half_width_mwh": q,
        "alpha": alpha,
        "nominal_coverage_pct": (1 - alpha) * 100.0,
        "empirical_coverage_pct": 100.0 * float(np.mean(r <= q)),
        "n_residuals": n,
        "method": "split conformal on out-of-sample validation residuals",
    }


# ── pre-registered acceptance evaluation ─────────────────────────────────────
#
# The method the client's acceptance figure is produced by, specified in
# docs/forecast-prereg.md BEFORE it was run.
#
# There is no model selection here, and that is the entire point. The model and
# its hyperparameters were fixed in writing in advance, so every walk-forward
# day is legitimately out of sample and all of them can be scored. That is how
# ~60 scored days are obtained from a 100-day archive without either starving
# the training set or leaking selection into the result.

PREREG_ROLLING_WINDOW = 60      # train on at most this many most-recent days
PREREG_MIN_TRAIN = 30           # scoring starts once this many days exist


def preregistered_walk_forward(frame: Dict[str, Any],
                               model_name: str,
                               ghi_index: Optional[int] = None,
                               rolling_window: int = PREREG_ROLLING_WINDOW,
                               min_train: int = PREREG_MIN_TRAIN
                               ) -> Dict[str, Any]:
    """
    Score one fixed model, one day at a time, over every day the archive allows.

    For each day i: fit on the most recent `rolling_window` days strictly before
    i, predict day i, record. Nothing from day i or later ever reaches the fit.
    """
    X, y = frame["X"], frame["y"]
    dates, meta = frame["dates"], frame["meta"]
    n = len(y)
    if n <= min_train:
        return {"ok": False,
                "reason": f"Only {n} usable rows; need more than {min_train}."}

    records: List[Dict[str, Any]] = []
    for i in range(min_train, n):
        lo = max(0, i - rolling_window)
        try:
            model = M.fit_model(model_name, X[lo:i], y[lo:i], ghi_index=ghi_index)
            p = float(M.predict(model, X[i:i + 1])[0])
        except Exception:                               # noqa: BLE001
            continue
        records.append({"d": dates[i], "horizon": 1, "actual": float(y[i]),
                        "forecast": p, "condition": _condition(meta[i]),
                        "train_days": i - lo})
    if not records:
        return {"ok": False, "reason": "No day could be scored."}

    a = [r["actual"] for r in records]
    f = [r["forecast"] for r in records]
    return {
        "ok": True,
        "mode": "pre-registered day-ahead walk-forward, rolling window",
        "model": model_name,
        "rolling_window_days": rolling_window,
        "min_train_days": min_train,
        "n_scored": len(records),
        "first_scored": records[0]["d"], "last_scored": records[-1]["d"],
        "metrics": evaluate(a, f),
        "by_condition": evaluate_by_condition(records),
        "records": records,
    }


def bootstrap_ci(records: List[Dict[str, Any]], alpha: float = 0.10,
                 n_boot: int = 5000, seed: int = 0) -> Optional[Dict[str, Any]]:
    """
    A confidence interval on the score, because a point estimate from a few
    dozen days is not a number anyone should sign a contract against.
    """
    from app.forecast.metrics import forecast_score
    if len(records) < 5:
        return None
    a = np.array([r["actual"] for r in records], dtype=float)
    f = np.array([r["forecast"] for r in records], dtype=float)
    rng = np.random.default_rng(seed)
    vals = []
    for _ in range(n_boot):
        idx = rng.integers(0, len(a), len(a))
        s = forecast_score(a[idx], f[idx])
        if s is not None:
            vals.append(s)
    if not vals:
        return None
    v = np.array(vals)
    return {
        "lower_pct": float(np.percentile(v, 100 * alpha / 2)),
        "upper_pct": float(np.percentile(v, 100 * (1 - alpha / 2))),
        "confidence_pct": (1 - alpha) * 100,
        "n_boot": n_boot, "seed": seed,
        "p_meets_83": float(np.mean(v >= 83.0)),
    }
