# app/services/equipment_service.py

from sqlalchemy.orm import Session
from sqlalchemy import func, or_
from fastapi import HTTPException, status
from typing import Optional
import logging

from app.models.equipment import Equipment
from app.schemas.equipment import EquipmentCreate, EquipmentUpdate

logger = logging.getLogger(__name__)


class EquipmentService:

    # ── Create ────────────────────────────────────────────────────────────────
    @staticmethod
    def create(db: Session, payload: EquipmentCreate) -> Equipment:
        # Check for duplicate name
        existing = db.query(Equipment).filter(
            Equipment.equipment_name == payload.equipment_name
        ).first()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Equipment with name '{payload.equipment_name}' already exists.",
            )

        record = Equipment(
            equipment_name=payload.equipment_name,
            equipment_type=payload.equipment_type,
            status=payload.status.value,
        )
        db.add(record)
        db.flush()        # flush to get the generated id
        db.refresh(record)
        logger.info(f"Created equipment id={record.id} name='{record.equipment_name}'")
        return record

    # ── Get by ID ─────────────────────────────────────────────────────────────
    @staticmethod
    def get_by_id(db: Session, equipment_id: int) -> Equipment:
        record = db.query(Equipment).filter(Equipment.id == equipment_id).first()
        if not record:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Equipment with id={equipment_id} not found.",
            )
        return record

    # ── List with pagination + optional filter ─────────────────────────────────
    @staticmethod
    def list_all(
        db: Session,
        page: int = 1,
        page_size: int = 20,
        equipment_type: Optional[str] = None,
        status_filter: Optional[str] = None,
        search: Optional[str] = None,
    ) -> dict:
        query = db.query(Equipment)

        if equipment_type:
            query = query.filter(Equipment.equipment_type == equipment_type)
        if status_filter:
            query = query.filter(Equipment.status == status_filter)
        if search:
            like = f"%{search}%"
            query = query.filter(
                or_(
                    Equipment.equipment_name.like(like),
                    Equipment.equipment_type.like(like),
                )
            )

        total = query.with_entities(func.count(Equipment.id)).scalar()
        items = (
            query
            .order_by(Equipment.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        return {"total": total, "page": page, "page_size": page_size, "items": items}

    # ── Update ────────────────────────────────────────────────────────────────
    @staticmethod
    def update(db: Session, equipment_id: int, payload: EquipmentUpdate) -> Equipment:
        record = EquipmentService.get_by_id(db, equipment_id)

        update_data = payload.model_dump(exclude_unset=True)
        if not update_data:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No fields provided for update.",
            )

        # Check duplicate name if name is being changed
        if "equipment_name" in update_data and update_data["equipment_name"] != record.equipment_name:
            duplicate = db.query(Equipment).filter(
                Equipment.equipment_name == update_data["equipment_name"]
            ).first()
            if duplicate:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Equipment name '{update_data['equipment_name']}' already taken.",
                )

        for field, value in update_data.items():
            if field == "status" and hasattr(value, "value"):
                value = value.value
            setattr(record, field, value)

        db.flush()
        db.refresh(record)
        logger.info(f"Updated equipment id={record.id}")
        return record

    # ── Delete ────────────────────────────────────────────────────────────────
    @staticmethod
    def delete(db: Session, equipment_id: int) -> dict:
        record = EquipmentService.get_by_id(db, equipment_id)
        db.delete(record)
        db.flush()
        logger.info(f"Deleted equipment id={equipment_id}")
        return {"message": f"Equipment id={equipment_id} deleted successfully."}