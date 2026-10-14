# AI Assurance Layer — technical gap analysis

**Status:** pre-development assessment. No code written.
**Scope:** compare the client's reference forensic-audit methodology against what the
Kalyon installation can actually support today.
**Basis:** direct inspection of the live SQL Server database (90 tables) and the
existing FastAPI/React codebase on 30 Sep 2026.

Nothing in this document is taken from the reference report. Every number below was
measured from our own database.

---

## 1. The single most important finding

The reference report analyses **two full calendar years** (CY2024–CY2025) and derives
year-on-year degradation, seasonal matrices and an annual energy account.

Our database holds **a static historical snapshot of roughly 3.5 months**, and the
sources do not overlap for most of it:

| Source | First record | Last record | Rows |
|---|---|---|---|
| `INVERTER_01`…`_24` | 2024-02-08 10:20 | 2024-05-21 11:01 | 154,546 each |
| `PPC`, `PPC_TREND` | 2024-02-08 09:55 | 2024-05-21 11:01 | 154,564 |
| `Alarms` | 2024-02-08 08:50 | 2024-05-21 10:15 | 193,593 |
| `INVERTER_DAILY_GEN` | 2024-02-08 09:55 | 2024-05-21 11:01 | 147,901 |
| **`WMS`** (irradiance) | **2024-03-23 10:35** | 2024-05-21 11:01 | 90,141 |
| `POWER_VS_IRRADIANCE` | 2024-03-25 11:10 | 2024-05-21 11:01 | 87,262 |
| **`INVERTER_TEMP`** | **2024-04-20 09:48** | 2024-05-21 11:01 | 50,108 |
| **`TRACKER_MBOX_STATUS`** | **2024-04-20 10:28** | 2024-05-21 11:01 | 50,094 |

Two consequences that shape the whole design:

1. **Any analysis needing irradiance is valid only from 23 Mar 2024** — about
   **two months**, not 3.5. Performance ratio, loss attribution and weather correction
   cannot be computed for Feb–22 Mar at all.
2. **Anything needing tracker or cabinet-temperature context is valid only from
   20 Apr 2024** — about **one month**.

The data also **ends 21 May 2024**, over two years before today. This is an archive,
not a live feed. The AI report must therefore be framed as an analysis of a fixed
historical window, and the UI must state that window prominently rather than implying
current plant condition.

Sampling is **1-minute** (verified: 1,440 rows in 24 h for `INVERTER_01`), which is
excellent resolution for forensic work.

---

## 2. What the data supports — verdict by analysis

### 2.1 AVAILABLE with current data

These can be built now, deterministically, with no new inputs.

| Analysis | Evidence found in our DB |
|---|---|
| **Data-integrity / trust audit** | 1-min cadence; frozen-value detection is clean — over May 2024 only `0.0` repeats en masse (15,626×, i.e. night), while real values repeat ≤3×, so a latched sensor stands out unambiguously. Cadence gaps, duplicate rows and cross-table disagreement are all computable. |
| **Availability & downtime** | `PPC.INVERTER_RUNNING`, per-inverter run/downtime counters (`DAILY_RUN_MIN`, `DOWN_TIME_MIN`, `GRID_OUTAGE_TIME_MIN`, `SLEEP_TIME_MIN`, `EXT_STOP_TIME_MIN`), plus 193,593 alarm rows with ON/OFF pairing and duration. |
| **Curtailment quantification** | `PPC.ACTIVE_POWER_SET_POINT` moves across 55 distinct values; **17,295 minutes (~288 h)** run at or within 2 % of the ceiling. Energy foregone is computable against the unconstrained estimate. |
| **Clipping analysis** | Per-inverter AC/DC at 1 min; observed ceiling ≈ 4,703 kW AC vs 4,988 kW DC peak on `INVERTER_01`. The reference's S-curve and meter-minute histogram are reproducible. |
| **Per-inverter and per-string outlier ranking** | 24 inverters × **21 string-current channels** = 504 channels at 1 min. Chronic-offender ranking and IV-shape anomalies are well supported. |
| **Temperature effect** | Measured module temperature from 4 stations × 6 sensors, plus `ALL_WMS_AVG_MODULE_TEMP` (range −8.7 … 76.4 °C). Thermal behaviour is observable. |
| **Soiling** | **Measured, not assumed** — `AVG_IR_SOILING_RATIO1/2` (mean 99.05, i.e. ≈1 % loss) and `AVG_IR_TRANSMISSION_LOSS1/2` (mean 2.17 %), fully populated across all 90,141 WMS rows. |
| **Multi-station sensor cross-comparison and drift** | Four independent stations WMS1–WMS4, each with GHI, GTI, module temp, wind, humidity, pressure. This is the direct analogue of the reference's three-station drift study. |
| **GHI vs GTI transposition consistency** | Both measured on every station; internal plane-vs-horizontal coherence is checkable. |
| **AC-chain / metering-boundary loss** | `INVERTER_TOTAL_ACTIVE_POWER` vs `GRID_ACTIVE_POWER_MEASURED` on the same timestamps. |
| **Performance ratio** | Measured irradiance + nameplate from config (`INSTALLED_CAPACITY_MW`, default 130). |
| **Alarm → loss correlation** | Alarms are dominated by per-inverter DC faults (`INVERTER 16 DC` 10,380; `14` 9,668; `18` 9,497 …) plus tracker motor faults — ideal for chronic-offender and root-cause work. |
| **Tracker health** | `TRACKER_MBOX_STATUS` (107 cols: per-block alarm, battery, wind speed/direction) + tracker alarms. |

