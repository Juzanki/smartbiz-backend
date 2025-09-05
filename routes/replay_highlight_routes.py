from __future__ import annotations
# backend/routes/replay_highlights.py
import hashlib
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import (
    APIRouter, Depends, HTTPException, Query, Header, Response, status, Body, Path
)
from sqlalchemy.orm import Session
from sqlalchemy import and_

from backend.db import get_db

# ---- Schemas (tumia zako; hizi ni fallbacks endapo hazijapakiwa) ----------
with suppress(Exception):
    from backend.schemas.replay_highlight_schemas import (
        ReplayHighlightCreate, ReplayHighlightOut, ReplayHighlightUpdate
    )

if "ReplayHighlightCreate" not in globals():
    from pydantic import BaseModel, Field
    class ReplayHighlightCreate(BaseModel):
        title: str = Field(..., min_length=1, max_length=200)
        timestamp: float = Field(..., ge=0)  # sekunde ndani ya video
    class ReplayHighlightUpdate(BaseModel):
        title: Optional[str] = Field(None, min_length=1, max_length=200)
        timestamp: Optional[float] = Field(None, ge=0)
    class ReplayHighlightOut(ReplayHighlightCreate):
        id: int
        video_post_id: int
        created_at: Optional[datetime] = None
        updated_at: Optional[datetime] = None
        class Config: orm_mode = True
        model_config = {"from_attributes": True}

# ---- Model ---------------------------------------------------------------
HL = None
with suppress(Exception):
    from backend.models.replay_highlight import ReplayHighlight as HL  # type: ignore

router = APIRouter(prefix="/replay-highlights", tags=["Replay Highlights"])

# ---- Helpers -------------------------------------------------------------
def _utc() -> datetime:
    return datetime.now(timezone.utc)

def _etag(rows: List[Any]) -> str:
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

def _serialize(obj: Any) -> ReplayHighlightOut:
    if hasattr(ReplayHighlightOut, "model_validate"):
        return ReplayHighlightOut.model_validate(obj, from_attributes=True)  # pyd v2
    return ReplayHighlightOut.model_validate(obj)  # pyd v1

# ========================== CREATE (idempotent + dedup) ==========================
@router.post(
    "/{video_post_id}",
    response_model=ReplayHighlightOut,
    status_code=status.HTTP_201_CREATED,
    summary="Ongeza highlight (idempotent + dedup)"
)
def add_highlight(
    video_post_id: int,
    data: ReplayHighlightCreate,
    db: Session = Depends(get_db),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    dedup_seconds: int = Query(10, ge=0, le=600, description="Zuia duplicates ndani ya dirisha hili"),
    timestamp_tolerance: float = Query(0.7, ge=0.0, le=5.0, description="Uvumulivu wa sekunde kwenye timestamp")
):
    if not HL:
        raise HTTPException(status_code=500, detail="ReplayHighlight model haijapatikana")

    # Idempotency (kama una column idempotency_key)
    if idempotency_key and hasattr(HL, "idempotency_key"):
        exist = (
            db.query(HL)
            .filter(HL.video_post_id == video_post_id, HL.idempotency_key == idempotency_key)
            .first()
        )
        if exist:
            return _serialize(exist)

    # Dedup: title sawa karibu na timestamp sawa ndani ya dirisha
    if dedup_seconds > 0:
        win_start = _utc() - timedelta(seconds=dedup_seconds)
        q = db.query(HL).filter(
            HL.video_post_id == video_post_id,
            HL.title == data.title.strip(),
        )
        if hasattr(HL, "timestamp"):
            q = q.filter(and_(HL.timestamp >= data.timestamp - timestamp_tolerance,
                              HL.timestamp <= data.timestamp + timestamp_tolerance))
        if hasattr(HL, "created_at"):
            q = q.filter(HL.created_at >= win_start)
        dup = q.first()
        if dup:
            return _serialize(dup)

    row = HL(
        video_post_id=video_post_id,
        title=data.title.strip(),
        timestamp=float(data.timestamp) if hasattr(HL, "timestamp") else None,
    )
    if idempotency_key and hasattr(HL, "idempotency_key"):
        row.idempotency_key = idempotency_key
    if hasattr(HL, "created_at") and not getattr(row, "created_at", None):
        row.created_at = _utc()
    if hasattr(HL, "updated_at"):
        row.updated_at = _utc()

    db.add(row)
    db.commit()
    db.refresh(row)
    return _serialize(row)

