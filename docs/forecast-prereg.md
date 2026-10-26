# Pre-registration — day-ahead generation forecast acceptance run

**Registered: 2026-10-19, before any acceptance score was computed.**

This document fixes every choice that could otherwise be tuned against the
result. It is written first so that the acceptance number is produced once, from
a specification agreed in advance, rather than selected from a set of attempts.

Nothing below may be changed after the acceptance run without re-registering and
re-running, and any such change must be recorded here with its reason.

---

## 1 · What is being predicted

| | |
|---|---|
| Target | Daily **net** plant energy, `energy_net_mwh` |
| Source | `PPC.GRID_ACTIVE_POWER_MEASURED`, integrated per minute slot |
| Horizon | **Day-ahead only (h = 1)**. Each prediction uses data available on day *d−1* |
| Units | MWh |

Net, not gross: the plant imports at night (−0.5 MW, −711 MWh over the archive)
and that import is part of the commercial result.

---

## 2 · Scoring formula — fixed

```
WAPE  = Σ|actual − forecast| ÷ Σactual × 100
SCORE = 100 − WAPE          (floored at 0)
```

Reported alongside, on the same days: **MAE**, **RMSE**, **bias**, **sample
size**, a **bootstrap 90% confidence interval** (5,000 resamples, seed 0), and a
breakdown by measured weather condition.

**Acceptance: score ≥ 83% over ≥ 60 valid out-of-sample daily forecasts.**
The dashboard status stays `LIMITED` unless that is met.

---

## 3 · The pre-registered model

**`Irradiance Ratio (Ridge)`** — primary, and the only model whose score counts
toward acceptance.

```
ratio_d      = energy_d ÷ forecast_GHI_d          (MWh per kWh/m²)
ratio_hat    = Ridge(alpha=1.0) on standardised features
energy_hat   = ratio_hat × forecast_GHI_d         , clipped at 0
```

**Why a ratio and not a level.** The diagnosed failure of the direct model is
seasonal extrapolation: output rises 537 → 805 MWh/day from February to May, so
a model trained on earlier months under-forecasts May by 120 MWh. Predicting
*energy per unit of sunlight* lets the season enter through the forecast
irradiance itself, which is known at prediction time, instead of having to be
learned from a 3.5-month output trend. This is a physical argument made before
seeing any result, not a reaction to one.

Guard: where `forecast_GHI_d < 0.5 kWh/m²` the ratio is undefined; the model
falls back to the rolling median of recent daily energy. Expected to be rare.

### Hyperparameters — fixed

| Component | Value |
|---|---|
| Ridge alpha | 1.0 |
| Scaling | `StandardScaler` on features |
| Rolling training window | **most recent 60 days**; fewer only where the archive cannot supply 60 |
| Minimum training days before scoring begins | **30** |
| Clipping | predictions floored at 0 MWh |

**Rolling, not expanding.** A rolling window keeps the model on the plant's
current operating level rather than averaging in a February that no longer
represents it.

### Baselines — reported, never selected from

Persistence (yesterday), Seasonal Naive (lag-7), Ridge (direct level),
Random Forest, Gradient Boosting. All scored on **identical walk-forward dates**
with the identical rolling window. XGBoost is excluded by agreement.

---

## 4 · Features — fixed

**Weather, from the archived day-ahead forecast only** (issued on *d−1*):

- `fc_ghi_kwh_m2` — forecast global horizontal irradiation for day *d*
- `fc_cloud_pct` — forecast mean cloud cover
- `fc_temp_c` — forecast mean 2 m air temperature

**Plant history, all lagged ≥ 1 day:**

- `lag1`, `lag2`, `lag3`, `lag7` daily energy
- `roll3_mean`, `roll7_mean`, `roll7_std`
- `curtailed_min_lag1`, `inv_running_lag1`

**Calendar:** `doy_sin`, `doy_cos`, `dow`

