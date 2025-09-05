from __future__ import annotations
# backend/routes/recorded_streams.py
import hashlib
from contextlib import suppress
from datetime import datetime, timezone
from typing import Optional, List, Any, Dict

from fastapi import (
    APIRouter, Depends, HTTPException, status, Response, Query, Header, Path
)
from sqlalchemy.orm import Session
from sqlalchemy import or_

from backend.db import get_db

# --------- Auth (robust import) ---------
get_current_user = None
with suppress(Exception):
    from backend.auth import get_current_user as _gcu  # preferred
    get_current_user = _gcu
with suppress(Exception):
    if not get_current_user:
        from backend.dependencies import get_current_user as _gcu2
        get_current_user = _gcu2

# --------- Schemas (use your own; fallback if missing) ---------
with suppress(Exception):
    from backend.schemas.recorded_stream_schemas import (
        RecordedStreamCreate, RecordedStreamOut, RecordedStreamUpdate
    )

if "RecordedStreamCreate" not in globals():
    from pydantic import BaseModel
    class RecordedStreamCreate(BaseModel):
        stream_id: int
        title: Optional[str] = None
        description: Optional[str] = None
        video_url: Optional[str] = None
        thumbnail_url: Optional[str] = None
        duration_sec: Optional[int] = None
        is_public: bool = True
        tags: Optional[List[str]] = None

    class RecordedStreamUpdate(BaseModel):
        title: Optional[str] = None
        description: Optional[str] = None
        thumbnail_url: Optional[str] = None
        is_public: Optional[bool] = None
        tags: Optional[List[str]] = None
        status: Optional[str] = None  # draft|published|archived

    class RecordedStreamOut(RecordedStreamCreate):
        id: int
        owner_id: Optional[int] = None
        status: str = "draft"
        created_at: Optional[datetime] = None
        updated_at: Optional[datetime] = None
        class Config: orm_mode = True
        model_config = {"from_attributes": True}

# --------- Model / CRUD (use if available) ---------
RSModel = None
with suppress(Exception):
    from backend.models.recorded_stream import RecordedStream as RSModel  # type: ignore

with suppress(Exception):
    from backend.crud import recorded_stream_crud as _crud

CRUD_CREATE   = getattr(_crud, "create_recording", None) if "_crud" in globals() else None
CRUD_GET_BY_STREAM = getattr(_crud, "get_recording_by_stream", None) if "_crud" in globals() else None
CRUD_GET      = getattr(_crud, "get_recording", None) if "_crud" in globals() else None
CRUD_LIST     = getattr(_crud, "list_recordings", None) if "_crud" in globals() else None
CRUD_UPDATE   = getattr(_crud, "update_recording", None) if "_crud" in globals() else None
CRUD_DELETE   = getattr(_crud, "delete_recording", None) if "_crud" in globals() else None
CRUD_PUBLISH  = getattr(_crud, "publish_recording", None) if "_crud" in globals() else None
CRUD_UNPUBLISH= getattr(_crud, "unpublish_recording", None) if "_crud" in globals() else None

router = APIRouter(prefix="/recordings", tags=["Recorded Streams"])

# --------- Helpers ---------
def _utc() -> datetime:
    return datetime.now(timezone.utc)

def _etag_of(rows: List[Any]) -> str:
    if not rows:
        return 'W/"empty"'
    last = max(
        getattr(r, "updated_at", None)
        or getattr(r, "created_at", None)
        or datetime.min
        for r in rows
    )
    seed = ",".join(str(getattr(r, "id", "?")) for r in rows[:200]) + "|" + last.isoformat()
    return 'W/"' + hashlib.sha256(seed.encode()).hexdigest()[:16] + '"'

def _serialize(obj: Any) -> RecordedStreamOut:
    if hasattr(RecordedStreamOut, "model_validate"):
        return RecordedStreamOut.model_validate(obj, from_attributes=True)
    return RecordedStreamOut.model_validate(obj)

def _assert_owner_or_admin(obj: Any, user: Any):
    owner_id = getattr(obj, "owner_id", None)
    role = getattr(user, "role", "")
    if owner_id and owner_id != getattr(user, "id", None) and role not in ("admin", "owner"):
        raise HTTPException(status_code=403, detail="Not allowed on this recording")

# =============================================================================
# CREATE
# =============================================================================
@router.post(
    "",
    response_model=RecordedStreamOut,
    status_code=status.HTTP_201_CREATED,
    summary="Pakia/unda rekodi mpya ya live (metadata + URLs)"
)
def create_recording(
    payload: RecordedStreamCreate,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user) if get_current_user else None,
):
    if CRUD_CREATE:
        # hakikisha owner_id unaingia kwenye payload pale inapowezekana
        data: Dict = payload.dict()
        if current_user and "owner_id" not in data:
            data["owner_id"] = getattr(current_user, "id", None)
        row = CRUD_CREATE(db, RecordedStreamCreate(**data))
        return _serialize(row)

    if not RSModel:
        raise HTTPException(status_code=500, detail="Recorded stream storage not configured")

    row = RSModel(
        stream_id=payload.stream_id,
        title=payload.title,
        description=payload.description,
        video_url=payload.video_url,
        thumbnail_url=payload.thumbnail_url,
        duration_sec=payload.duration_sec,
        is_public=payload.is_public,
        tags=payload.tags,
        status="draft",
        created_at=_utc(),
        updated_at=_utc(),
    )
    if current_user and hasattr(row, "owner_id"):
        row.owner_id = getattr(current_user, "id", None)

    db.add(row)
    db.commit()
    db.refresh(row)
    return _serialize(row)

