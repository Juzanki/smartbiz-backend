# backend/routes/leaderboard_notification_routes.py
# -*- coding: utf-8 -*-
from __future__ import annotations
from typing import List, Optional, Dict
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Header, status
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy.orm import Session
from sqlalchemy import func

from backend.db import get_db
from backend.auth import get_current_user
from backend.models.user import User
from backend.models.leaderboard_notification import LeaderboardNotification
from backend.schemas.leaderboard_notification_schemas import LeaderboardNotificationOut

router = APIRouter(prefix="/leaderboard", tags=["Leaderboard Notifications"])

# ---------- Small DTOs ----------
class MessageOut(BaseModel):
    detail: str

class BatchMarkSeenRequest(BaseModel):
    ids: List[int] = Field(..., min_items=1, description="Notification IDs to mark as seen")

class BatchMarkSeenResult(BaseModel):
    updated: int
    skipped: int

class CountOut(BaseModel):
    total: int
    unseen: int

class PageMeta(BaseModel):
    total: int
    limit: int
    offset: int

class NotificationPage(BaseModel):
    items: List[LeaderboardNotificationOut]
    meta: PageMeta

# ---------- Helpers ----------
def _is_admin(user: User) -> bool:
    return getattr(user, "role", None) in {"admin", "owner"}

def _scope_query(db: Session, current_user: User, user_id: Optional[int]):
    """
    Admin/Owner: can scope to any user_id (or all if None).
    Others: can only access their own notifications.
    """
    q = db.query(LeaderboardNotification)
    if _is_admin(current_user):
        if user_id is not None:
            q = q.filter(LeaderboardNotification.user_id == user_id)
        return q
    # regular user must be scoped to themselves
    uid = current_user.id if user_id is None else user_id
    if uid != current_user.id:
        raise HTTPException(status_code=403, detail="Forbidden")
    return q.filter(LeaderboardNotification.user_id == current_user.id)

# ---------- Endpoints ----------

# Legacy-compatible (kept, but hardened + filters/pagination)
@router.get(
    "/stream/{stream_id}/user/{user_id}",
    response_model=List[LeaderboardNotificationOut],
    summary="List notifications for a user in a stream (legacy list)",
)
def get_user_notifications(
    stream_id: int,
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    unseen_only: bool = Query(False),
    since: Optional[datetime] = Query(None, description="Return notifications since this time"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
):
    q = _scope_query(db, current_user, user_id).filter(LeaderboardNotification.stream_id == stream_id)
    if unseen_only:
        q = q.filter(LeaderboardNotification.seen.is_(False))
    if since:
        q = q.filter(LeaderboardNotification.created_at >= since)
    q = q.order_by(LeaderboardNotification.created_at.asc() if order == "asc" else LeaderboardNotification.created_at.desc())
    return q.offset(offset).limit(limit).all()

# Recommended: “me” variant (no user_id in path)
@router.get(
    "/stream/{stream_id}/me",
    response_model=NotificationPage,
    summary="List my notifications for a stream (paged)",
)
def list_my_notifications(
    stream_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    limit: int = Query(30, ge=1, le=200),
    offset: int = Query(0, ge=0),
    unseen_only: bool = Query(False),
    since: Optional[datetime] = Query(None),
    order: str = Query("desc", pattern="^(asc|desc)$"),
):
    base = _scope_query(db, current_user, current_user.id).filter(LeaderboardNotification.stream_id == stream_id)
    if unseen_only:
        base = base.filter(LeaderboardNotification.seen.is_(False))
    if since:
        base = base.filter(LeaderboardNotification.created_at >= since)

    total = base.count()
    items = (
        base.order_by(LeaderboardNotification.created_at.asc() if order == "asc" else LeaderboardNotification.created_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
    )
    return NotificationPage(items=items, meta=PageMeta(total=total, limit=limit, offset=offset))

@router.get(
    "/stream/{stream_id}/me/count",
    response_model=CountOut,
    summary="Get counts (total and unseen) for my notifications in a stream",
)
def count_my_notifications(
    stream_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    base = _scope_query(db, current_user, current_user.id).filter(LeaderboardNotification.stream_id == stream_id)
    total = base.count()
    unseen = base.filter(LeaderboardNotification.seen.is_(False)).count()
    return CountOut(total=total, unseen=unseen)

@router.post(
    "/mark-seen/{notification_id}",
    response_model=MessageOut,
    summary="Mark a single notification as seen",
)
def mark_notification_seen(
    notification_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    prefer: Optional[str] = Header(None, alias="Prefer", description="Use 'return=minimal' to get 204 if already seen"),
):
    notif = db.query(LeaderboardNotification).filter(LeaderboardNotification.id == notification_id).first()
    if not notif:
        raise HTTPException(status_code=404, detail="Notification not found")
    if not (_is_admin(current_user) or notif.user_id == current_user.id):
        raise HTTPException(status_code=403, detail="Forbidden")

    if notif.seen:
        if prefer == "return=minimal":
            # No change needed
            from fastapi import Response
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        return MessageOut(detail="Already seen")

    notif.seen = True
    db.commit()
    return MessageOut(detail="Marked as seen")

@router.post(
    "/mark-seen",
    response_model=BatchMarkSeenResult,
    summary="Batch mark notifications as seen",
)
def batch_mark_seen(
    body: BatchMarkSeenRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not body.ids:
        return BatchMarkSeenResult(updated=0, skipped=0)

    q = db.query(LeaderboardNotification).filter(LeaderboardNotification.id.in_(body.ids))
    if not _is_admin(current_user):
        q = q.filter(LeaderboardNotification.user_id == current_user.id)

    rows = q.all()
    updated = 0
    for n in rows:
        if not n.seen:
            n.seen = True
            updated += 1
    db.commit()
    skipped = max(0, len(rows) - updated)
    return BatchMarkSeenResult(updated=updated, skipped=skipped)

@router.post(
    "/stream/{stream_id}/me/mark-all-seen",
    response_model=MessageOut,
    summary="Mark all my notifications in a stream as seen",
)
def mark_all_seen_for_me(
    stream_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = (
        db.query(LeaderboardNotification)
        .filter(
            LeaderboardNotification.stream_id == stream_id,
            LeaderboardNotification.user_id == current_user.id,
            LeaderboardNotification.seen.is_(False),
        )
    )
    count = q.count()
    if count == 0:
        return MessageOut(detail="No unseen notifications")

    # Bulk update (SQLAlchemy will generate one UPDATE)
    q.update({LeaderboardNotification.seen: True}, synchronize_session=False)
    db.commit()
    return MessageOut(detail=f"Marked {count} notifications as seen")
