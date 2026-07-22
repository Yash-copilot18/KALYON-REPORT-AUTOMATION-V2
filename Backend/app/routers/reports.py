from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import text
import logging
from datetime import datetime

from app.database.session import get_db
from app.schemas.reports import ReportRequest, ExportRequest
from app.services.report_service import ReportService
from app.services.report_files import build_report_csv, fmt_timestamp, PROJECT_NAME

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/reports",
    tags=["Reports"],
)


@router.get("/tags", summary="Get all available tags per report type")
def get_available_tags():
    return ReportService.get_available_tags()


@router.get("/kpis", summary="Get latest plant KPIs from PPC and WMS")
def get_kpis(db: Session = Depends(get_db)):
    return ReportService.get_kpis(db)


@router.post("/data", summary="Fetch report data from any plant table")
def get_report_data(req: ReportRequest, db: Session = Depends(get_db)):
    return ReportService.get_report(db, req)


@router.post("/export/csv", summary="Export report data as CSV")
def export_csv(req: ExportRequest, db: Session = Depends(get_db)):
    req.page_size = 5000
    req.page = 1
    result = ReportService.get_report(db, req)
    rows = result.get("rows", [])
    cols = result.get("columns", [])

    # Same standard, professional CSV format used everywhere in the app.
    metadata = [
        ("Project Name",          PROJECT_NAME),
        ("Report Name",           f"{getattr(req, 'equipment_type', 'Report')} Report"),
        ("Equipment",             getattr(req, "equipment_id", "") or ""),
        ("Generated Date & Time", datetime.now().strftime("%d/%m/%Y %H:%M:%S")),
        ("From Date",             fmt_timestamp(getattr(req, "from_datetime", None))),
        ("To Date",               fmt_timestamp(getattr(req, "to_datetime", None))),
    ]
    data = build_report_csv(cols, rows, metadata=metadata)
    filename = f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return StreamingResponse(
        iter([data]),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename={filename}",
            "Access-Control-Expose-Headers": "Content-Disposition",
        },
    )


@router.get("/inverter/{inverter_no}/summary", summary="Summary stats for one inverter")
def inverter_summary(
    inverter_no: int,
    from_dt: datetime = Query(...),
    to_dt: datetime = Query(...),
    db: Session = Depends(get_db),
):
    if inverter_no < 1 or inverter_no > 24:
        raise HTTPException(400, detail="inverter_no must be 1-24")
    table = f"INVERTER_{str(inverter_no).zfill(2)}"
    sql = text(f"""
        SELECT
            COUNT(*) AS total_records,
            AVG(CAST(DC_VOLTAGE AS FLOAT)) AS avg_dc_voltage,
            AVG(CAST(DC_CURRENT AS FLOAT)) AS avg_dc_current,
            AVG(CAST(ACTIVE_POWER AS FLOAT)) AS avg_active_power,
            MAX(CAST(ACTIVE_POWER AS FLOAT)) AS max_active_power,
            MAX(CAST(DAILY_ENERGY AS FLOAT)) AS max_daily_energy,
            MAX(CAST(MONTHLY_ENERGY AS FLOAT)) AS max_monthly_energy,
            AVG(CAST(MV_TRAFO_TEMP AS FLOAT)) AS avg_mv_trafo_temp,
            MAX(CAST(PRIMEPACK_IGBT_HEATSINK_TEMP AS FLOAT)) AS max_igbt_temp
        FROM [{table}]
        WHERE TimeCol BETWEEN :from_dt AND :to_dt
    """)
    row = db.execute(sql, {"from_dt": from_dt, "to_dt": to_dt}).fetchone()
    return {
        "inverter": table,
        "from_datetime": from_dt.isoformat(),
        "to_datetime": to_dt.isoformat(),
        "total_records": row[0],
        "avg_dc_voltage_v": round(row[1], 2) if row[1] else None,
        "avg_dc_current_a": round(row[2], 2) if row[2] else None,
        "avg_active_power_kw": round(row[3], 2) if row[3] else None,
        "max_active_power_kw": round(row[4], 2) if row[4] else None,
        "max_daily_energy_kwh": round(row[5], 2) if row[5] else None,
        "max_monthly_energy_kwh": round(row[6], 2) if row[6] else None,
        "avg_mv_trafo_temp_c": round(row[7], 2) if row[7] else None,
        "max_igbt_temp_c": round(row[8], 2) if row[8] else None,
    }


@router.get("/wms/latest", summary="Get latest weather station reading")
def wms_latest(db: Session = Depends(get_db)):
    sql = text("""
        SELECT TOP 1
            TimeCol,
            AVG_AIR_TEMP, AVG_AIR_PRESSURE, AVG_RELATIVE_HUMIDITY,
            AVG_WIND_SPEED, AVG_WIND_DIRECTION,
            AVG_GHI_IRRADIATION, AVG_GTI_IRRADIATION,
            AVG_ALBEDO_UP_IRRADIATION,
            TOTAL_IRRADIANCE, ALL_WMS_AVG_MODULE_TEMP,
            AVG_IR_SOILING_RATIO1, AVG_IR_SOILING_RATIO2,
            AVG_PRECIPITATION_INTENSITY
        FROM [WMS]
        ORDER BY TimeCol DESC
    """)
    row = db.execute(sql).fetchone()
    if not row:
        raise HTTPException(404, detail="No WMS data found")
    return {
        "timestamp": row[0].isoformat() if row[0] else None,
        "avg_air_temp_c": row[1],
        "avg_air_pressure_hpa": row[2],
        "avg_relative_humidity_pct": row[3],
        "avg_wind_speed_ms": row[4],
        "avg_wind_direction_deg": row[5],
        "avg_ghi_irradiation": row[6],
        "avg_gti_irradiation": row[7],
        "avg_albedo_up_irradiation": row[8],
        "total_irradiance": row[9],
        "all_wms_avg_module_temp_c": row[10],
        "avg_ir_soiling_ratio1": row[11],
        "avg_ir_soiling_ratio2": row[12],
        "avg_precipitation_intensity": row[13],
    }


@router.get("/ppc/latest", summary="Get latest PPC reading")
def ppc_latest(db: Session = Depends(get_db)):
    sql = text("""
        SELECT TOP 1
            TimeCol,
            GRID_ACTIVE_POWER_MEASURED, GRID_REACTIVE_POWER_MEASURED,
            GRID_VOLTAGE_L_L_MEASURED, GRID_FREQUENCY_MEASURED,
            GRID_PF_MEASURED, INVERTER_RUNNING,
            INVERTER_TOTAL_ACTIVE_POWER, INVERTER_TOTAL_REACTIVE_POWER,
            PLANT_DAILY_PRODUCTION, PLANT_MONTHLY_PRODUCTION,
            PLANT_YEARLY_PRODUCTION, PLANT_LIFETIME_PRODUCTION
        FROM [PPC]
        ORDER BY TimeCol DESC
    """)
    row = db.execute(sql).fetchone()
    if not row:
        raise HTTPException(404, detail="No PPC data found")
    return {
        "timestamp": row[0].isoformat() if row[0] else None,
        "grid_active_power_kw": row[1],
        "grid_reactive_power_kvar": row[2],
        "grid_voltage_l_l_v": row[3],
        "grid_frequency_hz": row[4],
        "grid_power_factor": row[5],
        "inverters_running": row[6],
        "inverter_total_active_power_kw": row[7],
        "inverter_total_reactive_power": row[8],
        "plant_daily_production_kwh": row[9],
        "plant_monthly_production_kwh": row[10],
        "plant_yearly_production_kwh": row[11],
        "plant_lifetime_production_kwh": row[12],
    }