# =============================================================================
# GET BY STREAM_ID (existing route parity)
# =============================================================================
@router.get(
    "/stream/{stream_id}",
    response_model=RecordedStreamOut,
    summary="Pata rekodi kwa stream_id"
)
def get_by_stream_id(
    stream_id: int,
    response: Response,
    db: Session = Depends(get_db),
    if_none_match: Optional[str] = Header(None, alias="If-None-Match"),
):
    row = None
    if CRUD_GET_BY_STREAM:
        row = CRUD_GET_BY_STREAM(db, stream_id)
    else:
        if not RSModel:
            raise HTTPException(status_code=500, detail="Recorded stream storage not configured")
        row = db.query(RSModel).filter(RSModel.stream_id == stream_id).first()

    if not row:
        raise HTTPException(status_code=404, detail="Recording not found")

    etag = _etag_of([row])
    if if_none_match and if_none_match == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED)
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "public, max-age=30"
    return _serialize(row)

# =============================================================================
# GET BY ID
# =============================================================================
@router.get(
    "/{recording_id}",
    response_model=RecordedStreamOut,
    summary="Pata rekodi kwa ID"
)
def get_by_id(
    recording_id: int = Path(..., ge=1),
    response: Response = None,
    db: Session = Depends(get_db),
    if_none_match: Optional[str] = Header(None, alias="If-None-Match"),
):
    row = None
    if CRUD_GET:
        row = CRUD_GET(db, recording_id)
    else:
        if not RSModel:
            raise HTTPException(status_code=500, detail="Recorded stream storage not configured")
        row = db.query(RSModel).filter(RSModel.id == recording_id).first()

    if not row:
        raise HTTPException(status_code=404, detail="Recording not found")

    etag = _etag_of([row])
    if if_none_match and if_none_match == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED)
    if response:
        response.headers["ETag"] = etag
        response.headers["Cache-Control"] = "public, max-age=30"
    return _serialize(row)

# =============================================================================
# LIST (pagination + filters + ETag)
# =============================================================================
@router.get(
    "",
    response_model=List[RecordedStreamOut],
    summary="Orodha ya rekodi (tafuta, paginate, mine)"
)
def list_recordings(
    response: Response,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user) if get_current_user else None,
    mine: bool = Query(False, description="Ikiwa True, rudisha za mtumiaji aliyeingia"),
    user_id: Optional[int] = Query(None, ge=1, description="Chuja kwa owner"),
    q: Optional[str] = Query(None, description="tafuta kwa title/description/tag"),
    status_eq: Optional[str] = Query(None, description="draft|published|archived"),
    public_only: bool = Query(False, description="Rudisha tu zilizo public"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    if_none_match: Optional[str] = Header(None, alias="If-None-Match"),
):
    rows: List[Any] = []
    total = 0

    if CRUD_LIST and not RSModel:
        rows = CRUD_LIST(db, user_id=user_id, mine=mine, q=q, status_eq=status_eq,
                         public_only=public_only, limit=limit, offset=offset) or []
        total = len(rows) if rows and len(rows) < limit else offset + len(rows)
    else:
        if not RSModel:
            raise HTTPException(status_code=500, detail="Recorded stream storage not configured")
        qry = db.query(RSModel)
        # Access rules: ikiwa si public na si owner/admin â†’ iondoe
        if public_only and hasattr(RSModel, "is_public"):
            qry = qry.filter(RSModel.is_public.is_(True))
        if mine and current_user and hasattr(RSModel, "owner_id"):
            qry = qry.filter(RSModel.owner_id == getattr(current_user, "id", None))
        elif user_id and hasattr(RSModel, "owner_id"):
            qry = qry.filter(RSModel.owner_id == user_id)
        if status_eq and hasattr(RSModel, "status"):
            qry = qry.filter(RSModel.status == status_eq)
        if q:
            like = f"%{q}%"
            ors = []
            for col in ("title", "description"):
                if hasattr(RSModel, col):
                    ors.append(getattr(RSModel, col).ilike(like))
            if hasattr(RSModel, "tags"):
                # rudisha kwa tags JSON/text mnapotumia
                ors.append(getattr(RSModel, "tags").cast(str).ilike(like))  # best-effort
            if ors:
                qry = qry.filter(or_(*ors))
        # Order na paginate
        order_col = getattr(RSModel, "updated_at", getattr(RSModel, "created_at", getattr(RSModel, "id")))
        qry = qry.order_by(order_col.desc())
        total = qry.count()
        rows = qry.offset(offset).limit(limit).all()

    etag = _etag_of(rows)
    if if_none_match and if_none_match == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED)

    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "public, max-age=15"
    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Limit"] = str(limit)
    response.headers["X-Offset"] = str(offset)
    return [_serialize(r) for r in rows]

