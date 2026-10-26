# Generation Forecasting & Deviation Detection

Day-ahead prediction of plant energy, scored on a held-out period, with
deviation detection against what actually happened.

Everything below was measured on the Kalyon Niğde archive on the date this was
written. **No figure here is a target, an estimate or a projection.**

---

## 0. ACCEPTANCE RESULT (pre-registered, 2026-10-19)

Scored once against `docs/forecast-prereg.md`, which was written before the run.

| | |
|---|---|
| Model | **Irradiance Ratio (Ridge)** — pre-registered |
| Method | Day-ahead walk-forward, rolling 60-day training window |
| Scored period | 2024-03-18 → 2024-05-20 |
| **Sample** | **63 days** (≥60 required ✔) |
| **SCORE** | **70.0%** |
| WAPE / MAE / RMSE / bias | 30.0% / 201 MWh / 274 MWh / +6 MWh |
| 90% confidence interval | 63.6% – 75.5% |
| P(score ≥ 83%) | **0%** |
| **Target 83% — MET** | **NO** |

Comparison on identical dates (reported only; never used for selection):

| Model | Score | WAPE | MAE |
|---|---|---|---|
| Ridge Regression | 72.1% | 27.9% | 188 |
| Random Forest | 69.2% | 30.8% | 207 |
| **Irradiance Ratio (Ridge)** — pre-registered | **70.0%** | 30.0% | 201 |
| Gradient Boosting | 68.0% | 32.0% | 215 |
| Persistence (yesterday) | 62.3% | 37.7% | 254 |
| Seasonal Naive (lag-7) | 40.5% | 59.5% | 400 |

**Why this is lower than the 82.9% reported earlier.** That figure came from a
19-day test block falling entirely in **May** — the sunniest, most predictable
stretch of the archive. Extending to the agreed 63-day sample brings in March
and April, including the five-day April curtailment shutdown and far more
variable weather. The model did not get worse; **the earlier window was
flattering**, which is precisely the risk the ≥60-day requirement exists to
remove. It is a concrete demonstration that a 19-day acceptance test would have
been unsafe.

**The pre-registered model placed third.** Plain Ridge scored 72.1%. That is
reported and not acted upon: switching models after seeing acceptance results is
the tuning the brief forbids. Any change requires a fresh pre-registration and a
new run.

**Status: `LIMITED`.** The dashboard will not show `VALIDATED` at 70.0%.

---

## 1. Earlier exploratory results

| | |
|---|---|
| Agreed target | **85%** score (WAPE below 15%) |
| **Measured held-out score** | **69.3%** (WAPE 30.7%) |
| **Target met** | **No** |
| Selected model | **Persistence (yesterday)** |
| Test window | 2024-05-02 → 2024-05-20, 19 days, never used for training |
| MAE / RMSE | 254 MWh / 350 MWh |

The 85% target is **not met**, and the application says so everywhere it shows a
score. The reliability banner reads `LIMITED`, not `VALIDATED`, and it is driven
by the measured number rather than set by hand.

### Why, specifically

1. **There is no weather-forecast feed.** The database holds measured history
   only. Day-to-day generation is driven by cloud cover, and no amount of past
   generation anticipates tomorrow's clouds. This is the single largest factor.
2. **Without weather, every machine-learning model lost to a one-line baseline.**
   On day-ahead validation, "the same as yesterday" scored 67.1% while Ridge
   managed 56.8% and Random Forest 53.5%. Given weather, this reverses:
   Gradient Boosting wins the ceiling test at 91.2%. The models are not the
   problem — the missing input is.
3. **The archive is short.** 100 complete days, two complete calendar months,
   ending 21 May 2024. No seasonal cycle is observable.
4. **Accuracy collapses on the days that matter.** Scored by measured
   condition: high irradiance 75.4%, moderate 79.0%, **low irradiance 21.2%**,
   curtailed 0.0%.

### What would move it

| Change | Why |
|---|---|
| A numerical weather-prediction feed | **The decisive lever.** A ceiling test replacing the forecast with *measured* weather reached **91.2%** (Gradient Boosting), against 69.3% without. The target is therefore reachable in principle — but 91.2% assumes a *perfect* forecast. A real feed carries its own error, and how much of the 22-point gain survives it cannot be measured from this database. |
| ≥ 12 months of history | Lets a model learn a seasonal cycle instead of a 3-month slice. |
| Curtailment as a known input | Curtailed days currently score 0%. A dispatch schedule would move them out of "unpredictable". |
| Agreeing the score granularity | See §6. |

