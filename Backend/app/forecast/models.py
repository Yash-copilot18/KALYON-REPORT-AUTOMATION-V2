"""
Candidate forecast models, and the baselines they have to beat.

The two baselines are not decoration. A machine-learning model that cannot beat
"the same as yesterday" has earned nothing, and on this archive that is a live
possibility rather than a rhetorical one - so both baselines are scored on
exactly the same folds as the fitted models and reported whether they flatter
the result or not.

XGBoost is deliberately absent: it is not installed in this environment, and on
roughly ninety training rows a gradient-boosted ensemble of that size would fit
noise. Gradient boosting from scikit-learn is included as the same family's
representative, kept small for the same reason.
"""

from __future__ import annotations

from typing import Any, Dict

import numpy as np
from sklearn.ensemble import GradientBoostingRegressor, RandomForestRegressor
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

# Index of the lag features inside the frame built by features.build_frame.
# Baselines read them directly rather than being fitted.
IDX_LAG1 = 0
IDX_LAG7 = 3

# The model named in docs/forecast-prereg.md. Its score is the one that counts
# toward acceptance; everything else is a reported comparison.
PREREGISTERED_MODEL = "Irradiance Ratio (Ridge)"


class Baseline:
    """A rule, not a model: no fitting, no parameters, nothing to overfit."""

    def __init__(self, name: str, column: int):
        self.name = name
        self.column = column

    def fit(self, X, y):                      # noqa: D102 - interface parity
        return self

    def predict(self, X):
        return np.asarray(X, dtype=float)[:, self.column]


class IrradianceRatio:
    """
    The pre-registered model: predict energy PER UNIT OF FORECAST SUNLIGHT,
    then multiply back by the forecast.

        ratio      = energy / forecast_GHI          (MWh per kWh/m2)
        ratio_hat  = Ridge(alpha=1.0) on scaled features
        energy_hat = ratio_hat * forecast_GHI       , floored at 0

    Why this and not a direct level model. Output on this plant rises 537 ->
    805 MWh/day across the archive, so a model trained on earlier months must
    extrapolate a season it has never seen and under-forecasts by ~120 MWh. A
    ratio carries the season in the forecast irradiance - which IS known at
    prediction time - instead of asking the model to learn a 3.5-month trend.

    Where the forecast irradiance is near zero the ratio is undefined; the
    model falls back to the mean of the training targets rather than dividing
    by something tiny and producing a wild number.
    """

    MIN_GHI = 0.5          # kWh/m2; below this the ratio is not meaningful

    def __init__(self, ghi_index: int, alpha: float = 1.0):
        self.ghi_index = ghi_index
        self.alpha = alpha
        self._model = None
        self._fallback = 0.0

    def fit(self, X, y):
        X = np.asarray(X, float); y = np.asarray(y, float)
        ghi = X[:, self.ghi_index]
        ok = ghi >= self.MIN_GHI
        self._fallback = float(np.mean(y)) if len(y) else 0.0
        if ok.sum() < 5:               # not enough sunlit days to fit a ratio
            self._model = None
            return self
        ratio = y[ok] / ghi[ok]
        self._model = make_pipeline(StandardScaler(), Ridge(alpha=self.alpha))
        self._model.fit(X[ok], ratio)
        return self

    def predict(self, X):
        X = np.asarray(X, float)
        ghi = X[:, self.ghi_index]
        if self._model is None:
            return np.full(len(X), self._fallback, dtype=float)
        ratio = np.asarray(self._model.predict(X), dtype=float)
        out = ratio * ghi
        # Where the forecast says there is almost no sun, the ratio form says
        # almost nothing; fall back rather than multiply by ~0.
        out = np.where(ghi >= self.MIN_GHI, out, self._fallback)
        return out


def candidates(ghi_index: int = None) -> Dict[str, Any]:
    """
    Every model scored, keyed by the name shown in the UI.

    Ridge is scaled because its penalty is scale-sensitive and the features mix
    MWh with sines and weekday numbers. The tree models need no scaling.
    Hyper-parameters are conservative on purpose: with under a hundred rows, the
    honest choice is a model that cannot memorise the training set.
    """
    out = {
        "Persistence (yesterday)": Baseline("Persistence (yesterday)", IDX_LAG1),
        "Seasonal Naive (lag-7)": Baseline("Seasonal Naive (lag-7)", IDX_LAG7),
        "Ridge Regression": make_pipeline(StandardScaler(), Ridge(alpha=1.0)),
        "Random Forest": RandomForestRegressor(
            n_estimators=300, min_samples_leaf=2, random_state=0, n_jobs=-1),
        "Gradient Boosting": GradientBoostingRegressor(
            n_estimators=300, max_depth=2, learning_rate=0.05, random_state=0),
    }
    # Only offered when forecast irradiance is actually a feature.
    if ghi_index is not None:
        out[PREREGISTERED_MODEL] = IrradianceRatio(ghi_index=ghi_index, alpha=1.0)
    return out


def is_baseline(name: str) -> bool:
    return name in ("Persistence (yesterday)", "Seasonal Naive (lag-7)")


def fit_model(name: str, X: np.ndarray, y: np.ndarray,
              ghi_index: int = None) -> Any:
    """Fit one named candidate on the data given and return it."""
    model = candidates(ghi_index)[name]
    model.fit(X, y)
    return model


def predict(model: Any, X: np.ndarray) -> np.ndarray:
    """
    Predict, with generation clipped at zero.

    A negative daily generation forecast is not physically meaningful: the plant
    can import overnight, but a whole day's net total below zero happens only
    during a shutdown, which no model should be predicting in advance.
    """
    p = np.asarray(model.predict(X), dtype=float)
    return np.maximum(p, 0.0)
