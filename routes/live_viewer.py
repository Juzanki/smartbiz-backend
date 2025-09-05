# backend/routes/live_viewer_routes.py
# -*- coding: utf-8 -*-
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func, and_

from backend.db import get_db
from backend.auth import get_current_user
from backend.models.user import User
from backend.models.live_viewer import LiveViewer
from backend.schemas.live_viewer_schemas import LiveViewerIn, LiveViewerOut

# Optional realtime broadcast (safe if missing)
try:
    from backend.utils.websocket_manager import WebSocketManager
    ws_manager: Optional[WebSocketManager] = WebSocketManager()
except Exception:
    ws_manager = None

router = APIRouter(prefix="/live-viewers", tags=["Live Viewers"])

UTC_NOW = lambda: datetime.now(timezone.utc)

# ---------- DTOs ----------
class PageMeta(BaseModel):
    total: int
    limit: int
    offset: int

class ViewerPage(BaseModel):
    items: List[LiveViewerOut]
    meta: PageMeta

class CountOut(BaseModel):
    stream_id: int | str
    active: int

# ---------- Helpers ----------
def _force_user(payload: LiveViewerIn, user_id: int) -> LiveViewerIn:
    """Ensure the viewer user_id comes from the authenticated session."""
    if hasattr(payload, "user_id"):
        try:
            return payload.model_copy(update={"user_id": user_id})  # Pydantic v2
        except AttributeError:
            return payload.copy(update={"user_id": user_id})        # Pydantic v1
    return payload

async def _broadcast(stream_id: int | str, event: Dict[str, Any]) -> None:
    if ws_manager:
        try:
            await ws_manager.broadcast(str(stream_id), event)
        except Exception:
            pass

def _apply_active_filter(q, ttl_seconds: Optional[int]) :
    """
    Consider a viewer 'active' if:
      - is_active is True
      - and (last_seen >= now - ttl) when column exists,
        otherwise fallback to `left_at IS NULL`.
    """
    q = q.filter(LiveViewer.is_active.is_(True))
    if ttl_seconds and hasattr(LiveViewer, "last_seen"):
        cutoff = UTC_NOW() - timedelta(seconds=ttl_seconds)
        q = q.filter(LiveViewer.last_seen >= cutoff)
    else:
        # Fallback: still active if not left
        if hasattr(LiveViewer, "left_at"):
            q = q.filter(LiveViewer.left_at.is_(None))
    return q

# ---------- Endpoints ----------
@router.post("/join", response_model=LiveViewerOut, status_code=status.HTTP_201_CREATED)
async def join_stream(
    data: LiveViewerIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    response: Response = None,
):
    """
    Idempotent join:
      - Forces user_id from session (never trust client).
      - If an active row already exists, returns it with 200 OK and refreshes `last_seen`.
    """
    payload = _force_user(data, current_user.id)

    # Look for an already-active session
    existing = (
        db.query(LiveViewer)
          .filter_by(user_id=payload.user_id, stream_id=payload.stream_id, is_active=True)
          .first()
    )
    if existing:
        # Soft refresh presence
        if hasattr(existing, "last_seen"):
            existing.last_seen = UTC_NOW()
            db.commit()
            db.refresh(existing)
        if response is not None:
            response.status_code = status.HTTP_200_OK
        return existing

    # Create a new active viewer row
    values = payload.dict()
    if hasattr(LiveViewer, "joined_at") and "joined_at" not in values:
        values["joined_at"] = UTC_NOW()
    if hasattr(LiveViewer, "last_seen") and "last_seen" not in values:
        values["last_seen"] = UTC_NOW()
    values["is_active"] = True

    try:
        viewer = LiveViewer(**values)
        db.add(viewer)
        db.commit()
        db.refresh(viewer)
    except IntegrityError:
        db.rollback()
        # Handle races: fetch the row created by a concurrent request
        viewer = (
            db.query(LiveViewer)
              .filter_by(user_id=payload.user_id, stream_id=payload.stream_id, is_active=True)
              .order_by(LiveViewer.id.desc())
              .first()
        )
        if viewer is None:
            raise HTTPException(status_code=409, detail="Join conflict")
        if response is not None:
            response.status_code = status.HTTP_200_OK
        return viewer
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to join stream: {exc}")

    # Broadcast (optional)
    await _broadcast(payload.stream_id, {
        "type": "viewer_joined",
        "stream_id": payload.stream_id,
        "user_id": payload.user_id,
        "ts": UTC_NOW().isoformat(),
    })

    return viewer