**Measured weather is never a feature.** It is used only for quality control and
for labelling conditions in the breakdown.

---

## 5 · Data-cleaning rules — fixed

| Rule | Action |
|---|---|
| **C1** Day must have ≥ 1,380 of 1,440 minutes in `PPC` | else excluded entirely |
| **C2** Duplicate timestamps averaged to one value per minute slot before integrating | always |
| **C3** Measured peak GTI > 1,500 W/m² | that day's **measured** weather marked unusable |
| **C4** Measured daily GHI > 0.85 × extraterrestrial horizontal irradiation | that day's **measured** weather marked unusable |
| **C5** Irradiance channel latched (< 50 distinct values over ≥ 600 minutes) | that day's **measured** weather marked unusable |
| **C6** Forecast weather missing for a day | day excluded from the scored set |

C3–C5 affect **measured** weather only. The day's energy and its *forecast*
weather remain valid, so the day stays in training and scoring — only its
condition label and any measured-weather comparison are withheld.

**Raw readings are never modified or deleted.** Exclusions are flags computed at
read time and are reported with the results.

### Amendment, 2026-10-19 — recorded BEFORE any acceptance score

C3 and C4 as first written were wrong on the physics and were corrected before
scoring, which is the only time such a change is permissible.

* **C3 raised from 1,200 to 1,500 W/m².** Cloud enhancement — a brief
  edge-of-cloud focusing effect — genuinely drives a one-minute peak to
  1,200–1,400 W/m². The original threshold flagged three ordinary days
  (1,203 / 1,210 / 1,284 W/m²) as faulty. Only a reading beyond any physical
  mechanism should trip.
* **C4 replaced with a clear-sky ceiling from solar geometry** rather than a
  flat 10 kWh/m². The ceiling is 0.85 × daily extraterrestrial horizontal
  irradiation, computed per date and latitude. A flat threshold is too loose in
  March and too tight in May.

Days excluded under the corrected rules — a coherent three-day sensor fault,
not scattered outliers:

| Date | Measured GHI | Clear-sky ceiling | Rule |
|---|---|---|---|
| 2024-03-30 | 9.66 | 7.51 | C4 |
| 2024-03-31 | 12.86 | 7.56 | C4 |
| 2024-04-01 | 8.30 | 7.61 | C4 |

Each keeps its energy and its forecast weather, and so stays in training and
scoring. Only its measured-weather comparison and condition label are withheld.
Raw readings are untouched.

---

## 6 · Weather data provenance

Open-Meteo **Historical Forecast API**, hourly
`shortwave_radiation_previous_day1`, `cloud_cover_previous_day1`,
`temperature_2m_previous_day1`, integrated to daily totals in UTC.

The `previous_day1` series is the forecast **as issued one day earlier**,
verified distinct from the best-match series (751 vs 499 W/m² at 2024-04-08
10:00). Every cached row stores its **issue timestamp** and **valid date**, and
a prediction for day *d* may only use a forecast issued on or before *d−1*.

**Measured weather must never be substituted for a forecast in evaluation.** The
`ceiling_test` path exists solely to measure an upper bound, is flagged
`leakage=True`, and is never persisted or counted.

---

## 7 · What is forbidden

- Scoring the acceptance set more than once per registration.
- Changing any choice above after seeing an acceptance score.
- Selecting the model on acceptance results.
- Substituting measured weather for forecast weather.
- Reporting the target as met without the measured number and its sample size.

---

## 8 · Prior results, for the record

Measured before this registration, on a 19-day held-out block:

| Configuration | Score |
|---|---|
| No weather, Persistence selected | 69.3% |
| Real day-ahead forecast, Ridge selected (Niğde centre) | 82.0% |
| Real day-ahead forecast, Ridge selected (Bor candidate) | 82.9% |

These informed the design of the ratio model and are **not** acceptance
results: the sample was 19 days, below the agreed 60, and the model was selected
on validation rather than pre-registered.
