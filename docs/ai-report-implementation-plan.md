# AI Forensic Report + Predictive Analysis — implementation plan

**Status:** plan only, following re-inspection on 01 Oct 2026. Phase 1 and part of
Phase 2 already exist from the previous round (see §7).
**Rule throughout:** every number comes from the Kalyon database or from a labelled
model prediction. Nothing is copied from the reference report.

---

## 1. Five constraints found during re-inspection

These shape the plan more than anything in the requirement list.

### 1.1 There is no numeric stack installed

`numpy`, `pandas`, `scipy`, `scikit-learn`, `statsmodels`, `xgboost`, `lightgbm`,
`prophet` — **all missing**, and none are declared in `requirements.txt`. The backend
today is pure SQLAlchemy + FastAPI.

Forecasting cannot be built without adding dependencies. Recommended minimum:

| Package | Why |
|---|---|
| `numpy`, `pandas` | the whole pipeline |
| `scikit-learn` | gradient boosting, random forest, metrics, conformal residuals |
| `statsmodels` | SARIMAX, seasonal decomposition |

`lightgbm` optional (a wheel, installs cleanly). **Prophet is not recommended** — it
pulls a Stan toolchain, is heavy to deploy on this Windows/service setup, and with
the history available here it would be fitting seasonality that the data cannot
support anyway. `scikit-learn` + `statsmodels` covers every candidate model that is
defensible on this dataset.

### 1.2 The archive holds 100 complete days, and only 2 complete months

Measured from `PPC`:

| | |
|---|---|
| Calendar days present | 104 |
| Days with ≥1380 of 1440 minutes | **100** |
| Span | 08 Feb 2024 → 21 May 2024 |
| Complete calendar months | **2** (Mar: 29 days, Apr: 30 days) |
| Partial months | Feb (21 days), May (20 days) |

### 1.3 Units are mixed between tables — verified, not assumed

| Source | Unit | Evidence |
|---|---|---|
| `PPC.GRID_ACTIVE_POWER_MEASURED` | **MW** | peaks ~102 on a 130 MW plant |
| `INVERTER_xx.ACTIVE_POWER` | **kW** | peaks ~4,700; 24 × 4,209 kW = 101 MW ≈ PPC's 100.17 MW at the same timestamp |
| `PPC.PLANT_DAILY_PRODUCTION` | **MWh** | ~1,002 vs Σ(MW)/60 = 926 MWh the same day |

Any energy analysis must normalise to one unit at the boundary or be wrong by 1000×.
This is encoded as a conversion layer, not left to each module.

### 1.4 Two independent energy measures disagree by ~8 %

For 01–07 Apr, `PLANT_DAILY_PRODUCTION` reads 1,002 MWh where integrating
`GRID_ACTIVE_POWER_MEASURED` gives 926 MWh. That gap is a **finding to adjudicate**
(metering boundary, register reset timing, or integration method), not an error to
paper over. It becomes the first worked example of the two-method verification model.

### 1.5 Source windows still differ

`WMS` from 23 Mar (≈60 days), `INVERTER_TEMP` and `TRACKER_MBOX_STATUS` from 20 Apr
(≈31 days). Anything combining irradiance with generation is limited to ~60 days;
anything needing tracker or cabinet temperature to ~31.

---

## 2. Forecast feasibility — the honest verdict

The requirement asks for a 12-month forecast and proposes backtesting on
Jan–Aug / Sep–Oct / Nov–Dec. **That split does not exist in this archive** — there is
no data after 21 May. The plan therefore derives the splits from the data, and refuses
targets the data cannot carry.

| Target | Verdict | Basis |
|---|---|---|
| **Intraday plant power, 24–48 h** | **STRONG** | ~148k 1-min samples; irradiance, module temp, wind as features |
| **Daily generation, 7–14 day horizon** | **FEASIBLE** | 100 complete days → rolling-origin backtest ≈ 70 train / 15 validate / 15 test |
| **Per-inverter daily generation** | **FEASIBLE** | 24 series × 100 days, pooled model with unit as a feature |
| **Curtailment risk (daily probability)** | **FEASIBLE, weak** | 27 affected days, 12,991 limited minutes — enough for a base rate, not for causal modelling |
| **Availability trend** | **FEASIBLE as trend** | descriptive, not a forecast |
| **Irradiance** | **WEAK** | 60 days only; short horizon at best |
| **Monthly generation** | **INSUFFICIENT HISTORICAL DATA** | 2 complete months cannot fit or validate a seasonal model |
| **Next-12-month / annual forecast** | **INSUFFICIENT HISTORICAL DATA** | Feb–May is one rising-into-summer segment; projecting it across an unobserved autumn and winter is extrapolation, not forecasting |

**This is the point to agree before I build.** I will implement the full forecasting
framework — backtesting, model comparison, conformal intervals — and it will produce
real forecasts for the horizons above. For the annual target it will render:

> **Forecast: Unavailable** — Reason: insufficient historical observations
> (100 days, 2 complete months). Required: at least one full seasonal cycle.

That is §19 and §28 applied literally. Producing a 12-month number from this archive
would be the "fake prediction" the brief forbids.

---

## 3. Architecture

Extends what already exists; no parallel system.

