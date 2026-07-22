# app/services/report_service.py

from sqlalchemy.orm import Session
from sqlalchemy import text
from datetime import datetime
from typing import Optional, List, Dict, Any
from fastapi import HTTPException, status
import logging

from app.schemas.reports import (
    ReportRequest, ReportType, TimeInterval, AggFunction
)

logger = logging.getLogger(__name__)

# All 24 inverter table names
INVERTER_TABLES = [f"INVERTER_{str(i).zfill(2)}" for i in range(1, 25)]

# Inverter number → table name
def inverter_table(n: int) -> str:
    return f"INVERTER_{str(n).zfill(2)}"

# All available tags per table
INVERTER_TAGS = [
    "DC_VOLTAGE","DC_CURRENT","DC_POWER",
    "GRID_CURRENT_PHASE1","GRID_CURRENT_PHASE2","GRID_CURRENT_PHASE3",
    "GRID_LINE_VOLT_UV","GRID_LINE_VOLT_VW","GRID_LINE_VOLT_WU",
    "ACTIVE_POWER","REACTIVE_POWER","APPARENT_POWER",
    "DAILY_ENERGY","MONTHLY_ENERGY","YEARLY_ENERGY","LIFETIME_ENERGY",
    "DAILY_RUN_MIN","MONTHLY_RUN_HR","YEARLY_RUN_HR","LIFETIME_RUN_HR",
    "DOWN_TIME_MIN","OK_TIME_MIN","SLEEP_TIME_MIN",
    "PRIMEPACK_IGBT_HEATSINK_TEMP","IHM_IGBT_HEAT_SINK_TEMP",
    "AMPLIFIER_BOARD_TEMP","AIR_INLET_HEAT_SINK_TEMP",
    "CONTROL_SECTION_TEMP","MV_TRAFO_TEMP",
]

WMS_TAGS = [
    "AVG_AIR_TEMP","AVG_AIR_PRESSURE","AVG_RELATIVE_HUMIDITY",
    "AVG_WIND_SPEED","AVG_WIND_DIRECTION",
    "AVG_GHI_IRRADIATION","AVG_GTI_IRRADIATION",
    "AVG_ALBEDO_UP_IRRADIATION","AVG_ALBEDO_DOWN_IRRADIATION",
    "AVG_GHI_CUMM_IRRADIATION","AVG_GTI_CUMM_IRRADIATION",
    "TOTAL_IRRADIANCE","ALL_WMS_AVG_MODULE_TEMP",
    "AVG_IR_SOILING_RATIO1","AVG_IR_SOILING_RATIO2",
    "AVG_IR_BACKPLANE_TEMP","AVG_PRECIPITATION_INTENSITY",
    "AVG_WMS1_MODULE_TEMP","AVG_WMS2_MODULE_TEMP",
    "AVG_WMS3_MODULE_TEMP","AVG_WMS4_MODULE_TEMP",
]

PPC_TAGS = [
    "ACTIVE_POWER_SET_POINT","GRID_ACTIVE_POWER_MEASURED",
    "GRID_FREQUENCY_MEASURED","GRID_PF_MEASURED",
    "GRID_REACTIVE_POWER_MEASURED","GRID_VOLTAGE_L_L_MEASURED",
    "INVERTER_RUNNING","INVERTER_TOTAL_ACTIVE_POWER",
    "INVERTER_TOTAL_REACTIVE_POWER","PLANT_DAILY_PRODUCTION",
    "PLANT_MONTHLY_PRODUCTION","PLANT_YEARLY_PRODUCTION",
    "PLANT_LIFETIME_PRODUCTION","DAILY_OPERATING_TIME",
]


