# app/services/multi_report_service.py
"""
Multi-equipment report — one timestamp-aligned dataset spanning several tables.

The Preconfigured Reports page lets a user pick columns from different equipment
types (Inverter + WMS + PPC …). Those live in separate SQL Server tables, so the
report is assembled by querying each source through the SAME repository call the
single-equipment reports use — inheriting its interval bucketing, aggregation
rules, tag validation and SQL-injection guards — and then aligning the results on
the interval bucket key.

Alignment, not a SQL join: each source is bucketed to the same interval, so its
timestamps are directly comparable. A bucket present in one source but not another
simply has no value for the absent source's columns, which surfaces as null (blank
in Excel) rather than dropping the row or failing the report.

Column keys are namespaced `<equipment_id>.<column>` so two tables exposing the
same column name (e.g. ACTIVE_POWER) can coexist in one row, and the caller's
selection ORDER is preserved end to end.
"""

import logging
from typing import Any, Dict, List

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.repositories.reports_repository import ReportsRepository
from app.services import intervals

logger = logging.getLogger(__name__)

# Hard ceiling on the rows pulled per source, so a wide range cannot exhaust memory.
MAX_ROWS_PER_SOURCE = 20_000


def _key(equipment_id: str, column: str) -> str:
    return f"{equipment_id}.{column}"


def build_dataset(db: Session, req) -> Dict[str, Any]:
    """
    Query every source and merge on the interval bucket.

    Returns the full merged table: {columns, labels, rows, sources, total_records}.
    `columns` starts with 'timestamp' and then follows the caller's selection order.
    """
    sources = [s for s in (req.sources or []) if s.tags]
    if not sources:
        raise HTTPException(400, detail="At least one source with tags is required")

    from_dt = req.from_datetime.strftime("%Y-%m-%d %H:%M:%S")
    to_dt   = req.to_datetime.strftime("%Y-%m-%d %H:%M:%S")
    interval = intervals.norm(req.interval)
    agg      = intervals.effective_agg(req.interval, req.agg_function) or "avg"

    merged: Dict[str, Dict[str, Any]] = {}
    columns: List[str] = ["timestamp"]
    labels:  Dict[str, str] = {"timestamp": "Timestamp"}
    summary: List[Dict[str, Any]] = []

    for src in sources:
        result = ReportsRepository.get_report_data(
            db=db,
            equipment_type=src.equipment_type,
            equipment_id=src.equipment_id,
            tags=list(src.tags),
            from_datetime=from_dt,
            to_datetime=to_dt,
            interval=interval,
            agg_function=agg,
            page=1,
            page_size=MAX_ROWS_PER_SOURCE,
            compute_total=False,
        )

        # Columns the source actually returned, in the caller's requested order.
        returned = [c for c in (result.get("columns") or []) if c != "timestamp"]
        ordered  = [c for c in src.tags if c in returned] + \
                   [c for c in returned if c not in src.tags]

        for col in ordered:
            k = _key(src.equipment_id, col)
            if k not in labels:
                columns.append(k)
                labels[k] = f"{src.equipment_id} — {col.replace('_', ' ').title()}"

        rows = result.get("rows") or []
        for row in rows:
            ts = row.get("timestamp")
            if ts is None:
                continue
            bucket = merged.setdefault(str(ts), {"timestamp": ts})
            for col in ordered:
                bucket[_key(src.equipment_id, col)] = row.get(col)

        summary.append({
            "equipment_type": src.equipment_type,
            "equipment_id":   src.equipment_id,
            "columns":        len(ordered),
            "rows":           len(rows),
            "skipped_tags":   result.get("skipped_tags") or [],
        })
        logger.info(
            "Multi-report source | %s/%s | tags=%d | columns=%d | rows=%d",
            src.equipment_type, src.equipment_id, len(src.tags), len(ordered), len(rows),
        )

    # Chronological order; every row carries every column (absent → None/blank).
    ordered_rows = [
        {c: merged[ts].get(c) for c in columns}
        for ts in sorted(merged.keys())
    ]

    logger.info("Multi-report merged | sources=%d | columns=%d | rows=%d",
                len(sources), len(columns) - 1, len(ordered_rows))

    return {
        "columns":       columns,
        "labels":        labels,
        "rows":          ordered_rows,
        "sources":       summary,
        "total_records": len(ordered_rows),
    }


def get_page(db: Session, req) -> Dict[str, Any]:
    """One page of the merged dataset — drives the on-screen preview table."""
    data = build_dataset(db, req)
    total = data["total_records"]
    size  = max(1, int(req.page_size or 100))
    page  = max(1, int(req.page or 1))
    start = (page - 1) * size

    return {
        "columns":       data["columns"],
        "labels":        data["labels"],
        "rows":          data["rows"][start:start + size],
        "sources":       data["sources"],
        "total_records": total,
        "page":          page,
        "page_size":     size,
        "total_pages":   max(1, -(-total // size)),
        "interval":      intervals.norm(req.interval),
    }
