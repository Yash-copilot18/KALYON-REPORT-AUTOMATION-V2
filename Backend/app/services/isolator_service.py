# app/services/isolator_service.py
"""
Isolator data service — dynamic, ID-scoped querying for the `dbo.T1_IS*` /
`dbo.T2_IS*` tables (e.g. T1_IS4: 126 isolators IS01–IS126, six params each).

The whole point: never read all ~750 columns. The caller selects isolator IDs;
the backend expands each to its six `<PARAM>_ID<n>` columns (+ TimeCol always),
builds the SQL dynamically and safely, and returns React-friendly JSON.

Performance:
  • Only the selected IDs' columns are projected — never SELECT *.
  • The heavy lifting reuses ReportsRepository.get_report_data, which already
    does windowed aggregation + pagination (scans only the page's time window).
  • Wide selections are split into column BATCHES fetched in PARALLEL (one DB
    session per worker) and merged by timestamp — narrower queries, overlapped
    execution, one total-count query overall.

Scalability: IDs are discovered from the live schema, so IS127+ needs no code
change. Table access is whitelisted to the isolator families.
"""

import time
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Optional

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database.session import SessionLocal
from app.services import schema_cache, isolator_columns as ic
from app.repositories.reports_repository import ReportsRepository, _build_interval_expr

logger = logging.getLogger(__name__)

# A selection wider than this many IDs is fetched as parallel column batches.
_PARALLEL_ID_BATCH = 24          # 24 IDs × 6 = 144 columns per query
_MAX_WORKERS       = 4           # bounded — SQL Server Express is CPU-capped
_EQUIP_TYPE        = "T1 Isolation"   # reuses the registry-agnostic data path

# Live-status snapshot cache — collapses concurrent polls/WS ticks into one query.
_SNAPSHOT_TTL   = 3.0            # seconds a latest-row snapshot stays fresh
_LOW_BATTERY_PCT = 20.0         # display threshold for the "low battery" count
_snap_cache: Dict[str, tuple] = {}     # table -> (expires_at, snapshot)
_snap_lock = threading.Lock()


# ── Validation ───────────────────────────────────────────────────────────────
def _require_table(db: Session, table: str) -> None:
    if not ic.is_isolator_table(table):
        raise HTTPException(400, detail=f"Invalid isolator table: {table!r}")
    if not schema_cache.table_exists(db, table):
        raise HTTPException(404, detail=f"Table '{table}' not found")


# ── Metadata (for the UI selector) ───────────────────────────────────────────
def get_metadata(db: Session, table: str) -> Dict:
    _require_table(db, table)
    ids, _ = ic.discover_ids(db, table)
    return {
        "table": table,
        "count": len(ids),
        "isolator_ids": ids,
        "params": ic.param_meta(),
    }


# ── Data (dynamic, ID-scoped, batched + parallel) ────────────────────────────
def _column_meta(columns: List[str]) -> List[Dict]:
    meta = []
    for c in columns:
        if c == "timestamp":
            meta.append({"column": "timestamp", "label": "Timestamp", "unit": "",
                         "isolator_id": None, "param": None})
            continue
        prefix, tid = ic.parse_column(c)
        meta.append({
            "column": c, "label": ic.col_label(c), "unit": ic.col_unit(c),
            "isolator_id": tid if tid >= 0 else None,
            "param": prefix or None,
        })
    return meta


def _fetch_batch(table, cols, from_dt, to_dt, interval, agg, page, page_size, compute_total):
    """Run one column-batch query on its own session (thread-safe)."""
    db = SessionLocal()
    try:
        return ReportsRepository.get_report_data(
            db=db, equipment_type=_EQUIP_TYPE, equipment_id=table, tags=cols,
            from_datetime=from_dt, to_datetime=to_dt, interval=interval,
            agg_function=agg, page=page, page_size=page_size,
            compute_total=compute_total,
        )
    finally:
        db.close()


