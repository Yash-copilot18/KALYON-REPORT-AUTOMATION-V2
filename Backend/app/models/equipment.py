# app/models/equipment.py

from sqlalchemy import Column, Integer, String, DateTime, Enum as SAEnum
from sqlalchemy.sql import func
from datetime import datetime, timezone
import enum

from app.database.base import Base


class EquipmentStatus(str, enum.Enum):
    ONLINE  = "Online"
    OFFLINE = "Offline"
    WARNING = "Warning"
    FAULT   = "Fault"
    MAINTENANCE = "Maintenance"


class Equipment(Base):
    """
    Equipment table — tracks all physical devices in the solar plant.
    Maps to SQL Server table: [Kalyan].[dbo].[equipment]
    """
    __tablename__ = "equipment"

    id = Column(
        Integer,
        primary_key=True,
        index=True,
        autoincrement=True,
        comment="Primary key",
    )
    equipment_name = Column(
        String(100),
        nullable=False,
        index=True,
        comment="Human-readable equipment name, e.g. INV-001",
    )
    equipment_type = Column(
        String(100),
        nullable=False,
        index=True,
        comment="Equipment category, e.g. Inverter, Transformer",
    )
    status = Column(
        String(50),
        nullable=False,
        default=EquipmentStatus.OFFLINE.value,
        comment="Operational status of the equipment",
    )
    created_at = Column(
        DateTime,
        nullable=False,
        server_default=func.now(),
        comment="Record creation timestamp (UTC)",
    )
    updated_at = Column(
        DateTime,
        nullable=True,
        onupdate=func.now(),
        comment="Last update timestamp (UTC)",
    )

    def __repr__(self) -> str:
        return (
            f"<Equipment(id={self.id}, "
            f"name='{self.equipment_name}', "
            f"type='{self.equipment_type}', "
            f"status='{self.status}')>"
        )