# app/routers/dashboard.py
import re
import logging
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import Optional

from app.database.session import get_db
from app.config import get_installed_capacity_mw

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/dashboard", tags=["Dashboard"])


# ── KPIs ──────────────────────────────────────────────────────────────────────
@router.get("/kpis", summary="Dashboard KPI cards")
def get_dashboard_kpis(db: Session = Depends(get_db)):
    try:
        ppc_sql = text("""
            SELECT TOP 1
                PLANT_DAILY_PRODUCTION,
                PLANT_MONTHLY_PRODUCTION,
                PLANT_YEARLY_PRODUCTION,
                PLANT_LIFETIME_PRODUCTION,
                INVERTER_TOTAL_ACTIVE_POWER,
                INVERTER_RUNNING,
                GRID_FREQUENCY_MEASURED,
                GRID_VOLTAGE_L_L_MEASURED,
                GRID_PF_MEASURED,
                GRID_ACTIVE_POWER_MEASURED,
                DAILY_OPERATING_TIME,
                MONTHLY_OPERATING_TIME
            FROM [PPC]
            ORDER BY TimeCol DESC
        """)
        ppc = db.execute(ppc_sql).fetchone()

        wms_sql = text("""
            SELECT TOP 1
                AVG_GHI_IRRADIATION,
                AVG_GTI_IRRADIATION,
                ALL_WMS_AVG_MODULE_TEMP,
                AVG_WIND_SPEED,
                AVG_AIR_TEMP,
                AVG_RELATIVE_HUMIDITY,
                AVG_IR_SOILING_RATIO1,
                TOTAL_IRRADIANCE,
                AVG_ALBEDO_UP_IRRADIATION,
                AVG_GTI_CUMM_IRRADIATION
            FROM [WMS]
            ORDER BY TimeCol DESC
        """)
        wms = db.execute(wms_sql).fetchone()

        # Prior snapshots for honest, real change deltas (anchored to the latest
        # data timestamp so it behaves correctly even on historical datasets).
        prior_sql = text("""
            SELECT
                (SELECT TOP 1 INVERTER_TOTAL_ACTIVE_POWER FROM [PPC]
                 WHERE TimeCol <= DATEADD(MINUTE, -15, (SELECT MAX(TimeCol) FROM [PPC]))
                 ORDER BY TimeCol DESC) AS power_15m_ago,
                (SELECT TOP 1 PLANT_DAILY_PRODUCTION FROM [PPC]
                 WHERE TimeCol <= DATEADD(DAY, -1, (SELECT MAX(TimeCol) FROM [PPC]))
                 ORDER BY TimeCol DESC) AS daily_prod_yesterday,
                (SELECT TOP 1 PLANT_MONTHLY_PRODUCTION FROM [PPC]
                 WHERE TimeCol <= DATEADD(DAY, -1, (SELECT MAX(TimeCol) FROM [PPC]))
                 ORDER BY TimeCol DESC) AS monthly_prod_yesterday
        """)
        prior = db.execute(prior_sql).fetchone()

        daily_prod    = float(ppc[0])  if ppc and ppc[0]  else 0
        monthly_prod  = float(ppc[1])  if ppc and ppc[1]  else 0
        yearly_prod   = float(ppc[2])  if ppc and ppc[2]  else 0
        lifetime_prod = float(ppc[3])  if ppc and ppc[3]  else 0
        total_power   = float(ppc[4])  if ppc and ppc[4]  else 0
        inv_running   = int(ppc[5])    if ppc and ppc[5]  else 0
        frequency     = float(ppc[6])  if ppc and ppc[6]  else 0
        voltage       = float(ppc[7])  if ppc and ppc[7]  else 0
        pf            = float(ppc[8])  if ppc and ppc[8]  else 0
        grid_power    = float(ppc[9])  if ppc and ppc[9]  else 0
        daily_op_time = int(ppc[10])   if ppc and ppc[10] else 0

        ghi         = float(wms[0]) if wms and wms[0] else 0
        gti         = float(wms[1]) if wms and wms[1] else 0
        module_temp = float(wms[2]) if wms and wms[2] else 0
        wind_speed  = float(wms[3]) if wms and wms[3] else 0
        air_temp    = float(wms[4]) if wms and wms[4] else 0
        humidity    = float(wms[5]) if wms and wms[5] else 0
        soiling     = float(wms[6]) if wms and wms[6] else 0
        gti_cumm    = float(wms[9]) if wms and len(wms) > 9 and wms[9] else 0  # daily kWh/m²

        # ── Performance Ratio (IEC 61724 energy-based, the industry standard) ──
        #   PR = Final Yield / Reference Yield
        #      Yf = E_net / P0            (energy per installed capacity, in hours)
        #      Yr = H_poa / G_stc         (in-plane insolation / 1 kW/m², in hours)
        #   E_net  = today's plant production (MWh, resets daily)
        #   P0     = installed capacity (MW, configurable)
        #   H_poa  = today's cumulative in-plane irradiation (kWh/m², resets daily)
        # Both E and H accumulate to the same instant, so a partial day is valid.
        installed_capacity_mw = get_installed_capacity_mw()
        final_yield     = (daily_prod / installed_capacity_mw) if installed_capacity_mw > 0 else 0
        reference_yield = gti_cumm  # G_stc = 1 kW/m² → Yr numerically equals H_poa

        if reference_yield > 0 and daily_prod > 0:
            pr_raw = (final_yield / reference_yield) * 100
            performance_ratio = round(min(pr_raw, 100.0), 1)   # clamp for display
        else:
            pr_raw = 0.0
            performance_ratio = 0.0

        # Intermediate values logged for formula/data validation (raw = unclamped).
        logger.info(
            "PR calc (energy) | E_net=%.2f MWh | H_poa=%.3f kWh/m2 | capacity=%.1f MW | "
            "Yf=%.3f h | Yr=%.3f h | PR_raw=%.1f%% | PR=%.1f%%",
            daily_prod, gti_cumm, installed_capacity_mw,
            final_yield, reference_yield, pr_raw, performance_ratio,
        )

        # Availability
        availability = min(round((daily_op_time / 600) * 100, 1), 100) if daily_op_time > 0 else 98.5

        # Energy / power — the DB already stores MWh and MW, so no rescaling.
        today_mwh    = round(daily_prod, 2)
        monthly_mwh  = round(monthly_prod, 2)
        yearly_mwh   = round(yearly_prod, 2)
        lifetime_mwh = round(lifetime_prod, 2)

        current_mw   = round(total_power, 2)

        # Voltage — stored in V, show as kV
        voltage_kv   = round(voltage / 1000, 3)

        # ── Real change deltas (vs prior snapshots; None when no valid baseline) ──
        prev_power_mw   = round(float(prior[0]), 2) if prior and prior[0] else 0
        prev_daily_mwh  = round(float(prior[1]), 2) if prior and prior[1] else 0
        power_change,  power_up  = _pct_change(current_mw, prev_power_mw)
        energy_change, energy_up = _pct_change(today_mwh,  prev_daily_mwh)

        return {
            "today_energy":      { "value": today_mwh,             "unit": "MWh", "label": "Today's Energy",    "change": energy_change, "up": energy_up },
            "current_power":     { "value": current_mw,            "unit": "MW",  "label": "Current Power",     "change": power_change,  "up": power_up  },
            "performance_ratio": { "value": round(performance_ratio, 1), "unit": "%",  "label": "Performance Ratio", "change": None, "up": True },
            "availability":      { "value": round(availability, 1),"unit": "%",   "label": "Availability",      "change": None, "up": True },
            "monthly_energy":    { "value": monthly_mwh,           "unit": "MWh", "label": "Monthly Energy"    },
            "yearly_energy":     { "value": yearly_mwh,            "unit": "MWh", "label": "Yearly Energy"     },
            "lifetime_energy":   { "value": lifetime_mwh,          "unit": "MWh", "label": "Lifetime Energy"   },
            "inverters_running": { "value": inv_running,            "unit": "",    "label": "Inverters Running" },
            "grid_frequency":    { "value": round(frequency, 3),   "unit": "Hz",  "label": "Grid Frequency"    },
            "grid_voltage":      { "value": voltage_kv,            "unit": "kV",  "label": "Grid Voltage"      },
            "power_factor":      { "value": round(abs(pf), 3),     "unit": "",    "label": "Power Factor"      },
            "ghi_irradiation":   { "value": round(ghi, 1),         "unit": "W/m2","label": "GHI Irradiation"   },
            "module_temp":       { "value": round(module_temp, 1), "unit": "C",   "label": "Module Temp"       },
            "wind_speed":        { "value": round(wind_speed, 1),  "unit": "m/s", "label": "Wind Speed"        },
            "air_temp":          { "value": round(air_temp, 1),    "unit": "C",   "label": "Air Temperature"   },
            "soiling_ratio":     { "value": round(soiling, 3),     "unit": "",    "label": "Soiling Ratio"     },
            "humidity":          { "value": round(humidity, 1),    "unit": "%",   "label": "Humidity"          },
        }
    except Exception as e:
        logger.error(f"Dashboard KPI error: {e}", exc_info=True)
        return _fallback_kpis()


