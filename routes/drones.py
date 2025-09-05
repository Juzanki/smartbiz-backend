# backend/routes/drones.py
# -*- coding: utf-8 -*-
from __future__ import annotations
"""
Drones API (mobile-first, international-ready)

Key features
- POST /drones/dispatch                    : dispatch an available drone (idempotent, ETA hint)
- GET  /drones/missions                    : list missions (filters + cursor pagination)
- GET  /drones/missions/{mission_id}       : get mission details
- PATCH/drones/missions/{mission_id}/status: update mission status (safe transitions)
- POST /drones/missions/{mission_id}/telemetry : update live telemetry (optional columns)
- POST /drones/missions/{mission_id}/cancel: cancel a mission (safe)
- GET  /drones/missions/active             : convenience alias for status=in-transit

Notes
- Concurrency: row locks when selecting product or an available drone (if CRUD helper exists)
- Idempotency: optional header; used when your model/CRUD supports it
- Mobile-first: compact payloads & responses; cursor pagination
- UTC ISO timestamps
- Backward compatible: if no Drone CRUD is present, falls back to a static drone id
"""
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Header, Query, status
from pydantic import BaseModel, Field, conint, confloat
from sqlalchemy.orm import Session

from backend.db import get_db
from backend.models.drone_mission import DroneMission
from backend.models.product import Product
from backend.models.user import User
from backend.auth import get_current_user  # optional security

# Optional fleet/telemetry helpers if you have them
try:
    from backend.crud import drone_crud  # type: ignore
except Exception:  # pragma: no cover
    drone_crud = None  # type: ignore

router = APIRouter(prefix="/drones", tags=["Drones"])

# ======== mobile-first defaults ========
DEFAULT_ETA_MIN = 15
DEFAULT_PAGE_SIZE = 30
MAX_PAGE_SIZE = 100

# ======== helpers ========
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _to_iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.astimezone(timezone.utc).isoformat() if isinstance(dt, datetime) else None