@router.post("/leave", response_model=LiveViewerOut)
async def leave_stream(
    data: LiveViewerIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Idempotent leave:
      - Forces user_id from session.
      - Marks viewer inactive if found; safe to call multiple times.
    """
    payload = _force_user(data, current_user.id)

    viewer = (
        db.query(LiveViewer)
          .filter_by(user_id=payload.user_id, stream_id=payload.stream_id, is_active=True)
          .first()
    )
    if not viewer:
        raise HTTPException(status_code=404, detail="Viewer not found in active session")

    # Mark as left
    if hasattr(viewer, "left_at"):
        viewer.left_at = UTC_NOW()
    viewer.is_active = False
    if hasattr(viewer, "last_seen"):
        viewer.last_seen = UTC_NOW()
    try:
        db.commit()
        db.refresh(viewer)
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to leave stream: {exc}")

    # Broadcast (optional)
    await _broadcast(payload.stream_id, {
        "type": "viewer_left",
        "stream_id": payload.stream_id,
        "user_id": payload.user_id,
        "ts": UTC_NOW().isoformat(),
    })

    return viewer


@router.post("/heartbeat", response_model=LiveViewerOut)
def heartbeat(
    data: LiveViewerIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Update presence for an active viewer without sending chat data.
    Mobile clients can call this every ~20–60 seconds.
    """
    payload = _force_user(data, current_user.id)
    viewer = (
        db.query(LiveViewer)
          .filter_by(user_id=payload.user_id, stream_id=payload.stream_id, is_active=True)
          .first()
    )
    if not viewer:
        raise HTTPException(status_code=404, detail="Viewer not found in active session")

    if hasattr(viewer, "last_seen"):
        viewer.last_seen = UTC_NOW()
    try:
        db.commit()
        db.refresh(viewer)
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to update heartbeat: {exc}")
    return viewer


@router.get("/stream/{stream_id}/count", response_model=CountOut)
def count_active_viewers(
    stream_id: int,
    db: Session = Depends(get_db),
    ttl_seconds: int = Query(60, ge=1, le=600, description="Presence TTL; viewers older than this are considered inactive"),
):
    """
    Return the number of active viewers for a stream, honoring a presence TTL.
    """
    q = db.query(func.count(LiveViewer.id)).filter(LiveViewer.stream_id == stream_id)
    q = _apply_active_filter(q, ttl_seconds)

    try:
        total = int(q.scalar() or 0)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to count viewers: {exc}")
    return CountOut(stream_id=stream_id, active=total)


@router.get("/stream/{stream_id}/active", response_model=ViewerPage)
def list_active_viewers(
    stream_id: int,
    db: Session = Depends(get_db),
    ttl_seconds: int = Query(60, ge=1, le=600),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    order: str = Query("desc", pattern="^(asc|desc)$"),
):
    """
    Paged listing of active viewers, deterministic ordering by last_seen/joined_at.
    """
    base = db.query(LiveViewer).filter(LiveViewer.stream_id == stream_id)
    base = _apply_active_filter(base, ttl_seconds)

    total = base.count()

    # Prefer last_seen if available, else joined_at, else id
    if hasattr(LiveViewer, "last_seen"):
        base = base.order_by(LiveViewer.last_seen.asc() if order == "asc" else LiveViewer.last_seen.desc())
    elif hasattr(LiveViewer, "joined_at"):
        base = base.order_by(LiveViewer.joined_at.asc() if order == "asc" else LiveViewer.joined_at.desc())
    else:
        base = base.order_by(LiveViewer.id.asc() if order == "asc" else LiveViewer.id.desc())

    items = base.offset(offset).limit(limit).all()
    return ViewerPage(items=items, meta=PageMeta(total=total, limit=limit, offset=offset))
