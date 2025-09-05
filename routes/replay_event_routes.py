from __future__ import annotations
# backend/routes/replay_events.py
import hashlib
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any

from fastapi import (
    APIRouter, Depends, HTTPException, status, Header, Query, Response
)
from sqlalchemy.orm import Session
from sqlalchemy import and_, func

from backend.db import get_db
from backend.models.replay_events import ReplayEvent
from backend.schemas.replay_event_schemas import ReplayEventCreate

router = APIRouter(prefix="/replay-events", tags=["Replay Events"])

# -------------------- Helpers --------------------
def _utc() -> datetime:
    return datetime.now(timezone.utc)

def _has_attr(obj: Any, name: str) -> bool:
    return hasattr(obj, name)

def _etag_of(rows: List[Any]) -> str:
    if not rows:
        return 'W/"empty"'
    last = max(
        getattr(r, "updated_at", None)
        or getattr(r, "created_at", None)
        or datetime.min
        for r in rows
    )
    seed = f"{len(rows)}|{last.isoformat()}"
    return 'W/"' + hashlib.sha256(seed.encode()).hexdigest()[:16] + '"'

# -------------------- CREATE (idempotent + dedup) --------------------
@router.post(
    "/auto-gift-marker",
    status_code=status.HTTP_201_CREATED,
    summary="Ongeza gift marker (idempotent + dedup)"
)
def add_gift_marker(
    data: ReplayEventCreate,
    db: Session = Depends(get_db),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    dedup_seconds: int = Query(10, ge=0, le=600, description="Dirisha la dedup (sekunde)"),
    time_tolerance: float = Query(1.0, ge=0.0, le=5.0, description="Uvumulivu wa timestamp (sekunde)"),
):
    """
    - **Idempotency-Key**: ukituma mara mbili kwa network retries, hatutaandika tena.
    - **Dedup**: tukio sawa (stream_id, type, content) ndani ya `dedup_seconds`
      na timestamp iliyo karibu (±`time_tolerance`) halitaandikwa mara mbili.
    """
    # 0) Idempotency (kama una column `idempotency_key` kwenye model)
    if idempotency_key and _has_attr(ReplayEvent, "idempotency_key"):
        existing = (
            db.query(ReplayEvent)
            .filter(
                ReplayEvent.event_type == "gift_marker",
                ReplayEvent.stream_id == data.stream_id,
                ReplayEvent.idempotency_key == idempotency_key,
            )
            .first()
        )
        if existing:
            return {"message": "Already exists (idempotent)", "event_id": existing.id}

    # 1) Dedup window (best-effort)
    if dedup_seconds > 0:
        ts = getattr(data, "timestamp", None)
        # timestamp inaweza kuwa float (sekunde) au datetime; jaribu kulainisha
        with suppress(Exception):
            if isinstance(ts, (int, float)):
                # kama model yako huihifadhi kama float, tuisome tu
                pass
            elif isinstance(ts, str):
                # jaribu kuiparse ISO
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            elif isinstance(ts, datetime):
                ts = ts.timestamp()
        if isinstance(ts, (int, float)):
            start = ts - dedup_seconds
            q = db.query(ReplayEvent).filter(
                ReplayEvent.stream_id == data.stream_id,
                ReplayEvent.event_type == "gift_marker",
                ReplayEvent.content == data.content,
            )
            # timestmp (float) karibu na tukio jipya
            if _has_attr(ReplayEvent, "timestamp"):
                q = q.filter(ReplayEvent.timestamp >= start,
                             ReplayEvent.timestamp <= ts + time_tolerance)
            # aidha tumie created_at kama huna 'timestamp'
            elif _has_attr(ReplayEvent, "created_at"):
                win_start = _utc() - timedelta(seconds=dedup_seconds)
                q = q.filter(ReplayEvent.created_at >= win_start)
            dup = q.first()
            if dup:
                return {"message": "Deduped", "event_id": dup.id}

    # 2) Tunga row mpya
    row = ReplayEvent(
        stream_id=data.stream_id,
        content=data.content,
        event_type="gift_marker",
    )
    # weka timestamp kama ipo kwenye schema
    if getattr(data, "timestamp", None) is not None and _has_attr(ReplayEvent, "timestamp"):
        row.timestamp = data.timestamp
    # idempotency_key (kama column ipo)
    if idempotency_key and _has_attr(ReplayEvent, "idempotency_key"):
        row.idempotency_key = idempotency_key
    # timestamps za audit
    if _has_attr(ReplayEvent, "created_at") and not getattr(row, "created_at", None):
        row.created_at = _utc()
    if _has_attr(ReplayEvent, "updated_at"):
        row.updated_at = _utc()

    db.add(row)
    db.commit()
    db.refresh(row)
    return {"message": "Gift marker added", "event_id": row.id}

