# app/services/analytics_service.py
"""
Analytics overview — every figure computed from real SQL Server telemetry.

One call assembles the whole Analytics page for a date range:
  • 6 plant KPIs (energy, PR, CUF, availability, peak power)
  • daily generation trend, monthly generation trend, plant PR trend
  • per-inverter generation + PR ranking (drives the top-10 chart and both tables)

Data model (verified against the live DB):
  PPC.PLANT_DAILY_PRODUCTION   – daily-reset MWh counter → a day's energy is its MAX
  PPC.GRID_ACTIVE_POWER_MEASURED – instantaneous plant power in MW
  WMS.AVG_GHI_CUMM_IRRADIATION – daily-reset kWh/m² insolation counter → day MAX = H
  INVERTER_DAILY_GEN.INVERTER_xx_GEN – daily-reset kWh counter per inverter → day MAX

Performance Ratio uses the plant's configured capacity as the reference, capped to
[0,100] — the same convention as the existing pr-irradiance endpoint. (If PR reads a
flat ~100%, INSTALLED_CAPACITY_MW is the AC nameplate; PR should reference the DC
kWp, which is higher — set INSTALLED_CAPACITY_MW accordingly for accurate PR.)
"""

import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_installed_capacity_mw
from app.services import schema_cache

logger = logging.getLogger(__name__)

_GEN_RE = re.compile(r"^INVERTER_(\d+)_GEN$")
_INV_TABLE = "INVERTER_DAILY_GEN"
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _latest_ppc_date(db: Session) -> date:
    d = db.execute(text("SELECT CAST(MAX(TimeCol) AS DATE) FROM [dbo].[PPC]")).scalar()
    if not d:
        raise HTTPException(status_code=503, detail="No PPC telemetry available")
    return d


