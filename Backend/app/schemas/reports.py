# app/schemas/reports.py

from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, List, Any, Dict
from enum import Enum


# ── Enums ─────────────────────────────────────────────────────────────────────
class ReportType(str, Enum):
    INVERTER         = "inverter"
    DAILY_GEN        = "daily_generation"
    MONTHLY_GEN      = "monthly_generation"
    POWER_GRAPH      = "power_graph"
    POWER_IRRADIANCE = "power_vs_irradiance"
    PPC              = "ppc"
    WMS              = "wms"
    ALARMS           = "alarms"
    TEMPERATURE      = "temperature"

class TimeInterval(str, Enum):
    RAW      = "raw"
    MINUTE_1 = "1min"
    MINUTE_5 = "5min"
    MINUTE_15= "15min"
    MINUTE_30= "30min"
    HOURLY   = "hourly"
    DAILY    = "daily"
    MONTHLY  = "monthly"

class AggFunction(str, Enum):
    AVG = "avg"
    MIN = "min"
    MAX = "max"
    SUM = "sum"


# ── Request Schemas ───────────────────────────────────────────────────────────
class ReportRequest(BaseModel):
    report_type:   ReportType  = Field(..., description="Type of report to generate")
    inverter_no:   Optional[int] = Field(None, ge=1, le=24, description="Inverter number 1-24 (for inverter report)")
    from_datetime: datetime    = Field(..., description="Start datetime")
    to_datetime:   datetime    = Field(..., description="End datetime")
    tags:          Optional[List[str]] = Field(None, description="Specific columns to include. Empty = all.")
    interval:      TimeInterval = Field(default=TimeInterval.RAW, description="Time aggregation interval")
    agg_function:  AggFunction  = Field(default=AggFunction.AVG,  description="Aggregation function")
    page:          int = Field(default=1, ge=1)
    page_size:     int = Field(default=100, ge=1, le=5000)

    model_config = {"json_schema_extra": {
        "example": {
            "report_type": "inverter",
            "inverter_no": 1,
            "from_datetime": "2026-01-01T00:00:00",
            "to_datetime":   "2026-01-01T23:59:59",
            "tags": ["DC_VOLTAGE","DC_CURRENT","ACTIVE_POWER","DAILY_ENERGY"],
            "interval": "15min",
            "agg_function": "avg",
            "page": 1,
            "page_size": 100,
        }
    }}


class ExportRequest(ReportRequest):
    format: str = Field(default="excel", description="Export format: excel or csv")


# ── Response Schemas ──────────────────────────────────────────────────────────
class ReportRow(BaseModel):
    timestamp: datetime
    data:      Dict[str, Any]


class ReportResponse(BaseModel):
    report_type:   str
    from_datetime: datetime
    to_datetime:   datetime
    interval:      str
    agg_function:  str
    total_records: int
    page:          int
    page_size:     int
    columns:       List[str]
    rows:          List[Dict[str, Any]]


# ── Inverter Summary ──────────────────────────────────────────────────────────
class InverterSummary(BaseModel):
    inverter:      str
    total_records: int
    avg_dc_voltage:  Optional[float]
    avg_dc_current:  Optional[float]
    avg_active_power:Optional[float]
    max_active_power:Optional[float]
    total_daily_energy: Optional[float]


class DailyGenRow(BaseModel):
    timestamp:       datetime
    inverter_totals: Dict[str, Optional[float]]
    grand_total:     Optional[float]


class PowerGraphRow(BaseModel):
    timestamp:    datetime
    inverter_powers: Dict[str, Optional[float]]
    total_power:  Optional[float]


class PPCSummary(BaseModel):
    timestamp:                    datetime
    grid_active_power_measured:   Optional[float]
    grid_reactive_power_measured: Optional[float]
    grid_voltage_l_l_measured:    Optional[float]
    grid_frequency_measured:      Optional[float]
    grid_pf_measured:             Optional[float]
    inverter_running:             Optional[int]
    inverter_total_active_power:  Optional[float]
    plant_daily_production:       Optional[float]
    plant_monthly_production:     Optional[float]


class WMSSummary(BaseModel):
    timestamp:                      datetime
    avg_air_temp:                   Optional[float]
    avg_air_pressure:               Optional[float]
    avg_relative_humidity:          Optional[float]
    avg_wind_speed:                 Optional[float]
    avg_wind_direction:             Optional[float]
    avg_ghi_irradiation:            Optional[float]
    avg_gti_irradiation:            Optional[float]
    avg_albedo_up_irradiation:      Optional[float]
    total_irradiance:               Optional[float]
    all_wms_avg_module_temp:        Optional[float]
    avg_ir_soiling_ratio1:          Optional[float]


class AlarmRow(BaseModel):
    timestamp:    datetime
    event:        Optional[str]
    description:  Optional[str]
    ev_desc:      Optional[str]
    duration_sec: Optional[int]
    comment:      Optional[str]


class KPIResponse(BaseModel):
    today_energy_mwh:       Optional[float]
    current_power_mw:       Optional[float]
    inverters_running:      Optional[int]
    grid_frequency:         Optional[float]
    grid_voltage:           Optional[float]
    performance_ratio:      Optional[float]
    avg_irradiance:         Optional[float]
    avg_module_temp:        Optional[float]
    plant_monthly_mwh:      Optional[float]
    plant_yearly_mwh:       Optional[float]