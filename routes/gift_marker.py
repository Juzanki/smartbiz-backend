# -*- coding: utf-8 -*-
from __future__ import annotations
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Header, Query, status
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from backend.db import get_db
# Optional auth: ukiitaka, ondoa comment line ifuatayo na tumia kwenye deps
from backend.auth import get_current_user
from backend.models.user import User

from backend.models.gift_marker import GiftMarker
from backend.schemas.gift_marker_schemas import GiftMarkerCreate, GiftMarkerOut
from backend.utils.websocket_manager import WebSocketManager

router = APIRouter(prefix="/gift-markers", tags=["Gift Markers"])
manager = WebSocketManager()

NOW = lambda: datetime.now(timezone.utc)


def _iso(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.isoformat().replace("+00:00", "Z")


@router.post(
    "/",
    response_model=GiftMarkerOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_gift_marker(
    marker: GiftMarkerCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),  # lazimisha login
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    """
    Unda gift marker kwa stream fulani.
    - **Auth**: Inahitaji mtumiaji aliyeingia.
    - **Idempotency** (optional): tumia `Idempotency-Key` kuzuia duplicates.
    - **Broadcast**: hutuma `gift_marker` kwa websocket room ya stream.
    """
    # Basic validations
    if not marker.gift_name or not marker.gift_name.strip():
        raise HTTPException(status_code=422, detail="gift_name is required")

    # Position lazima isiwe hasi (kawaida ni sekunde kwenye replay timeline)
    pos = getattr(marker, "position", None)
    if pos is None or (isinstance(pos, (int, float)) and pos < 0):
        raise HTTPException(status_code=422, detail="position must be >= 0")

    # TODO (optional): hakiki kuwa current_user ana ruhusa kwenye stream hii (host/mod, admin)
    # if not stream_crud.user_can_mark(db, stream_id=marker.stream_id, user_id=current_user.id):
    #     raise HTTPException(status_code=403, detail="Not allowed to mark on this stream")

    try:
        kwargs = dict(
            stream_id=marker.stream_id,
            gift_name=marker.gift_name.strip(),
            position=marker.position,
            timestamp=NOW(),  # TZ-aware
            # unaweza pia kuhifadhi user_id ya aliyeweka marker ukiongeza column
            # created_by=current_user.id,
        )
        if hasattr(GiftMarker, "idempotency_key") and idempotency_key:
            kwargs["idempotency_key"] = idempotency_key

        new_marker = GiftMarker(**kwargs)
        db.add(new_marker)
        db.commit()
        db.refresh(new_marker)

    except IntegrityError as ie:
        db.rollback()
        # Ikiwa una UNIQUE (idempotency_key) au unique dedupe, rudisha 409
        raise HTTPException(status_code=409, detail="Duplicate request (idempotency)") from ie
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to create gift marker") from exc

    # Broadcast to stream room (string key ni salama)
    await manager.broadcast(str(marker.stream_id), {
        "type": "gift_marker",
        "marker_id": getattr(new_marker, "id", None),
        "stream_id": marker.stream_id,
        "gift_name": marker.gift_name.strip(),
        "position": marker.position,
        "timestamp": _iso(new_marker.timestamp),
    })

    return new_marker


@router.get(
    "/stream/{stream_id}",
    response_model=List[GiftMarkerOut],
    status_code=status.HTTP_200_OK,
)
def get_gift_markers(
    stream_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),  # weka auth kama unataka private streams
    gift_name: Optional[str] = Query(None, description="Filter by specific gift name"),
    since: Optional[str] = Query(
        None,
        description="ISO8601 datetime; rudisha markers kuanzia wakati huu na kuendelea"
    ),
    order: str = Query("asc", pattern="^(asc|desc)$"),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """
    Orodhesha gift markers za stream moja, zikiwa na:
    - Filters: `gift_name`, `since` (ISO8601)
    - Pagination: `limit`, `offset`
    - Order: `asc` au `desc` kwa `timestamp`
    """
    q = db.query(GiftMarker).filter(GiftMarker.stream_id == stream_id)

    if gift_name:
        q = q.filter(GiftMarker.gift_name == gift_name)

    if since:
        # Kubali 'Z' mwishoni (ISO8601)
        since_norm = since.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(since_norm)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid 'since' datetime")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        q = q.filter(GiftMarker.timestamp >= dt)

    if order == "asc":
        q = q.order_by(GiftMarker.timestamp.asc())
    else:
        q = q.order_by(GiftMarker.timestamp.desc())

    return q.offset(offset).limit(limit).all()