### 2.2 PARTIALLY available

Buildable, but the conclusion must be narrower than the reference report's.

| Analysis | What we can do | What is missing |
|---|---|---|
| **Energy account / loss bridge** | A bridge from a **measured** expectation (irradiance- and temperature-corrected) to delivered energy, decomposing curtailment, clipping, availability, soiling and residual. | No P50, no design model, no owner's budget — so it cannot be *"vs P50"*. It is a measured-expectation bridge, and must be labelled as such. |
| **Monthly performance gap** | Month-by-month for Mar–May 2024 (3 partial months with irradiance). | No seasonality, no monsoon/winter months, no year-on-year baseline. |
| **Timestamp-convention analysis** | Internal cadence, alignment between tables, and start-vs-centre labelling can be tested across our own sources. | No independent reference (satellite) to prove an absolute half-hour shift the way the reference report did. |
| **Module / cohort analysis** | Group by inverter and rank shortfall per kWp; string-level dispersion. | **No module supplier, serial, Wp, cohort or warranty metadata anywhere** — the `equipment` table holds a single placeholder row (`INV-001`, `Offline`). Cannot attribute to a manufacturer cohort or support a warranty claim. |
| **Per-inverter nameplate** | Only plant-level 130 MW exists, in config. Per-inverter rating can be *inferred* from observed peak. | Inferred values must be flagged as inferred, never presented as design data. |

### 2.3 REQUIRES ADDITIONAL DATA

Nothing in the database supports these. Each is a concrete ask.

| Missing input | Unlocks |
|---|---|
| PVsyst / P50 design yield (monthly) | "vs P50" energy account, design-vs-actual bridge |
| Owner's operating budget | Budget variance |
| **Tariff / PPA terms** | **Every ₹ figure** — currently no rate column exists anywhere |
| Satellite irradiance feed | Satellite-vs-ground validation, independent transposition truth |
| Pyranometer calibration certificates / travelling-standard record | Turns station drift from *observed* into *adjudicated* |
| Module datasheet (Wp, temperature coefficient, degradation warranty) | Temperature-corrected expectation, warranty position |
| Grid dispatch instructions | Classifies curtailment as ordered vs plant-side |
| Maintenance, cleaning and work-order logs | Closes the finding → action loop; validates soiling programme |
| Commissioning date, SLD, metering scheme, CT/PT ratios | Metering-boundary adjudication |
| ABT settlement register | Settlement-vs-telemetry reconciliation |

### 2.4 NOT POSSIBLE with current data

Not a data-quality issue — the observation window is too short or the domain is absent.