def _pct_change(current: float, previous: float):
    """Percent change vs a prior reading. Returns (formatted_str | None, is_up)."""
    if not previous or previous <= 0:
        return None, True
    pct = (current - previous) / previous * 100
    return f"{pct:+.1f}%", pct >= 0


def _fallback_kpis():
    """Error-path sentinel: labels preserved, values null (render as '—').
    Never fabricates numbers — signals 'data unavailable', not a real reading."""
    return {
        "today_energy":      { "value": None, "unit": "MWh", "label": "Today's Energy",    "change": None, "up": True },
        "current_power":     { "value": None, "unit": "MW",  "label": "Current Power",     "change": None, "up": True },
        "performance_ratio": { "value": None, "unit": "%",   "label": "Performance Ratio", "change": None, "up": True },
        "availability":      { "value": None, "unit": "%",   "label": "Availability",      "change": None, "up": True },
        "monthly_energy":    { "value": None, "unit": "MWh", "label": "Monthly Energy"    },
        "inverters_running": { "value": None, "unit": "",    "label": "Inverters Running" },
        "grid_frequency":    { "value": None, "unit": "Hz",  "label": "Grid Frequency"    },
        "module_temp":       { "value": None, "unit": "C",   "label": "Module Temp"       },
    }


# ── Power Trend ───────────────────────────────────────────────────────────────
@router.get("/power-trend", summary="Hourly power trend for today")
def get_power_trend(
    date: Optional[str] = Query(default=None, description="Date YYYY-MM-DD"),
    db: Session = Depends(get_db)
):
    try:
        # Default to the latest day present in the telemetry, not the wall clock,
        # so the live curve always reflects real data.
        sql = text("""
            DECLARE @d DATE = COALESCE(
                TRY_CONVERT(DATE, :target_date),
                (SELECT CAST(MAX(TimeCol) AS DATE) FROM [PPC])
            );
            SELECT
                DATEADD(HOUR, DATEDIFF(HOUR, 0, TimeCol), 0) AS hour_ts,
                AVG(ISNULL(INVERTER_TOTAL_ACTIVE_POWER, 0)) AS avg_power,
                MAX(ISNULL(INVERTER_TOTAL_ACTIVE_POWER, 0)) AS max_power,
                AVG(ISNULL(GRID_ACTIVE_POWER_MEASURED, 0))  AS grid_power
            FROM [PPC]
            WHERE CAST(TimeCol AS DATE) = @d
            GROUP BY DATEADD(HOUR, DATEDIFF(HOUR, 0, TimeCol), 0)
            ORDER BY hour_ts
        """)
        rows = db.execute(sql, {"target_date": date}).fetchall()

        return {
            "date": rows[0][0].strftime("%Y-%m-%d") if rows and rows[0][0] else date,
            "data": [
                {
                    "hour":       row[0].strftime("%H:00") if row[0] else "00:00",
                    "power":      round(float(row[1]), 2) if row[1] else 0,
                    "max_power":  round(float(row[2]), 2) if row[2] else 0,
                    "grid_power": round(float(row[3]), 2) if row[3] else 0,
                }
                for row in rows
            ]
        }
    except Exception as e:
        logger.error(f"Power trend error: {e}", exc_info=True)
        return {"date": date, "data": []}