---

## 2. How it works

```
PPC + WMS  ──►  daily frame  ──►  chronological validation  ──►  model selection
   (SQL)        (features.py)        (backtest.py)                (models.py)
                                           │
                                           ▼
                            held-out test  ──►  score, MAE, RMSE, WAPE
                                           │
                                           ▼
                       forward prediction + conformal interval
                                           │
                                           ▼
                    stored  ──►  reconciled against actuals  ──►  deviations
                  (forecast_runs / forecast_points)            (service.py)
```

### Leakage control

* Every plant feature is **lagged at least one day**. `lag1` is yesterday's
  energy, never today's. There is an automated test asserting this.
* **Measured weather is never a model input.** It is used for reporting and for
  labelling conditions only. `weather.observed_as_forecast()` exists purely to
  measure the ceiling a perfect forecast would reach, is flagged `leakage=True`,
  and its result is never saved.
* The **test block is held out of model selection**. Candidates are compared on
  validation folds; the winner is scored once on the test block.

---

## 3. Daily energy — how it is calculated

```sql
-- one row per minute first: duplicates averaged, never summed twice
WITH minute_slot AS (
    SELECT DATEADD(minute, DATEDIFF(minute, 0, TimeCol), 0) AS ts,
           AVG(GRID_ACTIVE_POWER_MEASURED) AS mw
    FROM PPC GROUP BY DATEADD(minute, DATEDIFF(minute, 0, TimeCol), 0)
)
SELECT CAST(ts AS DATE) AS d, SUM(mw) / 60.0 AS energy_net_mwh
FROM minute_slot GROUP BY CAST(ts AS DATE)
HAVING COUNT(*) >= 1380;
```

Three decisions are embedded here, each from a measured property of the archive:

* **De-duplication.** PPC carries 278 duplicate timestamps and roughly 7,000
  surplus rows from change-of-value logging. Summing raw rows double-counts
  those minutes. Collapsing to minute slots first lowers the archive total from
  65,605 MWh to **62,006 MWh** — the earlier figure was inflated by about 4.6%.
* **Net vs gross.** The query above is net export and goes negative at night
  (81,921 minutes at about −0.5 MW, −711 MWh over the archive). `energy_gross_mwh`
  clips at zero. The target is configurable; the default is net.
* **UTC grouping.** `TimeCol` is UTC and the site is UTC+3 — proven by the
  production register resetting at 21:00 UTC and output peaking at 08:00 UTC.
  Generation falls inside one UTC date, so daily grouping never splits a solar
  day. `LocalCol` is **not** used: it carries +05:30 on PPC and +03:00 on WMS.

---

## 4. Metrics

| Metric | Definition |
|---|---|
| **WAPE** | `Σ\|actual − forecast\| ÷ Σactual × 100` |
| **Forecast score** | `100 − WAPE`, floored at 0 |
| MAE / RMSE | mean absolute error / root mean square error, MWh |
| Bias | mean signed error; positive = over-forecasting |
| MAPE | **suppressed** when any day is ≤ 10 MWh, with the reason stated |

WAPE rather than MAPE because generation legitimately approaches zero on a
curtailed or overcast day, and a percentage against a near-zero actual explodes
without being informative.

---

## 5. Deviation detection

Once a predicted day completes, the actual is filled in and the deviation
classified:

| Status | Meaning |
|---|---|
| `OK` | Within both thresholds |
| `UNDER` / `OVER` | Beyond `FORECAST_DEVIATION_PCT` **and** `FORECAST_DEVIATION_MIN_MWH` |
| `UNRELIABLE` | The day was not fully recorded — never alerted on |

Both thresholds must be crossed, so a large percentage on a tiny day does not
raise an alert, and neither does a large MWh swing on a huge day.

**Evidence is measured; causes are hypotheses.** Each deviation carries
curtailment minutes, mean setpoint, inverter running time, producing minutes,
peak MW and irradiance — all measured. The hypotheses name the evidence behind
them and are phrased as possibilities, because this database holds no dispatch
log, work orders or outage records, so a cause cannot be confirmed from it.

