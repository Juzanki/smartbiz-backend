from __future__ import annotations
# backend/routes/replay_summary.py
import hashlib
from contextlib import suppress
from datetime import datetime, timezone
from typing import Optional, List, Any

from fastapi import APIRouter, Depends, HTTPException, Header, Response, Query, status, Path
from sqlalchemy.orm import Session

from backend.db import get_db

# ---------------- Pydantic v2/v1 compatibility ----------------
try:
    from pydantic import BaseModel, Field, ConfigDict  # v2
    _P2 = True
except Exception:  # v1 fallback
    from pydantic import BaseModel, Field  # type: ignore
    ConfigDict = dict  # type: ignore
    _P2 = False

# ---------- Models & Schemas ----------
RSModel = None
with suppress(Exception):
    from backend.models.replay_summary import ReplaySummary as RSModel  # type: ignore

with suppress(Exception):
    from backend.schemas.replay_summary_schemas import (  # type: ignore
        ReplaySummaryCreate, ReplaySummaryOut, ReplaySummaryUpdate
    )

# Fallbacks endapo schema zako hazijapakiwa bado:
if "ReplaySummaryCreate" not in globals():
    class ReplaySummaryCreate(BaseModel):
        stream_id: int = Field(..., ge=1)
        summary: str = Field(..., min_length=1, max_length=5000)
        lang: str = Field("sw", min_length=2, max_length=8)
        key_points: Optional[List[str]] = None

        if _P2:
            model_config = ConfigDict(extra="ignore")
        else:
            class Config:  # type: ignore
                orm_mode = True
                extra = "ignore"

    class ReplaySummaryUpdate(BaseModel):
        summary: Optional[str] = Field(None, min_length=1, max_length=5000)
        lang: Optional[str] = Field(None, min_length=2, max_length=8)
        key_points: Optional[List[str]] = None

        if _P2:
            model_config = ConfigDict(extra="ignore")
        else:
            class Config:  # type: ignore
                orm_mode = True
                extra = "ignore"

    class ReplaySummaryOut(ReplaySummaryCreate):
        id: int
        created_at: Optional[datetime] = None
        updated_at: Optional[datetime] = None

        # TUMIA config MOJA kulingana na toleo
        if _P2:
            model_config = ConfigDict(from_attributes=True, extra="ignore")
        else:
            class Config:  # type: ignore
                orm_mode = True
                extra = "ignore"

router = APIRouter(prefix="/replay-summary", tags=["Replay Summary"])

# ---------- Helpers ----------
def _utc() -> datetime:
    return datetime.now(timezone.utc)

def _etag_of(obj: Any) -> str:
    """
    ETag thabiti kwa 304 Not Modified.
    """
    if not obj:
        return 'W/"empty"'
    ts = getattr(obj, "updated_at", None) or getattr(obj, "created_at", None) or _utc()
    text = getattr(obj, "summary", "") or ""
    seed = f"{ts.isoformat()}|{len(text)}|{getattr(obj, 'stream_id', '')}"
    return 'W/"' + hashlib.sha256(seed.encode()).hexdigest()[:16] + '"'

def _serialize(obj: Any) -> ReplaySummaryOut:
    # v2: model_validate(..., from_attributes=True)
    if hasattr(ReplaySummaryOut, "model_validate"):
        try:
            return ReplaySummaryOut.model_validate(obj, from_attributes=True)  # type: ignore
        except TypeError:
            return ReplaySummaryOut.model_validate(obj)  # type: ignore
    # v1
    return ReplaySummaryOut.model_validate(obj)  # type: ignore