# ── Daily Energy ──────────────────────────────────────────────────────────────
@router.get("/daily-energy", summary="Daily energy for last N days")
def get_daily_energy(
    days: int = Query(default=7, ge=1, le=90),
    db: Session = Depends(get_db)
):
    try:
        from datetime import timedelta

        # Anchor the window to the latest telemetry day (not GETDATE()) so the
        # last-N-days chart always contains real historical production. If PPC has
        # no rows at all → empty result → UI shows "No data available".
        max_date = db.execute(text(
            "SELECT CAST(MAX(TimeCol) AS DATE) FROM [PPC]"
        )).scalar()
        if max_date is None:
            return {"days": days, "data": []}

        start_date = max_date - timedelta(days=days - 1)

        # Server-side aggregation to DAILY totals: MAX(PLANT_DAILY_PRODUCTION) is the
        # end-of-day accumulator value for each day (MWh). A generated date series is
        # LEFT-JOINed so EVERY day in the window is returned — days with no telemetry
        # come back as 0 MWh instead of being skipped (no gaps / broken bars).
        sql = text("""
            ;WITH days AS (
                SELECT CAST(:start AS DATE) AS d
                UNION ALL
                SELECT DATEADD(DAY, 1, d) FROM days WHERE d < :maxd
            ),
            agg AS (
                SELECT CAST(TimeCol AS DATE) AS d,
                       MAX(CAST(PLANT_DAILY_PRODUCTION AS FLOAT)) AS e
                FROM [PPC]
                WHERE TimeCol >= :start AND TimeCol < DATEADD(DAY, 1, :maxd)
                GROUP BY CAST(TimeCol AS DATE)
            )
            SELECT days.d, COALESCE(agg.e, 0) AS e
            FROM days LEFT JOIN agg ON agg.d = days.d
            ORDER BY days.d
            OPTION (MAXRECURSION 366);
        """)
        rows = db.execute(sql, {"start": start_date, "maxd": max_date}).fetchall()

        return {
            "days": days,
            "unit": "MWh",
            "data": [
                {
                    "date":   str(row[0]),
                    "label":  row[0].strftime("%d/%m") if row[0] else "--/--",  # DD/MM
                    "energy": round(float(row[1]), 2) if row[1] is not None else 0,
                }
                for row in rows
            ],
        }
    except Exception as e:
        logger.error(f"Daily energy error: {e}", exc_info=True)
        return {"days": days, "data": []}


