# backend/routes/order_notification.py
# -*- coding: utf-8 -*-
from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional, Dict

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy.orm import Session

from backend.db import get_db

# Auth (adjust to your project)
try:
    from backend.auth import get_current_user
    from backend.models.user import User
except Exception:  # pragma: no cover
    get_current_user = None  # type: ignore
    User = object  # type: ignore

# Data access: prefer Order model; fall back to CRUD if you have one
try:
    from backend.models.order import Order  # expected fields: id, status, updated_at, user_id, eta_at?
except Exception:  # pragma: no cover
    Order = None  # type: ignore

try:
    from backend.crud import order_crud  # expected: get_order(db, order_id), update_status(db, order_id, status)
except Exception:  # pragma: no cover
    order_crud = None  # type: ignore


router = APIRouter(prefix="/orders", tags=["Orders"])

# ---- Status flow & helpers ---------------------------------------------------

ALLOWED_STATUSES = [
    "pending",     # created, awaiting confirmation
    "processing",  # paid/confirmed, being prepared
    "packed",      # packaged and ready
    "shipped",     # handed to courier / en route
    "delivered",   # received by customer
    "canceled",    # canceled by user/store
    "failed",      # payment/fulfillment failed
    "returned",    # returned by customer
]

# Define the order (for progress & regression checks)
STATUS_PROGRESS_ORDER = ["pending", "processing", "packed", "shipped", "delivered"]

PROGRESS_MAP: Dict[str, int] = {
    "pending": 10,
    "processing": 35,
    "packed": 55,
    "shipped": 80,
    "delivered": 100,
    "canceled": 0,
    "failed": 0,
    "returned": 0,
}

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _progress(status: str) -> int:
    return PROGRESS_MAP.get(status, 0)

def _can_advance(from_status: str, to_status: str, is_admin: bool) -> bool:
    """
    Prevent regressions for normal users; allow any change for admins/owners.
    """
    if is_admin:
        return True
    if from_status == to_status:
        return True
    # Only forward movement on the happy path for non-admins
    try:
        return STATUS_PROGRESS_ORDER.index(to_status) >= STATUS_PROGRESS_ORDER.index(from_status)
    except ValueError:
        # If either status isn't in progress list (e.g., canceled), only allow setting it once
        return to_status in {"canceled", "failed", "returned"}

def _etag_for(order_id: int | str, status: str, updated_at: Optional[datetime]) -> str:
    import hashlib
    basis = f"id:{order_id}|st:{status}|upd:{updated_at or ''}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()

# ---- Schemas ----------------------------------------------------------------

class OrderStatusOut(BaseModel):
    order_id: int | str
    status: str = Field(examples=["processing"])
    progress_pct: int = Field(0, ge=0, le=100)
    updated_at: Optional[datetime] = None
    eta_at: Optional[datetime] = Field(None, description="Estimated delivery time if available")
    message: str = Field(..., example="Your order is being processed")
    model_config = ConfigDict(from_attributes=True)

class OrderStatusUpdate(BaseModel):
    status: str = Field(..., description=f"One of: {', '.join(ALLOWED_STATUSES)}")

# ---- Data access helpers -----------------------------------------------------

def _get_order(db: Session, order_id: int | str):
    if Order:
        obj = db.query(Order).filter(getattr(Order, "id") == order_id).first()
        return obj
    if order_crud and hasattr(order_crud, "get_order"):
        return order_crud.get_order(db, order_id)  # type: ignore
    return None

def _update_order_status(db: Session, order_id: int | str, new_status: str):
    if Order:
        obj = db.query(Order).filter(getattr(Order, "id") == order_id).first()
        if not obj:
            return None
        setattr(obj, "status", new_status)
        if hasattr(obj, "updated_at"):
            setattr(obj, "updated_at", _utcnow())
        db.commit()
        db.refresh(obj)
        return obj
    if order_crud and hasattr(order_crud, "update_status"):
        return order_crud.update_status(db, order_id, new_status)  # type: ignore
    return None

# ---- Endpoints ---------------------------------------------------------------

# Backward-compatible minimal endpoint (kept as-is)
@router.get("/order-status", summary="Legacy: generic order status")
def order_status():
    return {"status": "order is being processed"}

@router.get("/{order_id}/status", response_model=OrderStatusOut, summary="Get order status")
def get_order_status(
    order_id: int | str,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user) if get_current_user else None,  # optional if auth not wired
):
    order = _get_order(db, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    # Ownership check if model exposes user_id (non-admin users only see their own)
    if current_user and hasattr(order, "user_id"):
        role = getattr(current_user, "role", None)
        if role not in {"admin", "owner"} and getattr(order, "user_id", None) != getattr(current_user, "id", None):
            raise HTTPException(status_code=403, detail="Forbidden")

    status_val = str(getattr(order, "status", "processing") or "processing")
    updated_at = getattr(order, "updated_at", None)
    eta_at = getattr(order, "eta_at", None)

    # ETag for efficient polling
    etag = _etag_for(order_id, status_val, updated_at)
    if request.headers.get("If-None-Match") == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED)
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-store"

    return OrderStatusOut(
        order_id=order_id,
        status=status_val,
        progress_pct=_progress(status_val),
        updated_at=updated_at,
        eta_at=eta_at,
        message=_status_message(status_val),
)

def _status_message(status_val: str) -> str:
    return {
        "pending": "Order received — awaiting confirmation.",
        "processing": "Your order is being processed.",
        "packed": "Your order has been packed.",
        "shipped": "Your order is on the way.",
        "delivered": "Delivered — thank you!",
        "canceled": "Order was canceled.",
        "failed": "Order processing failed.",
        "returned": "Order was returned.",
    }.get(status_val, "Your order status was updated.")

@router.put("/{order_id}/status", response_model=OrderStatusOut, summary="Update order status")
def update_order_status(
    order_id: int | str,
    payload: OrderStatusUpdate,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user) if get_current_user else None,
):
    """
    Update the status for an order.
    - Admin/Owner can set any status.
    - Non-admins can only move forward along the standard flow (no regressions).
    """
    new_status = payload.status.strip().lower()
    if new_status not in ALLOWED_STATUSES:
        raise HTTPException(status_code=422, detail=f"Invalid status. Allowed: {', '.join(ALLOWED_STATUSES)}")

    order = _get_order(db, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    is_admin = bool(current_user and getattr(current_user, "role", None) in {"admin", "owner"})
    current_status = str(getattr(order, "status", "pending") or "pending")

    # Ownership: if not admin, ensure user owns the order
    if current_user and not is_admin and hasattr(order, "user_id"):
        if getattr(order, "user_id", None) != getattr(current_user, "id", None):
            raise HTTPException(status_code=403, detail="Forbidden")

    if not _can_advance(current_status, new_status, is_admin):
        raise HTTPException(status_code=409, detail=f"Cannot change status from {current_status} to {new_status}")

    updated = _update_order_status(db, order_id, new_status)
    if not updated:
        raise HTTPException(status_code=500, detail="Failed to update order status")

    status_val = str(getattr(updated, "status", new_status))
    return OrderStatusOut(
        order_id=order_id,
        status=status_val,
        progress_pct=_progress(status_val),
        updated_at=getattr(updated, "updated_at", None),
        eta_at=getattr(updated, "eta_at", None),
        message=_status_message(status_val),
    )
