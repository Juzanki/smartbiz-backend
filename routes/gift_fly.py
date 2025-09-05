# backend/routes/gift_fly.py
# -*- coding: utf-8 -*-
from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional, List

from fastapi import (
    APIRouter, Depends, HTTPException, Header, Query, status, WebSocket
)
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from backend.db import get_db
from backend.auth import get_current_user
from backend.models.user import User
from backend.models.gift_fly import GiftFly
from backend.schemas.gift_fly_schemas import GiftFlyCreate, GiftFlyOut
from backend.utils.websocket_manager import WebSocketManager

router = APIRouter(prefix="/gift-fly", tags=["Gift Fly"])
manager = WebSocketManager()  # shared hub for this module

# ===== Config =====
COMBO_WINDOW_SECONDS: float = 2.0  # repeat same gift within this window => combo++
MAX_GIFT_NAME_LEN: int = 120
NOW = lambda: datetime.now(timezone.utc)

# ===== Helpers =====
async def _db_commit_refresh(db: Session, instance) -> None:
    """
    Commit + refresh without blocking the event loop.
    We run the blocking DB calls in a threadpool.
    """
    try:
        await run_in_threadpool(db.commit)
    except Exception:
        await run_in_threadpool(db.rollback)
        raise
    await run_in_threadpool(db.refresh, instance)

async def _create_gift_fly(
    db: Session,
    *,
    stream_id: int,
    user_id: int,
    gift_name: str,
    idempotency_key: Optional[str],
    combo_count: Optional[int],
) -> GiftFly:
    """Create and commit a GiftFly row (optionally using extra columns if present)."""
    def _sync_create() -> GiftFly:
        kwargs = dict(
            stream_id=stream_id,
            user_id=user_id,
            gift_name=gift_name,
            sent_at=NOW(),
        )
        # Optional fields only if your model defines them
        if hasattr(GiftFly, "idempotency_key") and idempotency_key:
            kwargs["idempotency_key"] = idempotency_key
        if hasattr(GiftFly, "combo_count") and combo_count is not None:
            kwargs["combo_count"] = combo_count

        obj = GiftFly(**kwargs)
        db.add(obj)
        return obj

    obj = await run_in_threadpool(_sync_create)
    await _db_commit_refresh(db, obj)
    return obj

def _iso(ts: datetime) -> str:
    """Return an ISO-8601 UTC timestamp with 'Z' suffix."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.isoformat().replace("+00:00", "Z")

async def _get_last_user_gift(db: Session, stream_id: int, user_id: int) -> Optional[GiftFly]:
    """Fetch the most recent gift by this user on this stream."""
    def _sync_get():
        return (
            db.query(GiftFly)
            .filter(GiftFly.stream_id == stream_id, GiftFly.user_id == user_id)
            .order_by(GiftFly.sent_at.desc())
            .first()
        )
    return await run_in_threadpool(_sync_get)

def _calc_combo(prev: Optional[GiftFly], gift_name: str) -> int:
    """
    If the same gift is sent again within COMBO_WINDOW_SECONDS, increment combo.
    Otherwise, start at 1.
    """
    if not prev or prev.gift_name != gift_name:
        return 1
    prev_ts = prev.sent_at.replace(tzinfo=prev.sent_at.tzinfo or timezone.utc)
    delta = (NOW() - prev_ts).total_seconds()
    base = getattr(prev, "combo_count", 1)
    return base + 1 if delta <= COMBO_WINDOW_SECONDS else 1

# ===== HTTP Endpoints =====
@router.post("/", response_model=GiftFlyOut, status_code=status.HTTP_201_CREATED)
async def send_gift(
    data: GiftFlyCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    """
    Create a "gift fly-in" event for a stream.

    Security:
    - `user_id` is taken from the authenticated session (client-provided value is ignored).

    Functionality:
    - Combo counting: increments `combo_count` when the same gift repeats quickly.
    - Idempotency (optional): use `Idempotency-Key` header if your model has that column.
    """
    user_id = int(current_user.id)
    stream_id = int(data.stream_id)
    gift_name = (data.gift_name or "").strip()

    if not gift_name:
        raise HTTPException(status_code=422, detail="gift_name is required")
    if len(gift_name) > MAX_GIFT_NAME_LEN:
        raise HTTPException(status_code=422, detail=f"gift_name too long (>{MAX_GIFT_NAME_LEN})")

    # TODO: optionally check stream permissions/visibility for this user.

    last_gift = await _get_last_user_gift(db, stream_id, user_id)
    combo_count = _calc_combo(last_gift, gift_name)

    try:
        new_gift = await _create_gift_fly(
            db=db,
            stream_id=stream_id,
            user_id=user_id,
            gift_name=gift_name,
            idempotency_key=idempotency_key,
            combo_count=combo_count,
        )
    except IntegrityError as ie:
        # Likely a unique violation on idempotency_key
        raise HTTPException(status_code=409, detail="Duplicate request (idempotency)") from ie
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Failed to create gift event") from exc

    # Broadcast real-time event to all sockets in this stream room
    payload = {
        "type": "gift_fly",
        "event_id": getattr(new_gift, "id", None),
        "stream_id": stream_id,
        "user_id": user_id,
        "gift_name": gift_name,
        "combo_count": getattr(new_gift, "combo_count", combo_count),
        "timestamp": _iso(new_gift.sent_at),
    }
    await manager.broadcast(stream_id, payload)

    return new_gift

@router.get("/stream/{stream_id}/recent", response_model=List[GiftFlyOut])
async def recent_gifts(
    stream_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    limit: int = Query(30, ge=1, le=100),
):
    """Return the most recent gift events for a stream (useful for resume/reconnect)."""
    def _sync_get():
        return (
            db.query(GiftFly)
            .filter(GiftFly.stream_id == stream_id)
            .order_by(GiftFly.sent_at.desc())
            .limit(limit)
            .all()
        )
    return await run_in_threadpool(_sync_get)

# ===== WebSocket Endpoint =====
@router.websocket("/ws/{stream_id}")
async def ws_endpoint(websocket: WebSocket, stream_id: int):
    """
    Minimal WebSocket room for the given stream_id.
    In production, you should authenticate the connection (e.g., token in query/header).
    """
    await manager.connect(stream_id, websocket)
    try:
        while True:
            # Keep the socket alive. If you expect client messages, handle them here.
            # You can also use `await websocket.receive_json()` if sending JSON.
            await websocket.receive_text()
    except Exception:
        # Socket closed or errored; fall through to cleanup.
        pass
    finally:
        await manager.disconnect(stream_id, websocket)