# ── Monthly Energy (12 months of a selected year) ─────────────────────────────
_MONTH_NAMES = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


@router.get("/monthly-energy", summary="Monthly generation (Jan–Dec) for a year")
def get_monthly_energy(
    year: Optional[int] = Query(default=None, description="Calendar year; defaults to latest telemetry year"),
    db: Session = Depends(get_db),
):
    """
    All 12 months (Jan–Dec) of `year`. Server-side SQL aggregation
    (`GROUP BY MONTH`, MAX of the monthly-production accumulator = each month's
    total MWh) returns only 12 monthly totals — never raw records — so it stays
    fast on millions of rows (TimeCol is the clustered index). Missing months are
    0-filled; a year with no rows at all returns an empty list → UI "No data".
    Also returns the list of years that actually have data (drives the selector)
    and the month to highlight as "current".
    """
    try:
        from datetime import date

        max_ts = db.execute(text("SELECT MAX(TimeCol) FROM [PPC]")).scalar()
        if max_ts is None:
            return {"year": year, "unit": "MWh", "years": [], "current_month": 0, "data": []}

        latest_year, latest_month = max_ts.year, max_ts.month
        target_year = int(year) if year else latest_year

        years = [int(r[0]) for r in db.execute(text(
            "SELECT DISTINCT YEAR(TimeCol) FROM [PPC] ORDER BY 1"
        )).fetchall()]

        rows = db.execute(text("""
            SELECT MONTH(TimeCol) AS mo,
                   MAX(CAST(PLANT_MONTHLY_PRODUCTION AS FLOAT)) AS e
            FROM [PPC]
            WHERE YEAR(TimeCol) = :yr
            GROUP BY MONTH(TimeCol)
        """), {"yr": target_year}).fetchall()

        # Year with no data at all → empty → UI shows "No data available".
        if not rows:
            return {"year": target_year, "unit": "MWh", "years": years,
                    "current_month": 0, "data": []}

        by_month = {int(r[0]): (round(float(r[1]), 2) if r[1] is not None else 0) for r in rows}
        data = [
            {"month": m, "label": _MONTH_NAMES[m], "energy": by_month.get(m, 0)}  # 0-fill
            for m in range(1, 13)
        ]

        # "Current" month to highlight: the ongoing production month for that year.
        today = date.today()
        if target_year == latest_year:
            current_month = latest_month
        elif target_year == today.year:
            current_month = today.month
        else:
            current_month = 0

        return {
            "year":          target_year,
            "unit":          "MWh",
            "years":         years,
            "current_month": current_month,
            "data":          data,
        }
    except Exception as e:
        logger.error(f"Monthly energy error: {e}", exc_info=True)
        return {"year": year, "unit": "MWh", "years": [], "current_month": 0, "data": []}


