# app/routers/equipment.py

from fastapi import APIRouter, Depends, Query, Path, status
from sqlalchemy.orm import Session
from typing import Optional

from app.database.session import get_db
from app.schemas.equipment import (
    EquipmentCreate,
    EquipmentUpdate,
    EquipmentResponse,
    EquipmentListResponse,
)
from app.services.equipment_service import EquipmentService
from app.models.equipment import EquipmentStatus

router = APIRouter(
    prefix="/equipment",
    tags=["Equipment"],
)


@router.post(
    "/",
    response_model=EquipmentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create new equipment record",
)
def create_equipment(
    payload: EquipmentCreate,
    db: Session = Depends(get_db),
):
    return EquipmentService.create(db, payload)


@router.get(
    "/",
    response_model=EquipmentListResponse,
    summary="List all equipment with optional filtering and pagination",
)
def list_equipment(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    equipment_type: Optional[str] = Query(default=None),
    status: Optional[EquipmentStatus] = Query(default=None),
    search: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
):
    return EquipmentService.list_all(
        db,
        page=page,
        page_size=page_size,
        equipment_type=equipment_type,
        status_filter=status.value if status else None,
        search=search,
    )


@router.get(
    "/{equipment_id}",
    response_model=EquipmentResponse,
    summary="Get equipment by ID",
)
def get_equipment(
    equipment_id: int = Path(..., ge=1),
    db: Session = Depends(get_db),
):
    return EquipmentService.get_by_id(db, equipment_id)


@router.put(
    "/{equipment_id}",
    response_model=EquipmentResponse,
    summary="Update equipment record",
)
def update_equipment(
    payload: EquipmentUpdate,
    equipment_id: int = Path(..., ge=1),
    db: Session = Depends(get_db),
):
    return EquipmentService.update(db, equipment_id, payload)


@router.delete(
    "/{equipment_id}",
    status_code=status.HTTP_200_OK,
    summary="Delete equipment record",
)
def delete_equipment(
    equipment_id: int = Path(..., ge=1),
    db: Session = Depends(get_db),
):
    return EquipmentService.delete(db, equipment_id)