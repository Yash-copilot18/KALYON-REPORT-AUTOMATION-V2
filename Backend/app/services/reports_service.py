# app/services/reports_service.py

from sqlalchemy.orm import Session
from typing import List, Optional, Dict, Callable, Tuple
from fastapi import HTTPException
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
import threading
import time

from sqlalchemy import text

from app.repositories.reports_repository import (
    ReportsRepository, EQUIPMENT_REGISTRY, _safe_name, _build_interval_expr,
)
from app.services import schema_cache, intervals, isolator_columns
from app.schemas.reports_schema import ReportDataRequest
from app.database.session import SessionLocal

logger = logging.getLogger(__name__)

# Concurrency for multi-equipment batch loads. Bounded so 12+ equipment don't
# stampede SQL Server with heavy aggregations at once (each worker uses its own
# DB session/connection from the pool).
_BATCH_WORKERS = 5

# The multi-equipment batch load returns the FULL selected dataset (every device,
# every selected column, all rows in range) — the client renders it through a
# virtualized table, so the DOM stays tiny regardless of size. `req.page_size`
# is the per-equipment fetch bound (the client requests a large value to pull the
# whole range); a very high hard ceiling only guards against pathological OOM and
# is never hit in normal use.
_BATCH_MAX_ROWS_PER_EQUIPMENT = 200_000

# ── Merged-page row counts cache ──────────────────────────────────────────────
# Per-device bucket/row count for (table, from, to, interval). Independent of the
# selected tags (counting groups on the interval key only), so it is reused across
# tag selections and across every page request of an infinite-scroll session.
# Cheap to compute (~tens of ms) but cached so paging never recomputes it.
_count_cache: Dict[Tuple[str, str, str, str], Tuple[int, float]] = {}
_count_lock = threading.Lock()
_COUNT_TTL = 300  # seconds
_COUNT_WORKERS = 6


def _map_tags_to_columns(equipment_type: str, tags: List[str]) -> List[str]:
    """
    Resolve tag labels → real column names ONCE (shared across the batch), then apply
    this equipment type's canonical column ORDER.

    For the isolator/Tracker types the order comes from
    `isolator_columns.order_columns_by_id` — the SAME function the Excel export uses —
    so the Report Data table and the generated workbook always present the columns in
    the identical Id-first sequence, whatever order the client happened to send its
    tag list in. Every other equipment type (Inverter, WMS, PPC, Alarms, MFM, String
    Combiner, …) keeps the caller's order exactly as before.
    """
    tag_meta = EQUIPMENT_REGISTRY.get(equipment_type, {}).get("tags", {})
    if not tag_meta:
        out = list(tags)
    else:
        out = []
        for tag in tags:
            if tag in tag_meta:
                out.append(tag)
            else:
                out.append(next(
                    (col for col, meta in tag_meta.items()
                     if meta["label"] == tag or col == tag),
                    tag,
                ))
    if equipment_type in isolator_columns.ISOLATION_EQUIPMENT_TYPES:
        out = isolator_columns.order_columns_by_id(out)
    return out


