# app/routers/equipment_live.py
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text
import logging

from app.database.session import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/equipment-live", tags=["Equipment Live"])


@router.get("/", summary="Live status for all 24 inverters")
def get_equipment_live(db: Session = Depends(get_db)):
    try:
        results = []
        for i in range(1, 25):
            table = f"INVERTER_{str(i).zfill(2)}"
            sql = text(f"""
                SELECT TOP 1
                    TimeCol,
                    DC_VOLTAGE, DC_CURRENT, DC_POWER,
                    ACTIVE_POWER, REACTIVE_POWER, APPARENT_POWER,
                    DAILY_ENERGY, MONTHLY_ENERGY,
                    PRIMEPACK_IGBT_HEATSINK_TEMP,
                    MV_TRAFO_TEMP,
                    DAILY_RUN_MIN, DOWN_TIME_MIN, OK_TIME_MIN
                FROM [{table}]
                ORDER BY TimeCol DESC
            """)
            row = db.execute(sql).fetchone()

            active_power = float(row[4]) if row and row[4] else 0
            igbt_temp    = float(row[9]) if row and row[9] else 0
            down_time    = int(row[12])  if row and row[12] else 0
            ok_time      = int(row[13])  if row and row[13] else 0

            if active_power > 100:
                status = "Online"
            elif igbt_temp > 80:
                status = "Warning"
            elif down_time > ok_time and down_time > 0:
                status = "Offline"
            else:
                status = "Online" if row else "Offline"

            results.append({
                "id":           f"INV-{str(i).zfill(3)}",
                "table":        table,
                "type":         "Inverter",
                "status":       status,
                "timestamp":    row[0].isoformat() if row and row[0] else None,
                "dc_voltage":   round(float(row[1]), 1) if row and row[1] else 0,
                "dc_current":   round(float(row[2]), 1) if row and row[2] else 0,
                "dc_power":     round(float(row[3]), 1) if row and row[3] else 0,
                "active_power": round(active_power, 1),
                "daily_energy": round(float(row[7]), 1) if row and row[7] else 0,
                "igbt_temp":    round(igbt_temp, 1),
                "mv_trafo_temp": round(float(row[10]), 1) if row and row[10] else 0,
                "power":        f"{round(active_power / 1000, 1)} MW",
                "temp":         f"{round(igbt_temp, 1)}C",
                "eff":          f"{round(min((active_power / 100000) * 100, 100), 1)}%",
                "hours":        str(int(row[11]) // 60) if row and row[11] else "0",
            })

        wms_sql = text("""
            SELECT TOP 1
                TimeCol,
                AVG_GHI_IRRADIATION,
                ALL_WMS_AVG_MODULE_TEMP,
                AVG_WIND_SPEED
            FROM [WMS]
            ORDER BY TimeCol DESC
        """)
        wms = db.execute(wms_sql).fetchone()

        for j in range(1, 3):
            results.append({
                "id":     f"WS-{str(j).zfill(3)}",
                "type":   "Weather Station",
                "status": "Online" if wms else "Offline",
                "power":  "---",
                "temp":   f"{round(float(wms[2]), 1)}C" if wms and wms[2] else "---",
                "eff":    "---",
                "hours":  "8760",
                "ghi":    round(float(wms[1]), 1) if wms and wms[1] else 0,
                "wind":   round(float(wms[3]), 1) if wms and wms[3] else 0,
            })

        total   = len(results)
        online  = sum(1 for r in results if r.get("status") == "Online")
        warning = sum(1 for r in results if r.get("status") == "Warning")
        fault   = sum(1 for r in results if r.get("status") == "Fault")

        return {
            "total":   total,
            "online":  online,
            "warning": warning,
            "fault":   fault,
            "items":   results,
        }
    except Exception as e:
        logger.error(f"Equipment live error: {e}", exc_info=True)
        return {"total": 0, "online": 0, "warning": 0, "fault": 0, "items": []}


@router.get("/inverter/{inverter_no}", summary="Single inverter live data")
def get_inverter_live(inverter_no: int, db: Session = Depends(get_db)):
    if inverter_no < 1 or inverter_no > 24:
        raise HTTPException(400, detail="inverter_no must be 1-24")
    try:
        table = f"INVERTER_{str(inverter_no).zfill(2)}"
        sql = text(f"""
            SELECT TOP 1
                TimeCol,
                DC_VOLTAGE, DC_CURRENT, DC_POWER,
                ACTIVE_POWER, REACTIVE_POWER, APPARENT_POWER,
                DAILY_ENERGY, MONTHLY_ENERGY, YEARLY_ENERGY, LIFETIME_ENERGY,
                PRIMEPACK_IGBT_HEATSINK_TEMP, MV_TRAFO_TEMP,
                DAILY_RUN_MIN, MONTHLY_RUN_HR,
                DOWN_TIME_MIN, OK_TIME_MIN, SLEEP_TIME_MIN,
                STRING_CURRENT1, STRING_CURRENT2, STRING_CURRENT3,
                STRING_CURRENT4, STRING_CURRENT5
            FROM [{table}]
            ORDER BY TimeCol DESC
        """)
        row = db.execute(sql).fetchone()
        if not row:
            return {"error": f"No data for {table}"}

        return {
            "inverter":       f"INV-{str(inverter_no).zfill(3)}",
            "table":          table,
            "timestamp":      row[0].isoformat() if row[0] else None,
            "dc_voltage":     round(float(row[1]), 2) if row[1] else 0,
            "dc_current":     round(float(row[2]), 2) if row[2] else 0,
            "dc_power":       round(float(row[3]), 2) if row[3] else 0,
            "active_power":   round(float(row[4]), 2) if row[4] else 0,
            "reactive_power": round(float(row[5]), 2) if row[5] else 0,
            "apparent_power": round(float(row[6]), 2) if row[6] else 0,
            "daily_energy":   round(float(row[7]), 2) if row[7] else 0,
            "monthly_energy": round(float(row[8]), 2) if row[8] else 0,
            "igbt_temp":      round(float(row[11]), 2) if row[11] else 0,
            "mv_trafo_temp":  round(float(row[12]), 2) if row[12] else 0,
            "daily_run_min":  int(row[13]) if row[13] else 0,
            "down_time_min":  int(row[15]) if row[15] else 0,
            "string_currents": [
                round(float(row[18 + j]), 2) if row[18 + j] else 0
                for j in range(5)
            ],
        }
    except Exception as e:
        logger.error(f"Inverter live error: {e}", exc_info=True)
        return {"error": str(e)}