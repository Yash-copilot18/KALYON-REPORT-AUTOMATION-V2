# app/services/tracker_service.py
"""
Tracker module — production service for dbo.T1_IS2 (tracker IDs 1–125).

The table stores N trackers side-by-side as columns named `<PARAM>_ID<n>`
(e.g. ALARM_ID1, BATTERY_LEVEL_ID1 …), plus per-row timestamp columns
(TimeCol / LocalCol / MSecCol). The set of tracker IDs is DISCOVERED from the
live schema (never hardcoded), so the module scales automatically as trackers
are added.

Design:
  • Dynamic, parameterized SQL — only the selected trackers' columns are read
    (never SELECT *). Column names are generated programmatically and validated
    against the cached schema (whitelist) to prevent injection.
  • Preview reuses ReportsRepository.get_report_data (windowed aggregation,
    pagination, tag-skipping — all already optimized).
  • Excel export streams the data in batches and writes one worksheet per
    tracker with XlsxWriter in constant-memory mode (fast + low memory even for
    100+ trackers).

Business logic lives here; the router stays a thin controller.
"""

import time
import logging
from typing import List, Dict, Optional, Callable

from sqlalchemy.orm import Session

from app.services import schema_cache, isolator_columns as ic, isolator_excel
from app.repositories.reports_repository import ReportsRepository

logger = logging.getLogger(__name__)

# ── Domain configuration ─────────────────────────────────────────────────────
TRACKER_TABLE = "T1_IS2"

# Leading timestamp columns carried into every worksheet (before the params).
# MSecCol is intentionally excluded — worksheets show TimeCol + LocalCol only.
TS_COLS = ["TimeCol", "LocalCol"]

# The six parameters per tracker + the per-ID column model come from the shared
# isolator_columns module (single source of truth across tracker/isolation code).
TRACKER_PARAMS = ic.PARAMS

XLSX_MIME = isolator_excel.XLSX_MIME

# Discovered-id memo (schema is stable within a process; keyed by column count).
_ids_memo: Dict[int, List[int]] = {}


# ── Discovery & column generation (shared isolator column model) ─────────────
def discover_tracker_ids(db: Session) -> List[int]:
    """All tracker IDs present in TRACKER_TABLE (a tracker needs ALL six params)."""
    cols = schema_cache.get_columns(db, TRACKER_TABLE)
    memo_key = len(cols)
    if memo_key in _ids_memo:
        return _ids_memo[memo_key]

    ids, _ = ic.discover_ids(db, TRACKER_TABLE)
    _ids_memo[memo_key] = ids
    logger.info("Tracker discovery | table=%s | trackers=%d (range %s-%s)",
                TRACKER_TABLE, len(ids), ids[0] if ids else "-", ids[-1] if ids else "-")
    return ids


def get_available_trackers(db: Session) -> Dict:
    """Metadata for the UI: available tracker IDs + parameter definitions."""
    ids = discover_tracker_ids(db)
    return {
        "table": TRACKER_TABLE,
        "count": len(ids),
        "tracker_ids": ids,
        "params": ic.param_meta(),
    }


def _validate_ids(db: Session, requested: List[int]) -> List[int]:
    """Keep only requested IDs that exist, preserving the caller's order."""
    return ic.validate_ids(requested, discover_tracker_ids(db))


def columns_for_ids(db: Session, ids: List[int]) -> List[str]:
    """DB column list for the selected trackers — whitelisted, injection-safe, never *."""
    return ic.columns_for_ids(ids, set(schema_cache.get_columns(db, TRACKER_TABLE)))


# ── Preview (reuses the optimized report repository) ─────────────────────────
def get_tracker_data(db: Session, tracker_ids: List[int], from_dt: str, to_dt: str,
                     interval: str, agg: str, page: int, page_size: int) -> Dict:
    """Paginated preview for the selected trackers (flat columns)."""
    ids = _validate_ids(db, tracker_ids)
    if not ids:
        return {
            "table_name": TRACKER_TABLE, "tracker_ids": [], "columns": ["timestamp"],
            "rows": [], "total_records": 0, "page": page, "page_size": page_size,
            "params": [{"key": k, "label": lbl, "unit": u} for k, lbl, u in TRACKER_PARAMS],
        }
    cols = columns_for_ids(db, ids)
    result = ReportsRepository.get_report_data(
        db=db, equipment_type="Tracker", equipment_id=TRACKER_TABLE, tags=cols,
        from_datetime=from_dt, to_datetime=to_dt, interval=interval,
        agg_function=agg, page=page, page_size=page_size,
    )
    result["tracker_ids"] = ids
    result["params"] = [{"key": k, "label": lbl, "unit": u} for k, lbl, u in TRACKER_PARAMS]
    return result


# ── Excel export — one worksheet per tracker (shared isolator_excel builder) ─
def build_tracker_workbook(tracker_ids: List[int], from_dt: str, to_dt: str,
                           interval: str, agg: str,
                           progress: Optional[Callable[[int, str], None]] = None):
    """
    Build one workbook with one worksheet per selected tracker of TRACKER_TABLE
    (dbo.T1_IS2). Delegates to the generic isolator_excel builder with the
    tracker's leading timestamp columns (TimeCol + LocalCol). Returns
    (bytes, filename). Memory-bounded even for all 125 trackers.
    """
    data, ids = isolator_excel.build_workbook(
        TRACKER_TABLE, tracker_ids, from_dt, to_dt, interval, agg,
        ts_cols=TS_COLS, sheet_title="Tracker", progress=progress,
    )
    total = len(ids)
    filename = (f"Tracker_{ids[0]:02d}_Report_{time.strftime('%d-%m-%Y')}.xlsx"
                if total == 1 else
                f"Trackers_{total}_Report_{time.strftime('%d-%m-%Y')}.xlsx")
    return data, filename