def _merge_by_timestamp(base_rows: List[Dict], others: List[List[Dict]]) -> List[Dict]:
    """Merge parallel column-batch rows onto the base rows, keyed by timestamp."""
    by_ts: Dict[str, Dict] = {}
    order: List[str] = []
    for row in base_rows:
        ts = row.get("timestamp")
        by_ts[ts] = dict(row)
        order.append(ts)
    for rows in others:
        for row in rows:
            tgt = by_ts.get(row.get("timestamp"))
            if tgt is None:
                continue
            for k, v in row.items():
                if k != "timestamp":
                    tgt[k] = v
    return [by_ts[ts] for ts in order]


def get_data(
    db: Session,
    table: str,
    isolator_ids: List[int],
    from_dt: str,
    to_dt: str,
    interval: str,
    agg: str,
    page: int = 1,
    page_size: int = 100,
) -> Dict:
    """
    Return paginated telemetry for the selected isolator IDs as React-ready JSON.
    Only TimeCol + the selected IDs' six params are queried (never SELECT *).
    """
    _require_table(db, table)

    available, valid_cols = ic.discover_ids(db, table)
    valid_ids = ic.validate_ids(isolator_ids, available)
    skipped_ids = [i for i in isolator_ids if i not in valid_ids]

    base = {
        "table": table,
        "isolator_ids": valid_ids,
        "skipped_ids": skipped_ids,
        "from_datetime": from_dt,
        "to_datetime": to_dt,
        "interval": interval,
        "agg_function": agg,
        "page": page,
        "page_size": page_size,
        "params": ic.param_meta(),
    }

    if not valid_ids:
        logger.warning("Isolator data | table=%s | no valid IDs from %s", table, isolator_ids)
        return {**base, "columns": ["timestamp"], "column_meta": _column_meta(["timestamp"]),
                "rows": [], "total_records": 0}

    # Dynamic, whitelisted column list — six per selected ID, TimeCol implicit.
    columns = ic.columns_for_ids(valid_ids, valid_cols)
    id_batches = [valid_ids[i:i + _PARALLEL_ID_BATCH]
                  for i in range(0, len(valid_ids), _PARALLEL_ID_BATCH)]

    t0 = time.perf_counter()

    if len(id_batches) == 1:
        # Small selection — one query is already optimal.
        result = _fetch_batch(table, columns, from_dt, to_dt, interval, agg,
                              page, page_size, compute_total=True)
        rows, total = result["rows"], result["total_records"]
        mode = "single"
    else:
        # Wide selection — parallel column batches merged by timestamp. Only the
        # first batch computes the (identical) total count.
        batch_cols = [ic.columns_for_ids(b, valid_cols) for b in id_batches]
        results: List[Optional[Dict]] = [None] * len(id_batches)

        def work(idx):
            return idx, _fetch_batch(table, batch_cols[idx], from_dt, to_dt,
                                     interval, agg, page, page_size,
                                     compute_total=(idx == 0))

        with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(id_batches))) as ex:
            for idx, res in ex.map(work, range(len(id_batches))):
                results[idx] = res

        total = results[0]["total_records"]
        rows = _merge_by_timestamp(results[0]["rows"], [r["rows"] for r in results[1:]])
        mode = f"parallel×{len(id_batches)}"

    elapsed = (time.perf_counter() - t0) * 1000
    logger.info(
        "Isolator data | table=%s | ids=%d | cols=%d | rows=%d | mode=%s | %.0fms",
        table, len(valid_ids), len(columns), len(rows), mode, elapsed,
    )

    ordered_cols = ["timestamp"] + columns
    return {
        **base,
        "columns": ordered_cols,
        "column_meta": _column_meta(ordered_cols),
        "rows": rows,
        "total_records": total,
    }