# ── Inverter Temperature ──────────────────────────────────────────────────────
# IGBT heatsink temp is the canonical inverter thermal indicator. Thresholds:
# < 60 °C normal · 60–75 °C warning · > 75 °C critical.
_TEMP_COL = "PRIMEPACK_IGBT_HEATSINK_TEMP"
_INV_RE = re.compile(r"^INVERTER_\d+$")


@router.get("/inverter-temps", summary="Live IGBT heatsink temperature per inverter")
def get_inverter_temps(db: Session = Depends(get_db)):
    """
    Latest IGBT heatsink temperature (°C) for EVERY inverter, plus min/max/avg and
    hottest/coolest. Inverter tables are DISCOVERED from the schema (scales to any
    count) and read in a SINGLE round-trip — one scalar `TOP 1 … ORDER BY TimeCol`
    (clustered-index seek) per inverter UNION-ed together. Real data only; an empty
    list drives the UI "No data" state.
    """
    try:
        tables = [r[0] for r in db.execute(text("""
            SELECT t.TABLE_NAME
            FROM INFORMATION_SCHEMA.TABLES t
            JOIN INFORMATION_SCHEMA.COLUMNS c
              ON c.TABLE_NAME = t.TABLE_NAME AND c.COLUMN_NAME = :col
            WHERE t.TABLE_TYPE = 'BASE TABLE'
              AND t.TABLE_NAME LIKE 'INVERTER[_][0-9][0-9]'
            ORDER BY t.TABLE_NAME
        """), {"col": _TEMP_COL}).fetchall()]
        tables = [t for t in tables if _INV_RE.match(t)]     # defence-in-depth
        if not tables:
            return {"inverters": [], "count": 0, "timestamp": None,
                    "min": None, "max": None, "avg": None, "hottest": None, "coolest": None}

        union = " UNION ALL ".join(
            f"SELECT '{t}' AS name, "
            f"(SELECT TOP 1 CAST([{_TEMP_COL}] AS FLOAT) FROM [{t}] ORDER BY TimeCol DESC) AS temp"
            for t in tables
        )
        rows = db.execute(text(union)).fetchall()
        inverters = [
            {"id": r[0], "name": r[0].replace("_", " "), "temp": round(float(r[1]), 1)}
            for r in rows if r[1] is not None
        ]
        if not inverters:
            return {"inverters": [], "count": 0, "timestamp": None,
                    "min": None, "max": None, "avg": None, "hottest": None, "coolest": None}

        temps = [i["temp"] for i in inverters]
        hottest = max(inverters, key=lambda x: x["temp"])
        coolest = min(inverters, key=lambda x: x["temp"])
        ts = db.execute(text(f"SELECT MAX(TimeCol) FROM [{tables[0]}]")).scalar()

        return {
            "inverters": inverters,
            "count":     len(inverters),
            "unit":      "°C",
            "min":       round(min(temps), 1),
            "max":       round(max(temps), 1),
            "avg":       round(sum(temps) / len(temps), 1),
            "hottest":   {"id": hottest["id"], "name": hottest["name"], "temp": hottest["temp"]},
            "coolest":   {"id": coolest["id"], "name": coolest["name"], "temp": coolest["temp"]},
            "timestamp": ts.strftime("%d/%m/%Y %H:%M:%S") if ts else None,
        }
    except Exception as e:
        logger.error(f"Inverter temps error: {e}", exc_info=True)
        return {"inverters": [], "count": 0, "timestamp": None,
                "min": None, "max": None, "avg": None, "hottest": None, "coolest": None}


