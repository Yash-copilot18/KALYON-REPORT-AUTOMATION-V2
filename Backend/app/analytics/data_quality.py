"""
Data integrity - can the record be trusted?

This runs before any performance claim, because a performance number derived from a
frozen sensor or a duplicated row is worse than no number at all. It audits the data
the way the plant would be audited: coverage, continuity, duplication, frozen
channels, and values that physics does not permit.

Everything is measured. The sampling cadence is detected from the data (the modal gap
between rows), so a source that samples at some other rate is described correctly
rather than judged against an assumption.

One deliberate subtlety: a solar plant legitimately reports long runs of exactly zero
at night, so a naive "repeated value" test flags every healthy inverter. Frozen-channel
detection therefore ignores zero runs and reports non-zero plateaus, which is what a
latched or failed sensor actually looks like.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.ai.evidence import (TIME_COL, _check, detect_cadence_seconds,
                             numeric_columns, table_exists)
from app.ai.schemas import (AnalysisStatus, Classification, ConfidenceBreakdown,
                            Derivation, Evidence, Finding, ModuleResult, Severity)

logger = logging.getLogger(__name__)

ANALYSIS = "data_integrity"

# A frozen run must be at least this long to be worth reporting. At 1-minute cadence
# that is two hours of an unchanging non-zero reading.
FROZEN_MIN_RUN = 120

# A column whose readings decrease on fewer than this fraction of transitions is a
# counter/register rather than an instantaneous measurement. See _is_accumulator.
ACCUMULATOR_DROP_RATIO = 0.005

# Physically impossible or implausible ranges, used only where the column's meaning is
# unambiguous from its name. Anything not listed is not range-checked rather than
# guessed at.
RANGE_RULES = [
    ("IRRADIATION", -5.0, 1600.0, "W/m2"),
    ("IRRADIANCE",  -5.0, 1600.0, "W/m2"),
    ("_TEMP",      -40.0,  120.0, "degC"),
    ("AIR_TEMP",   -40.0,   70.0, "degC"),
    ("WIND_SPEED",   0.0,   90.0, "m/s"),
    ("RELATIVE_HUMIDITY", 0.0, 105.0, "%"),
]


def _rule_for(column: str):
    up = column.upper()
    for token, lo, hi, units in RANGE_RULES:
        if token in up:
            return lo, hi, units
    return None


def required_inputs() -> List[str]:
    return ["At least one telemetry table with a TimeCol"]


def run(db: Session, tables: List[str], start: str, end: str,
        inventory: Optional[Dict[str, Any]] = None,
        progress=None) -> ModuleResult:
    """
    Audit every named table over [start, end].

    Returns PARTIAL rather than AVAILABLE when some requested table is absent or
    empty in the window - the audit still stands for the rest, and the gap is stated.
    """
    t0 = datetime.now()
    present = [t for t in tables if table_exists(db, t)]
    missing = [t for t in tables if t not in present]

    if not present:
        return ModuleResult.blocked(
            ANALYSIS, "None of the requested telemetry tables exist.",
            required_inputs(), unlocks="Data-integrity audit")

    per_source: List[Dict[str, Any]] = []
    findings: List[Finding] = []
    derivations: List[Derivation] = []

    for i, table in enumerate(present):
        if progress:
            progress(int(100 * i / max(1, len(present))), f"Auditing {table}…")
        try:
            audit = _audit_table(db, table, start, end)
        except Exception as exc:                      # noqa: BLE001
            logger.warning("Data-integrity audit failed for %s: %s", table, exc)
            per_source.append({"table": table, "error": f"{type(exc).__name__}: {exc}"})
            continue
        per_source.append(audit)
        derivations.append(audit["_derivation"])
        audit.pop("_derivation", None)

    # Peer adjudication BEFORE raising findings. A channel that is flat on one unit
    # while its peers move is an anomaly; the same channel flat across the whole
    # fleet at the same time is a plant-wide operating condition (a cumulative
    # register with nothing to accumulate, for instance), not 24 sensor faults.
    systemic = _systemic_frozen(per_source)
    for audit in per_source:
        if "error" in audit:
            continue
        findings.extend(_findings_for(audit["table"], audit, start, end, systemic))

    audited = [s for s in per_source if "error" not in s]
    status = AnalysisStatus.AVAILABLE if not missing and len(audited) == len(tables) \
        else AnalysisStatus.PARTIAL
    reason = "" if status is AnalysisStatus.AVAILABLE else \
        f"{len(audited)} of {len(tables)} requested sources audited" + \
        (f"; absent: {', '.join(missing)}" if missing else "")

    return ModuleResult(
        analysis=ANALYSIS, status=status, reason=reason,
        window_start=start, window_end=end,
        metrics={
            "sources_requested": len(tables),
            "sources_audited": len(audited),
            "sources_absent": missing,
            "per_source": per_source,
            "totals": _totals(audited),
        },
        findings=findings,
        derivations=derivations,
        duration_ms=int((datetime.now() - t0).total_seconds() * 1000),
    )


def _audit_table(db: Session, table: str, start: str, end: str) -> Dict[str, Any]:
    _check(table)
    params = {"ws": start, "we": end}
    w = f"WHERE {TIME_COL} >= :ws AND {TIME_COL} <= :we"

    row = db.execute(text(
        f"SELECT COUNT(*), MIN({TIME_COL}), MAX({TIME_COL}), COUNT(DISTINCT {TIME_COL}) "
        f"FROM [{table}] {w}"), params).first()
    rows, first, last, distinct_ts = int(row[0] or 0), row[1], row[2], int(row[3] or 0)

    if rows == 0:
        return {"table": table, "rows": 0, "in_window": False,
                "note": "No rows in the requested window.",
                "_derivation": Derivation(
                    method="row count", value=0, units="rows",
                    formula="COUNT(*) over the window",
                    code_ref="data_quality._audit_table",
                    source_tables=[table], source_columns=[TIME_COL],
                    window_start=start, window_end=end, row_count=0)}

    cadence = detect_cadence_seconds(db, table, start, end)
    duplicates = rows - distinct_ts

    # Continuity: how many sampling intervals are missing, and the worst single gap.
    gaps: Dict[str, Any] = {"gap_count": None, "missing_intervals": None,
                            "largest_gap_seconds": None, "largest_gap_at": None}
    if cadence and cadence > 0:
        g = db.execute(text(f"""
            SELECT COUNT(*) AS gap_count,
                   SUM(CASE WHEN delta > :cad THEN (delta / :cad) - 1 ELSE 0 END) AS missing,
                   MAX(delta) AS worst
            FROM (
                SELECT DATEDIFF(second, LAG({TIME_COL}) OVER (ORDER BY {TIME_COL}), {TIME_COL}) AS delta
                FROM [{table}] {w}
            ) d
            WHERE delta IS NOT NULL AND delta > :cad * 1.5
        """), {**params, "cad": cadence}).first()
        worst_at = None
        if g and g[2]:
            worst_at = db.execute(text(f"""
                SELECT TOP 1 {TIME_COL} FROM (
                    SELECT {TIME_COL},
                           DATEDIFF(second, LAG({TIME_COL}) OVER (ORDER BY {TIME_COL}), {TIME_COL}) AS delta
                    FROM [{table}] {w}
                ) d WHERE delta = :worst ORDER BY {TIME_COL}
            """), {**params, "worst": int(g[2])}).scalar()
        expected = int((last - first).total_seconds() // cadence) + 1 if first and last else None
        missing = int(g[1] or 0) if g and g[1] is not None else 0
        # Coverage is derived from the intervals actually MISSING, not from the row
        # count. Several sources log change-of-value on top of the periodic sample,
        # so rows can exceed a strict grid - dividing by expected rows then yields a
        # nonsensical >100%. Missing intervals are measured directly by the gap query
        # and stay correct however densely the source logs.
        coverage = None
        if expected and expected > 0:
            coverage = round(100.0 * max(0, expected - missing) / expected, 3)
        gaps = {
            "gap_count": int(g[0] or 0) if g else 0,
            "missing_intervals": missing,
            "largest_gap_seconds": int(g[2]) if g and g[2] else 0,
            "largest_gap_at": worst_at.isoformat() if worst_at else None,
            "expected_rows": expected,
            "actual_rows": distinct_ts,
            # >0 means the source logs more often than its own modal cadence.
            "excess_samples": max(0, distinct_ts - expected) if expected else None,
            "coverage_pct": coverage,
        }

    cols = numeric_columns(db, table)
    frozen = _frozen_channels(db, table, cols, start, end, cadence)
    invalid = _invalid_values(db, table, cols, start, end)

    return {
        "table": table,
        "in_window": True,
        "rows": rows,
        "first": first.isoformat() if first else None,
        "last": last.isoformat() if last else None,
        "cadence_seconds": cadence,
        "distinct_timestamps": distinct_ts,
        "duplicate_timestamps": duplicates,
        "numeric_columns": len(cols),
        "continuity": gaps,
        "frozen_channels": frozen,
        "invalid_values": invalid,
        "_derivation": Derivation(
            method="integrity audit", value=float(rows), units="rows",
            formula="COUNT(*), COUNT(DISTINCT TimeCol), modal LAG() delta, "
                    "gaps-and-islands run length, range rules",
            code_ref="data_quality._audit_table",
            source_tables=[table], source_columns=[TIME_COL] + cols[:8],
            window_start=start, window_end=end, row_count=rows,
            inputs={"cadence_seconds": cadence, "distinct_timestamps": distinct_ts},
        ),
    }


def _frozen_channels(db: Session, table: str, cols: List[str], start: str, end: str,
                     cadence: Optional[int]) -> List[Dict[str, Any]]:
    """
    Longest run of an unchanging NON-ZERO value per column.

    Zero runs are excluded on purpose: night-time zeros are correct behaviour, not a
    fault, and including them would flag every healthy channel. A long non-zero
    plateau is the signature of a latched reading or a dead sensor still reporting.

    Implemented with gaps-and-islands (the difference of two row numbers is constant
    within a run of equal values), which is one pass per column.
    """
    out: List[Dict[str, Any]] = []

    # Cheap pre-filter: a frozen channel has very few DISTINCT non-zero values for the
    # number of rows it reported. Counting distincts for every column costs one pass;
    # the expensive run-length scan then runs only on the handful of suspects. Without
    # this the audit is one heavy query per column - 64 per inverter, ~1,500 fleet-wide.
    candidates = _frozen_candidates(db, table, cols, start, end)
    activity_col = _activity_column(cols)
    for col in candidates:
        if not _check(col):
            continue
        # A counter standing still is the absence of an event, not a stuck sensor.
        if _is_accumulator(db, table, col, start, end):
            continue
        try:
            row = db.execute(text(f"""
                SELECT TOP 1 run_len, val, started, ended FROM (
                    SELECT COUNT(*) AS run_len, [{col}] AS val,
                           MIN({TIME_COL}) AS started, MAX({TIME_COL}) AS ended
                    FROM (
                        SELECT {TIME_COL}, [{col}],
                               ROW_NUMBER() OVER (ORDER BY {TIME_COL})
                             - ROW_NUMBER() OVER (PARTITION BY [{col}] ORDER BY {TIME_COL}) AS grp
                        FROM [{table}]
                        WHERE {TIME_COL} >= :ws AND {TIME_COL} <= :we
                          AND [{col}] IS NOT NULL AND [{col}] <> 0
                    ) s
                    GROUP BY [{col}], grp
                ) r
                WHERE run_len >= :minrun
                ORDER BY run_len DESC
            """), {"ws": start, "we": end, "minrun": FROZEN_MIN_RUN}).first()
        except Exception as exc:                      # noqa: BLE001
            logger.debug("frozen check skipped for %s.%s: %s", table, col, exc)
            continue
        if row:
            # A plateau while the source was idle is correct behaviour, not a fault.
            if not _was_active(db, table, activity_col, row[2], row[3]):
                continue
            mins = round(row[0] * (cadence or 60) / 60.0, 1)
            out.append({
                "column": col, "run_length": int(row[0]), "value": float(row[1]),
                "started": row[2].isoformat() if row[2] else None,
                "ended": row[3].isoformat() if row[3] else None,
                "duration_minutes": mins,
            })
    out.sort(key=lambda d: d["run_length"], reverse=True)
    return out[:20]


def _is_accumulator(db: Session, table: str, col: str, start: str, end: str) -> bool:
    """
    True when the column never decreases over the window - i.e. it is a counter or a
    lifetime register, not an instantaneous measurement.

    This matters because a flat counter is CORRECT behaviour: DOWN_TIME_MIN that does
    not move means the inverter was not down. Treating that as a frozen sensor buries
    the real findings under one false positive per counter per inverter. Monotonicity
    is measured from the data, so no column-name heuristic is needed and the rule
    holds for registers this code has never seen.

    Monotonic in EITHER direction counts. Reactive-energy registers on this plant
    accumulate negatively (a lifetime register reading -327,042), so a rule that only
    recognised non-decreasing counters flagged four of them per inverter - about a
    hundred false findings across the fleet.

    The test is "rarely moves against its trend", not "never": these registers
    occasionally roll back a step (measured: 4 reversals in 13,092 rows on a
    lifetime-hours register). A genuine instantaneous measurement reverses on roughly
    half its transitions, so the two are separated by orders of magnitude and the
    threshold is not delicate.
    """
    try:
        row = db.execute(text(f"""
            SELECT SUM(CASE WHEN v < prev THEN 1 ELSE 0 END) AS drops,
                   SUM(CASE WHEN v > prev THEN 1 ELSE 0 END) AS rises,
                   COUNT(*) AS n
            FROM (
                SELECT [{col}] AS v,
                       LAG([{col}]) OVER (ORDER BY {TIME_COL}) AS prev
                FROM [{table}]
                WHERE {TIME_COL} >= :ws AND {TIME_COL} <= :we AND [{col}] IS NOT NULL
            ) d
            WHERE prev IS NOT NULL
        """), {"ws": start, "we": end}).first()
    except Exception:                                 # noqa: BLE001
        return False                                  # unknown -> analyse it normally
    if not row or not row[2]:
        return False
    n = float(row[2])
    against_trend = min((row[0] or 0), (row[1] or 0)) / n
    return against_trend < ACCUMULATOR_DROP_RATIO


# Columns that indicate the source was actually doing something, in order of
# preference. A plateau only matters if it happened while the plant was running.
ACTIVITY_COLUMNS = ["ACTIVE_POWER", "DC_POWER", "GRID_ACTIVE_POWER_MEASURED",
                    "INVERTER_TOTAL_ACTIVE_POWER", "AVG_GHI_IRRADIATION",
                    "TOTAL_IRRADIANCE", "POWER_GENERATION"]
# Below this the source is treated as idle (night, or the inverter asleep).
ACTIVITY_FLOOR = 1.0


def _activity_column(cols: List[str]) -> Optional[str]:
    upper = {c.upper(): c for c in cols}
    for pref in ACTIVITY_COLUMNS:
        if pref in upper:
            return upper[pref]
    return None


def _was_active(db: Session, table: str, activity_col: Optional[str],
                started, ended) -> bool:
    """
    Was the source actually operating during this run?

    This is what separates a stuck channel from correct idle behaviour. A reactive-
    energy register holding the same value all night is not frozen - nothing was
    flowing to accumulate. The same register flat while the inverter exports power
    is a genuine anomaly. Without this gate the audit reports one false finding per
    energy register per inverter, every night in the window.
    """
    if not activity_col or not started or not ended:
        return True                                   # cannot tell -> keep the finding
    try:
        peak = db.execute(text(
            f"SELECT MAX([{activity_col}]) FROM [{table}] "
            f"WHERE {TIME_COL} >= :a AND {TIME_COL} <= :b"),
            {"a": started, "b": ended}).scalar()
    except Exception:                                 # noqa: BLE001
        return True
    return peak is not None and abs(float(peak)) > ACTIVITY_FLOOR


def _frozen_candidates(db: Session, table: str, cols: List[str],
                       start: str, end: str) -> List[str]:
    """
    Columns worth scanning for a frozen run, found by counting CONSECUTIVE REPEATS.

    A long constant run necessarily produces many consecutive equal readings, so a
    column whose non-zero value repeats fewer than FROZEN_MIN_RUN times cannot
    contain a run that long and is dismissed without the costly scan.

    An earlier version pre-filtered on "few DISTINCT values", which was wrong: a
    sensor that reads normally for weeks and then sticks for two days has thousands
    of distinct values and was skipped - missing exactly the intermittent failures
    this audit exists to find. Repeat-counting has no such blind spot.

    Batched so each statement carries a sane number of window functions; one pass per
    batch rather than one pass per column.
    """
    if not cols:
        return []
    picked: List[str] = []
    CHUNK = 12                     # 12 columns -> 12 LAG() windows per statement
    for i in range(0, len(cols), CHUNK):
        batch = [c for c in cols[i:i + CHUNK] if _check(c)]
        if not batch:
            continue
        inner = ", ".join(
            f"[{c}] AS v{j}, LAG([{c}]) OVER (ORDER BY {TIME_COL}) AS p{j}"
            for j, c in enumerate(batch))
        outer = ", ".join(
            f"SUM(CASE WHEN v{j} IS NOT NULL AND v{j} <> 0 AND v{j} = p{j} "
            f"THEN 1 ELSE 0 END) AS r{j}" for j in range(len(batch)))
        try:
            row = db.execute(text(
                f"SELECT {outer} FROM (SELECT {inner} FROM [{table}] "
                f"WHERE {TIME_COL} >= :ws AND {TIME_COL} <= :we) d"),
                {"ws": start, "we": end}).first()
        except Exception as exc:                      # noqa: BLE001
            logger.debug("frozen pre-filter skipped for %s: %s", table, exc)
            picked.extend(batch)                      # fall back to scanning them
            continue
        if not row:
            continue
        for j, c in enumerate(batch):
            if (row[j] or 0) >= FROZEN_MIN_RUN - 1:
                picked.append(c)
    return picked


def _invalid_values(db: Session, table: str, cols: List[str],
                    start: str, end: str) -> List[Dict[str, Any]]:
    """Count readings outside a physically possible range, where the range is known."""
    out: List[Dict[str, Any]] = []
    for col in cols:
        rule = _rule_for(col)
        if not rule or not _check(col):
            continue
        lo, hi, units = rule
        try:
            row = db.execute(text(f"""
                SELECT SUM(CASE WHEN [{col}] < :lo THEN 1 ELSE 0 END),
                       SUM(CASE WHEN [{col}] > :hi THEN 1 ELSE 0 END),
                       MIN([{col}]), MAX([{col}])
                FROM [{table}]
                WHERE {TIME_COL} >= :ws AND {TIME_COL} <= :we AND [{col}] IS NOT NULL
            """), {"ws": start, "we": end, "lo": lo, "hi": hi}).first()
        except Exception:                             # noqa: BLE001
            continue
        below, above = int(row[0] or 0), int(row[1] or 0)
        if below or above:
            out.append({"column": col, "units": units, "below_min": below,
                        "above_max": above, "min_seen": float(row[2]),
                        "max_seen": float(row[3]), "rule": [lo, hi]})
    out.sort(key=lambda d: d["below_min"] + d["above_max"], reverse=True)
    return out[:20]


def _systemic_frozen(per_source: List[Dict[str, Any]]) -> Dict[str, int]:
    """
    How many audited sources reported a frozen run on each column name.

    Used to tell a per-unit fault from a fleet-wide condition. Keyed by column name
    because the fleet tables share a schema, so the same channel on many units is
    directly comparable.
    """
    counts: Dict[str, int] = {}
    for a in per_source:
        for fz in (a.get("frozen_channels") or []):
            counts[fz["column"]] = counts.get(fz["column"], 0) + 1
    return counts


# A frozen channel seen on at least this many sources at once is treated as a
# plant-wide condition rather than a per-unit sensor fault.
SYSTEMIC_MIN_SOURCES = 3


def _findings_for(table: str, a: Dict[str, Any], start: str, end: str,
                  systemic: Optional[Dict[str, int]] = None) -> List[Finding]:
    """Turn measurements into defensible statements. Severity follows magnitude."""
    out: List[Finding] = []
    if not a.get("in_window"):
        return out

    cont = a.get("continuity") or {}
    cov = cont.get("coverage_pct")
    if cov is not None and cov < 99.0:
        sev = Severity.HIGH if cov < 90 else Severity.MEDIUM if cov < 97 else Severity.LOW
        out.append(Finding(
            finding_id=f"di_coverage_{table}",
            title=f"{table}: {cov:.2f}% timestamp coverage",
            summary=(f"{cont.get('missing_intervals', 0):,} sampling intervals are absent "
                     f"across {cont.get('gap_count', 0):,} gaps; the largest is "
                     f"{cont.get('largest_gap_seconds', 0):,}s"
                     + (f" at {cont['largest_gap_at']}" if cont.get("largest_gap_at") else "")),
            analysis=ANALYSIS, severity=sev,
            classification=Classification.DATA_INTEGRITY,
            source_tables=[table], source_columns=[TIME_COL],
            window_start=start, window_end=end,
            impact_value=cov, impact_units="% coverage",
            metrics=cont,
            recommended_action="Check the historian/RTU link for this source over the "
                               "largest gap; confirm whether the outage was plant-side "
                               "or collection-side.",
            supporting_evidence=[Evidence(
                statement=f"Detected cadence {a.get('cadence_seconds')}s; "
                          f"{a.get('distinct_timestamps'):,} distinct timestamps vs "
                          f"{cont.get('expected_rows')} expected",
                supports=True)],
        ))

    dup = a.get("duplicate_timestamps") or 0
    if dup > 0:
        out.append(Finding(
            finding_id=f"di_duplicates_{table}",
            title=f"{table}: {dup:,} duplicate timestamps",
            summary=f"{dup:,} rows share a timestamp with another row, so any aggregate "
                    f"over this source double-counts unless de-duplicated.",
            analysis=ANALYSIS,
            severity=Severity.HIGH if dup > a["rows"] * 0.01 else Severity.MEDIUM,
            classification=Classification.DATA_INTEGRITY,
            source_tables=[table], source_columns=[TIME_COL],
            window_start=start, window_end=end,
            impact_value=float(dup), impact_units="rows",
            recommended_action="De-duplicate at the collection layer; confirm whether the "
                               "RTU is re-sending on reconnect.",
        ))

    systemic = systemic or {}
    for fz in (a.get("frozen_channels") or [])[:5]:
        peers = systemic.get(fz["column"], 1)
        fleet_wide = peers >= SYSTEMIC_MIN_SOURCES
        if fleet_wide:
            sev = Severity.INFO
            extra = Evidence(
                statement=f"The same channel is flat on {peers} sources over this "
                          f"window, so this is a plant-wide condition rather than a "
                          f"fault on {table}",
                supports=False)
        else:
            sev = Severity.HIGH if fz["run_length"] >= 600 else Severity.MEDIUM
            extra = Evidence(
                statement=f"No peer source shows this channel flat over the same "
                          f"window ({peers} of the audited sources affected)",
                supports=True)
        out.append(Finding(
            finding_id=f"di_frozen_{table}_{fz['column']}",
            title=f"{table}.{fz['column']}: unchanged for {fz['duration_minutes']:,.0f} min",
            summary=(f"Held exactly {fz['value']} for {fz['run_length']:,} consecutive "
                     f"samples ({fz['started']} to {fz['ended']}). Zero runs are excluded, "
                     f"so this is a non-zero plateau."),
            analysis=ANALYSIS,
            severity=sev,
            classification=Classification.DATA_INTEGRITY,
            source_tables=[table], source_columns=[fz["column"]],
            window_start=fz["started"], window_end=fz["ended"],
            impact_value=float(fz["duration_minutes"]), impact_units="minutes",
            metrics=fz,
            recommended_action="Inspect the sensor and its signal path for this channel; "
                               "a constant non-zero reading usually means a latched value "
                               "rather than a stable process.",
            supporting_evidence=[Evidence(
                statement="Run detected by gaps-and-islands over consecutive equal values",
                supports=True)],
            contradicting_evidence=[
                Evidence(statement="A genuinely steady process can hold a value; "
                                   "corroborate against a peer channel before calling "
                                   "the sensor failed", supports=False),
            ] + ([extra] if not extra.supports else []),
        ))
        if extra.supports:
            out[-1].supporting_evidence.append(extra)

    for iv in (a.get("invalid_values") or [])[:5]:
        total = iv["below_min"] + iv["above_max"]
        out.append(Finding(
            finding_id=f"di_range_{table}_{iv['column']}",
            title=f"{table}.{iv['column']}: {total:,} readings outside a physical range",
            summary=(f"{iv['below_min']:,} below {iv['rule'][0]} and {iv['above_max']:,} "
                     f"above {iv['rule'][1]} {iv['units']}; observed range "
                     f"{iv['min_seen']} to {iv['max_seen']}."),
            analysis=ANALYSIS,
            severity=Severity.MEDIUM,
            classification=Classification.DATA_INTEGRITY,
            source_tables=[table], source_columns=[iv["column"]],
            window_start=start, window_end=end,
            impact_value=float(total), impact_units="readings",
            metrics=iv,
            recommended_action="Screen these readings out of downstream KPIs and check "
                               "sensor scaling/units at the RTU.",
        ))
    return out


def _totals(audited: List[Dict[str, Any]]) -> Dict[str, Any]:
    rows = sum(a.get("rows", 0) for a in audited)
    dups = sum(a.get("duplicate_timestamps", 0) or 0 for a in audited)
    missing = sum((a.get("continuity") or {}).get("missing_intervals", 0) or 0
                  for a in audited)
    frozen = sum(len(a.get("frozen_channels") or []) for a in audited)
    invalid = sum(len(a.get("invalid_values") or []) for a in audited)
    covs = [(a.get("continuity") or {}).get("coverage_pct") for a in audited]
    covs = [c for c in covs if c is not None]
    return {
        "rows_examined": rows,
        "duplicate_timestamps": dups,
        "missing_intervals": missing,
        "frozen_channels_flagged": frozen,
        "range_violations_flagged": invalid,
        "mean_coverage_pct": round(sum(covs) / len(covs), 3) if covs else None,
        "worst_coverage_pct": round(min(covs), 3) if covs else None,
    }


def confidence_for_dataset(audited_totals: Dict[str, Any]) -> ConfidenceBreakdown:
    """
    How much the rest of the report should be trusted, derived only from measured
    integrity. Exposed so other modules can fold data quality into their own
    confidence rather than inventing one.
    """
    cov = (audited_totals.get("mean_coverage_pct") or 0) / 100.0
    dup_pen = 0.15 if (audited_totals.get("duplicate_timestamps") or 0) else 0.0
    frz_pen = min(0.20, 0.02 * (audited_totals.get("frozen_channels_flagged") or 0))
    inv_pen = min(0.15, 0.02 * (audited_totals.get("range_violations_flagged") or 0))
    score = max(0.0, min(1.0, cov - dup_pen - frz_pen - inv_pen))
    return ConfidenceBreakdown(
        score=score,
        factors={"coverage": round(cov, 4), "duplicate_penalty": dup_pen,
                 "frozen_penalty": round(frz_pen, 4), "range_penalty": round(inv_pen, 4)},
        rationale="Coverage of the sampling grid, reduced by duplicated timestamps, "
                  "frozen non-zero channels and readings outside physical range.",
    )