def _parse(d: Optional[str], fallback: date) -> date:
    if not d:
        return fallback
    try:
        return datetime.strptime(d, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid date: {d!r} (expected YYYY-MM-DD)")


def _inverter_gen_columns(db: Session) -> List[str]:
    """INVERTER_xx_GEN columns that map to a real inverter, in inverter order."""
    from app.services.reports_service import ReportsService
    real = {e["equipment_id"] for e in ReportsService.get_equipment_list(db, "Inverter")}
    cols = schema_cache.get_columns(db, _INV_TABLE) or {}
    matched = [(int(m.group(1)), c) for c in cols
               if (m := _GEN_RE.match(c)) and c[: -len("_GEN")] in real]
    return [c for _, c in sorted(matched)]


def _num(v) -> float:
    return float(v) if v is not None else 0.0


def _inverter_peak_mw(db: Session, inverter_id: str, from_d, to_d) -> Optional[float]:
    """Peak active power (MW) for ONE inverter over the range, from [dbo].[POWER_GRAPH]
    (INVERTER_xx_ACTIVE_POWER, stored in kW → MW). Returns None if the table/column is
    absent so the KPI degrades gracefully. `inverter_id` is already validated against the
    real INVERTER_DAILY_GEN columns by the caller, and the column's existence is checked
    here (parameterised) before it is used, so the interpolation is safe."""
    col = f"{inverter_id}_ACTIVE_POWER"
    try:
        exists = db.execute(text(
            "SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_NAME = 'POWER_GRAPH' AND COLUMN_NAME = :c"
        ), {"c": col}).scalar()
        if not exists:
            return None
        v = db.execute(text(
            f"SELECT MAX(CAST([{col}] AS FLOAT)) FROM [dbo].[POWER_GRAPH] "
            "WHERE CAST(TimeCol AS DATE) BETWEEN :a AND :b"
        ), {"a": from_d, "b": to_d}).scalar()
        return round(_num(v) / 1000.0, 1) if v is not None else None
    except Exception:                                   # never let a missing source 500 the page
        return None


def get_overview(db: Session, from_str: Optional[str], to_str: Optional[str],
                 equipment: Optional[str] = None,
                 equipment_ids: Optional[str] = None) -> Dict[str, Any]:
    latest = _latest_ppc_date(db)
    to_d = _parse(to_str, latest)
    from_d = _parse(from_str, to_d - timedelta(days=29))
    if from_d > to_d:
        raise HTTPException(status_code=400, detail="from date must be on or before to date")

    cap_mw = get_installed_capacity_mw()
    params = {"a": from_d, "b": to_d}

    # ── Daily plant energy + peak power (PPC) ────────────────────────────────
    plant_rows = db.execute(text("""
        SELECT CAST(TimeCol AS DATE) AS d,
               MAX(CAST(PLANT_DAILY_PRODUCTION AS FLOAT))      AS energy_mwh,
               MAX(CAST(GRID_ACTIVE_POWER_MEASURED AS FLOAT))  AS peak_mw
        FROM [dbo].[PPC]
        WHERE CAST(TimeCol AS DATE) BETWEEN :a AND :b
        GROUP BY CAST(TimeCol AS DATE)
        ORDER BY d
    """), params).fetchall()

    # ── Daily insolation H (WMS), keyed by date for the PR join ──────────────
    insol = {r[0]: _num(r[1]) for r in db.execute(text("""
        SELECT CAST(TimeCol AS DATE) AS d, MAX(CAST(AVG_GHI_CUMM_IRRADIATION AS FLOAT)) AS h
        FROM [dbo].[WMS]
        WHERE CAST(TimeCol AS DATE) BETWEEN :a AND :b
        GROUP BY CAST(TimeCol AS DATE)
    """), params).fetchall()}

    daily: List[Dict[str, Any]] = []
    pr_trend: List[Dict[str, Any]] = []
    total_energy = 0.0
    peak_power = 0.0
    pr_values: List[float] = []
    monthly_acc: Dict[str, float] = {}

    for r in plant_rows:
        d, e, pk = r[0], _num(r[1]), _num(r[2])
        total_energy += e
        peak_power = max(peak_power, pk)
        h = insol.get(d, 0.0)
        pr = min(round((e / cap_mw) / h * 100, 1), 100.0) if h > 0 and e > 0 else None
        if pr is not None:
            pr_values.append(pr)
        label = d.strftime("%d/%m")
        daily.append({"date": d.isoformat(), "label": label, "generation_mwh": round(e, 1)})
        pr_trend.append({"date": d.isoformat(), "label": label, "pr": pr})
        ym = d.strftime("%Y-%m")
        monthly_acc[ym] = monthly_acc.get(ym, 0.0) + e

    monthly = [
        {"ym": ym, "label": f"{_MONTHS[int(ym[5:7]) - 1]} {ym[:4]}",
         "generation_mwh": round(v, 1)}
        for ym, v in sorted(monthly_acc.items())
    ]

    # ── Per-inverter generation + PR over the range (INVERTER_DAILY_GEN) ─────
    gen_cols = _inverter_gen_columns(db)
    inverters: List[Dict[str, Any]] = []
    availability_pct: Optional[float] = None
    inv_rows: List[Any] = []
    if gen_cols:
        select = ", ".join(f"MAX(CAST([{c}] AS FLOAT)) AS [{c}]" for c in gen_cols)
        inv_rows = db.execute(text(f"""
            SELECT CAST(TimeCol AS DATE) AS d, {select}
            FROM [dbo].[{_INV_TABLE}]
            WHERE CAST(TimeCol AS DATE) BETWEEN :a AND :b
            GROUP BY CAST(TimeCol AS DATE)
            ORDER BY d
        """), params).fetchall()

    # Only build the ranking when the range actually contains inverter telemetry —
    # an out-of-data range returns empty tables + null availability, not 24 zeros.
    if inv_rows:
        totals = {c: 0.0 for c in gen_cols}     # kWh per inverter over the range
        insol_seen = 0.0                          # Σ H over days that have generation
        avail_daily: List[float] = []
        for row in inv_rows:
            d = row[0]
            producing = 0
            for i, c in enumerate(gen_cols, start=1):
                v = _num(row[i])
                if v > 0:
                    producing += 1
                totals[c] += max(0.0, v)
            avail_daily.append(producing / len(gen_cols) * 100)
            insol_seen += insol.get(d, 0.0)
        availability_pct = round(sum(avail_daily) / len(avail_daily), 1) if avail_daily else None

        # PR per inverter = (E_kWh / P0_kW) / ΣH, referenced to an equal share of
        # plant capacity. Capped to 100 like the plant PR.
        per_inv_cap_kw = (cap_mw * 1000) / len(gen_cols)
        for c in gen_cols:
            kwh = totals[c]
            pr = (min(round((kwh / per_inv_cap_kw) / insol_seen * 100, 1), 100.0)
                  if insol_seen > 0 and kwh > 0 else None)
            inverters.append({
                "inverter":       c[: -len("_GEN")],
                "generation_kwh": round(kwh, 2),
                "pr":             pr,
            })
        inverters.sort(key=lambda x: x["generation_kwh"], reverse=True)
        for rank, inv in enumerate(inverters, start=1):
            inv["rank"] = rank

    # Every real inverter (stable order), used for the checkbox list AND to detect the
    # "all selected" case — derived from the schema so the list is complete even when the
    # chosen range has no telemetry.
    all_ids = [c[: -len("_GEN")] for c in gen_cols]
    equipment_options = ["all"] + sorted(all_ids)

    # ── Resolve the inverter selection (checkbox multi-select) ───────────────────
    # `equipment_ids` (comma-separated) drives the multi-select and takes precedence;
    # `equipment` (single id) is kept for backward-compatibility.
    #   · param absent               → ALL inverters (plant-level view)
    #   · list == every inverter     → ALL inverters (plant-level view)
    #   · non-empty subset           → recompute from ONLY those inverters
    #   · present but empty          → nothing selected → empty analytics
    none_selected = False
    if equipment_ids is not None:
        req = [s.strip() for s in equipment_ids.split(",") if s.strip()]
        sel_ids = [i for i in req if i in all_ids]
        none_selected = (len(sel_ids) == 0)
        is_all = (not none_selected) and len(sel_ids) == len(all_ids)
    elif equipment and equipment != "all":
        sel_ids = [equipment] if equipment in all_ids else []
        none_selected = (len(sel_ids) == 0)
        is_all = False
    else:
        sel_ids, is_all = list(all_ids), True
    sel_set = set(sel_ids)

    ndays = len(plant_rows) or 1
    # Plant-level KPIs — the ALL-inverters (or default) view.
    kpis = {
        "today_energy":  {"label": "Latest Day Energy", "value": round(daily[-1]["generation_mwh"], 1) if daily else None, "unit": "MWh"},
        "period_energy": {"label": "Period Energy",      "value": round(total_energy, 1) if daily else None, "unit": "MWh"},
        "avg_pr":        {"label": "Average PR",         "value": round(sum(pr_values) / len(pr_values), 1) if pr_values else None, "unit": "%"},
        "avg_cuf":       {"label": "Average CUF",        "value": round(total_energy / (cap_mw * 24 * ndays) * 100, 1) if daily else None, "unit": "%"},
        "availability":  {"label": "Plant Availability", "value": availability_pct, "unit": "%"},
        "peak_power":    {"label": "Peak Power",         "value": round(peak_power, 1) if daily else None, "unit": "MW"},
    }
    ranked = inverters                      # ALL view: full ranking
    selected_label = "all"

    if none_selected:
        # No inverter checked → every chart/table empty, KPIs null (the UI shows its
        # "No Data Available" states instead of any plant/aggregate figures).
        daily, monthly, pr_trend, ranked = [], [], [], []
        kpis = {k: {"label": v["label"], "value": None, "unit": v["unit"]} for k, v in kpis.items()}
        selected_label = "none"

    elif not is_all:
        # ── SUBSET view — recompute generation / PR / KPIs from ONLY the selected
        # inverters (the ALL path above stays the plant-level combined view). Uses the
        # already-fetched per-day per-inverter rows (inv_rows); the only extra query is
        # each selected inverter's peak power from POWER_GRAPH. Generation kWh → MWh so
        # the existing (MWh) charts/axes hold. This generalises the former single-inverter
        # path to any subset (a subset of 1 behaves exactly as before).
        ranked = [i for i in inverters if i["inverter"] in sel_set]
        selected_label = sel_ids[0] if len(sel_ids) == 1 else "multiple"
        sel_cols = [c for c in gen_cols if c[: -len("_GEN")] in sel_set]
        if sel_cols and inv_rows:
            idxs = [gen_cols.index(c) + 1 for c in sel_cols]     # +1: row[0] is the date
            n_sel = len(sel_cols)
            per_inv_cap_kw = (cap_mw * 1000) / len(gen_cols)
            subset_cap_kw = per_inv_cap_kw * n_sel               # selected share of capacity
            i_daily, i_pr_trend, i_monthly_acc, i_avail = [], [], {}, []
            i_total_kwh, i_pr_values = 0.0, []
            for row in inv_rows:
                d = row[0]
                day_kwh   = sum(max(0.0, _num(row[j])) for j in idxs)
                producing = sum(1 for j in idxs if _num(row[j]) > 0)
                i_total_kwh += day_kwh
                i_avail.append(producing / n_sel * 100)
                mwh = day_kwh / 1000.0
                h = insol.get(d, 0.0)
                pr = (min(round((day_kwh / subset_cap_kw) / h * 100, 1), 100.0)
                      if h > 0 and day_kwh > 0 else None)
                if pr is not None:
                    i_pr_values.append(pr)
                label = d.strftime("%d/%m")
                i_daily.append({"date": d.isoformat(), "label": label, "generation_mwh": round(mwh, 3)})
                i_pr_trend.append({"date": d.isoformat(), "label": label, "pr": pr})
                ym = d.strftime("%Y-%m")
                i_monthly_acc[ym] = i_monthly_acc.get(ym, 0.0) + mwh
            i_ndays = len(inv_rows) or 1
            peaks = [_inverter_peak_mw(db, sid, from_d, to_d) for sid in sel_ids]
            peak_val = (round(sum(p for p in peaks if p is not None), 1)
                        if any(p is not None for p in peaks) else None)

            daily = i_daily
            pr_trend = i_pr_trend
            monthly = [{"ym": ym, "label": f"{_MONTHS[int(ym[5:7]) - 1]} {ym[:4]}",
                        "generation_mwh": round(v, 3)} for ym, v in sorted(i_monthly_acc.items())]
            kpis = {
                "today_energy":  {"label": "Latest Day Energy", "value": i_daily[-1]["generation_mwh"] if i_daily else None, "unit": "MWh"},
                "period_energy": {"label": "Period Energy",      "value": round(i_total_kwh / 1000.0, 3) if i_daily else None, "unit": "MWh"},
                "avg_pr":        {"label": "Average PR",         "value": round(sum(i_pr_values) / len(i_pr_values), 1) if i_pr_values else None, "unit": "%"},
                "avg_cuf":       {"label": "Average CUF",        "value": round(i_total_kwh / (subset_cap_kw * 24 * i_ndays) * 100, 1) if i_daily else None, "unit": "%"},
                "availability":  {"label": "Plant Availability", "value": round(sum(i_avail) / len(i_avail), 1) if i_avail else None, "unit": "%"},
                "peak_power":    {"label": "Peak Power",         "value": peak_val, "unit": "MW"},
            }
        else:
            # A valid subset but the range has no inverter telemetry → empty, not zeros.
            daily, monthly, pr_trend = [], [], []
            kpis = {k: {"label": v["label"], "value": None, "unit": v["unit"]} for k, v in kpis.items()}

    logger.info("Analytics overview | %s→%s | days=%d | inverters=%d | selected=%d/%d (%s) | "
                "energy=%.0f MWh | peak=%.1f MW | avgPR=%s",
                from_d, to_d, ndays, len(inverters), len(sel_ids), len(all_ids), selected_label,
                total_energy, peak_power, kpis["avg_pr"]["value"])

    return {
        "range": {"from": from_d.isoformat(), "to": to_d.isoformat()},
        "capacity_mw": cap_mw,
        "kpis": kpis,
        "daily": daily,
        "monthly": monthly,
        "pr_trend": pr_trend,
        "inverters": ranked,
        "equipment_options": equipment_options,
        "selected_equipment": selected_label,
        "selected_equipment_ids": sel_ids,
    }
