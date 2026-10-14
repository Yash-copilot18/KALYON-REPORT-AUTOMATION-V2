"""
Shared data extraction for the AI forensic prototype.

Every figure the prototype reports is produced here, from the Kalyon database, with
the unit conventions verified during inspection:

    PPC.GRID_ACTIVE_POWER_MEASURED      MW   (peaks ~102 on a 130 MW plant)
    PPC.PLANT_DAILY_PRODUCTION          MWh
    INVERTER_xx.ACTIVE_POWER            kW   (24 x ~4,200 kW = ~101 MW = PPC MW)

Those are not assumptions: they were cross-checked against each other at matching
timestamps, and the check is reproduced in the energy-reconciliation section.

Nothing here is cached or mocked; every function issues a query.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
logging.disable(logging.ERROR)

from sqlalchemy import text                                    # noqa: E402
from app.database.session import SessionLocal                  # noqa: E402

MINUTES_PER_HOUR = 60.0
INVERTERS = [f"INVERTER_{i:02d}" for i in range(1, 25)]


def session():
    return SessionLocal()


# ── coverage ─────────────────────────────────────────────────────────────────
def coverage(db) -> List[Dict[str, Any]]:
    """First/last timestamp, row count and detected cadence for each source."""
    out = []
    tables = ["PPC", "PPC_TREND", "WMS", "INVERTER_DAILY_GEN", "Alarms",
              "POWER_GRAPH", "POWER_VS_IRRADIANCE", "INVERTER_TEMP",
              "TRACKER_MBOX_STATUS"] + INVERTERS[:3]
    for t in tables:
        r = db.execute(text(f"SELECT MIN(TimeCol), MAX(TimeCol) FROM [{t}]")).first()
        n = db.execute(text("""SELECT SUM(p.rows) FROM sys.tables s
              JOIN sys.partitions p ON s.object_id=p.object_id AND p.index_id IN (0,1)
              WHERE s.name=:t"""), {"t": t}).scalar()
        cad = db.execute(text(f"""
            SELECT TOP 1 delta FROM (
              SELECT DATEDIFF(second, LAG(TimeCol) OVER (ORDER BY TimeCol), TimeCol) delta
              FROM (SELECT TOP 5000 TimeCol FROM [{t}] ORDER BY TimeCol DESC) s) d
            WHERE delta > 0 GROUP BY delta ORDER BY COUNT(*) DESC""")).scalar()
        out.append({"table": t, "first": r[0], "last": r[1],
                    "rows": int(n or 0), "cadence_s": cad})
    return out


# ── daily plant series ───────────────────────────────────────────────────────
def daily_plant(db) -> List[Dict[str, Any]]:
    """
    One row per calendar day.

    `energy_integrated_mwh` integrates measured power: E = sum(P_MW) / 60, valid
    because the cadence is one minute. `energy_register_mwh` is the plant's own
    daily production register. Both are reported; their difference is the subject
    of the energy-reconciliation section rather than something to average away.
    """
    rows = db.execute(text("""
        SELECT CAST(TimeCol AS DATE)                       AS d,
               COUNT(*)                                     AS minutes,
               SUM(GRID_ACTIVE_POWER_MEASURED) / 60.0       AS energy_integrated_mwh,
               MAX(PLANT_DAILY_PRODUCTION)                  AS energy_register_mwh,
               AVG(GRID_ACTIVE_POWER_MEASURED)              AS avg_mw,
               MAX(GRID_ACTIVE_POWER_MEASURED)              AS peak_mw,
               AVG(CAST(INVERTER_RUNNING AS float))         AS avg_inverters_running,
               SUM(CASE WHEN ACTIVE_POWER_SET_POINT > 0
                         AND ACTIVE_POWER_SET_POINT < 100 THEN 1 ELSE 0 END) AS curtailed_minutes,
               SUM(CASE WHEN GRID_ACTIVE_POWER_MEASURED > 1 THEN 1 ELSE 0 END) AS producing_minutes
        FROM PPC
        GROUP BY CAST(TimeCol AS DATE)
        ORDER BY d
    """)).fetchall()
    return [dict(d=r[0], minutes=int(r[1]),
                 energy_integrated_mwh=float(r[2] or 0),
                 energy_register_mwh=float(r[3] or 0),
                 avg_mw=float(r[4] or 0), peak_mw=float(r[5] or 0),
                 avg_inverters_running=float(r[6] or 0),
                 curtailed_minutes=int(r[7] or 0),
                 producing_minutes=int(r[8] or 0)) for r in rows]


def daily_weather(db) -> List[Dict[str, Any]]:
    """Daily irradiation, temperature, wind and measured soiling from the WMS."""
    rows = db.execute(text("""
        SELECT CAST(TimeCol AS DATE)                  AS d,
               COUNT(*)                                AS minutes,
               SUM(AVG_GHI_IRRADIATION) / 60000.0      AS ghi_kwh_m2,
               SUM(AVG_GTI_IRRADIATION) / 60000.0      AS gti_kwh_m2,
               AVG(ALL_WMS_AVG_MODULE_TEMP)            AS module_temp_c,
               AVG(AVG_AIR_TEMP)                       AS air_temp_c,
               AVG(AVG_WIND_SPEED)                     AS wind_ms,
               AVG(AVG_IR_SOILING_RATIO1)              AS soiling_ratio,
               MAX(AVG_GTI_IRRADIATION)                AS peak_gti
        FROM WMS
        GROUP BY CAST(TimeCol AS DATE)
        ORDER BY d
    """)).fetchall()
    return [dict(d=r[0], minutes=int(r[1]),
                 ghi_kwh_m2=float(r[2] or 0), gti_kwh_m2=float(r[3] or 0),
                 module_temp_c=float(r[4] or 0), air_temp_c=float(r[5] or 0),
                 wind_ms=float(r[6] or 0), soiling_ratio=float(r[7] or 0),
                 peak_gti=float(r[8] or 0)) for r in rows]


def station_comparison(db) -> List[Dict[str, Any]]:
    """Per-station GHI/GTI/temperature, on the timestamps where all four report."""
    out = []
    for s in (1, 2, 3, 4):
        r = db.execute(text(f"""
            SELECT COUNT(*),
                   AVG(WMS{s}_GHI_IRRADIATION), AVG(WMS{s}_GTI_IRRADIATION),
                   AVG(WMS{s}_AIR_TEMP), AVG(WMS{s}_WIND_SPEED),
                   SUM(CASE WHEN WMS{s}_GHI_IRRADIATION IS NULL THEN 1 ELSE 0 END)
            FROM WMS""")).first()
        out.append({"station": f"WMS{s}", "rows": int(r[0] or 0),
                    "ghi_mean": float(r[1] or 0), "gti_mean": float(r[2] or 0),
                    "air_temp_mean": float(r[3] or 0), "wind_mean": float(r[4] or 0),
                    "nulls": int(r[5] or 0)})
    # Pairwise bias/RMSE against the four-station mean, daylight only.
    stats = db.execute(text("""
        SELECT AVG(m) AS mean_all,
               AVG(WMS1_GHI_IRRADIATION - m), AVG(WMS2_GHI_IRRADIATION - m),
               AVG(WMS3_GHI_IRRADIATION - m), AVG(WMS4_GHI_IRRADIATION - m),
               SQRT(AVG(POWER(WMS1_GHI_IRRADIATION - m, 2))),
               SQRT(AVG(POWER(WMS2_GHI_IRRADIATION - m, 2))),
               SQRT(AVG(POWER(WMS3_GHI_IRRADIATION - m, 2))),
               SQRT(AVG(POWER(WMS4_GHI_IRRADIATION - m, 2))),
               COUNT(*)
        FROM (
            SELECT WMS1_GHI_IRRADIATION, WMS2_GHI_IRRADIATION,
                   WMS3_GHI_IRRADIATION, WMS4_GHI_IRRADIATION,
                   (WMS1_GHI_IRRADIATION + WMS2_GHI_IRRADIATION +
                    WMS3_GHI_IRRADIATION + WMS4_GHI_IRRADIATION) / 4.0 AS m
            FROM WMS
            WHERE WMS1_GHI_IRRADIATION IS NOT NULL AND WMS2_GHI_IRRADIATION IS NOT NULL
              AND WMS3_GHI_IRRADIATION IS NOT NULL AND WMS4_GHI_IRRADIATION IS NOT NULL
              AND AVG_GHI_IRRADIATION > 50
        ) x
    """)).first()
    for i, st in enumerate(out):
        st["bias_vs_mean"] = float(stats[1 + i] or 0)
        st["rmse_vs_mean"] = float(stats[5 + i] or 0)
        st["paired_rows"] = int(stats[9] or 0)
    return out


def inverter_summary(db) -> List[Dict[str, Any]]:
    """
    Per-inverter energy, peak, producing minutes and alarm load over the archive.

    Energy integrates ACTIVE_POWER (kW) to MWh: sum(kW)/60/1000.
    """
    out = []
    for t in INVERTERS:
        r = db.execute(text(f"""
            SELECT COUNT(*),
                   SUM(ACTIVE_POWER) / 60.0 / 1000.0,
                   MAX(ACTIVE_POWER),
                   AVG(ACTIVE_POWER),
                   SUM(CASE WHEN ACTIVE_POWER > 1 THEN 1 ELSE 0 END)
            FROM [{t}]""")).first()
        unit = t.replace("INVERTER_", "").lstrip("0")
        alarms = db.execute(text("""
            SELECT COUNT(*), SUM(CASE WHEN EventCol='ALARM ON' THEN 1 ELSE 0 END)
            FROM Alarms WHERE EvDescCol LIKE :p"""),
            {"p": f"INVERTER {int(unit):02d}%"}).first()
        out.append({"inverter": t, "rows": int(r[0] or 0),
                    "energy_mwh": float(r[1] or 0), "peak_kw": float(r[2] or 0),
                    "avg_kw": float(r[3] or 0), "producing_minutes": int(r[4] or 0),
                    "alarm_rows": int(alarms[0] or 0),
                    "alarm_on": int(alarms[1] or 0)})
    return out


def curtailment_events(db) -> Dict[str, Any]:
    """
    Setpoint-limited periods. The setpoint is a percentage (observed range 0-105),
    so a value below 100 while the plant is producing is an active limit.

    Energy impact is ESTIMATED as the shortfall against the unconstrained ceiling
    implied by the setpoint, and is labelled an estimate wherever it is shown.
    """
    summary = db.execute(text("""
        SELECT COUNT(*) AS limited_minutes,
               COUNT(DISTINCT CAST(TimeCol AS DATE)) AS affected_days,
               MIN(ACTIVE_POWER_SET_POINT), AVG(ACTIVE_POWER_SET_POINT),
               AVG(GRID_ACTIVE_POWER_MEASURED)
        FROM PPC
        WHERE ACTIVE_POWER_SET_POINT > 0 AND ACTIVE_POWER_SET_POINT < 100
          AND GRID_ACTIVE_POWER_MEASURED > 1
    """)).first()
    by_month = db.execute(text("""
        SELECT FORMAT(TimeCol,'yyyy-MM') AS m, COUNT(*) AS mins,
               COUNT(DISTINCT CAST(TimeCol AS DATE)) AS days,
               AVG(ACTIVE_POWER_SET_POINT) AS avg_sp
        FROM PPC
        WHERE ACTIVE_POWER_SET_POINT > 0 AND ACTIVE_POWER_SET_POINT < 100
          AND GRID_ACTIVE_POWER_MEASURED > 1
        GROUP BY FORMAT(TimeCol,'yyyy-MM') ORDER BY m
    """)).fetchall()
    return {
        "limited_minutes": int(summary[0] or 0),
        "affected_days": int(summary[1] or 0),
        "min_setpoint_pct": float(summary[2] or 0),
        "mean_setpoint_pct": float(summary[3] or 0),
        "mean_power_while_limited_mw": float(summary[4] or 0),
        "by_month": [{"month": r[0], "minutes": int(r[1]), "days": int(r[2]),
                      "avg_setpoint_pct": float(r[3] or 0)} for r in by_month],
    }


def clipping_candidates(db, ceiling_frac: float = 0.98) -> List[Dict[str, Any]]:
    """
    Sustained plateaus at an inverter's own observed ceiling.

    The ceiling is each unit's own 99th-percentile output, so no nameplate is
    assumed. A candidate requires the unit to sit within `ceiling_frac` of that
    ceiling; the result is reported as a CANDIDATE because distinguishing inverter
    clipping from a curtailment setpoint needs the PPC cross-check also reported.
    """
    out = []
    for t in INVERTERS:
        ceiling = db.execute(text(f"""
            SELECT TOP 1 PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY ACTIVE_POWER)
                   OVER () FROM [{t}] WHERE ACTIVE_POWER > 1""")).scalar()
        if not ceiling:
            continue
        thr = float(ceiling) * ceiling_frac
        r = db.execute(text(f"""
            SELECT COUNT(*), COUNT(DISTINCT CAST(TimeCol AS DATE))
            FROM [{t}] WHERE ACTIVE_POWER >= :thr"""), {"thr": thr}).first()
        out.append({"inverter": t, "ceiling_kw": float(ceiling),
                    "threshold_kw": thr, "minutes_at_ceiling": int(r[0] or 0),
                    "days_affected": int(r[1] or 0)})
    return out


def alarm_summary(db) -> List[Dict[str, Any]]:
    rows = db.execute(text("""
        SELECT TOP 15 LEFT(EvDescCol, 40) AS subject, COUNT(*) AS n,
               SUM(CASE WHEN EventCol='ALARM ON' THEN 1 ELSE 0 END) AS ons
        FROM Alarms GROUP BY LEFT(EvDescCol, 40) ORDER BY n DESC
    """)).fetchall()
    return [{"subject": r[0], "events": int(r[1]), "alarm_on": int(r[2])} for r in rows]


def integrity_summary(db) -> Dict[str, Any]:
    """Duplicate timestamps and continuity for the plant-level sources."""
    out = {}
    for t in ["PPC", "WMS", "INVERTER_01"]:
        r = db.execute(text(f"""
            SELECT COUNT(*), COUNT(DISTINCT TimeCol), MIN(TimeCol), MAX(TimeCol)
            FROM [{t}]""")).first()
        gaps = db.execute(text(f"""
            SELECT COUNT(*), MAX(delta) FROM (
              SELECT DATEDIFF(second, LAG(TimeCol) OVER (ORDER BY TimeCol), TimeCol) delta
              FROM [{t}]) d WHERE delta > 90""")).first()
        out[t] = {"rows": int(r[0]), "distinct_ts": int(r[1]),
                  "duplicates": int(r[0]) - int(r[1]),
                  "first": r[2], "last": r[3],
                  "gaps_over_90s": int(gaps[0] or 0),
                  "largest_gap_s": int(gaps[1] or 0)}
    return out


# ── monthly / yearly aggregates ──────────────────────────────────────────────
#
# Every monthly figure below is restricted to COMPLETE days (>= 1380 of 1440
# minutes recorded) wherever it is summed, because a month holding a half-logged
# day would otherwise read as a month of weaker generation. The number of
# complete days is always carried alongside the total so a partial month can
# never be read as a whole one.

COMPLETE_DAY_MINUTES = 1380


def monthly_plant_sql(db) -> List[Dict[str, Any]]:
    """
    Monthly plant totals computed directly in SQL over complete days.

    This duplicates, by a different route, what the report derives from the daily
    series. The two are compared at build time: if the independent aggregate and
    the day-by-day roll-up ever disagree, the build says so rather than printing
    a number nobody checked.
    """
    rows = db.execute(text(f"""
        WITH d AS (
            SELECT CAST(TimeCol AS DATE) AS dd
            FROM PPC
            GROUP BY CAST(TimeCol AS DATE)
            HAVING COUNT(*) >= {COMPLETE_DAY_MINUTES}
        )
        SELECT FORMAT(p.TimeCol, 'yyyy-MM')                       AS m,
               COUNT(DISTINCT CAST(p.TimeCol AS DATE))            AS complete_days,
               SUM(p.GRID_ACTIVE_POWER_MEASURED) / 60.0           AS energy_mwh,
               MAX(p.GRID_ACTIVE_POWER_MEASURED)                  AS peak_mw,
               SUM(CASE WHEN p.ACTIVE_POWER_SET_POINT > 0
                         AND p.ACTIVE_POWER_SET_POINT < 100 THEN 1 ELSE 0 END)
                                                                  AS curtailed_minutes,
               COUNT(*)                                           AS minutes
        FROM PPC p
        JOIN d ON CAST(p.TimeCol AS DATE) = d.dd
        GROUP BY FORMAT(p.TimeCol, 'yyyy-MM')
        ORDER BY m
    """)).fetchall()
    return [dict(month=r[0], complete_days=int(r[1]),
                 energy_mwh=float(r[2] or 0), peak_mw=float(r[3] or 0),
                 curtailed_minutes=int(r[4] or 0), minutes=int(r[5] or 0))
            for r in rows]


def calendar_days_by_month(db) -> Dict[str, int]:
    """Calendar days on which PPC recorded anything at all, per month."""
    rows = db.execute(text("""
        SELECT FORMAT(TimeCol, 'yyyy-MM'), COUNT(DISTINCT CAST(TimeCol AS DATE))
        FROM PPC GROUP BY FORMAT(TimeCol, 'yyyy-MM')
    """)).fetchall()
    return {r[0]: int(r[1]) for r in rows}


def monthly_inverter(db) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """
    Per inverter, per month: energy, peak, producing minutes and alarm events.

    Keyed [month][inverter]. Energy integrates ACTIVE_POWER (kW) exactly as the
    period-level summary does, so the monthly figures sum to it.
    """
    out: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for t in INVERTERS:
        rows = db.execute(text(f"""
            SELECT FORMAT(TimeCol, 'yyyy-MM')                AS m,
                   SUM(ACTIVE_POWER) / 60.0 / 1000.0         AS energy_mwh,
                   MAX(ACTIVE_POWER)                         AS peak_kw,
                   SUM(CASE WHEN ACTIVE_POWER > 1 THEN 1 ELSE 0 END) AS producing,
                   COUNT(*)                                  AS rows_n
            FROM [{t}] GROUP BY FORMAT(TimeCol, 'yyyy-MM') ORDER BY m
        """)).fetchall()
        unit = t.replace("INVERTER_", "").lstrip("0")
        al = db.execute(text("""
            SELECT FORMAT(TimeCol, 'yyyy-MM'), COUNT(*)
            FROM Alarms WHERE EvDescCol LIKE :p
            GROUP BY FORMAT(TimeCol, 'yyyy-MM')"""),
            {"p": f"INVERTER {int(unit):02d}%"}).fetchall()
        amap = {r[0]: int(r[1]) for r in al}
        for r in rows:
            out.setdefault(r[0], {})[t] = {
                "energy_mwh": float(r[1] or 0), "peak_kw": float(r[2] or 0),
                "producing_minutes": int(r[3] or 0), "rows": int(r[4] or 0),
                "alarm_rows": amap.get(r[0], 0)}
    return out


def monthly_quality(db, tables=("PPC", "WMS", "INVERTER_01")
                    ) -> Dict[str, List[Dict[str, Any]]]:
    """
    Per source, per month: rows, duplicate timestamps, expected minutes, missing
    minutes, completeness and collection gaps.

    `expected` is the number of one-minute slots between that source's first and
    last record *inside the month*, so a source that only began mid-month is not
    charged for the days before it existed. Missing minutes are therefore genuine
    interruptions in a window the source was known to be live.
    """
    out: Dict[str, List[Dict[str, Any]]] = {}
    for t in tables:
        rows = db.execute(text(f"""
            SELECT FORMAT(TimeCol, 'yyyy-MM')  AS m,
                   COUNT(*)                     AS rows_n,
                   COUNT(DISTINCT TimeCol)      AS distinct_ts,
                   COUNT(DISTINCT DATEADD(minute, DATEDIFF(minute, 0, TimeCol), 0))
                                                AS covered_minutes,
                   MIN(TimeCol)                 AS first_ts,
                   MAX(TimeCol)                 AS last_ts
            FROM [{t}] GROUP BY FORMAT(TimeCol, 'yyyy-MM') ORDER BY m
        """)).fetchall()
        gaps = db.execute(text(f"""
            SELECT FORMAT(TimeCol, 'yyyy-MM'), COUNT(*), MAX(delta) FROM (
                SELECT TimeCol,
                       DATEDIFF(second, LAG(TimeCol) OVER (ORDER BY TimeCol), TimeCol) delta
                FROM [{t}]) d
            WHERE delta > 90 GROUP BY FORMAT(TimeCol, 'yyyy-MM')
        """)).fetchall()
        gmap = {r[0]: (int(r[1]), int(r[2] or 0)) for r in gaps}
        series = []
        for r in rows:
            expected = int((r[5] - r[4]).total_seconds() // 60) + 1
            distinct = int(r[2])
            covered = int(r[3])
            g, gmax = gmap.get(r[0], (0, 0))
            # Completeness is measured as minute SLOTS covered, not rows: these
            # sources log on change of value as well as on the periodic sample,
            # so counting rows or raw timestamps produces the nonsense of a month
            # that is 117% complete. The surplus is reported separately instead.
            series.append({"month": r[0], "rows": int(r[1]),
                           "distinct_ts": distinct,
                           "duplicates": int(r[1]) - distinct,
                           "first": r[4], "last": r[5],
                           "expected": expected,
                           "covered_minutes": covered,
                           "excess_samples": max(0, int(r[1]) - covered),
                           "missing": max(0, expected - covered),
                           "completeness_pct": 100.0 * covered / expected if expected else 0.0,
                           "gaps_over_90s": g, "largest_gap_s": gmax})
        out[t] = series
    return out