def build_interval_group(interval: TimeInterval) -> str:
    """Build SQL Server DATEADD/DATEDIFF grouping expression."""
    mapping = {
        TimeInterval.MINUTE_1:  "DATEADD(MINUTE, DATEDIFF(MINUTE,  0, TimeCol), 0)",
        TimeInterval.MINUTE_5:  "DATEADD(MINUTE, DATEDIFF(MINUTE,  0, TimeCol)/5*5,  0)",
        TimeInterval.MINUTE_15: "DATEADD(MINUTE, DATEDIFF(MINUTE,  0, TimeCol)/15*15, 0)",
        TimeInterval.MINUTE_30: "DATEADD(MINUTE, DATEDIFF(MINUTE,  0, TimeCol)/30*30, 0)",
        TimeInterval.HOURLY:    "DATEADD(HOUR,   DATEDIFF(HOUR,    0, TimeCol), 0)",
        TimeInterval.DAILY:     "DATEADD(DAY,    DATEDIFF(DAY,     0, TimeCol), 0)",
        TimeInterval.MONTHLY:   "DATEADD(MONTH,  DATEDIFF(MONTH,   0, TimeCol), 0)",
    }
    return mapping.get(interval, "TimeCol")


def build_agg(col: str, fn: AggFunction) -> str:
    """Build aggregation SQL expression for a column."""
    mapping = {
        AggFunction.AVG: f"AVG(CAST([{col}] AS FLOAT))",
        AggFunction.MIN: f"MIN([{col}])",
        AggFunction.MAX: f"MAX([{col}])",
        AggFunction.SUM: f"SUM(CAST([{col}] AS FLOAT))",
    }
    return mapping.get(fn, f"AVG(CAST([{col}] AS FLOAT))")


def rows_to_dict(rows, columns: List[str]) -> List[Dict[str, Any]]:
    """Convert SQLAlchemy result rows to list of dicts."""
    result = []
    for row in rows:
        d = {}
        for i, col in enumerate(columns):
            val = row[i]
            if isinstance(val, datetime):
                d[col] = val.isoformat()
            else:
                d[col] = val
        result.append(d)
    return result


