from __future__ import annotations
# backend/routes/replay_captions.py
import hashlib
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Any

from fastapi import (
    APIRouter, Depends, HTTPException, status, Response, Header, Query, Path
)
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session
from sqlalchemy import and_

from backend.db import get_db

# -------------------- Pydantic v2/v1 compatibility --------------------
try:
    # Pydantic v2
    from pydantic import BaseModel, Field, ConfigDict
    _P2 = True
except Exception:  # Pydantic v1 fallback
    from pydantic import BaseModel, Field  # type: ignore
    ConfigDict = dict  # type: ignore
    _P2 = False

# --------- Schemas (tumia zako; hizi ni fallback tu kama hazipo) ---------
with suppress(Exception):
    from backend.schemas.replay_caption_schemas import (  # type: ignore
        ReplayCaptionCreate, ReplayCaptionOut, ReplayCaptionUpdate
    )

if "ReplayCaptionCreate" not in globals():
    class ReplayCaptionCreate(BaseModel):
        stream_id: int = Field(..., ge=1)
        start: float = Field(..., ge=0)     # sekunde
        end: float = Field(..., gt=0)       # sekunde
        text: str = Field(..., min_length=1, max_length=2000)
        lang: str = Field("sw", min_length=2, max_length=8)

        if _P2:
            model_config = ConfigDict(extra="ignore")
        else:
            class Config:  # type: ignore
                orm_mode = True
                extra = "ignore"

    class ReplayCaptionUpdate(BaseModel):
        start: Optional[float] = Field(None, ge=0)
        end: Optional[float] = Field(None, gt=0)
        text: Optional[str] = Field(None, min_length=1, max_length=2000)
        lang: Optional[str] = Field(None, min_length=2, max_length=8)

        if _P2:
            model_config = ConfigDict(extra="ignore")
        else:
            class Config:  # type: ignore
                orm_mode = True
                extra = "ignore"

    class ReplayCaptionOut(ReplayCaptionCreate):
        id: int
        created_at: Optional[datetime] = None
        updated_at: Optional[datetime] = None

        # **Tumia config MOJA tu kulingana na toleo**
        if _P2:
            model_config = ConfigDict(from_attributes=True, extra="ignore")
        else:
            class Config:  # type: ignore
                orm_mode = True
                extra = "ignore"

# --------- Model ---------
RCModel = None
with suppress(Exception):
    from backend.models.replay_caption import ReplayCaption as RCModel  # type: ignore

router = APIRouter(prefix="/replay-captions", tags=["Replay Captions"])

# --------- Helpers ---------
def _utc() -> datetime:
    return datetime.now(timezone.utc)

def _etag_of(rows: List[Any]) -> str:
    if not rows:
        return 'W/"empty"'
    last = max(
        getattr(r, "updated_at", None)
        or getattr(r, "created_at", None)
        or datetime.min.replace(tzinfo=timezone.utc)
        for r in rows
    )
    seed = f"{len(rows)}|{last.isoformat()}"
    return 'W/"' + hashlib.sha256(seed.encode()).hexdigest()[:16] + '"'

def _serialize(obj: Any) -> ReplayCaptionOut:
    # v2: BaseModel.model_validate(..., from_attributes=True)
    if hasattr(ReplayCaptionOut, "model_validate"):
        try:
            return ReplayCaptionOut.model_validate(obj, from_attributes=True)  # type: ignore
        except TypeError:
            return ReplayCaptionOut.model_validate(obj)  # type: ignore
    return ReplayCaptionOut.model_validate(obj)  # type: ignore

def _validate_times(start: float, end: float):
    if end <= start:
        raise HTTPException(status_code=400, detail="`end` must be greater than `start`")

# ============================ CREATE (idempotent + dedup) ============================
@router.post(
    "",
    response_model=ReplayCaptionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Ongeza caption mpya (idempotent + dedup window)"
)
def add_caption(
    data: ReplayCaptionCreate,
    db: Session = Depends(get_db),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    dedup_seconds: int = Query(10, ge=0, le=600, description="Dirisha la sekunde za dedup")
):
    if not RCModel:
        raise HTTPException(status_code=500, detail="ReplayCaption model not configured")

    _validate_times(data.start, data.end)

    # Idempotency key (ikiwa una column hiyo)
    if idempotency_key and hasattr(RCModel, "idempotency_key"):
        existing = (
            db.query(RCModel)
            .filter(RCModel.stream_id == data.stream_id, RCModel.idempotency_key == idempotency_key)
            .first()
        )
        if existing:
            return _serialize(existing)

    # Dedup window (text+lang+start karibu katika muda mfupi)
    if dedup_seconds > 0:
        win_start = _utc() - timedelta(seconds=dedup_seconds)
        q = db.query(RCModel).filter(
            RCModel.stream_id == data.stream_id,
            RCModel.lang == data.lang.lower(),
            RCModel.text == data.text.strip(),
            RCModel.start.between(data.start - 0.2, data.start + 0.2),
        )
        if hasattr(RCModel, "created_at"):
            q = q.filter(RCModel.created_at >= win_start)
        dup = q.first()
        if dup:
            return _serialize(dup)

    row = RCModel(
        stream_id=int(data.stream_id),
        start=float(data.start),
        end=float(data.end),
        text=data.text.strip(),
        lang=data.lang.lower(),
    )
    if idempotency_key and hasattr(RCModel, "idempotency_key"):
        row.idempotency_key = idempotency_key
    now = _utc()
    if hasattr(RCModel, "created_at") and not getattr(row, "created_at", None):
        row.created_at = now
    if hasattr(RCModel, "updated_at"):
        row.updated_at = now

    db.add(row)
    db.commit()
    db.refresh(row)
    return _serialize(row)

