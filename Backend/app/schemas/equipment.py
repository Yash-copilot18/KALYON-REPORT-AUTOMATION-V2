# app/schemas/equipment.py

from pydantic import BaseModel, Field, field_validator
from datetime import datetime
from typing import Optional
from app.models.equipment import EquipmentStatus


# ── Base ──────────────────────────────────────────────────────────────────────
class EquipmentBase(BaseModel):
    equipment_name: str = Field(
        ...,
        min_length=1,
        max_length=100,
        examples=["INV-001"],
        description="Unique equipment identifier name",
    )
    equipment_type: str = Field(
        ...,
        min_length=1,
        max_length=100,
        examples=["Inverter"],
        description="Category of equipment",
    )
    status: EquipmentStatus = Field(
        default=EquipmentStatus.OFFLINE,
        description="Operational status",
    )

    @field_validator("equipment_name", "equipment_type")
    @classmethod
    def strip_whitespace(cls, v: str) -> str:
        return v.strip()


# ── Create Request ────────────────────────────────────────────────────────────
class EquipmentCreate(EquipmentBase):
    """Schema for POST /equipment — create a new record."""
    pass


# ── Update Request ────────────────────────────────────────────────────────────
class EquipmentUpdate(BaseModel):
    """Schema for PUT /equipment/{id} — all fields optional."""
    equipment_name: Optional[str] = Field(None, min_length=1, max_length=100)
    equipment_type: Optional[str] = Field(None, min_length=1, max_length=100)
    status: Optional[EquipmentStatus] = None

    @field_validator("equipment_name", "equipment_type")
    @classmethod
    def strip_whitespace(cls, v: Optional[str]) -> Optional[str]:
        return v.strip() if v else v


# ── DB Response ───────────────────────────────────────────────────────────────
class EquipmentResponse(EquipmentBase):
    """Schema returned from all read endpoints."""
    id: int
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# ── List Response ─────────────────────────────────────────────────────────────
class EquipmentListResponse(BaseModel):
    """Paginated list response."""
    total: int
    page: int
    page_size: int
    items: list[EquipmentResponse]