class ReportService:

    # ── Generic Report Query ───────────────────────────────────────────────────
    @staticmethod
    def get_report(db: Session, req: ReportRequest) -> Dict[str, Any]:
        if req.report_type == ReportType.INVERTER:
            return ReportService.get_inverter_report(db, req)
        elif req.report_type == ReportType.DAILY_GEN:
            return ReportService.get_daily_gen(db, req)
        elif req.report_type == ReportType.MONTHLY_GEN:
            return ReportService.get_monthly_gen(db, req)
        elif req.report_type == ReportType.POWER_GRAPH:
            return ReportService.get_power_graph(db, req)
        elif req.report_type == ReportType.POWER_IRRADIANCE:
            return ReportService.get_power_vs_irradiance(db, req)
        elif req.report_type == ReportType.PPC:
            return ReportService.get_ppc(db, req)
        elif req.report_type == ReportType.WMS:
            return ReportService.get_wms(db, req)
        elif req.report_type == ReportType.ALARMS:
            return ReportService.get_alarms(db, req)
        elif req.report_type == ReportType.TEMPERATURE:
            return ReportService.get_temperature(db, req)
        else:
            raise HTTPException(status_code=400, detail=f"Unknown report type: {req.report_type}")

    # ── Inverter Report ────────────────────────────────────────────────────────
    @staticmethod
    def get_inverter_report(db: Session, req: ReportRequest) -> Dict[str, Any]:
        if not req.inverter_no:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="inverter_no is required for inverter report (1-24)"
            )
        table    = inverter_table(req.inverter_no)
        tags     = req.tags if req.tags else INVERTER_TAGS
        # Validate tags
        invalid = [t for t in tags if t not in INVERTER_TAGS]
        if invalid:
            raise HTTPException(400, detail=f"Invalid tags for inverter: {invalid}")

        if req.interval == TimeInterval.RAW:
            col_select = ", ".join([f"[{t}]" for t in tags])
            count_sql  = text(f"""
                SELECT COUNT(*) FROM [{table}]
                WHERE TimeCol BETWEEN :from_dt AND :to_dt
            """)
            data_sql = text(f"""
                SELECT TimeCol, {col_select}
                FROM [{table}]
                WHERE TimeCol BETWEEN :from_dt AND :to_dt
                ORDER BY TimeCol
                OFFSET :offset ROWS FETCH NEXT :page_size ROWS ONLY
            """)
            columns = ["timestamp"] + tags
        else:
            grp      = build_interval_group(req.interval)
            agg_cols = ", ".join([
                f"{build_agg(t, req.agg_function)} AS [{t}]" for t in tags
            ])
            count_sql = text(f"""
                SELECT COUNT(*) FROM (
                    SELECT {grp} AS ts
                    FROM [{table}]
                    WHERE TimeCol BETWEEN :from_dt AND :to_dt
                    GROUP BY {grp}
                ) x
            """)
            data_sql = text(f"""
                SELECT {grp} AS TimeCol, {agg_cols}
                FROM [{table}]
                WHERE TimeCol BETWEEN :from_dt AND :to_dt
                GROUP BY {grp}
                ORDER BY {grp}
                OFFSET :offset ROWS FETCH NEXT :page_size ROWS ONLY
            """)
            columns = ["timestamp"] + tags

        params = {
            "from_dt":   req.from_datetime,
            "to_dt":     req.to_datetime,
            "offset":    (req.page - 1) * req.page_size,
            "page_size": req.page_size,
        }
        total = db.execute(count_sql, {"from_dt": req.from_datetime, "to_dt": req.to_datetime}).scalar()
        rows  = db.execute(data_sql, params).fetchall()

        result_rows = []
        for row in rows:
            d = {"timestamp": row[0].isoformat() if row[0] else None}
            for i, tag in enumerate(tags):
                d[tag] = row[i + 1]
            result_rows.append(d)

        return {
            "report_type":   f"Inverter {str(req.inverter_no).zfill(2)} — {table}",
            "from_datetime": req.from_datetime.isoformat(),
            "to_datetime":   req.to_datetime.isoformat(),
            "interval":      req.interval.value,
            "agg_function":  req.agg_function.value,
            "total_records": total or 0,
            "page":          req.page,
            "page_size":     req.page_size,
            "columns":       columns,
            "rows":          result_rows,
        }

    # ── Daily Generation Report ────────────────────────────────────────────────
    @staticmethod
    def get_daily_gen(db: Session, req: ReportRequest) -> Dict[str, Any]:
        inv_cols = ", ".join([f"[INVERTER_{str(i).zfill(2)}_GEN]" for i in range(1, 25)])
        grand_total = " + ".join([
            f"ISNULL(CAST([INVERTER_{str(i).zfill(2)}_GEN] AS FLOAT), 0)" for i in range(1, 25)
        ])

        sql = text(f"""
            SELECT TimeCol, {inv_cols}, ({grand_total}) AS GRAND_TOTAL
            FROM [INVERTER_DAILY_GEN]
            WHERE TimeCol BETWEEN :from_dt AND :to_dt
            ORDER BY TimeCol
            OFFSET :offset ROWS FETCH NEXT :page_size ROWS ONLY
        """)
        count_sql = text("""
            SELECT COUNT(*) FROM [INVERTER_DAILY_GEN]
            WHERE TimeCol BETWEEN :from_dt AND :to_dt
        """)

        params = {
            "from_dt":   req.from_datetime,
            "to_dt":     req.to_datetime,
            "offset":    (req.page - 1) * req.page_size,
            "page_size": req.page_size,
        }
        total = db.execute(count_sql, {"from_dt": req.from_datetime, "to_dt": req.to_datetime}).scalar()
        rows  = db.execute(sql, params).fetchall()

        result_rows = []
        for row in rows:
            d = {"timestamp": row[0].isoformat() if row[0] else None}
            for i in range(1, 25):
                d[f"INVERTER_{str(i).zfill(2)}_GEN"] = row[i]
            d["GRAND_TOTAL"] = row[25]
            result_rows.append(d)

        columns = ["timestamp"] + [f"INVERTER_{str(i).zfill(2)}_GEN" for i in range(1, 25)] + ["GRAND_TOTAL"]

        return {
            "report_type":   "Daily Generation",
            "from_datetime": req.from_datetime.isoformat(),
            "to_datetime":   req.to_datetime.isoformat(),
            "interval":      req.interval.value,
            "agg_function":  req.agg_function.value,
            "total_records": total or 0,
            "page":          req.page,
            "page_size":     req.page_size,
            "columns":       columns,
            "rows":          result_rows,
        }

    # ── Monthly Generation Report ──────────────────────────────────────────────
    @staticmethod
    def get_monthly_gen(db: Session, req: ReportRequest) -> Dict[str, Any]:
        inv_cols    = ", ".join([f"[INVERTER_{str(i).zfill(2)}_GEN]" for i in range(1, 25)])
        grand_total = " + ".join([
            f"ISNULL(CAST([INVERTER_{str(i).zfill(2)}_GEN] AS FLOAT), 0)" for i in range(1, 25)
        ])

        sql = text(f"""
            SELECT TimeCol, {inv_cols}, ({grand_total}) AS GRAND_TOTAL
            FROM [INVERTER_MONTHLY_GEN]
            WHERE TimeCol BETWEEN :from_dt AND :to_dt
            ORDER BY TimeCol
            OFFSET :offset ROWS FETCH NEXT :page_size ROWS ONLY
        """)
        count_sql = text("""
            SELECT COUNT(*) FROM [INVERTER_MONTHLY_GEN]
            WHERE TimeCol BETWEEN :from_dt AND :to_dt
        """)

        params = {
            "from_dt":   req.from_datetime,
            "to_dt":     req.to_datetime,
            "offset":    (req.page - 1) * req.page_size,
            "page_size": req.page_size,
        }
        total = db.execute(count_sql, {"from_dt": req.from_datetime, "to_dt": req.to_datetime}).scalar()
        rows  = db.execute(sql, params).fetchall()

        result_rows = []
        for row in rows:
            d = {"timestamp": row[0].isoformat() if row[0] else None}
            for i in range(1, 25):
                d[f"INVERTER_{str(i).zfill(2)}_GEN"] = row[i]
            d["GRAND_TOTAL"] = row[25]
            result_rows.append(d)

        columns = ["timestamp"] + [f"INVERTER_{str(i).zfill(2)}_GEN" for i in range(1, 25)] + ["GRAND_TOTAL"]

        return {
            "report_type":   "Monthly Generation",
            "from_datetime": req.from_datetime.isoformat(),
            "to_datetime":   req.to_datetime.isoformat(),
            "interval":      req.interval.value,
            "agg_function":  req.agg_function.value,
            "total_records": total or 0,
            "page":          req.page,
            "page_size":     req.page_size,
            "columns":       columns,
            "rows":          result_rows,
        }

    # ── Power Graph ────────────────────────────────────────────────────────────
    @staticmethod
    def get_power_graph(db: Session, req: ReportRequest) -> Dict[str, Any]:
        inv_cols    = ", ".join([f"[INVERTER_{str(i).zfill(2)}_ACTIVE_POWER]" for i in range(1, 25)])
        total_power = " + ".join([
            f"ISNULL([INVERTER_{str(i).zfill(2)}_ACTIVE_POWER], 0)" for i in range(1, 25)
        ])

        if req.interval == TimeInterval.RAW:
            sql = text(f"""
                SELECT TimeCol, {inv_cols}, ({total_power}) AS TOTAL_POWER
                FROM [POWER_GRAPH]
                WHERE TimeCol BETWEEN :from_dt AND :to_dt
                ORDER BY TimeCol
                OFFSET :offset ROWS FETCH NEXT :page_size ROWS ONLY
            """)
        else:
            grp      = build_interval_group(req.interval)
            agg_cols = ", ".join([
                f"{build_agg(f'INVERTER_{str(i).zfill(2)}_ACTIVE_POWER', req.agg_function)} AS [INVERTER_{str(i).zfill(2)}_ACTIVE_POWER]"
                for i in range(1, 25)
            ])
            sql = text(f"""
                SELECT {grp} AS TimeCol, {agg_cols}
                FROM [POWER_GRAPH]
                WHERE TimeCol BETWEEN :from_dt AND :to_dt
                GROUP BY {grp}
                ORDER BY {grp}
                OFFSET :offset ROWS FETCH NEXT :page_size ROWS ONLY
            """)

        count_sql = text("""
            SELECT COUNT(*) FROM [POWER_GRAPH]
            WHERE TimeCol BETWEEN :from_dt AND :to_dt
        """)

        params = {
            "from_dt":   req.from_datetime,
            "to_dt":     req.to_datetime,
            "offset":    (req.page - 1) * req.page_size,
            "page_size": req.page_size,
        }
        total = db.execute(count_sql, {"from_dt": req.from_datetime, "to_dt": req.to_datetime}).scalar()
        rows  = db.execute(sql, params).fetchall()

        result_rows = []
        for row in rows:
            d = {"timestamp": row[0].isoformat() if row[0] else None}
            for i in range(1, 25):
                d[f"INVERTER_{str(i).zfill(2)}_ACTIVE_POWER"] = row[i]
            if req.interval == TimeInterval.RAW:
                d["TOTAL_POWER"] = row[25]
            result_rows.append(d)

        columns = (["timestamp"] +
                   [f"INVERTER_{str(i).zfill(2)}_ACTIVE_POWER" for i in range(1, 25)] +
                   (["TOTAL_POWER"] if req.interval == TimeInterval.RAW else []))

        return {
            "report_type":   "Power Graph",
            "from_datetime": req.from_datetime.isoformat(),
            "to_datetime":   req.to_datetime.isoformat(),
            "interval":      req.interval.value,
            "agg_function":  req.agg_function.value,
            "total_records": total or 0,
            "page":          req.page,
            "page_size":     req.page_size,
            "columns":       columns,
            "rows":          result_rows,
        }

    # ── Power vs Irradiance ────────────────────────────────────────────────────
    @staticmethod
    def get_power_vs_irradiance(db: Session, req: ReportRequest) -> Dict[str, Any]:
        tags = ["POWER_GENERATION", "AVG_ALBEDO_UP_CUMM_IRRADIATION", "AVG_GHI_IRRADIATION"]

        sql = text("""
            SELECT TimeCol, POWER_GENERATION, AVG_ALBEDO_UP_CUMM_IRRADIATION, AVG_GHI_IRRADIATION
            FROM [POWER_VS_IRRADIANCE]
            WHERE TimeCol BETWEEN :from_dt AND :to_dt
            ORDER BY TimeCol
            OFFSET :offset ROWS FETCH NEXT :page_size ROWS ONLY
        """)
        count_sql = text("""
            SELECT COUNT(*) FROM [POWER_VS_IRRADIANCE]
            WHERE TimeCol BETWEEN :from_dt AND :to_dt
        """)

        params = {
            "from_dt":   req.from_datetime,
            "to_dt":     req.to_datetime,
            "offset":    (req.page - 1) * req.page_size,
            "page_size": req.page_size,
        }
        total = db.execute(count_sql, {"from_dt": req.from_datetime, "to_dt": req.to_datetime}).scalar()
        rows  = db.execute(sql, params).fetchall()

        result_rows = []
        for row in rows:
            result_rows.append({
                "timestamp":                      row[0].isoformat() if row[0] else None,
                "POWER_GENERATION":               row[1],
                "AVG_ALBEDO_UP_CUMM_IRRADIATION": row[2],
                "AVG_GHI_IRRADIATION":            row[3],
            })

        return {
            "report_type":   "Power vs Irradiance",
            "from_datetime": req.from_datetime.isoformat(),
            "to_datetime":   req.to_datetime.isoformat(),
            "interval":      req.interval.value,
            "agg_function":  req.agg_function.value,
            "total_records": total or 0,
            "page":          req.page,
            "page_size":     req.page_size,
            "columns":       ["timestamp"] + tags,
            "rows":          result_rows,
        }

    # ── PPC Report ─────────────────────────────────────────────────────────────
    @staticmethod
    def get_ppc(db: Session, req: ReportRequest) -> Dict[str, Any]:
        tags = req.tags if req.tags else PPC_TAGS
        invalid = [t for t in tags if t not in PPC_TAGS]
        if invalid:
            raise HTTPException(400, detail=f"Invalid PPC tags: {invalid}")

        col_select = ", ".join([f"[{t}]" for t in tags])

        if req.interval == TimeInterval.RAW:
            sql = text(f"""
                SELECT TimeCol, {col_select}
                FROM [PPC]
                WHERE TimeCol BETWEEN :from_dt AND :to_dt
                ORDER BY TimeCol
                OFFSET :offset ROWS FETCH NEXT :page_size ROWS ONLY
            """)
        else:
            grp      = build_interval_group(req.interval)
            agg_cols = ", ".join([f"{build_agg(t, req.agg_function)} AS [{t}]" for t in tags])
            sql = text(f"""
                SELECT {grp} AS TimeCol, {agg_cols}
                FROM [PPC]
                WHERE TimeCol BETWEEN :from_dt AND :to_dt
                GROUP BY {grp}
                ORDER BY {grp}
                OFFSET :offset ROWS FETCH NEXT :page_size ROWS ONLY
            """)

        count_sql = text("SELECT COUNT(*) FROM [PPC] WHERE TimeCol BETWEEN :from_dt AND :to_dt")

        params = {
            "from_dt":   req.from_datetime,
            "to_dt":     req.to_datetime,
            "offset":    (req.page - 1) * req.page_size,
            "page_size": req.page_size,
        }
        total = db.execute(count_sql, {"from_dt": req.from_datetime, "to_dt": req.to_datetime}).scalar()
        rows  = db.execute(sql, params).fetchall()

        result_rows = []
        for row in rows:
            d = {"timestamp": row[0].isoformat() if row[0] else None}
            for i, tag in enumerate(tags):
                d[tag] = row[i + 1]
            result_rows.append(d)

        return {
            "report_type":   "PPC",
            "from_datetime": req.from_datetime.isoformat(),
            "to_datetime":   req.to_datetime.isoformat(),
            "interval":      req.interval.value,
            "agg_function":  req.agg_function.value,
            "total_records": total or 0,
            "page":          req.page,
            "page_size":     req.page_size,
            "columns":       ["timestamp"] + tags,
            "rows":          result_rows,
        }

    # ── WMS Report ─────────────────────────────────────────────────────────────
    @staticmethod
    def get_wms(db: Session, req: ReportRequest) -> Dict[str, Any]:
        tags = req.tags if req.tags else WMS_TAGS
        invalid = [t for t in tags if t not in WMS_TAGS]
        if invalid:
            raise HTTPException(400, detail=f"Invalid WMS tags: {invalid}")

        col_select = ", ".join([f"[{t}]" for t in tags])

        if req.interval == TimeInterval.RAW:
            sql = text(f"""
                SELECT TimeCol, {col_select}
                FROM [WMS]
                WHERE TimeCol BETWEEN :from_dt AND :to_dt
                ORDER BY TimeCol
                OFFSET :offset ROWS FETCH NEXT :page_size ROWS ONLY
            """)
        else:
            grp      = build_interval_group(req.interval)
            agg_cols = ", ".join([f"{build_agg(t, req.agg_function)} AS [{t}]" for t in tags])
            sql = text(f"""
                SELECT {grp} AS TimeCol, {agg_cols}
                FROM [WMS]
                WHERE TimeCol BETWEEN :from_dt AND :to_dt
                GROUP BY {grp}
                ORDER BY {grp}
                OFFSET :offset ROWS FETCH NEXT :page_size ROWS ONLY
            """)

        count_sql = text("SELECT COUNT(*) FROM [WMS] WHERE TimeCol BETWEEN :from_dt AND :to_dt")

        params = {
            "from_dt":   req.from_datetime,
            "to_dt":     req.to_datetime,
            "offset":    (req.page - 1) * req.page_size,
            "page_size": req.page_size,
        }
        total = db.execute(count_sql, {"from_dt": req.from_datetime, "to_dt": req.to_datetime}).scalar()
        rows  = db.execute(sql, params).fetchall()

        result_rows = []
        for row in rows:
            d = {"timestamp": row[0].isoformat() if row[0] else None}
            for i, tag in enumerate(tags):
                d[tag] = row[i + 1]
            result_rows.append(d)

        return {
            "report_type":   "WMS Weather Station",
            "from_datetime": req.from_datetime.isoformat(),
            "to_datetime":   req.to_datetime.isoformat(),
            "interval":      req.interval.value,
            "agg_function":  req.agg_function.value,
            "total_records": total or 0,
            "page":          req.page,
            "page_size":     req.page_size,
            "columns":       ["timestamp"] + tags,
            "rows":          result_rows,
        }

    # ── Alarms Report ──────────────────────────────────────────────────────────
    @staticmethod
    def get_alarms(db: Session, req: ReportRequest) -> Dict[str, Any]:
        sql = text("""
            SELECT TimeCol, EventCol, DescCol, EvDescCol, DurCol, CommCol, UniID, TraID
            FROM [Alarms]
            WHERE TimeCol BETWEEN :from_dt AND :to_dt
            ORDER BY TimeCol DESC
            OFFSET :offset ROWS FETCH NEXT :page_size ROWS ONLY
        """)
        count_sql = text("SELECT COUNT(*) FROM [Alarms] WHERE TimeCol BETWEEN :from_dt AND :to_dt")

        params = {
            "from_dt":   req.from_datetime,
            "to_dt":     req.to_datetime,
            "offset":    (req.page - 1) * req.page_size,
            "page_size": req.page_size,
        }
        total = db.execute(count_sql, {"from_dt": req.from_datetime, "to_dt": req.to_datetime}).scalar()
        rows  = db.execute(sql, params).fetchall()

        columns = ["timestamp","event","description","ev_description","duration_sec","comment","uni_id","tra_id"]
        result_rows = []
        for row in rows:
            result_rows.append({
                "timestamp":      row[0].isoformat() if row[0] else None,
                "event":          row[1],
                "description":    row[2],
                "ev_description": row[3],
                "duration_sec":   row[4],
                "comment":        row[5],
                "uni_id":         row[6],
                "tra_id":         row[7],
            })

        return {
            "report_type":   "Alarms",
            "from_datetime": req.from_datetime.isoformat(),
            "to_datetime":   req.to_datetime.isoformat(),
            "interval":      "raw",
            "agg_function":  "none",
            "total_records": total or 0,
            "page":          req.page,
            "page_size":     req.page_size,
            "columns":       columns,
            "rows":          result_rows,
        }

    # ── Temperature Report ─────────────────────────────────────────────────────
    @staticmethod
    def get_temperature(db: Session, req: ReportRequest) -> Dict[str, Any]:
        TEMP_TAGS = [
            "AC_BREAKER_TEMP_18","AC_BREAKER_TEMP_23",
            "AMBIENT_BOTTOM_TEMP_18","AMBIENT_BOTTOM_TEMP_23",
            "BECHKOFF_TEMP_18","BECHKOFF_TEMP_23",
            "CTRL_BOTTOM_18","CTRL_BOTTOM_23",
            "DC_INCOMER_18","DC_INCOMER_23",
            "OUT_HEAT_SINK_PSU1_18","OUT_HEAT_SINK_PSU1_23",
            "OUT_HEAT_SINK_PSU2_18","OUT_HEAT_SINK_PSU2_23",
            "PEC_18","PEC_23",
        ]
        tags       = req.tags if req.tags else TEMP_TAGS
        col_select = ", ".join([f"[{t}]" for t in tags])

        sql = text(f"""
            SELECT TimeCol, {col_select}
            FROM [TEMPERATURE_REPORT]
            WHERE TimeCol BETWEEN :from_dt AND :to_dt
            ORDER BY TimeCol
            OFFSET :offset ROWS FETCH NEXT :page_size ROWS ONLY
        """)
        count_sql = text("""
            SELECT COUNT(*) FROM [TEMPERATURE_REPORT]
            WHERE TimeCol BETWEEN :from_dt AND :to_dt
        """)

        params = {
            "from_dt":   req.from_datetime,
            "to_dt":     req.to_datetime,
            "offset":    (req.page - 1) * req.page_size,
            "page_size": req.page_size,
        }
        total = db.execute(count_sql, {"from_dt": req.from_datetime, "to_dt": req.to_datetime}).scalar()
        rows  = db.execute(sql, params).fetchall()

        result_rows = []
        for row in rows:
            d = {"timestamp": row[0].isoformat() if row[0] else None}
            for i, tag in enumerate(tags):
                d[tag] = row[i + 1]
            result_rows.append(d)

        return {
            "report_type":   "Temperature Report",
            "from_datetime": req.from_datetime.isoformat(),
            "to_datetime":   req.to_datetime.isoformat(),
            "interval":      req.interval.value,
            "agg_function":  req.agg_function.value,
            "total_records": total or 0,
            "page":          req.page,
            "page_size":     req.page_size,
            "columns":       ["timestamp"] + tags,
            "rows":          result_rows,
        }

    # ── KPI Dashboard ──────────────────────────────────────────────────────────
    @staticmethod
    def get_kpis(db: Session) -> Dict[str, Any]:
        """Fetch latest KPI values from PPC, WMS tables for dashboard."""
        try:
            ppc_sql = text("""
                SELECT TOP 1
                    PLANT_DAILY_PRODUCTION,
                    PLANT_MONTHLY_PRODUCTION,
                    PLANT_YEARLY_PRODUCTION,
                    INVERTER_TOTAL_ACTIVE_POWER,
                    INVERTER_RUNNING,
                    GRID_FREQUENCY_MEASURED,
                    GRID_VOLTAGE_L_L_MEASURED,
                    GRID_PF_MEASURED
                FROM [PPC]
                ORDER BY TimeCol DESC
            """)
            ppc = db.execute(ppc_sql).fetchone()

            wms_sql = text("""
                SELECT TOP 1
                    AVG_GHI_IRRADIATION,
                    ALL_WMS_AVG_MODULE_TEMP,
                    AVG_WIND_SPEED,
                    AVG_AIR_TEMP,
                    AVG_IR_SOILING_RATIO1,
                    TOTAL_IRRADIANCE
                FROM [WMS]
                ORDER BY TimeCol DESC
            """)
            wms = db.execute(wms_sql).fetchone()

            return {
                "today_energy_mwh":      round(ppc[0] / 1000, 3) if ppc and ppc[0] else None,
                "monthly_energy_mwh":    round(ppc[1] / 1000, 3) if ppc and ppc[1] else None,
                "yearly_energy_mwh":     round(ppc[2] / 1000, 3) if ppc and ppc[2] else None,
                "current_power_mw":      round(ppc[3] / 1000, 3) if ppc and ppc[3] else None,
                "inverters_running":     int(ppc[4]) if ppc and ppc[4] else None,
                "grid_frequency_hz":     round(ppc[5], 3) if ppc and ppc[5] else None,
                "grid_voltage_kv":       round(ppc[6] / 1000, 3) if ppc and ppc[6] else None,
                "grid_power_factor":     round(ppc[7], 4) if ppc and ppc[7] else None,
                "avg_irradiance_wm2":    round(wms[0], 2) if wms and wms[0] else None,
                "avg_module_temp_c":     round(wms[1], 2) if wms and wms[1] else None,
                "avg_wind_speed_ms":     round(wms[2], 2) if wms and wms[2] else None,
                "avg_air_temp_c":        round(wms[3], 2) if wms and wms[3] else None,
                "soiling_ratio":         round(wms[4], 4) if wms and wms[4] else None,
                "total_irradiance":      round(wms[5], 2) if wms and wms[5] else None,
            }
        except Exception as e:
            logger.error(f"KPI fetch error: {e}")
            raise HTTPException(500, detail=f"KPI fetch failed: {str(e)}")

    # ── Available Tags per Report Type ─────────────────────────────────────────
    @staticmethod
    def get_available_tags() -> Dict[str, Any]:
        return {
            "inverter":         INVERTER_TAGS,
            "ppc":              PPC_TAGS,
            "wms":              WMS_TAGS,
            "daily_generation": [f"INVERTER_{str(i).zfill(2)}_GEN" for i in range(1, 25)] + ["GRAND_TOTAL"],
            "power_graph":      [f"INVERTER_{str(i).zfill(2)}_ACTIVE_POWER" for i in range(1, 25)] + ["TOTAL_POWER"],
            "power_vs_irradiance": ["POWER_GENERATION","AVG_ALBEDO_UP_CUMM_IRRADIATION","AVG_GHI_IRRADIATION"],
            "alarms":           ["event","description","ev_description","duration_sec","comment"],
            "temperature":      ["AC_BREAKER_TEMP_18","AC_BREAKER_TEMP_23","AMBIENT_BOTTOM_TEMP_18","AMBIENT_BOTTOM_TEMP_23","PEC_18","PEC_23"],
        }