# ============================ LIST ============================
@router.get(
    "/{stream_id}",
    response_model=List[ReplayCaptionOut],
    summary="Orodha ya captions kwa stream (filters + pagination + ETag)"
)
def get_captions(
    stream_id: int = Path(..., ge=1),
    response: Response = None,  # FastAPI injects Response; no Optional/default needed, but keep param present
    db: Session = Depends(get_db),
    lang: Optional[str] = Query(None, min_length=2, max_length=8),
    q: Optional[str] = Query(None, description="tafuta kwenye maandishi"),
    overlaps: Optional[float] = Query(None, ge=0, description="rudi tu zinazogusa timestamp hii (sekunde)"),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    order: str = Query("asc", pattern="^(asc|desc)$"),
    if_none_match: Optional[str] = Header(None, alias="If-None-Match"),
):
    if not RCModel:
        raise HTTPException(status_code=500, detail="ReplayCaption model not configured")

    qry = db.query(RCModel).filter(RCModel.stream_id == stream_id)
    if lang:
        qry = qry.filter(RCModel.lang == lang.lower())
    if q:
        like = f"%{q}%"
        qry = qry.filter(RCModel.text.ilike(like))
    if overlaps is not None:
        qry = qry.filter(and_(RCModel.start <= overlaps, RCModel.end > overlaps))

    order_col = getattr(RCModel, "start", getattr(RCModel, "id"))
    qry = qry.order_by(order_col.asc() if order == "asc" else order_col.desc())

    total = qry.count()
    rows = qry.offset(offset).limit(limit).all()

    etag = _etag_of(rows)
    if if_none_match and if_none_match == etag:
        return Response(status_code=304)
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "public, max-age=20"
    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Limit"] = str(limit)
    response.headers["X-Offset"] = str(offset)
    return [_serialize(r) for r in rows]

# ============================ UPDATE (PATCH) ============================
@router.patch(
    "/{caption_id}",
    response_model=ReplayCaptionOut,
    summary="Sasisha caption (partial)"
)
def update_caption(
    caption_id: int = Path(..., ge=1),
    payload: ReplayCaptionUpdate = ...,
    db: Session = Depends(get_db),
):
    if not RCModel:
        raise HTTPException(status_code=500, detail="ReplayCaption model not configured")

    row = db.query(RCModel).filter(RCModel.id == caption_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Caption not found")

    data = payload.dict(exclude_unset=True)
    if "text" in data and data["text"] is not None:
        data["text"] = data["text"].strip()

    if ("start" in data and data["start"] is not None) or ("end" in data and data["end"] is not None):
        start = float(data.get("start", getattr(row, "start")))
        end = float(data.get("end", getattr(row, "end")))
        _validate_times(start, end)

    for k, v in data.items():
        if hasattr(row, k):
            setattr(row, k, v)

    if hasattr(row, "updated_at"):
        row.updated_at = _utc()

    db.commit()
    db.refresh(row)
    return _serialize(row)

# ============================ DELETE ============================
@router.delete(
    "/{caption_id}",
    response_model=dict,
    status_code=status.HTTP_200_OK,
    summary="Futa caption"
)
def delete_caption(
    caption_id: int = Path(..., ge=1),
    db: Session = Depends(get_db),
):
    if not RCModel:
        raise HTTPException(status_code=500, detail="ReplayCaption model not configured")
    row = db.query(RCModel).filter(RCModel.id == caption_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Caption not found")
    db.delete(row)
    db.commit()
    return {"detail": "deleted"}

# ============================ EXPORT: WebVTT ============================
def _fmt_ts(sec: float) -> str:
    # WebVTT "HH:MM:SS.mmm"
    if sec < 0:
        sec = 0
    ms = int(round((sec - int(sec)) * 1000))
    s = int(sec) % 60
    m = (int(sec) // 60) % 60
    h = int(sec) // 3600
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"

@router.get(
    "/{stream_id}.vtt",
    response_class=PlainTextResponse,
    summary="Pakua captions kama WebVTT (kwa player)"
)
def export_vtt(
    stream_id: int = Path(..., ge=1),
    db: Session = Depends(get_db),
    lang: Optional[str] = Query(None, min_length=2, max_length=8),
):
    if not RCModel:
        raise HTTPException(status_code=500, detail="ReplayCaption model not configured")

    qry = db.query(RCModel).filter(RCModel.stream_id == stream_id)
    if lang:
        qry = qry.filter(RCModel.lang == lang.lower())
    rows = qry.order_by(RCModel.start.asc()).all()

    lines = ["WEBVTT", ""]
    for i, r in enumerate(rows, 1):
        start = _fmt_ts(getattr(r, "start", 0.0))
        end = _fmt_ts(getattr(r, "end", 0.0))
        text = getattr(r, "text", "")
        lines.append(str(i))
        lines.append(f"{start} --> {end}")
        lines.append(text)
        lines.append("")  # blank separator

    return "\n".join(lines)

# ============================ HEAD (ETag quick) ============================
@router.head(
    "/{stream_id}",
    include_in_schema=False
)
def head_stream(
    stream_id: int = Path(..., ge=1),
    db: Session = Depends(get_db),
):
    if not RCModel:
        raise HTTPException(status_code=500, detail="ReplayCaption model not configured")
    last = (
        db.query(RCModel)
        .filter(RCModel.stream_id == stream_id)
        .order_by(getattr(RCModel, "updated_at", getattr(RCModel, "id")).desc())
        .limit(1)
        .all()
    )
    etag = _etag_of(last)
    return Response(status_code=204, headers={"ETag": etag, "Cache-Control": "public, max-age=20"})
