# app/services/schema_cache.py
"""
In-memory cache of the SQL Server `dbo` schema: table -> {column_name: data_type}.

Loaded once at application startup with a single INFORMATION_SCHEMA query for the
whole database, then reused for tag validation so report/export requests never
hit INFORMATION_SCHEMA per call. A cache miss (e.g. a table created after
startup) lazily loads just that table. The cache is refreshed only at startup or
via an explicit refresh() call.

Thread-safe: guarded by a re-entrant lock; safe for the multi-threaded export
workers.
"""

import time
import threading
import logging
from typing import Dict, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Non-data / system columns that are never selectable tags.
SYSTEM_COLUMNS = frozenset({"MSecCol", "LocalCol", "UserCol", "ReasonCol"})

_lock = threading.RLock()
_schema: Dict[str, Dict[str, str]] = {}          # table -> {column: data_type(lower)}
_loaded_at: Optional[float] = None


def load_all(db: Session) -> int:
    """Load the entire dbo schema in ONE query. Returns the number of tables."""
    global _schema, _loaded_at
    rows = db.execute(text("""
        SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = 'dbo'
    """)).fetchall()

    fresh: Dict[str, Dict[str, str]] = {}
    for tbl, col, dtype in rows:
        fresh.setdefault(tbl, {})[col] = (dtype or "").lower()

    with _lock:
        _schema = fresh
        _loaded_at = time.time()

    logger.info(
        "Schema cache loaded: %d tables / %d columns",
        len(fresh), sum(len(c) for c in fresh.values()),
    )
    return len(fresh)


def _load_table(db: Session, table: str) -> Dict[str, str]:
    """Lazily load a single table's columns on a cache miss."""
    rows = db.execute(text("""
        SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = :tbl
    """), {"tbl": table}).fetchall()
    cols = {r[0]: (r[1] or "").lower() for r in rows}
    if cols:
        with _lock:
            _schema[table] = cols
        logger.debug("Schema cache lazy-loaded %s (%d columns)", table, len(cols))
    return cols


def get_columns(db: Session, table: str) -> Dict[str, str]:
    """
    Return {column: data_type} for a table (cache hit = no DB round trip).
    Falls back to a single lazy query on a miss. Returns {} if the table
    does not exist.
    """
    with _lock:
        cols = _schema.get(table)
    if cols is not None:
        return cols
    return _load_table(db, table)


def get_data_columns(db: Session, table: str) -> Dict[str, str]:
    """Columns excluding system/non-data columns (TimeCol is kept out too)."""
    return {
        c: t for c, t in get_columns(db, table).items()
        if c not in SYSTEM_COLUMNS and c != "TimeCol"
    }


def table_exists(db: Session, table: str) -> bool:
    return bool(get_columns(db, table))


def refresh(db: Session, table: Optional[str] = None) -> int:
    """Explicitly refresh the whole cache, or a single table. Returns count."""
    if table:
        cols = _load_table(db, table)
        logger.info("Schema cache refreshed for %s: %d columns", table, len(cols))
        return len(cols)
    return load_all(db)


def stats() -> dict:
    with _lock:
        return {
            "tables": len(_schema),
            "columns": sum(len(c) for c in _schema.values()),
            "loaded_at": _loaded_at,
        }