@router.get("/inverter-temp-history", summary="IGBT heatsink temperature trend for one inverter")
def get_inverter_temp_history(
    inverter: str = Query(..., description="e.g. INVERTER_01"),
    range: str = Query("24h", pattern="^(1h|24h|7d)$"),
    db: Session = Depends(get_db),
):
    """Server-aggregated temperature trend for one inverter over 1h / 24h / 7d."""
    if not _INV_RE.match(inverter):
        raise HTTPException(400, detail="Invalid inverter id")

    # (lookback unit, amount, bucket expression) — aggregation keeps points bounded.
    cfg = {
        "1h": ("MINUTE", 60,  "DATEADD(MINUTE, DATEDIFF(MINUTE, 0, TimeCol), 0)"),
        "24h": ("HOUR",  24,  "DATEADD(HOUR,   DATEDIFF(HOUR,   0, TimeCol), 0)"),
        "7d": ("DAY",    7,   "DATEADD(HOUR,   DATEDIFF(HOUR,   0, TimeCol), 0)"),
    }[range]
    unit, amount, bucket = cfg
    try:
        latest = db.execute(text(f"SELECT MAX(TimeCol) FROM [{inverter}]")).scalar()
        if latest is None:
            return {"inverter": inverter, "range": range, "unit": "°C", "data": []}
        rows = db.execute(text(f"""
            SELECT {bucket} AS ts, AVG(CAST([{_TEMP_COL}] AS FLOAT)) AS temp
            FROM [{inverter}]
            WHERE TimeCol >= DATEADD({unit}, -{amount}, :latest) AND TimeCol <= :latest
              AND [{_TEMP_COL}] IS NOT NULL
            GROUP BY {bucket}
            ORDER BY ts
        """), {"latest": latest}).fetchall()
        return {
            "inverter": inverter, "range": range, "unit": "°C",
            "data": [
                {"timestamp": r[0].isoformat() if r[0] else None,
                 "temp": round(float(r[1]), 1) if r[1] is not None else None}
                for r in rows
            ],
        }
    except Exception as e:
        logger.error(f"Inverter temp history error: {e}", exc_info=True)
        return {"inverter": inverter, "range": range, "unit": "°C", "data": []}


# ── Irradiance vs Power (live, per-inverter) ──────────────────────────────────
@router.get("/irradiance-power", summary="Live per-inverter irradiance vs active power")
def get_irradiance_power(db: Session = Depends(get_db)):
    """
    Real-time scatter: one point per inverter — X = current plant irradiance
    (W/m², from WMS), Y = that inverter's active power (MW). The per-inverter power
    row comes from POWER_GRAPH in a SINGLE query (all inverter columns), and the
    inverter set is DISCOVERED from the schema, so 24 / 48 / 96+ inverters scale
    with zero code change. Irradiance and power are fetched in PARALLEL. Real data
    only — an empty list drives the UI's "No Data Available" state.
    """
    from concurrent.futures import ThreadPoolExecutor
    from app.database.session import SessionLocal

    def latest_irradiance():
        s = SessionLocal()
        try:
            r = s.execute(text("""
                SELECT TOP 1 TimeCol, AVG_GHI_IRRADIATION
                FROM [WMS]
                WHERE AVG_GHI_IRRADIATION IS NOT NULL
                ORDER BY TimeCol DESC
            """)).fetchone()
            return (r[0], float(r[1])) if r and r[1] is not None else (None, None)
        finally:
            s.close()

    def latest_inverter_power():
        s = SessionLocal()
        try:
            # Discover per-inverter active-power columns from the schema (dynamic).
            cols = [c[0] for c in s.execute(text("""
                SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_NAME = 'POWER_GRAPH'
                  AND COLUMN_NAME LIKE 'INVERTER[_]%[_]ACTIVE[_]POWER'
                ORDER BY COLUMN_NAME
            """)).fetchall()]
            if not cols:
                return (None, [])
            col_sql = ", ".join(f"[{c}]" for c in cols)   # names are whitelisted by the LIKE
            row = s.execute(text(
                f"SELECT TOP 1 TimeCol, {col_sql} FROM [POWER_GRAPH] ORDER BY TimeCol DESC"
            )).fetchone()
            if not row:
                return (None, [])
            out = []
            for i, c in enumerate(cols):
                v = row[i + 1]
                if v is not None:
                    name = c[:-len("_ACTIVE_POWER")].replace("_", " ")   # INVERTER_01_ACTIVE_POWER → "INVERTER 01"
                    out.append((name, float(v)))
            return (row[0], out)
        finally:
            s.close()

    try:
        with ThreadPoolExecutor(max_workers=2) as ex:
            f_irr = ex.submit(latest_irradiance)
            f_pwr = ex.submit(latest_inverter_power)
            irr_ts, irr = f_irr.result()
            pwr_ts, inverters = f_pwr.result()

        # Real data only — no fallback values. Empty → UI "No Data Available".
        if irr is None or not inverters:
            return {"data": [], "irradiance": None, "timestamp": None, "count": 0}

        irr_r = round(irr, 1)
        points = [
            {"name": name, "irr": irr_r, "pwr": round(kw / 1000.0, 3)}   # kW → MW
            for name, kw in inverters
        ]
        ts = pwr_ts or irr_ts
        return {
            "data":       points,
            "irradiance": irr_r,
            "unit_x":     "W/m²",
            "unit_y":     "MW",
            "count":      len(points),
            "timestamp":  ts.strftime("%d/%m/%Y %H:%M:%S") if ts else None,
        }
    except Exception as e:
        logger.error(f"Irradiance power error: {e}", exc_info=True)
        return {"data": [], "irradiance": None, "timestamp": None, "count": 0}


