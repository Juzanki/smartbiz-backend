# backend/routes/notification_routes.py
# -*- coding: utf-8 -*-
from __future__ import annotations
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Header, Query, Response, status
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func

from backend.db import get_db
from backend.auth import get_current_user
from backend.models.user import User
from backend.schemas.notification import NotificationCreate, NotificationOut
from backend.crud import notification_crud

# Optional: use model directly for fallbacks if CRUD lacks a method
try:
    from backend.models.notification import Notification  # type: ignore
except Exception:  # pragma: no cover
    Notification = None  # type: ignore

router = APIRouter(prefix="/notifications", tags=["Notifications"])

ALLOWED_CREATORS = {"admin", "owner", "system"}  # extend as needed


# ---------- DTOs ----------
class PageMeta(BaseModel):
    total: int
    limit: int
    offset: int

class NotificationPage(BaseModel):
    items: List[NotificationOut]
    meta: PageMeta


# ---------- Helpers ----------
def _can_create_for(current_user: User, target_user_id: int) -> bool:
    return current_user.id == target_user_id or getattr(current_user, "role", None) in ALLOWED_CREATORS


# ---------- Endpoints ----------
@router.post("/", response_model=NotificationOut, status_code=status.HTTP_201_CREATED)
def create_notification(
    notification: NotificationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    """
    Create a notification. Users can create for themselves. Admin/Owner/System
    can create for others. Supports optional idempotency.
    """
    if not _can_create_for(current_user, notification.user_id):
        raise HTTPException(status_code=403, detail="Unauthorized to send notifications to others")

    try:
        try:
            # If your CRUD supports idempotency, forward it
            created = notification_crud.create_notification(db, notification, idempotency_key=idempotency_key)  # type: ignore
        except TypeError:
            created = notification_crud.create_notification(db, notification)
        return created
    except IntegrityError:
        # Return existing on idempotent duplicate if model supports it
        if Notification and idempotency_key and hasattr(Notification, "idempotency_key"):
            existing = (
                db.query(Notification)
                .filter(Notification.user_id == notification.user_id, Notification.idempotency_key == idempotency_key)  # type: ignore[attr-defined]
                .order_by(getattr(Notification, "id", None))
                .first()
            )
            if existing:
                # Return OK for duplicate create
                from fastapi import Response
                Response.status_code = status.HTTP_200_OK  # type: ignore
                return existing
        raise HTTPException(status_code=409, detail="Duplicate notification")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to create notification: {exc}")


@router.get("/", response_model=List[NotificationOut])
def get_my_notifications(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=200),
    unread_only: bool = Query(False, description="Return only unread notifications"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
):
    """
    List your notifications (legacy list). Supports unread filter & ordering.
    """
    # Prefer CRUD if it supports filters
    try:
        return notification_crud.get_user_notifications(  # type: ignore
            db, user_id=current_user.id, skip=skip, limit=limit, unread_only=unread_only, order=order
        )
    except TypeError:
        # Fallback
        if not Notification:
            # If model missing, call original CRUD signature and slice
            items = notification_crud.get_user_notifications(db, user_id=current_user.id, skip=skip, limit=limit)  # type: ignore
            return items
        q = db.query(Notification).filter(Notification.user_id == current_user.id)
        if unread_only and hasattr(Notification, "is_read"):
            q = q.filter(Notification.is_read.is_(False))
        col = getattr(Notification, "created_at", getattr(Notification, "id"))
        q = q.order_by(col.asc() if order == "asc" else col.desc())
        return q.offset(skip).limit(limit).all()


@router.get("/page", response_model=NotificationPage)
def get_my_notifications_page(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    offset: int = Query(0, ge=0),
    limit: int = Query(30, ge=1, le=200),
    unread_only: bool = Query(False),
    order: str = Query("desc", pattern="^(asc|desc)$"),
):
    """
    Paged listing for mobile infinite scroll (items + meta).
    """
    if Notification:
        base = db.query(Notification).filter(Notification.user_id == current_user.id)
        if unread_only and hasattr(Notification, "is_read"):
            base = base.filter(Notification.is_read.is_(False))
        total = base.count()
        col = getattr(Notification, "created_at", getattr(Notification, "id"))
        base = base.order_by(col.asc() if order == "asc" else col.desc())
        items = base.offset(offset).limit(limit).all()
        return NotificationPage(items=items, meta=PageMeta(total=total, limit=limit, offset=offset))

    # CRUD fallback (no total)
    items = notification_crud.get_user_notifications(db, user_id=current_user.id, skip=offset, limit=limit)  # type: ignore
    return NotificationPage(items=items, meta=PageMeta(total=offset + len(items), limit=limit, offset=offset))


@router.get("/unread/count")
def unread_count(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Return the number of unread notifications for the current user.
    """
    if Notification and hasattr(Notification, "is_read"):
        count = (
            db.query(func.count(getattr(Notification, "id")))
            .filter(Notification.user_id == current_user.id, Notification.is_read.is_(False))
            .scalar()
            or 0
        )
        return {"unread": int(count)}
    # CRUD fallback (assumes it can handle unread_only)
    try:
        items = notification_crud.get_user_notifications(db, user_id=current_user.id, skip=0, limit=10_000, unread_only=True)  # type: ignore
        return {"unread": len(items)}
    except TypeError:
        # No unread filter available
        return {"unread": 0}


@router.put("/{notif_id}/read", response_model=NotificationOut)
def mark_as_read(
    notif_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    prefer: Optional[str] = Header(None, alias="Prefer", description="Use 'return=minimal' to get 204 if already read"),
):
    """
    Mark a single notification as read (ownership enforced).
    """
    # Enforce ownership before mutating
    if Notification:
        obj = db.query(Notification).filter(getattr(Notification, "id") == notif_id).first()
        if not obj:
            raise HTTPException(status_code=404, detail="Notification not found")
        if getattr(obj, "user_id", None) != current_user.id:
            raise HTTPException(status_code=403, detail="Not your notification")
    # Perform update using CRUD
    notif = notification_crud.mark_notification_as_read(db, notif_id)
    if not notif:
        raise HTTPException(status_code=404, detail="Notification not found")
    if getattr(notif, "user_id", None) != current_user.id:
        raise HTTPException(status_code=403, detail="Not your notification")
    if getattr(notif, "is_read", False) and prefer == "return=minimal":
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return notif


@router.post("/read-all")
def mark_all_as_read(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Mark all of the current user's notifications as read.
    """
    if Notification and hasattr(Notification, "is_read"):
        q = db.query(Notification).filter(Notification.user_id == current_user.id, Notification.is_read.is_(False))
        updated = q.update({Notification.is_read: True}, synchronize_session=False)  # type: ignore
        db.commit()
        return {"updated": int(updated)}
    # CRUD fallback (loop)
    items = notification_crud.get_user_notifications(db, user_id=current_user.id, skip=0, limit=10_000)  # type: ignore
    updated = 0
    for it in items:
        if not getattr(it, "is_read", True):
            notification_crud.mark_notification_as_read(db, getattr(it, "id"))  # type: ignore
            updated += 1
    return {"updated": updated}


@router.delete("/{notif_id}")
def delete_notification(
    notif_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Delete a notification you own. (Soft delete if your model supports it.)
    """
    if not Notification:
        raise HTTPException(status_code=501, detail="Delete not supported without Notification model")
    obj = db.query(Notification).filter(getattr(Notification, "id") == notif_id).first()
    if not obj:
        raise HTTPException(status_code=404, detail="Notification not found")
    if getattr(obj, "user_id", None) != current_user.id and getattr(current_user, "role", None) not in ALLOWED_CREATORS:
        raise HTTPException(status_code=403, detail="Not your notification")
    # Soft delete if supported
    if hasattr(Notification, "deleted_at"):
        from datetime import datetime, timezone
        obj.deleted_at = datetime.now(timezone.utc)
        db.commit()
        return {"detail": "Deleted"}
    # Hard delete
    db.delete(obj)
    db.commit()
    return {"detail": "Deleted"}