# =============================================================================
# PATCH (partial update)
# =============================================================================
@router.patch(
    "/{recording_id}",
    response_model=RecordedStreamOut,
    summary="Sasisha metadata/status ya rekodi (partial)"
)
def update_recording(
    recording_id: int,
    payload: RecordedStreamUpdate,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user) if get_current_user else None,
):
    if CRUD_UPDATE and not RSModel:
        row = CRUD_UPDATE(db, recording_id, payload)
        if not row:
            raise HTTPException(status_code=404, detail="Recording not found")
        if current_user:
            _assert_owner_or_admin(row, current_user)
        return _serialize(row)

    if not RSModel:
        raise HTTPException(status_code=500, detail="Recorded stream storage not configured")

    row = db.query(RSModel).filter(RSModel.id == recording_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Recording not found")
    if current_user:
        _assert_owner_or_admin(row, current_user)

    data = payload.dict(exclude_unset=True)
    for k, v in data.items():
        if hasattr(row, k):
            setattr(row, k, v)
    if hasattr(row, "updated_at"):
        row.updated_at = _utc()
    db.commit()
    db.refresh(row)
    return _serialize(row)

# =============================================================================
# PUBLISH / UNPUBLISH
# =============================================================================
@router.post(
    "/{recording_id}/publish",
    response_model=RecordedStreamOut,
    summary="Chapisha rekodi (iwapo tayari)"
)
def publish_recording(
    recording_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user) if get_current_user else None,
):
    if CRUD_PUBLISH:
        row = CRUD_PUBLISH(db, recording_id)
        if not row:
            raise HTTPException(status_code=404, detail="Recording not found")
        if current_user:
            _assert_owner_or_admin(row, current_user)
        return _serialize(row)

    if not RSModel:
        raise HTTPException(status_code=500, detail="Recorded stream storage not configured")
    row = db.query(RSModel).filter(RSModel.id == recording_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Recording not found")
    if current_user:
        _assert_owner_or_admin(row, current_user)

    if hasattr(row, "status"): row.status = "published"
    if hasattr(row, "updated_at"): row.updated_at = _utc()
    db.commit(); db.refresh(row)
    return _serialize(row)

@router.post(
    "/{recording_id}/unpublish",
    response_model=RecordedStreamOut,
    summary="Ondoa kwenye hadhara (unpublish)"
)
def unpublish_recording(
    recording_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user) if get_current_user else None,
):
    if CRUD_UNPUBLISH:
        row = CRUD_UNPUBLISH(db, recording_id)
        if not row:
            raise HTTPException(status_code=404, detail="Recording not found")
        if current_user:
            _assert_owner_or_admin(row, current_user)
        return _serialize(row)

    if not RSModel:
        raise HTTPException(status_code=500, detail="Recorded stream storage not configured")
    row = db.query(RSModel).filter(RSModel.id == recording_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Recording not found")
    if current_user:
        _assert_owner_or_admin(row, current_user)

    if hasattr(row, "status"): row.status = "draft"
    if hasattr(row, "updated_at"): row.updated_at = _utc()
    db.commit(); db.refresh(row)
    return _serialize(row)

# =============================================================================
# DELETE (soft/hard)
# =============================================================================
@router.delete(
    "/{recording_id}",
    response_model=dict,
    summary="Futa rekodi (soft ikiwa column ipo, vinginevyo hard)"
)
def delete_recording(
    recording_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user) if get_current_user else None,
    hard: bool = Query(False, description="True => hard delete"),
):
    if CRUD_DELETE and not RSModel:
        ok = CRUD_DELETE(db, recording_id, hard=hard)
        if not ok:
            raise HTTPException(status_code=404, detail="Recording not found")
        return {"detail": "deleted"}

    if not RSModel:
        raise HTTPException(status_code=500, detail="Recorded stream storage not configured")
    row = db.query(RSModel).filter(RSModel.id == recording_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Recording not found")
    if current_user:
        _assert_owner_or_admin(row, current_user)

    # soft delete kama tuna column
    if not hard and hasattr(row, "deleted_at"):
        row.deleted_at = _utc()
        if hasattr(row, "updated_at"): row.updated_at = _utc()
        db.commit()
    else:
        db.delete(row)
        db.commit()
    return {"detail": "deleted"}

# =============================================================================
# HEAD (ETag only)
# =============================================================================
@router.head(
    "/{recording_id}",
    include_in_schema=False
)
def head_recording(
    recording_id: int,
    db: Session = Depends(get_db),
):
    if CRUD_GET:
        row = CRUD_GET(db, recording_id)
    else:
        if not RSModel:
            raise HTTPException(status_code=500, detail="Recorded stream storage not configured")
        row = db.query(RSModel).filter(RSModel.id == recording_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Recording not found")
    etag = _etag_of([row])
    return Response(status_code=204, headers={"ETag": etag, "Cache-Control": "public, max-age=30"})

