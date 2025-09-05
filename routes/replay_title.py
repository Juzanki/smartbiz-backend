from __future__ import annotations
# backend/routes/replay_title.py
import hashlib
import re
from contextlib import suppress
from datetime import datetime, timezone
from typing import Optional, Any

from fastapi import (
    APIRouter, Depends, HTTPException, Header, Response,
    Query, status, Body, Path
)
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

# ============== Model & Schemas (with safe fallbacks) ==============
RTModel = None
with suppress(Exception):
    from backend.models.replay_title import ReplayTitle as RTModel  # type: ignore

with suppress(Exception):
    from backend.schemas.replay_title_schemas import (  # type: ignore
        ReplayTitleCreate, ReplayTitleOut, ReplayTitleUpdate
    )

if "ReplayTitleCreate" not in globals():
    class ReplayTitleCreate(BaseModel):
        stream_id: int = Field(..., ge=1)
        title: str = Field(..., min_length=1, max_length=200)
        lang: Optional[str] = Field("sw", min_length=2, max_length=8)

        if _P2:
            model_config = ConfigDict(extra="ignore")
        else:
            class Config:  # type: ignore
                orm_mode = True
                extra = "ignore"

    class ReplayTitleUpdate(BaseModel):
        title: Optional[str] = Field(None, min_length=1, max_length=200)
        lang: Optional[str] = Field(None, min_length=2, max_length=8)

        if _P2:
            model_config = ConfigDict(extra="ignore")
        else:
            class Config:  # type: ignore
                orm_mode = True
                extra = "ignore"

    class ReplayTitleOut(ReplayTitleCreate):
        id: int
        slug: Optional[str] = None
        created_at: Optional[datetime] = None
        updated_at: Optional[datetime] = None

        # TUMIA config MOJA kulingana na toleo
        if _P2:
            model_config = ConfigDict(from_attributes=True, extra="ignore")
        else:
            class Config:  # type: ignore
                orm_mode = True
                extra = "ignore"

router = APIRouter(prefix="/replay-title", tags=["Replay Title"])

# ============== Helpers ==============
def _utc() -> datetime:
    return datetime.now(timezone.utc)

def _slugify(text: str, max_len: int = 96) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"\s+", "-", text).strip("-")
    return text[:max_len] or "untitled"

def _etag_of(obj: Any) -> str:
    if not obj:
        return 'W/"empty"'
    ts = getattr(obj, "updated_at", None) or getattr(obj, "created_at", None) or _utc()
    title = getattr(obj, "title", "") or ""
    seed = f"{ts.isoformat()}|{len(title)}|{getattr(obj, 'stream_id', '')}"
    return 'W/"' + hashlib.sha256(seed.encode()).hexdigest()[:16] + '"'

def _serialize(obj: Any) -> ReplayTitleOut:
    # v2: model_validate(..., from_attributes=True)
    if hasattr(ReplayTitleOut, "model_validate"):
        try:
            return ReplayTitleOut.model_validate(obj, from_attributes=True)  # type: ignore
        except TypeError:
            return ReplayTitleOut.model_validate(obj)  # type: ignore
    return ReplayTitleOut.model_validate(obj)  # type: ignore