```
SQL (Kalyon)
   ↓  app/ai/evidence.py            inventory, cadence detection, unit normalisation
Deterministic analytics             app/analytics/*.py   (one module per domain)
   ↓  ModuleResult: AVAILABLE | PARTIAL | BLOCKED
Hypothesis engine                   app/ai/hypotheses.py (rules + statistics only)
   ↓
Verification engine                 app/ai/verification.py (CONVERGED/DIVERGED/SINGLE)
   ↓
Confidence model                    app/ai/confidence.py (computed, never chosen)
   ↓
Forecast engine                     app/forecast/*.py    (backtest → select → interval)
   ↓
EvidencePackage                     app/ai/schemas.py    (already built)
   ↓
LLM narration                       app/ai/llm_client.py + numeric validator
   ↓
UI / PDF / Excel
```

The LLM receives the package and returns prose. A post-generation validator rejects
any numeric token not present in the package, so "the model must not calculate" is
enforced mechanically rather than by instruction.

---

## 4. Forecasting method

**Features** (all from the database): lagged generation (1, 2, 7 days), rolling means,
day-of-year, month, day-of-week, GHI/GTI, module and air temperature, wind speed,
soiling ratio, inverter availability, curtailed minutes.

**Candidate models, compared on this dataset — not chosen by reputation:**

1. Seasonal naive (retained as the baseline it must beat)
2. SARIMAX (`statsmodels`)
3. Gradient boosting (`scikit-learn`)
4. Random forest (comparison baseline)
5. Ridge on lag features (a linear control)

**Backtesting** is rolling-origin, chronological, with splits computed from the actual
coverage — never a random split and never a hardcoded month. Metrics: MAE, RMSE,
MAPE (only where no actual is near zero, otherwise suppressed as undefined), R²,
and forecast bias. Every model's score is reported, including the losers.

**Uncertainty:** **split-conformal prediction intervals** as the primary method —
distribution-free, valid for any underlying model, and directly interpretable as the
coverage level requested. Bootstrap residual intervals as a cross-check. Both are
labelled a *prediction interval*, never a guarantee.

**Reliability gate:** if validation error exceeds a configured threshold, or coverage
is below target, the report states `Forecast reliability: Limited` with the measured
reason attached.

---

## 5. Module build order

Mapped onto the ten phases requested.

| Phase | Deliverable | Notes |
|---|---|---|
| 1 | AI Report UI + sidebar | **done** |
| 2a | Evidence engine, inventory, data integrity | **done** |
| 2b | Unit normalisation layer | new — §1.3 |
| 2c | Energy analysis + bridge | only components the data supports |
| 2d | Inverter fleet, alarms, curtailment, clipping, temperature, WMS, tracker | one module each |
| 3 | Hypothesis engine | deterministic rules over module outputs |
| 4 | Forecast framework + feature builder | needs §1.1 dependencies |
| 5 | Backtesting + model comparison | rolling origin, all metrics reported |
| 6 | Conformal prediction intervals | plus reliability gate |
| 7 | LLM narration + numeric validator | degrades gracefully when the LLM is unavailable |
| 8 | Charts | Recharts, already a dependency |
| 9 | PDF / Excel export | reuse `pdfReport`/`report_excel` |
| 10 | Production testing | extend `Backend/tests/run_tests.py` |

Energy bridge components will be emitted **only** where evidence exists:
curtailment (setpoint), clipping (plateau at the inverter ceiling), downtime
(availability), soiling (measured ratio), temperature (measured module temp).
Anything else renders as *"Not available with current data"*.

---

## 6. Financial handling

No tariff exists anywhere in the database or config. Every energy figure carries
`financial_impact: null` with reason *"Not available — tariff not configured."* A
single configurable setting (`TARIFF_PER_KWH`, alongside `INSTALLED_CAPACITY_MW`)
will be added so that when a rate is supplied, impact is computed deterministically as
`energy × tariff`. No default, no placeholder rate.

---

## 7. Already built (previous round)

- `app/ai/schemas.py` — evidence, findings, derivations, confidence, data asks
- `app/ai/evidence.py` — inventory, detected cadence, window clamping, caching
- `app/ai/orchestrator.py` — module gating, package assembly, structural limitations
- `app/analytics/data_quality.py` — coverage, continuity, duplicates, frozen channels,
  range violations, peer adjudication
- `app/routers/ai_report.py` — preflight / start / status on `export_jobs`
- `Frontend/src/pages/AIReport/AIReport.jsx` — page, sections, progress, provenance

## 8. Reused, not rebuilt

`reports_repository` (whitelisted queries, interval bucketing), `analytics_service`
(PR, peak MW), `export_jobs` (background jobs with progress), `report_excel` and
`pdfReport` (exports), Recharts (charts), the shared `Dialog`/`ConfirmDialog`
components, and the existing scheduling and delivery stack.

---

## 9. Decisions needed before Phase 4

1. **Approve adding `numpy`, `pandas`, `scikit-learn`, `statsmodels`** to
   `requirements.txt` (§1.1). Nothing predictive can be built otherwise.
2. **Confirm the forecast scope** of §2: short-horizon forecasts that the data
   supports, with the annual target reported as insufficient — rather than a
   12-month number the archive cannot justify.
3. **Confirm the ~8 % energy discrepancy** (§1.4) should be raised as a finding for
   the client rather than silently resolved to one source.
