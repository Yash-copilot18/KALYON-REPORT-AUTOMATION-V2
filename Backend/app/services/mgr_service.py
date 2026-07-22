# app/services/mgr_service.py
"""
Monthly Generation Report (MGR) — sourced directly from [dbo].[INVERTER_DAILY_GEN].

Each INVERTER_xx_GEN column is a per-inverter energy counter (kWh) that accumulates
through the day and resets to 0 at midnight. A day's generation for an inverter is
therefore that day's MAX of its column, and its monthly generation is the sum of
those daily maxima.

Both figures the page needs come from ONE aggregated query over the month:
  • per-inverter monthly generation = SUM over days of the day's MAX
  • plant generation per day        = SUM across inverters of that day's MAX

The query is a single clustered-index range scan on TimeCol, grouped by date, so it
returns at most 31 rows regardless of how many raw samples the month contains.
The inverter columns are discovered from the live schema — never hardcoded.
"""

import calendar
import logging
import re
from datetime import datetime
from typing import Any, Dict, List

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services import schema_cache

logger = logging.getLogger(__name__)

TABLE = "INVERTER_DAILY_GEN"
TIME_COL = "TimeCol"

# INVERTER_01_GEN … INVERTER_25_GEN
_GEN_COL_RE = re.compile(r"^INVERTER_(\d+)_GEN$")


def _gen_columns(db: Session) -> List[str]:
    """Every INVERTER_xx_GEN column in the table, in inverter order."""
    cols = schema_cache.get_columns(db, TABLE)
    if not cols:
        raise HTTPException(status_code=500, detail=f"Table {TABLE} not found in the database")

    matched = [(int(m.group(1)), c) for c in cols if (m := _GEN_COL_RE.match(c))]
    if not matched:
        raise HTTPException(
            status_code=500,
            detail=f"No INVERTER_xx_GEN columns found in {TABLE}",
        )
    return [c for _, c in sorted(matched)]


def _inverter_label(column: str) -> str:
    """INVERTER_01_GEN -> INVERTER_01"""
    return column[: -len("_GEN")]


def get_available_periods(db: Session) -> Dict[str, Any]:
    """
    The year/month combinations that actually carry generation data.

    Drives the MGR Month and Year dropdowns so neither can offer a period with no
    data. Purely derived from [dbo].[INVERTER_DAILY_GEN] — a new month of telemetry
    shows up in the pickers with no code change.
    """
    rows = db.execute(text(f"""
        SELECT DISTINCT YEAR([{TIME_COL}]) AS y, MONTH([{TIME_COL}]) AS m
        FROM [dbo].[{TABLE}]
        WHERE [{TIME_COL}] IS NOT NULL
        ORDER BY y, m
    """)).fetchall()

    months_by_year: Dict[str, List[int]] = {}
    for r in rows:
        if r[0] is None or r[1] is None:
            continue
        months_by_year.setdefault(str(int(r[0])), []).append(int(r[1]))

    logger.info("MGR available periods: %s", months_by_year)
    return {
        "years":          sorted(int(y) for y in months_by_year),
        "months_by_year": months_by_year,
    }


def get_monthly_generation(db: Session, month: int, year: int) -> Dict[str, Any]:
    """Per-inverter monthly generation + per-day plant generation for one month."""
    if not 1 <= month <= 12:
        raise HTTPException(status_code=400, detail="month must be between 1 and 12")
    if not 2000 <= year <= 2100:
        raise HTTPException(status_code=400, detail="year is out of range")

    gen_cols = _gen_columns(db)
    period_from = datetime(year, month, 1)
    last_day = calendar.monthrange(year, month)[1]
    period_to = datetime(year, month, last_day, 23, 59, 59, 999000)

    # One row per day: each inverter's daily counter peak (= that day's generation).
    # Column names come from the schema and are re-validated against the strict
    # INVERTER_xx_GEN pattern before interpolation, so nothing untrusted reaches SQL.
    daily_maxima = ", ".join(
        f"MAX(CAST([{c}] AS FLOAT)) AS [{c}]" for c in gen_cols if _GEN_COL_RE.match(c)
    )
    sql = text(f"""
        SELECT CAST([{TIME_COL}] AS DATE) AS gen_date, {daily_maxima}
        FROM [dbo].[{TABLE}]
        WHERE [{TIME_COL}] >= :period_from AND [{TIME_COL}] <= :period_to
        GROUP BY CAST([{TIME_COL}] AS DATE)
        ORDER BY gen_date
    """)

    try:
        rows = db.execute(sql, {"period_from": period_from, "period_to": period_to}).fetchall()
    except Exception:
        logger.error("MGR query failed for %02d/%d", month, year, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to read monthly generation data")

    totals = {c: 0.0 for c in gen_cols}
    daily: List[Dict[str, Any]] = []

    for row in rows:
        gen_date = row[0]
        day_total = 0.0
        for i, col in enumerate(gen_cols, start=1):
            v = row[i]
            if v is None:
                continue
            kwh = max(0.0, float(v))   # a negative reading can only be a bad sample
            totals[col] += kwh
            day_total += kwh
        daily.append({
            "date": gen_date.strftime("%d/%m/%Y"),
            "day": str(gen_date.day),
            "generation": round(day_total, 3),
        })

    inverters = [
        {"inverter": _inverter_label(c), "generation": round(totals[c], 3)}
        for c in gen_cols
    ]

    logger.info(
        "MGR %02d/%d | inverters=%d | days=%d | total=%.1f kWh",
        month, year, len(inverters), len(daily), sum(totals.values()),
    )

    return {
        "month": month,
        "year": year,
        "from_date": period_from.isoformat(),
        "to_date": period_to.isoformat(),
        "inverters": inverters,
        "daily": daily,
        "total_generation": round(sum(totals.values()), 3),
        "unit": "kWh",
    }