# -------------------- LIST (ETag + filters + pagination) --------------------
@router.get(
    "/{stream_id}/gift-markers",
    summary="Orodha ya gift markers kwa stream",
    response_model=List[Dict[str, Any]]
)
def list_gift_markers(
    stream_id: int,
    response: Response,
    db: Session = Depends(get_db),
    since_id: Optional[int] = Query(None, ge=1, description="Rudisha baada ya id hii"),
    since_time: Optional[float] = Query(None, ge=0, description="Rudisha matukio baada ya sekunde hizi"),
    limit: int = Query(200, ge=1, le=1000),
    order: str = Query("asc", pattern="^(asc|desc)$"),
    if_none_match: Optional[str] = Header(None, alias="If-None-Match"),
):
    q = db.query(ReplayEvent).filter(
        ReplayEvent.stream_id == stream_id,
        ReplayEvent.event_type == "gift_marker",
    )
    if since_id is not None:
        q = q.filter(ReplayEvent.id > since_id)
    if since_time is not None and _has_attr(ReplayEvent, "timestamp"):
        q = q.filter(ReplayEvent.timestamp >= float(since_time))

    order_col = ReplayEvent.id
    q = q.order_by(order_col.asc() if order == "asc" else order_col.desc()).limit(limit)
    rows = q.all()

    etag = _etag_of(rows)
    if if_none_match and if_none_match == etag:
        return Response(status_code=304)
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "public, max-age=5"

    # toleo nyepesi kwa mobile
    def _row_out(r: ReplayEvent) -> Dict[str, Any]:
        return {
            "id": r.id,
            "stream_id": r.stream_id,
            "content": r.content,
            "timestamp": getattr(r, "timestamp", None),
            "created_at": getattr(r, "created_at", None),
        }
    return [_row_out(r) for r in rows]

# -------------------- UPDATE (PATCH content/timestamp) --------------------
@router.patch(
    "/gift-marker/{event_id}",
    summary="Sasisha gift marker",
    response_model=Dict[str, Any]
)
def update_gift_marker(
    event_id: int,
    db: Session = Depends(get_db),
    content: Optional[str] = Query(None, min_length=1, max_length=2000),
    timestamp: Optional[float] = Query(None, ge=0),
):
    ev = db.query(ReplayEvent).filter(
        ReplayEvent.id == event_id,
        ReplayEvent.event_type == "gift_marker",
    ).first()
    if not ev:
        raise HTTPException(status_code=404, detail="Event not found")

    if content is not None:
        ev.content = content.strip()
    if timestamp is not None and _has_attr(ReplayEvent, "timestamp"):
        ev.timestamp = float(timestamp)
    if _has_attr(ReplayEvent, "updated_at"):
        ev.updated_at = _utc()

    db.commit()
    db.refresh(ev)
    return {"message": "updated", "event_id": ev.id}

# -------------------- DELETE --------------------
@router.delete(
    "/gift-marker/{event_id}",
    summary="Futa gift marker",
    response_model=Dict[str, Any]
)
def delete_gift_marker(
    event_id: int,
    db: Session = Depends(get_db),
):
    ev = db.query(ReplayEvent).filter(
        ReplayEvent.id == event_id,
        ReplayEvent.event_type == "gift_marker",
    ).first()
    if not ev:
        raise HTTPException(status_code=404, detail="Event not found")
    db.delete(ev)
    db.commit()
    return {"message": "deleted", "event_id": event_id}
