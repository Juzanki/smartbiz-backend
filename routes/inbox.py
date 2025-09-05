# backend/routes/inbox_routes.py
# -*- coding: utf-8 -*-
from __future__ import annotations
from typing import List, Optional, Dict
from datetime import datetime

from fastapi import APIRouter, Depends, Query, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session
from sqlalchemy import func

from backend.db import get_db
from backend.auth import get_current_user
from backend.models.message import MessageLog
from backend.models.user import User
router = APIRouter(prefix="/inbox", tags=["Inbox"])

# ---------- Schemas (Pydantic v2) ----------
class MessageOut(BaseModel):
    id: int
    sender_name: str
    message: str
    platform: str
    chat_id: str
    received_at: datetime
    model_config = ConfigDict(from_attributes=True)

class PageMeta(BaseModel):
    total: int
    limit: int
    offset: int

class MessagePage(BaseModel):
    items: List[MessageOut]
    meta: PageMeta

class StatsOut(BaseModel):
    since: Optional[datetime] = None
    until: Optional[datetime] = None
    total: int
    by_platform: Dict[str, int] = Field(default_factory=dict)

# ---------- Helpers ----------
def _scoped_query(db: Session, current_user: User):
    """Admins/Owners see all; others see only their own chat_id == user.id."""
    if getattr(current_user, "role", None) in {"admin", "owner"}:
        return db.query(MessageLog)
    return db.query(MessageLog).filter(MessageLog.chat_id == str(current_user.id))

def _apply_filters(
    query, *,
    platform: Optional[str],
    q: Optional[str],
    since: Optional[datetime],
    until: Optional[datetime],
):
    if platform:
        query = query.filter(MessageLog.platform == platform)
    if q:
        like = f"%{q}%"
        # Prefer ILIKE; fallback to LIKE if unsupported
        try:
            query = query.filter(MessageLog.message.ilike(like))
        except AttributeError:
            query = query.filter(MessageLog.message.like(like))
    if since:
        query = query.filter(MessageLog.received_at >= since)
    if until:
        query = query.filter(MessageLog.received_at <= until)
    return query

# ---------- Routes ----------
@router.get("/", response_model=List[MessageOut])
def get_all_messages(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    skip: int = Query(0, ge=0, description="Records to skip (pagination)"),
    limit: int = Query(50, ge=1, le=200, description="Max records to return"),
    platform: Optional[str] = Query(None, description="Filter by platform"),
    q: Optional[str] = Query(None, description="Full-text search on message"),
    since: Optional[datetime] = Query(None, description="Only messages since this time"),
    until: Optional[datetime] = Query(None, description="Only messages up to this time"),
    order: str = Query("received_at", description="Order by field: received_at|id"),
    sort: str = Query("desc", pattern="^(asc|desc)$"),
):
    """
    Admin/Owner: see all messages.
    Others: see messages where `chat_id == current_user.id`.
    Supports filters, ordering, and simple pagination (list only).
    """
    query = _scoped_query(db, current_user)
    query = _apply_filters(query, platform=platform, q=q, since=since, until=until)

    # ordering
    colmap = {
        "received_at": getattr(MessageLog, "received_at", None),
        "id": getattr(MessageLog, "id", None),
    }
    col = colmap.get(order, MessageLog.received_at)
    if col is not None:
        query = query.order_by(col.asc() if sort == "asc" else col.desc())

    return query.offset(skip).limit(limit).all()

@router.get("/page", response_model=MessagePage)
def get_messages_page(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    offset: int = Query(0, ge=0),
    limit: int = Query(30, ge=1, le=200),
    platform: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    since: Optional[datetime] = Query(None),
    until: Optional[datetime] = Query(None),
    order: str = Query("received_at"),
    sort: str = Query("desc", pattern="^(asc|desc)$"),
):
    """
    Paged listing (items + meta) â€” ideal for mobile infinite scroll.
    """
    base = _scoped_query(db, current_user)
    filtered = _apply_filters(base, platform=platform, q=q, since=since, until=until)

    total = filtered.count()

    colmap = {
        "received_at": getattr(MessageLog, "received_at", None),
        "id": getattr(MessageLog, "id", None),
    }
    col = colmap.get(order, MessageLog.received_at)
    if col is not None:
        filtered = filtered.order_by(col.asc() if sort == "asc" else col.desc())

    items = filtered.offset(offset).limit(limit).all()
    return MessagePage(items=items, meta=PageMeta(total=total, limit=limit, offset=offset))

@router.get("/stats", response_model=StatsOut)
def get_inbox_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    platform: Optional[str] = Query(None),
    since: Optional[datetime] = Query(None),
    until: Optional[datetime] = Query(None),
):
    """
    Quick aggregate stats (total + counts by platform) within scope.
    """
    query = _scoped_query(db, current_user)
    query = _apply_filters(query, platform=platform, q=None, since=since, until=until)

    # total
    total = query.count()

    # by platform
    rows = (
        query.with_entities(MessageLog.platform, func.count(MessageLog.id))
        .group_by(MessageLog.platform)
        .all()
    )
    by_platform = {p or "unknown": int(c) for p, c in rows}

    return StatsOut(since=since, until=until, total=total, by_platform=by_platform)

