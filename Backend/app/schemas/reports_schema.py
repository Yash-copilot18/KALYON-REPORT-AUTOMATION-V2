# app/schemas/reports_schema.py

from pydantic import BaseModel, Field, field_validator
from typing import Optional, List, Any, Dict
from datetime import datetime
from enum import Enum

from app.services import intervals


class IntervalEnum(str, Enum):
    raw     = "raw"
    min1    = "1min"
    min5    = "5min"
    min15   = "15min"
    min30   = "30min"
    hourly  = "hourly"
    daily   = "daily"
    monthly = "monthly"


class AggFuncEnum(str, Enum):
    avg = "avg"
    min = "min"
    max = "max"
    sum = "sum"


class MultiSourceSpec(BaseModel):
    """One equipment's contribution to a multi-equipment report."""
    equipment_type: str
    equipment_id:   str
    tags:           List[str] = Field(..., min_length=1)


class MultiReportRequest(BaseModel):
    """
    A report assembled from several equipment at once (Preconfigured Reports).

    Every source is bucketed to the same `interval` and merged on that bucket, so
    one row carries the selected columns of every equipment at that timestamp.
    """
    sources:       List[MultiSourceSpec] = Field(..., min_length=1)
    from_datetime: datetime
    to_datetime:   datetime
    interval:      IntervalEnum = IntervalEnum.hourly
    agg_function:  AggFuncEnum  = AggFuncEnum.avg
    page:          int = Field(default=1, ge=1)
    page_size:     int = Field(default=100, ge=1, le=200000)

    @field_validator("to_datetime")
    @classmethod
    def to_after_from(cls, v, info):
        if "from_datetime" in info.data and v <= info.data["from_datetime"]:
            raise ValueError("to_datetime must be after from_datetime")
        return v


class ReportDataRequest(BaseModel):
    equipment_type: str = Field(..., description="e.g. Inverter, WMS, PPC")
    equipment_id:   str = Field(..., description="e.g. INVERTER_01")
    # Optional multi-equipment selection — used by the hierarchical String Combiner
    # Excel export to place one worksheet per inverter in a single workbook.
    equipment_ids:  Optional[List[str]] = Field(default=None)
    tags:           List[str] = Field(..., min_length=1)
    from_datetime:  datetime
    to_datetime:    datetime
    interval:       IntervalEnum = IntervalEnum.raw
    agg_function:   AggFuncEnum  = AggFuncEnum.avg
    page:           int = Field(default=1, ge=1)
    # Upper bound raised so the multi-equipment table can load a full range per
    # device in one pass (rendered via a virtualized table). Bounded to guard
    # against pathological requests.
    page_size:      int = Field(default=100, ge=1, le=200000)
    # Excel export column policy. Default (False) keeps the long-standing behaviour:
    # every worksheet carries its equipment's COMPLETE tag set, discovered from that
    # equipment's own schema, so a shared selection never truncates a sheet.
    # Set True to export exactly `tags`, in exactly the order given — used by the
    # Preconfigured Reports page, where the column set and order ARE the report.
    use_selected_tags: bool = Field(default=False)

    @field_validator("to_datetime")
    @classmethod
    def to_must_be_after_from(cls, v, info):
        if "from_datetime" in info.data and v <= info.data["from_datetime"]:
            raise ValueError("to_datetime must be after from_datetime")
        return v

    @field_validator("tags")
    @classmethod
    def tags_not_empty(cls, v):
        if not v:
            raise ValueError("At least one tag required")
        return v

    @property
    def is_instant(self) -> bool:
        """Instant telemetry (raw / 1-minute) — served as raw records, never aggregated."""
        return intervals.is_instant(self.interval)

    @property
    def effective_agg(self) -> Optional[str]:
        """The aggregation actually applied — None for instant intervals."""
        return intervals.effective_agg(self.interval, self.agg_function)