def _normalize_destination(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()

# ======== schemas ========
class DispatchRequest(BaseModel):
    product_id: conint(strict=True, ge=1)
    destination: str = Field(..., min_length=3, description="Address or label. Coordinates can be sent via lat/lng.")
    eta_minutes: Optional[conint(ge=1, le=24 * 60)] = Field(None, description="Estimated minutes until arrival")
    lat: Optional[confloat(ge=-90, le=90)] = None
    lng: Optional[confloat(ge=-180, le=180)] = None
    priority: Literal["normal", "high", "urgent"] = "normal"
    auto_mode: bool = True
    metadata: Optional[Dict[str, Any]] = Field(None, description="Optional custom data (order id, note, etc.)")

class DispatchResponse(BaseModel):
    message: str
    mission_id: int
    drone_id: str
    status: str
    eta: Optional[str]

class MissionOut(BaseModel):
    id: int
    drone_id: str
    product_id: int
    product_name: Optional[str] = None
    destination: Optional[str] = None
    status: str
    eta: Optional[str]
    auto_mode: bool = False
    initiated_by: Optional[str] = None
    priority: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    battery_pct: Optional[int] = None

class PageMeta(BaseModel):
    next_cursor: Optional[int] = None
    count: int

class PageOut(BaseModel):
    meta: PageMeta
    items: List[MissionOut]

class StatusPatch(BaseModel):
    status: Literal["pending", "in-transit", "completed", "delivered", "failed", "cancelled"]

class TelemetryUpdate(BaseModel):
    lat: Optional[confloat(ge=-90, le=90)] = None
    lng: Optional[confloat(ge=-180, le=180)] = None
    altitude_m: Optional[float] = Field(None, ge=0)
    speed_kmh: Optional[float] = Field(None, ge=0)
    battery_pct: Optional[conint(ge=0, le=100)] = None
    note: Optional[str] = Field(None, max_length=140)

# ======== dispatch ========
@router.post(
    "/dispatch",
    response_model=DispatchResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Auto-dispatch a drone to deliver a product",
)
def dispatch_drone(
    payload: DispatchRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(
        None, convert_underscores=False, description="Prevent duplicate dispatches if supported"
    ),
) -> DispatchResponse:
    """
    Dispatch logic:
    1) Validate product (row-lock it to keep stock consistent if your Product has stock).
    2) Select an available drone (via drone_crud if present; else fallback id).
    3) Create a mission with ETA (payload.eta_minutes or DEFAULT_ETA_MIN).
    4) Optionally store idempotency key if your model supports it.
    """
    # Lock product row to ensure consistent reads
    product = (
        db.query(Product)
        .filter(Product.id == payload.product_id)
        .with_for_update()
        .one_or_none()
    )
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    # If your Product model has stock, enforce >0
    if hasattr(product, "stock") and product.stock is not None and product.stock <= 0:
        raise HTTPException(status_code=409, detail="Product out of stock")

    destination = _normalize_destination(payload.destination)
    if not destination:
        raise HTTPException(status_code=400, detail="Invalid destination")

    # Pick & lock an available drone using your CRUD if possible
    drone_id = "DRONE-AI-AUTO"
    if drone_crud and hasattr(drone_crud, "acquire_available_drone"):
        picked = drone_crud.acquire_available_drone(db, priority=payload.priority)
        if not picked:
            raise HTTPException(status_code=503, detail="No drone available")
        drone_id = picked if isinstance(picked, str) else getattr(picked, "drone_id", "DRONE-AI-AUTO")

    # Compute ETA
    minutes = payload.eta_minutes or DEFAULT_ETA_MIN
    eta_at = _utcnow() + timedelta(minutes=int(minutes))

    # Create mission
    mission_kwargs = dict(
        drone_id=drone_id,
        product_id=product.id,
        destination=destination,
        status="in-transit",
        eta=eta_at,
        initiated_by=str(getattr(current_user, "id", "system")),
        auto_mode=payload.auto_mode,
        priority=payload.priority,
    )
    # Optional geo & idempotency columns if present on your model
    if hasattr(DroneMission, "lat"):
        mission_kwargs["lat"] = payload.lat
    if hasattr(DroneMission, "lng"):
        mission_kwargs["lng"] = payload.lng
    if idempotency_key and hasattr(DroneMission, "idempotency_key"):
        mission_kwargs["idempotency_key"] = idempotency_key
    if hasattr(DroneMission, "metadata") and payload.metadata is not None:
        mission_kwargs["metadata"] = payload.metadata

    mission = DroneMission(**mission_kwargs)
    db.add(mission)

    # If product has stock, reserve one unit (optional)
    if hasattr(product, "stock") and product.stock is not None:
        product.stock -= 1

    db.commit()
    db.refresh(mission)

    # Optional: publish to realtime channel (if your CRUD exposes it)
    if drone_crud and hasattr(drone_crud, "publish_event"):
        try:
            drone_crud.publish_event(db, "drone.mission.created", mission)
        except Exception:
            # Non-fatal; ignore broker errors
            pass

    return DispatchResponse(
        message="Drone dispatched successfully",
        mission_id=mission.id,
        drone_id=mission.drone_id,
        status=mission.status,
        eta=_to_iso(mission.eta),
    )

# ======== listing & details ========
@router.get(
    "/missions",
    response_model=PageOut,
    summary="List missions with cursor pagination",
)
def get_missions(
    db: Session = Depends(get_db),
    status_eq: Optional[str] = Query(None, description="Filter by exact status, e.g., in-transit"),
    product_id: Optional[int] = Query(None, ge=1),
    cursor_id: Optional[int] = Query(None, description="Paginate backward from id < cursor_id"),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
) -> PageOut:
    q = db.query(DroneMission)
    if status_eq:
        q = q.filter(DroneMission.status == status_eq)
    if product_id:
        q = q.filter(DroneMission.product_id == product_id)
    if cursor_id:
        q = q.filter(DroneMission.id < cursor_id)
    items = q.order_by(DroneMission.id.desc()).limit(limit).all()

    # Batch fetch product names to avoid N+1
    prod_ids = {m.product_id for m in items if m.product_id}
    products = {}
    if prod_ids:
        for p in db.query(Product).filter(Product.id.in_(prod_ids)).all():
            products[p.id] = getattr(p, "name", None)

    def to_out(m: DroneMission) -> MissionOut:
        return MissionOut(
            id=m.id,
            drone_id=m.drone_id,
            product_id=m.product_id,
            product_name=products.get(m.product_id),
            destination=getattr(m, "destination", None),
            status=m.status,
            eta=_to_iso(getattr(m, "eta", None)),
            auto_mode=bool(getattr(m, "auto_mode", False)),
            initiated_by=getattr(m, "initiated_by", None),
            priority=getattr(m, "priority", None),
            created_at=_to_iso(getattr(m, "created_at", None)),
            updated_at=_to_iso(getattr(m, "updated_at", None)),
            lat=getattr(m, "lat", None) if hasattr(m, "lat") else None,
            lng=getattr(m, "lng", None) if hasattr(m, "lng") else None,
            battery_pct=getattr(m, "battery_pct", None) if hasattr(m, "battery_pct") else None,
        )

    next_cursor = items[-1].id if items else None
    return PageOut(meta=PageMeta(next_cursor=next_cursor, count=len(items)), items=[to_out(m) for m in items])

@router.get(
    "/missions/active",
    response_model=PageOut,
    summary="List active missions (in-transit)",
)
def get_active_missions(
    db: Session = Depends(get_db),
    cursor_id: Optional[int] = Query(None),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
) -> PageOut:
    return get_missions(db=db, status_eq="in-transit", cursor_id=cursor_id, limit=limit)

@router.get(
    "/missions/{mission_id}",
    response_model=MissionOut,
    summary="Get mission details",
)
def get_mission(
    mission_id: int,
    db: Session = Depends(get_db),
) -> MissionOut:
    m = db.query(DroneMission).filter(DroneMission.id == mission_id).one_or_none()
    if not m:
        raise HTTPException(status_code=404, detail="Mission not found")
    # Fetch product name (single)
    p = db.query(Product).filter(Product.id == m.product_id).one_or_none()
    return MissionOut(
        id=m.id,
        drone_id=m.drone_id,
        product_id=m.product_id,
        product_name=getattr(p, "name", None) if p else None,
        destination=getattr(m, "destination", None),
        status=m.status,
        eta=_to_iso(getattr(m, "eta", None)),
        auto_mode=bool(getattr(m, "auto_mode", False)),
        initiated_by=getattr(m, "initiated_by", None),
        priority=getattr(m, "priority", None),
        created_at=_to_iso(getattr(m, "created_at", None)),
        updated_at=_to_iso(getattr(m, "updated_at", None)),
        lat=getattr(m, "lat", None) if hasattr(m, "lat") else None,
        lng=getattr(m, "lng", None) if hasattr(m, "lng") else None,
        battery_pct=getattr(m, "battery_pct", None) if hasattr(m, "battery_pct") else None,
    )

# ======== status transitions ========
_ALLOWED_TRANSITIONS: Dict[str, set[str]] = {
    "pending": {"in-transit", "cancelled", "failed"},
    "in-transit": {"completed", "delivered", "failed", "cancelled"},
    "completed": set(),
    "delivered": set(),
    "failed": set(),
    "cancelled": set(),
}

@router.patch(
    "/missions/{mission_id}/status",
    response_model=MissionOut,
    summary="Update mission status (safe transitions)",
)
def update_status(
    mission_id: int,
    body: StatusPatch,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MissionOut:
    m = db.query(DroneMission).filter(DroneMission.id == mission_id).with_for_update().one_or_none()
    if not m:
        raise HTTPException(status_code=404, detail="Mission not found")

    current = getattr(m, "status", "pending")
    allowed = _ALLOWED_TRANSITIONS.get(current, set())
    if body.status not in allowed:
        raise HTTPException(status_code=409, detail=f"Illegal transition {current} -> {body.status}")

    m.status = body.status
    m.updated_at = _utcnow() if hasattr(m, "updated_at") else getattr(m, "updated_at", None)
    db.commit()
    db.refresh(m)

    if drone_crud and hasattr(drone_crud, "publish_event"):
        try:
            drone_crud.publish_event(db, "drone.mission.status", {"id": m.id, "status": m.status})
        except Exception:
            pass

    return get_mission(mission_id, db)

@router.post(
    "/missions/{mission_id}/cancel",
    response_model=MissionOut,
    summary="Cancel a mission safely",
)
def cancel_mission(
    mission_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MissionOut:
    return update_status(mission_id, StatusPatch(status="cancelled"), db, current_user)

# ======== telemetry ========
@router.post(
    "/missions/{mission_id}/telemetry",
    response_model=MissionOut,
    summary="Update mission telemetry (location, battery, etc.)",
)
def update_telemetry(
    mission_id: int,
    body: TelemetryUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MissionOut:
    """
    Updates location, battery, speed, altitude if those columns exist on DroneMission.
    If you store telemetry elsewhere, implement drone_crud.update_telemetry and it will be used.
    """
    if drone_crud and hasattr(drone_crud, "update_telemetry"):
        try:
            drone_crud.update_telemetry(db, mission_id, body.dict(exclude_unset=True))
            # Return fresh snapshot
            return get_mission(mission_id, db)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Telemetry update failed: {e}")

    m = db.query(DroneMission).filter(DroneMission.id == mission_id).with_for_update().one_or_none()
    if not m:
        raise HTTPException(status_code=404, detail="Mission not found")

    data = body.dict(exclude_unset=True)
    for field in ("lat", "lng", "battery_pct", "speed_kmh", "altitude_m"):
        if field in data and hasattr(m, field):
            setattr(m, field, data[field])

    if hasattr(m, "updated_at"):
        m.updated_at = _utcnow()

    db.commit()
    db.refresh(m)

    if drone_crud and hasattr(drone_crud, "publish_event"):
        try:
            drone_crud.publish_event(db, "drone.mission.telemetry", {"id": m.id, **data})
        except Exception:
            pass

    return get_mission(mission_id, db)