Availability is compared against **the plant's own median day**, not against the
24-unit fleet count: `avg_inverters_running` is a mean over all 1,440 minutes
including night, so it sits near 13 on a healthy day. An absolute threshold
fired on every single day during development and was fixed.

---

## 6. The granularity question — unresolved

WAPE depends entirely on what it is aggregated over. Measured on this archive:

| Aggregation | Score |
|---|---|
| Per day | ~57–69% |
| Per week | ~77% |
| Whole period | ~98% |

A monthly or period-level score above 85% is reachable immediately, because
over- and under-predictions cancel — which makes it a weak measure of forecast
quality. **The granularity should be agreed in writing before 85% is committed
to.** The application reports the per-day figure, which is the strictest and the
one matching a day-ahead product.

---

## 7. Configuration

All optional. Defaults are safe and nothing reaches the network unless enabled.

| Variable | Default | Purpose |
|---|---|---|
| `FORECAST_TARGET_SCORE` | `85` | The agreed goal shown in the UI |
| `FORECAST_DEVIATION_PCT` | `15` | Percentage threshold for an alert |
| `FORECAST_DEVIATION_MIN_MWH` | `50` | Absolute threshold for an alert |
| `WEATHER_PROVIDER` | unset | `open-meteo` to enable; unset = disabled |
| `WEATHER_LAT` / `WEATHER_LON` | unset | Site coordinates, required if enabled |
| `WEATHER_API_KEY` | unset | Only for providers that need one |
| `WEATHER_TIMEOUT` | `15` | Seconds |

**No key is ever logged, returned by the API, or stored in the database.** The
status endpoint reports only whether one is present.

Enabling a provider informs the **forward prediction**. It does not improve the
historical score, because the database holds no historical forecast values to
train against — only measured weather, which must not be used as a feature.

---

## 8. API

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/forecast/status` | Coverage, weather configuration, last run |
| `POST /api/v1/forecast/run` | Train, validate, select, predict, persist (~10 s) |
| `GET /api/v1/forecast/latest` | Last run with leaderboard and breakdowns |
| `GET /api/v1/forecast/next-day` | Next prediction with interval and reliability |
| `GET /api/v1/forecast/history` | Actual vs predicted series |
| `GET /api/v1/forecast/deviations` | Deviations with evidence and hypotheses |
| `POST /api/v1/forecast/reconcile` | Fill actuals for completed days |

`POST /run` accepts `{"ceiling_test": true}` to measure the upper bound a
perfect weather feed would reach — **91.2% on this archive**. That result leaks
future information by design, is labelled `leakage_ceiling_test: true`, returns
a warning string, and is never saved. It is a planning number, not a result.

---

## 9. Files

**New** — `app/forecast/{__init__,features,metrics,models,backtest,weather,service}.py`,
`app/routers/forecast.py`, `app/models/forecasts.py`,
`Frontend/src/pages/Forecast/Forecast.jsx`, `Backend/tests/test_forecast.py`.

**Modified** — `app/main.py` (one router line), `app/database/base.py` (one model
import), `requirements.txt` (numpy, scikit-learn),
`Frontend/src/{routes/AppRoutes.jsx,components/Sidebar/Sidebar.jsx,services/api.js}`.

**Two new tables**, created by `Base.metadata.create_all` like the existing
ones: `forecast_runs`, `forecast_points`. No existing table is read
destructively or altered.

---

## 10. Tests

```bash
cd Backend
venv\Scripts\python.exe tests\test_forecast.py   # 44 checks
venv\Scripts\python.exe tests\run_tests.py       # 28 existing checks
```

The forecast suite covers the WAPE/score arithmetic, MAPE suppression near
zero, the score floor, **an explicit no-leakage assertion on the feature
frame**, deviation classification and thresholds, the false-availability
regression, the weather provider being off and silent by default, and an
end-to-end read-only run against the database. It skips the database group with
a clear message if SQL Server is unreachable.

---

## 11. Limitations

* 100 complete days; 58 with usable weather; 93 supervised rows.
* No weather forecast, historical or live.
* Archive ends 21 May 2024, so "tomorrow" means the day after the last record.
* No dispatch log, work orders or outage records, so deviation causes remain
  hypotheses.
* XGBoost is not installed and is not used; on ~93 rows it would fit noise.
* Forward predictions beyond day 1 compound their own error; the interval is
  widened by √step to reflect that, and per-horizon accuracy is shown.