# =====================================================================
# CREATE/UPSERT  (idempotent)  -> POST /
# =====================================================================
@router.post(
    "",
    response_model=ReplaySummaryOut,
    status_code=status.HTTP_201_CREATED,
    summary="Hifadhi au sasisha muhtasari (upsert + idempotent)"
)
def save_summary(
    data: ReplaySummaryCreate,
    db: Session = Depends(get_db),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    upsert: bool = Query(True, description="True: tengeneza au sasisha kama ipo")
):
    if not RSModel:
        raise HTTPException(status_code=500, detail="ReplaySummary model haijapatikana")

    # Idempotency kupitia kolamu ya DB ikiwa inapatikana
    if idempotency_key and hasattr(RSModel, "idempotency_key"):
        existing_key = (
            db.query(RSModel)
            .filter(RSModel.stream_id == data.stream_id,
                    RSModel.idempotency_key == idempotency_key)
            .first()
        )
        if existing_key:
            return _serialize(existing_key)

    row = db.query(RSModel).filter(RSModel.stream_id == data.stream_id).first()
    now = _utc()

    if row:
        if not upsert:
            raise HTTPException(status_code=409, detail="Summary already exists for this stream_id")
        payload = data.dict(exclude_unset=True)
        for k, v in payload.items():
            if hasattr(row, k):
                setattr(row, k, v)
        if hasattr(row, "updated_at"):
            row.updated_at = now
        if idempotency_key and hasattr(RSModel, "idempotency_key"):
            row.idempotency_key = idempotency_key
        db.commit()
        db.refresh(row)
        return _serialize(row)

    # Create mpya
    row = RSModel(**data.dict())
    if hasattr(row, "created_at") and not getattr(row, "created_at", None):
        row.created_at = now
    if hasattr(row, "updated_at"):
        row.updated_at = now
    if idempotency_key and hasattr(RSModel, "idempotency_key"):
        row.idempotency_key = idempotency_key

    db.add(row)
    db.commit()
    db.refresh(row)
    return _serialize(row)

# =====================================================================
# GET /{stream_id}  (ETag + 304)
# =====================================================================
@router.get(
    "/{stream_id}",
    response_model=ReplaySummaryOut,
    summary="Pata muhtasari wa stream (ETag caching)"
)
def get_summary(
    stream_id: int = Path(..., ge=1),
    response: Response = None,
    db: Session = Depends(get_db),
    if_none_match: Optional[str] = Header(None, alias="If-None-Match"),
):
    if not RSModel:
        raise HTTPException(status_code=500, detail="ReplaySummary model haijapatikana")

    row = db.query(RSModel).filter(RSModel.stream_id == stream_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Summary not found")

    tag = _etag_of(row)
    if if_none_match and if_none_match == tag:
        return Response(status_code=304)

    response.headers["ETag"] = tag
    response.headers["Cache-Control"] = "public, max-age=60"
    return _serialize(row)

# =====================================================================
# PATCH /{stream_id}  (partial update)
# =====================================================================
@router.patch(
    "/{stream_id}",
    response_model=ReplaySummaryOut,
    summary="Sasisha sehemu ya muhtasari"
)
def update_summary(
    stream_id: int = Path(..., ge=1),
    payload: ReplaySummaryUpdate = ...,
    db: Session = Depends(get_db),
):
    if not RSModel:
        raise HTTPException(status_code=500, detail="ReplaySummary model haijapatikana")

    row = db.query(RSModel).filter(RSModel.stream_id == stream_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Summary not found")

    data = payload.dict(exclude_unset=True)
    for k, v in data.items():
        if hasattr(row, k):
            setattr(row, k, v)
    if hasattr(row, "updated_at"):
        row.updated_at = _utc()

    db.commit()
    db.refresh(row)
    return _serialize(row)

# =====================================================================
# DELETE /{stream_id}
# =====================================================================
@router.delete(
    "/{stream_id}",
    response_model=dict,
    summary="Futa muhtasari wa stream"
)
def delete_summary(
    stream_id: int = Path(..., ge=1),
    db: Session = Depends(get_db),
):
    if not RSModel:
        raise HTTPException(status_code=500, detail="ReplaySummary model haijapatikana")

    row = db.query(RSModel).filter(RSModel.stream_id == stream_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Summary not found")
    db.delete(row)
    db.commit()
    return {"detail": "deleted"}