| Analysis | Why |
|---|---|
| Degradation %/yr | Needs ≥1 year, ideally 2. We have ~2 months of irradiance-backed data. |
| Year-on-year comparison | Single partial year. |
| Seasonal deviation matrix | Feb–May only; no monsoon, no winter. |
| Annual energy account | No complete year. |
| Any ₹ / NPV / financial impact | No tariff, no capex, no budget. |
| Satellite-vs-ground validation | No satellite feed. |

---

## 3. Existing capability we would build on

The application already has the deterministic foundations this layer needs:

- **Query/aggregation engine** — `reports_repository` with whitelisted identifiers,
  interval bucketing and `fetchmany` batching.
- **An analysis service** — `analytics_service` already derives PR, peak MW and
  inverter comparisons.
- **Report builders** — Excel (xlsxwriter, constant-memory) and PDF, with vector
  charting already implemented for Analytics.
- **Background job infrastructure** — `export_jobs` with progress and polling, already
  used by exports and Generate & Send. A forensic run over 1-min data for 24 inverters
  will take minutes, so it must reuse this rather than run in a request.
- **Scheduling and delivery** — schedules, recipients, quota, run history.

What does **not** exist and would be new: an evidence model, hypothesis/confidence
handling, independent re-derivation, and the finding → work-order structure.

---

## 4. Proposed architecture (for discussion, not yet built)

The client's constraint — *the LLM must not compute engineering numbers* — maps to a
strict two-stage design.

**Stage 1 — Evidence engine (Python/SQL, deterministic, no LLM).**
Each analysis is an independent module that declares its required inputs up front and
self-reports `AVAILABLE` / `PARTIAL` / `BLOCKED` before running. Every finding it emits
carries full provenance:

```
finding_id, title, window_start, window_end,
source_tables[], source_columns[], row_counts,
formula (human-readable + code reference),
inputs{}, result{}, units,
independent_derivations[]        # ≥2 paths to the same number where possible
supporting_evidence[], contradicting_evidence[],
confidence (with the reason for it),
verification_status, recoverable | structural,
required_data[]                  # populated when PARTIAL/BLOCKED
```

**Stage 2 — Narration (LLM).**
Receives the evidence package as its only input and converts it to engineering prose.
It must be constrained so it cannot introduce a number that is not already in the
package — a post-generation check should reject any numeric token absent from the
evidence. Hard rule: **no input, no finding.** Where a module reports `BLOCKED`, the
report prints *"Additional data required"* and names the ask.

**Traceability.** "Why did the AI reach this conclusion?" is answered from the evidence
package, not from the model — table, columns, window, formula, inputs, both sides of
the evidence, and the confidence basis.

---

## 5. Recommended sequencing

1. **Confirm the data window and the ₹ question.** Is the AI report meant to analyse
   this 2024 archive, or is a live feed being restored? And is a tariff available? These
   two answers change the scope more than anything else here.
2. **Phase 1 — data-integrity and trust report.** Our strongest area, needs no new
   inputs, and is exactly the reference's most defensible section.
3. **Phase 2 — measured-expectation loss bridge** (curtailment, clipping, availability,
   soiling, temperature, residual) over 23 Mar – 21 May 2024.
4. **Phase 3 — per-inverter/string chronic-offender ranking and alarm correlation.**
5. **Phase 4 — sensor-estate audit** (4-station cross-comparison and drift).
6. **Phase 5 — narration, confidence and work-order mapping.**
7. **Phase 6 — anything requiring the external data in §2.3**, gated on receiving it.

---

## 6. Honest caveats to carry into the client conversation

- The reference report's headline shape — *"−36.3 GWh vs P50 over two years, ₹X Cr at
  stake"* — **is not reproducible here** without a design model and a tariff. We can
  produce a rigorous *measured* loss decomposition, which is a different and more
  defensible claim.
- Our usable multi-source window is **~2 months**, against the reference's 24.
- Several reference findings (degradation, cohort warranty, satellite bias) depend on
  data classes we do not hold at all.
- What we *can* do unusually well, because the plant is densely instrumented: 1-minute
  resolution, 504 string channels, four weather stations, and **measured** soiling —
  the reference report had to infer soiling from a literature allowance.
