# -*- coding: utf-8 -*-
from __future__ import annotations
from datetime import datetime, timezone
from typing import List, Optional, Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Header, Query, status
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from backend.db import get_db
from backend.auth import get_current_user
from backend.models.user import User
from backend.models.gift_movement import GiftMovement
from backend.schemas.gift_movement_schemas import GiftMovementCreate
from backend.utils.websocket_manager import WebSocketManager

router = APIRouter(prefix="/gift-movements", tags=["Gift Movements"])
manager = WebSocketManager()

NOW = lambda: datetime.now(timezone.utc)


# ---------- Helpers ----------
async def _commit_refresh(db: Session, instance):
    try:
        await run_in_threadpool(db.commit)
    except Exception:
        await run_in_threadpool(db.rollback)
        raise
    await run_in_threadpool(db.refresh, instance)


def _iso(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.isoformat().replace("+00:00", "Z")


def _asdict_safe(model) -> Dict[str, Any]:
    """Toa dict bila kuvunjika iwapo model haina baadhi ya attrs."""
    out = {}
    for attr in ("id", "stream_id", "user_id", "gift_name", "sent_at"):
        if hasattr(model, attr):
            out[attr] = getattr(model, attr)
    return out


# ---------- Endpoints ----------
@router.post("/send", status_code=status.HTTP_201_CREATED)
async def send_gift(
    data: GiftMovementCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    """
    Tuma tukio la *gift movement* kwa stream.
    - **Security**: Ikiwa `GiftMovement` ina `user_id`, tunalazimisha kutoka `current_user`.
    - **UTC timestamps**: `sent_at` ni TZ-aware (ISO8601 kwa clients).
    - **Idempotency (optional)**: tumia `Idempotency-Key` (weka unique index kwenye DB).
    - **Non-blocking**: DB ops zinaenda threadpool; WebSocket ni async.
    """
    if getattr(data, "stream_id", None) in (None, 0):
        raise HTTPException(status_code=422, detail="stream_id is required")

    payload = data.dict()

    # Force user_id kutoka session endapo column ipo
    if hasattr(GiftMovement, "user_id"):
        payload["user_id"] = current_user.id

    # Timestamps TZ-aware
    payload["sent_at"] = NOW()

    # Idempotency optional
    if hasattr(GiftMovement, "idempotency_key") and idempotency_key:
        payload["idempotency_key"] = idempotency_key

    # Unda na hifadhi
    try:
        movement = GiftMovement(**payload)
        db.add(movement)
        await _commit_refresh(db, movement)
    except IntegrityError as ie:
        # Mfano: UNIQUE (idempotency_key) ikigonga duplicate request
        raise HTTPException(status_code=409, detail="Duplicate request (idempotency)") from ie
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Failed to create gift movement") from exc

    # Tuma broadcast kwa room ya stream
    broadcast = {
        "type": "gift_movement",
        "movement": {
            **_asdict_safe(movement),
            "sent_at": _iso(movement.sent_at),
        },
        # Optional: rudisha pia fields zingine kutoka request (mf. path/coords) bila kuvuruga schema
        "data": data.dict(),
    }
    await manager.broadcast(str(getattr(movement, "stream_id", data.stream_id)), broadcast)

    return {"message": "Gift sent", "movement_id": getattr(movement, "id", None)}


@router.get("/stream/{stream_id}")
async def get_gift_movements(
    stream_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    since: Optional[str] = Query(None, description="ISO8601 datetime filter (inclusive)"),
    user_id: Optional[int] = Query(None, description="Filter kwa mtumiaji fulani (kama column ipo)"),
    gift_name: Optional[str] = Query(None, description="Filter kwa gift_name (kama column ipo)"),
):
    """
    Orodha ya gift movements kwa stream:
    - Pagination: `limit`, `offset`
    - Filters: `since` (ISO8601), `user_id`*, `gift_name`* (*ikiwa column ipo kwenye model)
    - Order: `asc` | `desc` kwa `sent_at`
    """
    def _query():
        q = db.query(GiftMovement).filter(GiftMovement.stream_id == stream_id)

        # since filter
        if since:
            try:
                dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            except ValueError:
                raise HTTPException(status_code=422, detail="Invalid 'since' datetime")
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            q = q.filter(GiftMovement.sent_at >= dt)

        # user_id filter if column exists
        if user_id is not None and hasattr(GiftMovement, "user_id"):
            q = q.filter(GiftMovement.user_id == user_id)

        # gift_name filter if column exists
        if gift_name and hasattr(GiftMovement, "gift_name"):
            q = q.filter(GiftMovement.gift_name == gift_name)

        # order
        q = q.order_by(GiftMovement.sent_at.asc() if order == "asc" else GiftMovement.sent_at.desc())

        return q.offset(offset).limit(limit).all()

    items = await run_in_threadpool(_query)
    return items


# Optional: paged variant with meta (great for mobile infinite scroll)
from pydantic import BaseModel, Field

class PageMeta(BaseModel):
    total: int
    limit: int
    offset: int

class MovementPage(BaseModel):
    items: List[Dict[str, Any]]
    meta: PageMeta

@router.get("/stream/{stream_id}/page", response_model=MovementPage)
async def get_gift_movements_page(
    stream_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    limit: int = Query(30, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """
    Toleo lenye `items + meta` kwa mobile infinite scroll.
    """
    def _count_and_fetch():
        base = db.query(GiftMovement).filter(GiftMovement.stream_id == stream_id)
        total = base.count()
        items = (
            base.order_by(GiftMovement.sent_at.desc())
                .offset(offset).limit(limit).all()
        )
        # Convert to safe dicts (unaweza kuacha kama una Out schema)
        norm = []
        for it in items:
            d = _asdict_safe(it)
            if "sent_at" in d:
                d["sent_at"] = _iso(d["sent_at"])
            norm.append(d)
        return total, norm

    total, items = await run_in_threadpool(_count_and_fetch)
    return MovementPage(items=items, meta=PageMeta(total=total, limit=limit, offset=offset))