# ── Live status snapshot (latest TimeCol row) ────────────────────────────────
def _num(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _iso(ts) -> Optional[str]:
    return ts.isoformat() if ts is not None and hasattr(ts, "isoformat") else (str(ts) if ts is not None else None)


def _compute_snapshot(db: Session, table: str) -> Dict:
    """
    Read the SINGLE newest telemetry row and derive per-tracker status + aggregate
    KPIs. Columns are generated dynamically from the discovered IDs (never SELECT *),
    and only the six params per tracker + TimeCol are projected.
    """
    if not ic.is_isolator_table(table):
        raise HTTPException(400, detail=f"Invalid isolator table: {table!r}")
    ids, valid_cols = ic.discover_ids(db, table)
    if not ids:
        raise HTTPException(404, detail=f"No trackers found in '{table}'")

    cols = ic.columns_for_ids(ids, valid_cols)          # 6 per tracker, in order
    col_sql = ", ".join(f"[{c}]" for c in cols)
    # ONE row, newest first — clustered index on TimeCol makes this an index seek.
    row = db.execute(text(
        f"SELECT TOP 1 TimeCol, {col_sql} FROM [{table}] ORDER BY TimeCol DESC"
    )).fetchone()

    if row is None:
        return {"table": table, "timestamp": None, "kpis": _empty_kpis(len(ids)), "trackers": []}

    ts = row[0]
    vals = dict(zip(cols, row[1:]))

    trackers: List[Dict] = []
    active_alarms = 0
    batt, terr, mcur = [], [], []
    modes: Dict[str, int] = {}
    low_battery = 0

    for tid in ids:
        alarm   = vals.get(f"ALARM_ID{tid}")
        battery = _num(vals.get(f"BATTERY_LEVEL_ID{tid}"))
        pos     = _num(vals.get(f"ELEVATION_POSITION_ID{tid}"))
        setp    = _num(vals.get(f"ELEVATION_SETPOINT_ID{tid}"))
        motor   = _num(vals.get(f"MAX_MOTOR_CURRENT_ID{tid}"))
        mode    = vals.get(f"OPERATION_MODE_ID{tid}")

        has_alarm = alarm is not None and _num(alarm) not in (0, None)
        if has_alarm:
            active_alarms += 1
        if battery is not None:
            batt.append(battery)
            if battery < _LOW_BATTERY_PCT:
                low_battery += 1
        tracking_error = abs(pos - setp) if pos is not None and setp is not None else None
        if tracking_error is not None:
            terr.append(tracking_error)
        if motor is not None:
            mcur.append(motor)
        mode_key = str(int(_num(mode))) if _num(mode) is not None else "unknown"
        modes[mode_key] = modes.get(mode_key, 0) + 1

        trackers.append({
            "id": tid,
            "label": ic.id_label(tid),
            "alarm": int(_num(alarm)) if _num(alarm) is not None else None,
            "battery_level": round(battery, 1) if battery is not None else None,
            "elevation_position": round(pos, 2) if pos is not None else None,
            "elevation_setpoint": round(setp, 2) if setp is not None else None,
            "tracking_error": round(tracking_error, 2) if tracking_error is not None else None,
            "max_motor_current": round(motor, 2) if motor is not None else None,
            "operation_mode": int(_num(mode)) if _num(mode) is not None else None,
            "status": "alarm" if has_alarm else "ok",
        })

    def _avg(xs): return round(sum(xs) / len(xs), 2) if xs else None
    kpis = {
        "total": len(ids),
        "active_alarms": active_alarms,
        "healthy": len(ids) - active_alarms,
        "avg_battery": round(sum(batt) / len(batt), 1) if batt else None,
        "min_battery": round(min(batt), 1) if batt else None,
        "low_battery": low_battery,
        "avg_tracking_error": _avg(terr),
        "max_tracking_error": round(max(terr), 2) if terr else None,
        "avg_motor_current": _avg(mcur),
        "max_motor_current": round(max(mcur), 2) if mcur else None,
        "mode_distribution": modes,
        "timestamp": _iso(ts),
    }
    return {"table": table, "timestamp": _iso(ts), "kpis": kpis, "trackers": trackers}


def _empty_kpis(total: int) -> Dict:
    return {"total": total, "active_alarms": 0, "healthy": total, "avg_battery": None,
            "min_battery": None, "low_battery": 0, "avg_tracking_error": None,
            "max_tracking_error": None, "avg_motor_current": None, "max_motor_current": None,
            "mode_distribution": {}, "timestamp": None}


def get_snapshot(db: Session, table: str) -> Dict:
    """Cached latest-row snapshot (per-tracker status + KPIs). TTL collapses polls."""
    now = time.time()
    with _snap_lock:
        ent = _snap_cache.get(table)
        if ent and ent[0] > now:
            return ent[1]
    snap = _compute_snapshot(db, table)          # DB call outside the lock
    with _snap_lock:
        _snap_cache[table] = (time.time() + _SNAPSHOT_TTL, snap)
    return snap


def snapshot_standalone(table: str) -> Dict:
    """Snapshot on a fresh session — used by the WebSocket loop (own thread)."""
    db = SessionLocal()
    try:
        return get_snapshot(db, table)
    finally:
        db.close()


def get_kpis(db: Session, table: str) -> Dict:
    """Lightweight KPI-only view (reuses the cached snapshot)."""
    snap = get_snapshot(db, table)
    return {"table": table, "timestamp": snap["timestamp"], "kpis": snap["kpis"]}


# ── Historical trend for charts (aggregated over TimeCol buckets) ────────────
def get_trend(db: Session, table: str, from_dt: str, to_dt: str, interval: str) -> Dict:
    """
    Per-interval trend across ALL trackers: fleet active-alarm count, average
    battery level, and average tracking error. One grouped pass over the range —
    expressions are generated dynamically from the discovered IDs (never SELECT *).
    """
    if not ic.is_isolator_table(table):
        raise HTTPException(400, detail=f"Invalid isolator table: {table!r}")
    ids, valid_cols = ic.discover_ids(db, table)
    if not ids:
        raise HTTPException(404, detail=f"No trackers found in '{table}'")

    # IDs are integers → safe to interpolate the generated column names.
    n = len(ids)
    alarm_terms = " + ".join(
        f"(CASE WHEN [ALARM_ID{i}] <> 0 THEN 1 ELSE 0 END)" for i in ids
        if f"ALARM_ID{i}" in valid_cols)
    batt_terms = " + ".join(
        f"CAST(ISNULL([BATTERY_LEVEL_ID{i}],0) AS FLOAT)" for i in ids
        if f"BATTERY_LEVEL_ID{i}" in valid_cols)
    te_terms = " + ".join(
        f"ABS(CAST(ISNULL([ELEVATION_POSITION_ID{i}],0) AS FLOAT) - "
        f"CAST(ISNULL([ELEVATION_SETPOINT_ID{i}],0) AS FLOAT))" for i in ids
        if f"ELEVATION_POSITION_ID{i}" in valid_cols and f"ELEVATION_SETPOINT_ID{i}" in valid_cols)

    grp = _build_interval_expr(interval)
    sql = text(f"""
        SELECT {grp} AS ts,
               AVG(CAST(({alarm_terms}) AS FLOAT))       AS active_alarms,
               AVG(({batt_terms}) / {float(n)})          AS avg_battery,
               AVG(({te_terms}) / {float(n)})            AS avg_tracking_error
        FROM [{table}]
        WHERE TimeCol BETWEEN :from_dt AND :to_dt
        GROUP BY {grp} ORDER BY {grp}
    """)
    t0 = time.perf_counter()
    rows = db.execute(sql, {"from_dt": from_dt, "to_dt": to_dt}).fetchall()
    points = [{
        "timestamp": _iso(r[0]),
        "active_alarms": round(_num(r[1]), 2) if r[1] is not None else 0,
        "avg_battery": round(_num(r[2]), 1) if r[2] is not None else None,
        "avg_tracking_error": round(_num(r[3]), 2) if r[3] is not None else None,
    } for r in rows]
    logger.info("Isolator trend | table=%s | ids=%d | interval=%s | points=%d | %.0fms",
                table, n, interval, len(points), (time.perf_counter() - t0) * 1000)
    return {"table": table, "interval": interval, "from_datetime": from_dt,
            "to_datetime": to_dt, "points": points}
