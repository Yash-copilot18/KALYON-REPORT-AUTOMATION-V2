# app/services/ygr_service.py
"""
Yearly Generation Report (YGR) — month-by-month plant performance from SQL Server.

Sources (all real, no synthetic values):
  · [dbo].[PPC].PLANT_DAILY_PRODUCTION     — daily-resetting cumulative MWh meter.
        The day's energy is that counter's MAX; the month is the sum of those daily
        maxima. This is the SAME rule the DGR table and MGR use, so YGR monthly
        totals reconcile with the other two reports.
  · [dbo].[PPC].GRID_ACTIVE_POWER_MEASURED — instantaneous MW; the month's peak is
        its MAX.
  · [dbo].[WMS].AVG_GHI_CUMM_IRRADIATION   — daily-resetting cumulative insolation
        (kWh/m²); the day's H is its MAX, the month's H the sum of those.

Derived, using the same conventions as the Analytics module:
  · PR  % = (E_kWh / capacity_kW) / H × 100, capped at 100 (AC nameplate reference).
  · CUF % = E_MWh / (capacity_MW × 24h × days_with_data) × 100.

Export / Import energy are NOT reported: the plant database exposes no
export/import meter (PPC carries only PLANT_*_PRODUCTION and grid power), so the
report omits those fields rather than inventing them.
"""

import logging
from typing import Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_installed_capacity_mw

logger = logging.getLogger(__name__)

PPC_TABLE = "PPC"
WMS_TABLE = "WMS"

MONTHS = ["January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December"]

MWH_TO_KWH = 1000.0


def get_available_years(db: Session) -> List[int]:
    """Years that actually carry plant data — drives the YGR year dropdown."""
    rows = db.execute(text(
        f"SELECT DISTINCT YEAR([TimeCol]) AS y FROM [dbo].[{PPC_TABLE}] "
        f"WHERE [TimeCol] IS NOT NULL ORDER BY y"
    )).fetchall()
    years = [int(r[0]) for r in rows if r[0] is not None]
    logger.info("YGR available years: %s", years)
    return years


def get_yearly_report(db: Session, year: int) -> Dict:
    """Per-month generation, peak power, PR and CUF for `year`."""
    if not (2000 <= int(year) <= 2100):
        raise HTTPException(400, detail=f"Invalid year: {year!r}")

    period_from = f"{year}-01-01 00:00:00"
    period_to   = f"{year + 1}-01-01 00:00:00"
    params      = {"period_from": period_from, "period_to": period_to}
    cap_mw      = get_installed_capacity_mw()

    # Energy, peak and PR inputs per month in ONE pass. The daily plant meter is
    # LEFT JOINed to the daily insolation so PR can be computed over only the days
    # that have BOTH readings — WMS logging starts later than PPC, and ratioing a
    # full month of energy against a partial month of irradiance would inflate PR.
    rows = db.execute(text(f"""
        SELECT MONTH(p.gen_date) AS m,
               SUM(p.day_mwh)    AS energy_mwh,
               MAX(p.day_peak)   AS peak_mw,
               COUNT(*)          AS ndays,
               SUM(CASE WHEN w.day_h > 0 THEN p.day_mwh ELSE 0 END) AS pr_energy_mwh,
               SUM(CASE WHEN w.day_h > 0 THEN w.day_h   ELSE 0 END) AS pr_insolation
        FROM (
            SELECT CAST([TimeCol] AS DATE) AS gen_date,
                   MAX(CAST([PLANT_DAILY_PRODUCTION]     AS FLOAT)) AS day_mwh,
                   MAX(CAST([GRID_ACTIVE_POWER_MEASURED] AS FLOAT)) AS day_peak
            FROM [dbo].[{PPC_TABLE}]
            WHERE [TimeCol] >= :period_from AND [TimeCol] < :period_to
            GROUP BY CAST([TimeCol] AS DATE)
        ) p
        LEFT JOIN (
            SELECT CAST([TimeCol] AS DATE) AS gen_date,
                   MAX(CAST([AVG_GHI_CUMM_IRRADIATION] AS FLOAT)) AS day_h
            FROM [dbo].[{WMS_TABLE}]
            WHERE [TimeCol] >= :period_from AND [TimeCol] < :period_to
            GROUP BY CAST([TimeCol] AS DATE)
        ) w ON w.gen_date = p.gen_date
        GROUP BY MONTH(p.gen_date)
        ORDER BY m
    """), params).fetchall()

    months: List[Dict] = []
    for r in rows:
        m         = int(r[0])
        energy    = float(r[1] or 0.0)          # MWh, every logged day
        peak      = float(r[2]) if r[2] is not None else None
        ndays     = int(r[3] or 0)
        pr_energy = float(r[4] or 0.0)          # MWh, irradiance-covered days only
        h         = float(r[5] or 0.0)          # kWh/m², same days

        # Same capped-PR convention the Analytics module uses (AC nameplate ref).
        pr: Optional[float] = None
        if h > 0 and pr_energy > 0:
            pr = min(round((pr_energy * MWH_TO_KWH / (cap_mw * MWH_TO_KWH)) / h * 100, 1), 100.0)

        cuf: Optional[float] = None
        if ndays > 0 and energy > 0:
            cuf = round(energy / (cap_mw * 24 * ndays) * 100, 1)

        months.append({
            "month":      MONTHS[m - 1],
            "m":          MONTHS[m - 1][:3],
            "month_no":   m,
            "generation": round(energy, 3),
            "peak":       round(peak, 3) if peak is not None else None,
            "pr":         pr,
            "cuf":        cuf,
            "days":       ndays,
        })

    total = round(sum(x["generation"] for x in months), 3) if months else 0.0
    prs   = [x["pr"] for x in months if x["pr"] is not None]

    logger.info("YGR %s | months=%d | total=%.1f MWh | cap=%.1f MW",
                year, len(months), total, cap_mw)

    return {
        "year":             int(year),
        "capacity_mw":      cap_mw,
        "months":           months,
        "total_generation": total,
        "avg_pr":           round(sum(prs) / len(prs), 1) if prs else None,
        "unit":             "MWh",
    }
