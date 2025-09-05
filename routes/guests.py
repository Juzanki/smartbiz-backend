# -*- coding: utf-8 -*-
from __future__ import annotations
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Header, Query, status
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from pydantic import BaseModel, Field

from backend.db import get_db
from backend.auth import get_current_user
from backend.models.user import User

from backend.schemas.guest import GuestCreate, GuestOut
from backend.crud import guest_crud

# Optional: WebSocket broadcast (ignore if you don't use it)
try:
    from backend.utils.websocket_manager import WebSocketManager
    ws_manager: Optional[WebSocketManager] = WebSocketManager()
except Exception:
    ws_manager = None  # gracefully disable websocket if not available

router = APIRouter(prefix="/guests", tags=["Guests"])


# ---------- Helpers ----------
ALLOWED_APPROVERS = {"admin", "owner", "moderator"}

def _is_approver(user: User) -> bool:
    return getattr(user, "role", None) in ALLOWED_APPROVERS

def _force_requester(payload: GuestCreate, user_id: int) -> GuestCreate:
    """
    If your GuestCreate schema/model includes a requester/user id field,
    force it from the authenticated session (avoid trusting client input).
    """
    updates = {}
    for key in ("requester_id", "user_id", "guest_user_id"):
        if hasattr(payload, key):
            updates[key] = user_id
    if not updates:
        return payload
    try:
        # Pydantic v2
        return payload.model_copy(update=updates)  # type: ignore[attr-defined]
    except AttributeError:
        # Pydantic v1
        return payload.copy(update=updates)  # type: ignore


# ---------- Pagination DTO ----------
class PageMeta(BaseModel):
    total: int
    limit: int
    offset: int

class GuestPage(BaseModel):
    items: List[GuestOut]
    meta: PageMeta


# ---------- Endpoints ----------
@router.post("/", response_model=GuestOut, status_code=status.HTTP_201_CREATED)
def create_guest(
    data: GuestCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    """
    Create a new guest request for a room.
    - Forces requester from the authenticated user if the schema supports it.
    - Optional Idempotency-Key to avoid duplicate submissions (requires DB support).
    """
    payload = _force_requester(data, current_user.id)

    try:
        try:
            # If your CRUD supports idempotency_key
            guest = guest_crud.create_guest(db, payload, idempotency_key=idempotency_key)  # type: ignore
        except TypeError:
            guest = guest_crud.create_guest(db, payload)
    except IntegrityError as ie:
        db.rollback()
        # e.g., unique(idempotency_key) or unique(room_id, requester_id, status='pending')
        raise HTTPException(status_code=409, detail="Duplicate guest request") from ie
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to create guest") from exc

    # Optional: notify listeners
    if ws_manager:
        try:
            ws_manager.broadcast(str(getattr(guest, "room_id", "")), {  # type: ignore
                "type": "guest_request_created",
                "guest_id": getattr(guest, "id", None),
            })
        except Exception:
            pass

    return guest


@router.post("/{guest_id}/approve", response_model=GuestOut)
def approve_guest(
    guest_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    """
    Approve a pending guest. Admin/Owner/Moderator only.
    """
    if not _is_approver(current_user):
        raise HTTPException(status_code=403, detail="Not allowed to approve guests")

    try:
        try:
            guest = guest_crud.approve_guest(db, guest_id, idempotency_key=idempotency_key)  # type: ignore
        except TypeError:
            guest = guest_crud.approve_guest(db, guest_id)
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as ie:
        db.rollback()
        raise HTTPException(status_code=409, detail="Duplicate approval request") from ie
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to approve guest") from exc

    if not guest:
        raise HTTPException(status_code=404, detail="Guest not found")

    if ws_manager:
        try:
            ws_manager.broadcast(str(getattr(guest, "room_id", "")), {  # type: ignore
                "type": "guest_approved",
                "guest_id": getattr(guest, "id", None),
            })
        except Exception:
            pass

    return guest


@router.post("/{guest_id}/reject", response_model=GuestOut)
def reject_guest(
    guest_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    reason: Optional[str] = Query(None, description="Optional rejection reason"),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    """
    Reject a pending guest. Admin/Owner/Moderator only.
    """
    if not _is_approver(current_user):
        raise HTTPException(status_code=403, detail="Not allowed to reject guests")

    try:
        try:
            guest = guest_crud.reject_guest(db, guest_id, reason=reason, idempotency_key=idempotency_key)  # type: ignore
        except TypeError:
            guest = guest_crud.reject_guest(db, guest_id, reason=reason)
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as ie:
        db.rollback()
        raise HTTPException(status_code=409, detail="Duplicate rejection request") from ie
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to reject guest") from exc

    if not guest:
        raise HTTPException(status_code=404, detail="Guest not found")

    if ws_manager:
        try:
            ws_manager.broadcast(str(getattr(guest, "room_id", "")), {  # type: ignore
                "type": "guest_rejected",
                "guest_id": getattr(guest, "id", None),
                "reason": reason,
            })
        except Exception:
            pass

    return guest


@router.delete("/{guest_id}", status_code=status.HTTP_200_OK)
def remove_guest(
    guest_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Remove a guest (kick or cancel). Admin/Owner/Moderator only.
    """
    if not _is_approver(current_user):
        raise HTTPException(status_code=403, detail="Not allowed to remove guests")

    try:
        removed = guest_crud.remove_guest(db, guest_id)
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to remove guest") from exc

    if not removed:
        raise HTTPException(status_code=404, detail="Guest not found")

    if ws_manager:
        try:
            ws_manager.broadcast(str(getattr(removed, "room_id", "")), {  # type: ignore
                "type": "guest_removed",
                "guest_id": getattr(removed, "id", None),
            })
        except Exception:
            pass

    return {"detail": "Guest removed"}


@router.get("/room/{room_id}", response_model=List[GuestOut])
def get_guests_by_room(
    room_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    status_filter: Optional[str] = Query(None, description="Filter by status: pending|approved|rejected"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    order: str = Query("created_at"),
    sort: str = Query("desc", pattern="^(asc|desc)$"),
):
    """
    List guests of a room (legacy list). Supports basic filters & pagination.
    """
    # Prefer your existing CRUD; if it already supports filters, great.
    try:
        # Try a richer CRUD variant first (if implemented)
        return guest_crud.get_guests_by_room(
            db, room_id,
            status_filter=status_filter, limit=limit, offset=offset, order=order, sort=sort  # type: ignore
        )
    except TypeError:
        # Fall back to the original CRUD signature
        items = guest_crud.get_guests_by_room(db, room_id)
        # Simple, in-Python slicing to stay backward compatible
        return items[offset:offset + limit]


@router.get("/room/{room_id}/page", response_model=GuestPage)
def get_guests_by_room_page(
    room_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    status_filter: Optional[str] = Query(None),
    limit: int = Query(30, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """
    Paged listing (items + meta) for mobile infinite scroll.
    Uses existing CRUD if it supports pagination; otherwise falls back.
    """
    try:
        # If you implement a paged CRUD, use it directly
        total, items = guest_crud.get_guests_by_room_paged(  # type: ignore
            db, room_id, status_filter=status_filter, limit=limit, offset=offset
        )
        return GuestPage(items=items, meta=PageMeta(total=total, limit=limit, offset=offset))
    except AttributeError:
        # Fallback: use non-paged + slice
        items_all = guest_crud.get_guests_by_room(db, room_id)
        if status_filter:
            items_all = [g for g in items_all if getattr(g, "status", None) == status_filter]
        total = len(items_all)
        return GuestPage(items=items_all[offset:offset + limit], meta=PageMeta(total=total, limit=limit, offset=offset))
