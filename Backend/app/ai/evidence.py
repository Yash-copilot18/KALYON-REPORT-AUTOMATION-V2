"""
Shared data access for the assurance engine.

Two jobs:

* **Source inventory** - discover what each table actually holds (coverage window,
  row count, detected sampling cadence) so modules can decide whether they may run.
  Nothing about the plant is hardcoded here; it is all read from the database.
* **Safe primitives** - every identifier is whitelisted through the repository's
  existing `_safe_name` before it reaches SQL, and every value is bound.

The cadence is *detected*, never assumed: the engine reads the modal gap between
consecutive timestamps. A source that changes cadence is therefore described
correctly rather than measured against a figure someone typed in.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.repositories.reports_repository import _safe_name

logger = logging.getLogger(__name__)

TIME_COL = "TimeCol"

# Tables the assurance engine reads. Grouped so a module can ask for "the inverter
# fleet" without knowing how many there are - the inventory confirms which exist.
PLANT_SOURCES: Dict[str, List[str]] = {
    "inverters":  [f"INVERTER_{i:02d}" for i in range(1, 25)],
    "smb":        [f"INVERTER{i}_SMB" for i in range(1, 25)],
    "trackers":   [f"T1_IS{i}" for i in range(1, 13)] + [f"T2_IS{i}" for i in range(13, 25)],
    "ppc":        ["PPC", "PPC_TREND"],
    "weather":    ["WMS"],
    "generation": ["INVERTER_DAILY_GEN", "INVERTER_MONTHLY_GEN"],
    "alarms":     ["Alarms"],
    "thermal":    ["INVERTER_TEMP", "TEMPERATURE_REPORT"],
    "tracker_status": ["TRACKER_MBOX_STATUS"],
    "derived":    ["POWER_GRAPH", "POWER_VS_IRRADIANCE"],
}


def _check(table: str) -> str:
    if not _safe_name(table):
        raise HTTPException(400, detail=f"Invalid table: {table!r}")
    return table


def table_exists(db: Session, table: str) -> bool:
    _check(table)
    return bool(db.execute(
        text("SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = :t"),
        {"t": table}).first())


def columns_of(db: Session, table: str) -> List[str]:
    _check(table)
    return [r[0] for r in db.execute(
        text("SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
             "WHERE TABLE_NAME = :t ORDER BY ORDINAL_POSITION"), {"t": table}).fetchall()]


def numeric_columns(db: Session, table: str) -> List[str]:
    """Measurement columns only - the bookkeeping columns are never analysed."""
    _check(table)
    rows = db.execute(
        text("""SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_NAME = :t
                  AND DATA_TYPE IN ('float','real','decimal','numeric','int','bigint','smallint')
                ORDER BY ORDINAL_POSITION"""), {"t": table}).fetchall()
    skip = {"MSecCol", "UserCol", "ReasonCol", "LocalCol"}
    return [r[0] for r in rows if r[0] not in skip]


def detect_cadence_seconds(db: Session, table: str,
                           start: Optional[str] = None,
                           end: Optional[str] = None,
                           sample: int = 5000) -> Optional[int]:
    """
    The MODAL gap between consecutive rows, in seconds - the source's real sampling
    interval, measured rather than assumed.

    The mode (not the mean) is used deliberately: outages and restarts create huge
    gaps that would drag an average far away from the true cadence.

    Measured over the most recent `sample` rows rather than the whole table. A modal
    interval is stable over a few thousand samples, and scanning every row of ~90
    tables made the pre-flight call take about a minute - far too slow for a page
    load, and no more accurate.
    """
    _check(table)
    where, params = _window_clause(start, end)
    row = db.execute(text(f"""
        SELECT TOP 1 delta, COUNT(*) AS n FROM (
            SELECT DATEDIFF(second, LAG({TIME_COL}) OVER (ORDER BY {TIME_COL}), {TIME_COL}) AS delta
            FROM (
                SELECT TOP {int(sample)} {TIME_COL} FROM [{table}] {where}
                ORDER BY {TIME_COL} DESC
            ) s
        ) d
        WHERE delta IS NOT NULL AND delta > 0
        GROUP BY delta ORDER BY n DESC
    """), params).first()
    return int(row[0]) if row else None


def _window_clause(start: Optional[str], end: Optional[str]) -> Tuple[str, Dict[str, Any]]:
    if start and end:
        return f"WHERE {TIME_COL} >= :ws AND {TIME_COL} <= :we", {"ws": start, "we": end}
    if start:
        return f"WHERE {TIME_COL} >= :ws", {"ws": start}
    if end:
        return f"WHERE {TIME_COL} <= :we", {"we": end}
    return "", {}


def source_coverage(db: Session, table: str) -> Dict[str, Any]:
    """First record, last record, row count and detected cadence for one table."""
    _check(table)
    if not table_exists(db, table):
        return {"table": table, "exists": False}
    # MIN/MAX ride the timestamp index; the row count comes from partition metadata
    # rather than COUNT(*), which would scan every one of ~90 tables just to build an
    # inventory. The count is used for sizing and display, never for a calculation,
    # so metadata accuracy is ample.
    row = db.execute(text(
        f"SELECT MIN({TIME_COL}), MAX({TIME_COL}) FROM [{table}]")).first()
    n = int(db.execute(text("""
        SELECT SUM(p.rows) FROM sys.tables t
        JOIN sys.partitions p ON t.object_id = p.object_id AND p.index_id IN (0, 1)
        WHERE t.name = :t
    """), {"t": table}).scalar() or 0)
    first, last = row[0], row[1]
    cadence = detect_cadence_seconds(db, table) if n > 1 else None
    return {
        "table": table, "exists": True, "rows": n,
        "first": first.isoformat() if first else None,
        "last": last.isoformat() if last else None,
        "cadence_seconds": cadence,
    }


_INVENTORY_CACHE: Dict[str, Any] = {}
_INVENTORY_TTL_SECONDS = 300


def build_inventory(db: Session, use_cache: bool = True) -> Dict[str, Any]:
    """
    What the engine has to work with. Computed once per report and handed to every
    module, so each can decide AVAILABLE / PARTIAL / BLOCKED from measured facts.

    Also reports the OVERLAP across groups, which is the figure that actually limits
    a multi-source analysis: an inverter/irradiance study can only span the period
    where both exist.
    """
    import time as _time
    if use_cache:
        hit = _INVENTORY_CACHE.get("data")
        if hit and (_time.time() - _INVENTORY_CACHE.get("at", 0)) < _INVENTORY_TTL_SECONDS:
            return hit

    groups: Dict[str, Any] = {}
    for group, tables in PLANT_SOURCES.items():
        entries = [source_coverage(db, t) for t in tables]
        present = [e for e in entries if e.get("exists") and e.get("rows")]
        firsts = [e["first"] for e in present if e["first"]]
        lasts = [e["last"] for e in present if e["last"]]
        cadences = [e["cadence_seconds"] for e in present if e["cadence_seconds"]]
        groups[group] = {
            "tables": entries,
            "present_count": len(present),
            "expected_count": len(tables),
            # Widest span the group covers, and the narrowest span common to all of
            # its tables - a group is only fully usable over the latter.
            "earliest": min(firsts) if firsts else None,
            "latest": max(lasts) if lasts else None,
            "common_start": max(firsts) if firsts else None,
            "common_end": min(lasts) if lasts else None,
            "cadence_seconds": _mode(cadences),
            "total_rows": sum(e.get("rows", 0) for e in present),
        }

    usable = [g for g in groups.values() if g["common_start"] and g["common_end"]]
    out = {
        "groups": groups,
        "plant_earliest": min((g["earliest"] for g in usable), default=None),
        "plant_latest": max((g["latest"] for g in usable), default=None),
        # The window where EVERY present group has data - the honest ceiling for a
        # cross-source forensic analysis.
        "all_source_overlap_start": max((g["common_start"] for g in usable), default=None),
        "all_source_overlap_end": min((g["common_end"] for g in usable), default=None),
    }
    _INVENTORY_CACHE["data"], _INVENTORY_CACHE["at"] = out, _time.time()
    return out


def _mode(values: List[int]) -> Optional[int]:
    if not values:
        return None
    counts: Dict[int, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return max(counts.items(), key=lambda kv: kv[1])[0]


def group_window(inventory: Dict[str, Any], group: str) -> Tuple[Optional[str], Optional[str]]:
    g = inventory.get("groups", {}).get(group) or {}
    return g.get("common_start"), g.get("common_end")


def clamp_window(requested_start: str, requested_end: str,
                 avail_start: Optional[str], avail_end: Optional[str]
                 ) -> Tuple[Optional[str], Optional[str], bool]:
    """
    Intersect what was asked for with what exists. Returns (start, end, clamped);
    (None, None, True) when the request lies entirely outside the data, which a
    module must treat as BLOCKED rather than returning an empty result as if it
    were a finding of "no activity".
    """
    if not avail_start or not avail_end:
        return None, None, True
    start = max(requested_start, avail_start)
    end = min(requested_end, avail_end)
    if start >= end:
        return None, None, True
    clamped = start != requested_start or end != requested_end
    return start, end, clamped


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "").replace("T", " ").strip()
                                  .replace(" ", "T", 1))