# ========================== LIST (filters + pagination + ETag) ==========================
@router.get(
    "/{video_post_id}",
    response_model=List[ReplayHighlightOut],
    summary="Orodha ya highlights (filters + pagination + ETag)"
)
def get_highlight_list(
    video_post_id: int,
    response: Response,
    db: Session = Depends(get_db),
    q: Optional[str] = Query(None, description="tafuta kwenye kichwa (ILIKE %q%)"),
    min_ts: Optional[float] = Query(None, ge=0),
    max_ts: Optional[float] = Query(None, ge=0),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    order: str = Query("asc", pattern="^(asc|desc)$"),
    if_none_match: Optional[str] = Header(None, alias="If-None-Match"),
):
    if not HL:
        raise HTTPException(status_code=500, detail="ReplayHighlight model haijapatikana")

    qry = db.query(HL).filter(HL.video_post_id == video_post_id)
    if q:
        qry = qry.filter(HL.title.ilike(f"%{q}%"))
    if min_ts is not None and hasattr(HL, "timestamp"):
        qry = qry.filter(HL.timestamp >= float(min_ts))
    if max_ts is not None and hasattr(HL, "timestamp"):
        qry = qry.filter(HL.timestamp <= float(max_ts))

    order_col = getattr(HL, "timestamp", getattr(HL, "id"))
    qry = qry.order_by(order_col.asc() if order == "asc" else order_col.desc())

    total = qry.count()
    rows = qry.offset(offset).limit(limit).all()

    tag = _etag(rows)
    if if_none_match and if_none_match == tag:
        return Response(status_code=304)
    response.headers["ETag"] = tag
    response.headers["Cache-Control"] = "public, max-age=20"
    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Limit"] = str(limit)
    response.headers["X-Offset"] = str(offset)

    return [_serialize(r) for r in rows]

# ========================== UPDATE (PATCH) ==========================
@router.patch(
    "/item/{highlight_id}",
    response_model=ReplayHighlightOut,
    summary="Sasisha highlight (partial)"
)
def update_highlight(
    highlight_id: int = Path(..., ge=1),
    payload: ReplayHighlightUpdate = Body(...),
    db: Session = Depends(get_db),
):
    if not HL:
        raise HTTPException(status_code=500, detail="ReplayHighlight model haijapatikana")

    row = db.query(HL).filter(HL.id == highlight_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Highlight not found")

    data = payload.dict(exclude_unset=True)
    if "title" in data and data["title"] is not None:
        data["title"] = data["title"].strip()

    for k, v in data.items():
        if hasattr(row, k):
            setattr(row, k, v)
    if hasattr(row, "updated_at"):
        row.updated_at = _utc()

    db.commit()
    db.refresh(row)
    return _serialize(row)

# ========================== DELETE ==========================
@router.delete(
    "/item/{highlight_id}",
    response_model=dict,
    summary="Futa highlight"
)
def delete_highlight(
    highlight_id: int,
    db: Session = Depends(get_db),
):
    if not HL:
        raise HTTPException(status_code=500, detail="ReplayHighlight model haijapatikana")

    row = db.query(HL).filter(HL.id == highlight_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Highlight not found")
    db.delete(row)
    db.commit()
    return {"detail": "deleted"}

# ========================== BATCH INGEST ==========================
class _BatchItem(ReplayHighlightCreate): ...
class _BatchPayload(Body):
    items: List[_BatchItem]  # type: ignore

@router.post(
    "/{video_post_id}/batch",
    summary="Ongeza highlights kwa batch (â‰¤ 500)",
    response_model=Dict[str, Any]
)
def add_highlights_batch(
    video_post_id: int,
    items: List[ReplayHighlightCreate] = Body(..., embed=True),
    db: Session = Depends(get_db),
    limit: int = Query(500, ge=1, le=1000),
):
    if not HL:
        raise HTTPException(status_code=500, detail="ReplayHighlight model haijapatikana")

    if not items:
        return {"inserted": 0}
    items = items[:limit]

    now = _utc()
    inserted = 0
    for it in items:
        row = HL(
            video_post_id=video_post_id,
            title=it.title.strip(),
            timestamp=float(it.timestamp) if hasattr(HL, "timestamp") else None,
        )
        if hasattr(HL, "created_at") and not getattr(row, "created_at", None):
            row.created_at = now
        if hasattr(HL, "updated_at"):
            row.updated_at = now
        db.add(row)
        inserted += 1

    db.commit()
    return {"inserted": inserted}

