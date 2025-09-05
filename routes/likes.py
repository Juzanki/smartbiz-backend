# backend/routes/like_routes.py
# -*- coding: utf-8 -*-
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Response, Query, status
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func

from backend.db import get_db
from backend.auth import get_current_user
from backend.models.user import User
from backend.models.like_model import Like
from backend.schemas.like_schema import LikeCreate, LikeResponse

# Optional: realtime fanout (safe if missing)
try:
    from backend.utils.websocket_manager import WebSocketManager
    ws_manager: Optional[WebSocketManager] = WebSocketManager()
except Exception:
    ws_manager = None

router = APIRouter(prefix="/likes", tags=["Likes"])

UTC_NOW = lambda: datetime.now(timezone.utc)


# ---------- Helpers ----------
def _force_user(payload: LikeCreate, user_id: int) -> LikeCreate:
    """Force user_id from session if field exists in the schema."""
    updates = {}
    for k in ("user_id", "sender_id", "liker_id"):
        if hasattr(payload, k):
            updates[k] = user_id
    if not updates:
        return payload
    try:
        # Pydantic v2
        return payload.model_copy(update=updates)  # type: ignore[attr-defined]
    except AttributeError:
        # Pydantic v1
        return payload.copy(update=updates)  # type: ignore


def _normalize_stream_id(stream_id: str) -> str:
    sid = (stream_id or "").strip()
    if not sid:
        raise HTTPException(status_code=422, detail="stream_id is required")
    return sid


async def _broadcast_like(event: Dict[str, Any]) -> None:
    if ws_manager:
        try:
            await ws_manager.broadcast(str(event.get("stream_id") or ""), event)
        except Exception:
            pass


# ---------- Endpoints ----------
@router.post("/", response_model=LikeResponse, status_code=status.HTTP_201_CREATED)
def add_like(
    like_data: LikeCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    response: Response = None,  # allows status override on idempotent duplicate
):
    """
    Add a new like to a stream. Idempotent per (user, stream).
    - Forces user_id from the authenticated session (never trust client).
    - If a like already exists for this user & stream, returns it with 200 OK.
    """
    payload = _force_user(like_data, current_user.id)
    # Best-effort timestamp if model defines it
    values = payload.dict()
    if hasattr(Like, "created_at") and "created_at" not in values:
        values["created_at"] = UTC_NOW()

    try:
        like = Like(**values)
        db.add(like)
        db.commit()
        db.refresh(like)

    except IntegrityError:
        # Likely UNIQUE(stream_id, user_id) or similar — return existing row
        db.rollback()
        existing = (
            db.query(Like)
            .filter(
                Like.stream_id == getattr(payload, "stream_id"),
                (Like.user_id == current_user.id) if hasattr(Like, "user_id") else True,
            )
            .order_by(Like.id.desc())
            .first()
        )
        if not existing:
            raise HTTPException(status_code=409, detail="Duplicate like")
        if response is not None:
            response.status_code = status.HTTP_200_OK
        return existing

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error adding like: {str(e)}")

    # Fire-and-forget broadcast (only if you use websockets; remove if not needed)
    try:
        import anyio
        anyio.from_thread.run(_broadcast_like, {
            "type": "like_added",
            "stream_id": getattr(like, "stream_id", None),
            "user_id": getattr(like, "user_id", getattr(like, "sender_id", None)),
            "like_id": getattr(like, "id", None),
            "created_at": getattr(like, "created_at", None).isoformat() if getattr(like, "created_at", None) else None,
        })
    except Exception:
        pass

    return like


@router.delete("/{stream_id}", status_code=status.HTTP_200_OK)
def remove_my_like(
    stream_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Remove the current user's like from a stream (if it exists).
    Safe to call multiple times.
    """
    sid = _normalize_stream_id(stream_id)
    q = db.query(Like).filter(Like.stream_id == sid)
    if hasattr(Like, "user_id"):
        q = q.filter(Like.user_id == current_user.id)
    deleted = 0
    try:
        # Prefer bulk delete (single SQL); fall back if you need soft-delete
        deleted = q.delete(synchronize_session=False)
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error removing like: {str(e)}")

    if deleted == 0:
        return {"detail": "No like found"}
    return {"detail": "Like removed"}


@router.get("/count/{stream_id}")
def get_like_count(
    stream_id: str,
    db: Session = Depends(get_db),
    since_seconds: Optional[int] = Query(
        None, ge=1, le=86_400, description="Count only likes created in the last N seconds"
    ),
    since: Optional[datetime] = Query(None, description="ISO8601 lower bound (inclusive)"),
):
    """
    Get total number of likes for a specific stream.
    Optional time filters for “recent likes”.
    """
    sid = _normalize_stream_id(stream_id)
    q = db.query(func.count(Like.id)).filter(Like.stream_id == sid)

    # Time filtering if the model has a timestamp column
    if hasattr(Like, "created_at"):
        if since_seconds is not None:
            q = q.filter(Like.created_at >= UTC_NOW() - timedelta(seconds=since_seconds))
        if since is not None:
            # Treat naive datetimes as UTC
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
            q = q.filter(Like.created_at >= since)

    try:
        count = q.scalar() or 0
        return {"stream_id": sid, "likes": int(count)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching like count: {str(e)}")


@router.get("/me/{stream_id}")
def did_i_like(
    stream_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Check if the current user has liked the stream.
    """
    sid = _normalize_stream_id(stream_id)
    q = db.query(Like).filter(Like.stream_id == sid)
    if hasattr(Like, "user_id"):
        q = q.filter(Like.user_id == current_user.id)
    like = q.first()
    return {
        "stream_id": sid,
        "liked": bool(like),
        "like_id": getattr(like, "id", None) if like else None,
        "created_at": getattr(like, "created_at", None).isoformat() if getattr(like, "created_at", None) else None,
    }


@router.get("/stream/{stream_id}/recent", response_model=List[LikeResponse])
def list_recent_likes(
    stream_id: str,
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """
    List recent likes for a stream (paged). Great for live feeds or dashboards.
    """
    sid = _normalize_stream_id(stream_id)
    q = db.query(Like).filter(Like.stream_id == sid)
    # Prefer deterministic ordering: newest first if timestamp available
    if hasattr(Like, "created_at"):
        q = q.order_by(Like.created_at.desc())
    else:
        q = q.order_by(Like.id.desc())
    return q.offset(offset).limit(limit).all()