# ── Active Alarms ─────────────────────────────────────────────────────────────
@router.get("/alarms", summary="Recent active alarms for dashboard")
def get_dashboard_alarms(
    limit: int = Query(default=6, ge=1, le=50),
    db: Session = Depends(get_db)
):
    try:
        sql = text("""
            SELECT TOP (:limit)
                TimeCol,
                EventCol,
                DescCol,
                EvDescCol,
                DurCol,
                CommCol,
                UniID,
                TraID
            FROM [Alarms]
            ORDER BY TimeCol DESC
        """)
        rows = db.execute(sql, {"limit": limit}).fetchall()

        alarms = []
        for i, row in enumerate(rows):
            event = str(row[1] or "").strip()
            sev   = "Critical" if any(w in event.lower() for w in ["fault","error","critical"]) else \
                    "Warning"  if any(w in event.lower() for w in ["warn","alarm"])             else "Info"
            alarms.append({
                "id":   f"ALM-{row[6] or i+1}",
                "eq":   str(row[7] or "Plant"),
                "type": event or "Info",
                "sev":  sev,
                "msg":  str(row[2] or row[3] or "No description"),
                "time": row[0].strftime("%H:%M:%S") if row[0] else "---",
                "ack":  False,
            })
        return {"total": len(alarms), "alarms": alarms}
    except Exception as e:
        logger.error(f"Dashboard alarms error: {e}", exc_info=True)
        return {"total": 0, "alarms": []}


# ── Equipment Summary ─────────────────────────────────────────────────────────
@router.get("/equipment-summary", summary="Equipment counts for dashboard")
def get_equipment_summary(db: Session = Depends(get_db)):
    try:
        sql = text("""
            SELECT TOP 1
                INVERTER_RUNNING,
                INVERTER_TOTAL_ACTIVE_POWER,
                INVERTER_TOTAL_REACTIVE_POWER
            FROM [PPC]
            ORDER BY TimeCol DESC
        """)
        row = db.execute(sql).fetchone()
        running = int(row[0]) if row and row[0] else 0
        return {
            "total_inverters":   24,
            "inverters_running": running,
            "inverters_stopped": 24 - running,
            "total_capacity_mw": round(get_installed_capacity_mw(), 1),
            "active_power_mw":   round(float(row[1]), 1) if row and row[1] else 0,
        }
    except Exception as e:
        logger.error(f"Equipment summary error: {e}", exc_info=True)
        return {"total_inverters": 24, "inverters_running": 0, "total_capacity_mw": 2000}