# ============== POST /  (Upsert + Idempotency + Auto Slug) ==============
@router.post(
    "",
    response_model=ReplayTitleOut,
    status_code=status.HTTP_201_CREATED,
    summary="Hifadhi au sasisha kichwa cha replay (upsert)"
)
def save_title(
    data: ReplayTitleCreate,
    db: Session = Depends(get_db),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    auto_slug: bool = Query(True, description="Unda/boresha slug kiotomatiki")
):
    if not RTModel:
        raise HTTPException(status_code=500, detail="ReplayTitle model haijapatikana")

    # Idempotency (kama una kolamu `idempotency_key`)
    if idempotency_key and hasattr(RTModel, "idempotency_key"):
        hit = (
            db.query(RTModel)
            .filter(RTModel.stream_id == data.stream_id,
                    RTModel.idempotency_key == idempotency_key)
            .first()
        )
        if hit:
            return _serialize(hit)

    row = db.query(RTModel).filter(RTModel.stream_id == data.stream_id).first()
    now = _utc()

    if row:
        row.title = data.title.strip()
        if hasattr(row, "lang") and data.lang is not None:
            row.lang = data.lang
        if auto_slug and hasattr(row, "slug"):
            row.slug = _slugify(row.title)
        if hasattr(row, "updated_at"):
            row.updated_at = now
        if idempotency_key and hasattr(RTModel, "idempotency_key"):
            row.idempotency_key = idempotency_key
        db.commit()
        db.refresh(row)
        return _serialize(row)

    # create mpya
    row = RTModel(stream_id=data.stream_id, title=data.title.strip())
    if hasattr(row, "lang"):
        row.lang = data.lang
    if auto_slug and hasattr(row, "slug"):
        row.slug = _slugify(data.title)
    if hasattr(row, "created_at") and not getattr(row, "created_at", None):
        row.created_at = now
    if hasattr(row, "updated_at"):
        row.updated_at = now
    if idempotency_key and hasattr(RTModel, "idempotency_key"):
        row.idempotency_key = idempotency_key

    db.add(row)
    db.commit()
    db.refresh(row)
    return _serialize(row)

# ============== GET /{stream_id}  (ETag + 304) ==============
@router.get(
    "/{stream_id}",
    response_model=ReplayTitleOut,
    summary="Pata kichwa cha stream (ETag caching)"
)
def get_title(
    stream_id: int = Path(..., ge=1),
    response: Response = None,
    db: Session = Depends(get_db),
    if_none_match: Optional[str] = Header(None, alias="If-None-Match"),
):
    if not RTModel:
        raise HTTPException(status_code=500, detail="ReplayTitle model haijapatikana")

    row = db.query(RTModel).filter(RTModel.stream_id == stream_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Title not found")

    tag = _etag_of(row)
    if if_none_match and if_none_match == tag:
        return Response(status_code=304)
    response.headers["ETag"] = tag
    response.headers["Cache-Control"] = "public, max-age=60"
    return _serialize(row)

# ============== PATCH /{stream_id}  (Partial Update + Auto Slug) ==============
@router.patch(
    "/{stream_id}",
    response_model=ReplayTitleOut,
    summary="Sasisha kichwa cha stream (sehemu tu)"
)
def update_title(
    stream_id: int = Path(..., ge=1),
    payload: ReplayTitleUpdate = Body(...),
    db: Session = Depends(get_db),
    auto_slug: bool = Query(True),
):
    if not RTModel:
        raise HTTPException(status_code=500, detail="ReplayTitle model haijapatikana")

    row = db.query(RTModel).filter(RTModel.stream_id == stream_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Title not found")

    data = payload.dict(exclude_unset=True)
    if "title" in data and data["title"] is not None:
        row.title = data["title"].strip()
        if auto_slug and hasattr(row, "slug"):
            row.slug = _slugify(row.title)
    if "lang" in data and hasattr(row, "lang"):
        row.lang = data["lang"]
    if hasattr(row, "updated_at"):
        row.updated_at = _utc()

    db.commit()
    db.refresh(row)
    return _serialize(row)

# ============== DELETE /{stream_id} ==============
@router.delete(
    "/{stream_id}",
    response_model=dict,
    summary="Futa kichwa cha stream"
)
def delete_title(
    stream_id: int = Path(..., ge=1),
    db: Session = Depends(get_db),
):
    if not RTModel:
        raise HTTPException(status_code=500, detail="ReplayTitle model haijapatikana")

    row = db.query(RTModel).filter(RTModel.stream_id == stream_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Title not found")
    db.delete(row)
    db.commit()
    return {"detail": "deleted"}