class ReportsService:

    @staticmethod
    def get_equipment_types(db: Session) -> List[Dict]:
        try:
            return ReportsRepository.get_equipment_types(db)
        except Exception as e:
            logger.error(f"get_equipment_types error: {e}", exc_info=True)
            return [
                {
                    "equipment_type": k,
                    "table_name": v["tables"][0],
                    "equipment_count": len(v["tables"]),
                }
                for k, v in EQUIPMENT_REGISTRY.items()
            ]

    @staticmethod
    def get_equipment_list(db: Session, equipment_type: str) -> List[Dict]:
        if not equipment_type:
            raise HTTPException(400, detail="equipment_type is required")
        try:
            return ReportsRepository.get_equipment_list(db, equipment_type)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"get_equipment_list error: {e}", exc_info=True)
            raise HTTPException(500, detail=str(e))

    @staticmethod
    def get_tags(
        db: Session,
        equipment_type: str,
        equipment_id: Optional[str] = None
    ) -> List[Dict]:
        if not equipment_type:
            raise HTTPException(400, detail="equipment_type is required")
        try:
            return ReportsRepository.get_tags(db, equipment_type, equipment_id)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"get_tags error: {e}", exc_info=True)
            raise HTTPException(500, detail=str(e))

    @staticmethod
    def get_report_data_batch(
        req: ReportDataRequest,
        equipment_ids: List[str],
        progress: Optional[Callable[[int, str], None]] = None,
    ) -> Dict:
        """
        Load a paginated preview for MANY equipment concurrently (bounded pool),
        merging into a single table with an `_equipment` column.

        - Tag→column mapping is resolved once and shared (no duplicate metadata).
        - Each worker gets its own DB session; missing tags/tables degrade to
          warnings per equipment and never fail the whole batch.
        """
        ids = [e for e in equipment_ids if e and str(e).strip()]
        if not ids:
            raise HTTPException(400, detail="No equipment selected")

        column_names = _map_tags_to_columns(req.equipment_type, req.tags)
        from_dt = req.from_datetime.strftime("%Y-%m-%d %H:%M:%S")
        to_dt   = req.to_datetime.strftime("%Y-%m-%d %H:%M:%S")
        # Fetch the full range per equipment (client requests a large page_size);
        # only the OOM ceiling bounds it. No column capping — every selected tag.
        per_equipment_rows = min(req.page_size or _BATCH_MAX_ROWS_PER_EQUIPMENT,
                                 _BATCH_MAX_ROWS_PER_EQUIPMENT)
        t0 = time.perf_counter()

        def _work(eid: str):
            db = SessionLocal()
            try:
                r = ReportsRepository.get_report_data(
                    db=db, equipment_type=req.equipment_type, equipment_id=eid,
                    tags=column_names, from_datetime=from_dt, to_datetime=to_dt,
                    interval=req.interval.value, agg_function=req.agg_function.value,
                    page=1, page_size=per_equipment_rows,
                    compute_total=False,   # merged view stacks every equipment's rows
                )
                return eid, r, None
            except HTTPException as he:
                return eid, None, he.detail
            except Exception as e:  # noqa: BLE001 — isolate one equipment's failure
                logger.error("Batch load failed for %s: %s", eid, e, exc_info=True)
                return eid, None, str(e)
            finally:
                db.close()

        results: Dict[str, Dict] = {}
        errors:  Dict[str, str]  = {}
        done = 0
        total = len(ids)
        if progress:
            progress(1, f"Loading {total} equipment ({_BATCH_WORKERS} in parallel)…")

        with ThreadPoolExecutor(max_workers=_BATCH_WORKERS) as ex:
            futures = {ex.submit(_work, eid): eid for eid in ids}
            for fut in as_completed(futures):
                eid, r, err = fut.result()
                if r is not None:
                    results[eid] = r
                if err:
                    errors[eid] = err
                done += 1
                if progress:
                    progress(int(done / total * 100), f"Loaded {done}/{total} equipment")

        # Merge in the caller's original order. Prefer a base column set from an
        # equipment that had all tags available.
        merged_rows, skipped_groups, base_cols = [], [], None
        for eid in ids:
            r = results.get(eid)
            if not r:
                continue
            if base_cols is None or not r.get("skipped_tags"):
                base_cols = r.get("columns") or base_cols
            for row in r.get("rows", []):
                merged_rows.append({"_equipment": eid, **row})
            if r.get("skipped_tags"):
                skipped_groups.append({"id": eid, "tags": r["skipped_tags"]})

        failed = [{"id": eid, "error": errors[eid]} for eid in ids if eid in errors]

        # Full column set — every selected tag (no display truncation). base_cols is
        # [timestamp, <tag cols…>]; the merged table prefixes an `_equipment` column.
        display_columns = ["_equipment"] + (base_cols or ["timestamp"])

        logger.info(
            "Batch load | type=%s | equipment=%d (ok=%d, failed=%d) | tags=%d | "
            "rows=%d (<=%d/eq) | cols=%d | %.0fms",
            req.equipment_type, total, len(results), len(failed),
            len(column_names), len(merged_rows), per_equipment_rows,
            len(display_columns), (time.perf_counter() - t0) * 1000,
        )

        return {
            "equipment_type": req.equipment_type,
            "interval":       req.interval.value,
            "agg_function":   req.agg_function.value,
            "columns":        display_columns,
            "rows":           merged_rows,
            "total_records":  len(merged_rows),
            "equipment_count": total,
            "skipped_groups": skipped_groups,
            "failed":         failed,
        }

    # ── Server-side paginated merged view (infinite scroll) ───────────────────
    @staticmethod
    def _device_row_count(table: str, from_dt: str, to_dt: str, interval: str) -> int:
        """Cached, tag-independent row/bucket count for one device over a range."""
        key = (table, from_dt, to_dt, interval)
        now = time.time()
        with _count_lock:
            hit = _count_cache.get(key)
            if hit and now - hit[1] < _COUNT_TTL:
                return hit[0]
        db = SessionLocal()
        try:
            n = ReportsRepository.count_report_rows(db, table, from_dt, to_dt, interval)
        finally:
            db.close()
        with _count_lock:
            _count_cache[key] = (n, now)
        return n

    @staticmethod
    def _equipment_counts(
        ids: List[str], column_names: List[str], from_dt: str, to_dt: str,
        interval: str, progress: Optional[Callable[[int, str], None]] = None,
    ) -> Dict[str, int]:
        """
        Per-device row counts, computed in parallel (cached). A device with none of
        the selected tags present contributes 0 rows — matching what get_report_data
        returns — so the global offset mapping stays exact.
        """
        counts: Dict[str, int] = {}
        done = [0]
        lock = threading.Lock()

        def work(eid):
            # A device whose schema has none of the selected tags returns no rows.
            db = SessionLocal()
            try:
                cols = schema_cache.get_columns(db, eid)
                has_valid = any(_safe_name(t) and t in cols for t in column_names)
                n = ReportsService._device_row_count(eid, from_dt, to_dt, interval) if has_valid else 0
            finally:
                db.close()
            return eid, n

        with ThreadPoolExecutor(max_workers=_COUNT_WORKERS) as ex:
            futs = {ex.submit(work, e): e for e in ids}
            for fut in as_completed(futs):
                eid, n = fut.result()
                counts[eid] = n
                with lock:
                    done[0] += 1
                    if progress:
                        progress(int(done[0] / len(ids) * 100),
                                 f"Preparing {done[0]}/{len(ids)} equipment…")
        return counts

    @staticmethod
    def _ordered_page_keys(
        active_ids: List[str], from_dt: str, to_dt: str, interval: str,
        offset: int, limit: int,
    ) -> List[Tuple[object, int]]:
        """
        The page's (timestamp, device-ordinal) pairs in (equipment, timestamp) order.
        Built from a UNION-ALL of per-device interval-key selects — this touches only
        TimeCol (clustered index) and groups on the key, so it is cheap even across
        all devices, and paginates with OFFSET/FETCH. `ordinal` indexes `active_ids`
        (selection order), so equipment blocks appear in the user's selection order.

        Ordering is (equipment-ordinal, timestamp): each device's rows form one
        continuous block, timestamps ascending within the block, and the blocks
        follow the selection order — the report reads one equipment at a time rather
        than interleaving devices at each timestamp.
        """
        if not active_ids:
            return []
        agg_interval = not intervals.is_instant(interval)
        grp = _build_interval_expr(interval) if agg_interval else "TimeCol"
        parts = []
        for i, eid in enumerate(active_ids):
            if not _safe_name(eid):
                continue
            grp_by = f" GROUP BY {grp}" if agg_interval else ""
            parts.append(
                f"SELECT {grp} AS ts, {i} AS eord FROM [{eid}] "
                f"WHERE TimeCol BETWEEN :a AND :b{grp_by}"
            )
        if not parts:
            return []
        union = " UNION ALL ".join(parts)
        sql = (f"SELECT ts, eord FROM ({union}) u "
               f"ORDER BY eord, ts OFFSET :off ROWS FETCH NEXT :lim ROWS ONLY")
        db = SessionLocal()
        try:
            rows = db.execute(text(sql),
                              {"a": from_dt, "b": to_dt, "off": offset, "lim": limit}).fetchall()
        finally:
            db.close()
        return [(r[0], int(r[1])) for r in rows]

    @staticmethod
    def get_merged_page(
        req: ReportDataRequest, equipment_ids: List[str],
        page: int, page_size: int,
        progress: Optional[Callable[[int, str], None]] = None,
    ) -> Dict:
        """
        Return ONE global page of the merged multi-equipment view, ordered by
        (equipment, timestamp): each selected device appears as ONE continuous block
        (timestamps ascending within it) and the blocks follow the selection order,
        so the report never interleaves devices. Pagination is fully server-side:

          1. per-device row counts (cached) → `total_records` and progress,
          2. a cheap keys query → this page's exact (device, timestamp) pairs,
          3. aggregate ONLY each device's narrow page window, in parallel,
          4. assemble the rows in (equipment, timestamp) order.

        Only ~`page_size` rows ever leave the DB, so it stays fast for hundreds of
        thousands of records and the browser never holds the whole dataset. Because a
        device's rows can span many pages, one equipment's block may continue across
        page boundaries — the blocks still never interleave.
        """
        ids = [e for e in equipment_ids if e and str(e).strip()]
        if not ids:
            raise HTTPException(400, detail="No equipment selected")

        t0 = time.perf_counter()
        column_names = _map_tags_to_columns(req.equipment_type, req.tags)
        from_dt = req.from_datetime.strftime("%Y-%m-%d %H:%M:%S")
        to_dt   = req.to_datetime.strftime("%Y-%m-%d %H:%M:%S")
        interval = req.interval.value
        agg      = req.agg_function.value

        counts = ReportsService._equipment_counts(ids, column_names, from_dt, to_dt, interval, progress)
        total = sum(counts.values())
        active_ids = [e for e in ids if counts.get(e, 0) > 0]   # selection order

        page = max(1, page)
        page_size = max(1, min(page_size, 2000))
        offset = (page - 1) * page_size

        columns: Optional[List[str]] = None
        merged: List[dict] = []

        pairs = (ReportsService._ordered_page_keys(active_ids, from_dt, to_dt, interval, offset, page_size)
                 if offset < total else [])

        if pairs:
            # Group the page's timestamps PER DEVICE. With (equipment, timestamp)
            # ordering a single page is one device's continuous block (or, at a block
            # boundary, the tail of one device plus the head of the next) — it is no
            # longer a narrow GLOBAL time window, so each device is fetched in its OWN
            # [min, max] window. That window contains exactly this device's page rows,
            # keeping every fetch narrow and correct (a single global window would let
            # one device's early rows push its needed tail rows past the row limit).
            dev_page_ts: Dict[int, List[object]] = {}
            for ts, o in pairs:
                dev_page_ts.setdefault(o, []).append(ts)

            def fetch_window(o):
                eid = active_ids[o]
                tss = dev_page_ts[o]
                # Lower bound = this device's first page bucket (skips earlier buckets
                # on later pages). Upper bound = the ORIGINAL request end, NOT max(tss):
                # a bucket keyed e.g. 11:00 spans forward to 11:14, so capping at the
                # bucket KEY would under-aggregate that last bucket. Buckets are grouped
                # independently, so widening the upper bound never changes the value of
                # any earlier bucket — it only lets the final bucket aggregate its full
                # span, exactly matching a direct per-device query. The row limit keeps
                # the fetch narrow: only this device's page buckets (+1) are read.
                w_start_s = min(tss).strftime("%Y-%m-%d %H:%M:%S")
                db = SessionLocal()
                try:
                    r = ReportsRepository.get_report_data(
                        db=db, equipment_type=req.equipment_type, equipment_id=eid,
                        tags=column_names, from_datetime=w_start_s, to_datetime=to_dt,
                        interval=interval, agg_function=agg,
                        page=1, page_size=len(tss) + 1, compute_total=False,
                    )
                    return o, r
                finally:
                    db.close()

            dev_maps: Dict[int, Dict[str, dict]] = {}
            with ThreadPoolExecutor(max_workers=_BATCH_WORKERS) as ex:
                for o, r in ex.map(fetch_window, dev_page_ts.keys()):
                    if columns is None and r.get("columns"):
                        columns = r["columns"]
                    dev_maps[o] = {row["timestamp"]: row for row in r.get("rows", [])}

            tag_cols = (columns or ["timestamp"])[1:]
            for ts, o in pairs:
                ts_iso = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
                row = dev_maps.get(o, {}).get(ts_iso)
                if row is None:   # gap: device had the key but not in the window fetch
                    row = {"timestamp": ts_iso, **{t: None for t in tag_cols}}
                merged.append({"_equipment": active_ids[o], **row})

        if columns is None:
            columns = ["timestamp"] + column_names

        logger.info(
            "Merged page | type=%s | equipment=%d | page=%d size=%d | total=%d | "
            "rows=%d | %.0fms",
            req.equipment_type, len(ids), page, page_size, total, len(merged),
            (time.perf_counter() - t0) * 1000,
        )
        return {
            "equipment_type":  req.equipment_type,
            "interval":        interval,
            "agg_function":    agg,
            "columns":         ["_equipment"] + columns,
            "rows":            merged,
            "total_records":   total,
            "page":            page,
            "page_size":       page_size,
            "equipment_count": len(ids),
            "total_pages":     max(1, (total + page_size - 1) // page_size),
        }

    @staticmethod
    def get_tag_availability(
        db: Session,
        equipment_type: str,
        equipment_ids: List[str],
    ) -> Dict:
        if not equipment_type:
            raise HTTPException(400, detail="equipment_type is required")
        try:
            return ReportsRepository.get_tag_availability(db, equipment_type, equipment_ids)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"get_tag_availability error: {e}", exc_info=True)
            raise HTTPException(500, detail=str(e))

    @staticmethod
    def get_report_data(db: Session, req: ReportDataRequest) -> Dict:
        if not req.tags:
            raise HTTPException(400, detail="At least one tag is required")

        if req.to_datetime <= req.from_datetime:
            raise HTTPException(400, detail="to_datetime must be after from_datetime")

        # Same resolver + canonical ordering the batch path uses, so a single-equipment
        # preview and a merged multi-equipment preview return columns in one order.
        column_names = _map_tags_to_columns(req.equipment_type, req.tags)

        try:
            return ReportsRepository.get_report_data(
                db             = db,
                equipment_type = req.equipment_type,
                equipment_id   = req.equipment_id,
                tags           = column_names,
                from_datetime  = req.from_datetime.strftime("%Y-%m-%d %H:%M:%S"),
                to_datetime    = req.to_datetime.strftime("%Y-%m-%d %H:%M:%S"),
                interval       = req.interval.value,
                agg_function   = req.agg_function.value,
                page           = req.page,
                page_size      = req.page_size,
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"get_report_data error: {e}", exc_info=True)
            raise HTTPException(500, detail=f"Report failed: {str(e)}")