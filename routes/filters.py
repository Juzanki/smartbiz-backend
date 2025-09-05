from __future__ import annotations
# backend/routes/filters.py
import hashlib
from datetime import datetime, timezone
from typing import Optional, List, Any, Dict
from contextlib import suppress

from fastapi import (
    APIRouter, Depends, HTTPException, status, Query, Header, Response, Path
)
from sqlalchemy.orm import Session
from sqlalchemy import JSON as SA_JSON
try:
    from sqlalchemy.dialects.postgresql import JSONB  # type: ignore
    JSON_VARIANT = SA_JSON().with_variant(JSONB, "postgresql")
except Exception:  # pragma: no cover
    JSON_VARIANT = SA_JSON()
from sqlalchemy import func, and_, or_
from sqlalchemy import JSON as SA_JSON
try:
    from sqlalchemy.dialects.postgresql import JSONB  # type: ignore
    JSON_VARIANT = SA_JSON().with_variant(JSONB, "postgresql")
except Exception:  # pragma: no cover
    JSON_VARIANT = SA_JSON()

from backend.db import get_db

# ---------- Auth (ikipo) ----------
with suppress(Exception):
    from backend.auth import get_current_user  # lazima irejeshe user obj yenye .id, .role

# ---------- Schemas (fallback kama hazipo) ----------
try:
    from backend.schemas.filter import FilterCreate, FilterOut, FilterUpdate
except Exception:
    from pydantic import BaseModel, Field

    class FilterCreate(BaseModel):
        name: str = Field(..., min_length=2, max_length=80)
        description: Optional[str] = None
        tags: Optional[List[str]] = None
        is_public: bool = False
        config_json: Optional[Dict[str, Any]] = None  # param za filter zako

    class FilterUpdate(BaseModel):
        name: Optional[str] = Field(None, min_length=2, max_length=80)
        description: Optional[str] = None
        tags: Optional[List[str]] = None
        is_public: Optional[bool] = None
        config_json: Optional[Dict[str, Any]] = None

    class FilterOut(FilterCreate):
        id: int
        user_id: Optional[int] = None
        created_at: Optional[datetime] = None
        updated_at: Optional[datetime] = None
        deleted_at: Optional[datetime] = None

# ---------- CRUD primary (kama zipo), vinginevyo ORM fallback ----------
with suppress(Exception):
    from backend.crud import filter_crud as _crud

CRUD_CREATE = getattr(_crud, "create_filter", None) if "_crud" in globals() else None
CRUD_LIST   = getattr(_crud, "get_all_filters", None) if "_crud" in globals() else None
CRUD_GET    = getattr(_crud, "get_filter_by_id", None) if "_crud" in globals() else None
CRUD_UPDATE = getattr(_crud, "update_filter", None) if "_crud" in globals() else None
CRUD_DELETE = getattr(_crud, "delete_filter", None) if "_crud" in globals() else None

FilterModel = None
with suppress(Exception):
    from backend.models.filter import Filter as FilterModel  # sqlalchemy model

router = APIRouter(prefix="/filters", tags=["Filters"])

# ================= Helpers =================
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _etag_row(name: str, updated_at: Any, cfg: Optional[Dict[str, Any]] = None) -> str:
    base = f"{name}|{updated_at or ''}|{hashlib.sha256(str(cfg or {}).encode()).hexdigest()[:16]}"
    return 'W/"' + hashlib.sha256(base.encode()).hexdigest()[:16] + '"'

def _etag_many(rows: List[Any]) -> str:
    if not rows:
        return 'W/"empty"'
    last = max(
        getattr(r, "updated_at", None) or getattr(r, "created_at", None) or datetime.min
        for r in rows
    )
    ids = ",".join(str(getattr(r, "id", 0)) for r in rows[:100])
    base = f"{ids}|{last}"
    return 'W/"' + hashlib.sha256(base.encode()).hexdigest()[:16] + '"'

def _serialize_one(row: Any) -> FilterOut:
    if hasattr(FilterOut, "model_validate"):
        return FilterOut.model_validate(row, from_attributes=True)  # pyd v2
    return FilterOut.model_validate(row)  # pyd v1

def _is_admin(user) -> bool:
    return bool(user and getattr(user, "role", None) in {"admin", "owner"})

# ================= CREATE (idempotent) =================
@router.post(
    "",
    response_model=FilterOut,
    status_code=status.HTTP_201_CREATED,
    summary="Unda filter (idempotent; scoped kwa user)"
)
def create_filter(
    filter_data: FilterCreate,
    response: Response,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user) if "get_current_user" in globals() else None,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    """Ikiwa jina lipo kwa user yuleyule na content ni ile ile Ã¢â€ â€™ rudisha tu rekodi (no duplicate)."""
    user_id = getattr(current_user, "id", None)

    # Tumia CRUD ikiwa ipo
    if CRUD_CREATE:
        row = CRUD_CREATE(db, filter_data)  # hakikisha CRUD yako inaweka user_id ndani
    else:
        if not FilterModel:
            raise HTTPException(status_code=500, detail="Filter storage not configured")

        q = db.query(FilterModel).filter(
            (FilterModel.user_id == user_id) if hasattr(FilterModel, "user_id") else True,
            func.lower(FilterModel.name) == filter_data.name.lower(),
        )
        existing = q.first()
        checksum_new = hashlib.sha256(
            (filter_data.name + "|" + str(filter_data.config_json or {})).encode()
        ).hexdigest()

        if existing:
            checksum_old = hashlib.sha256(
                (existing.name + "|" + str(getattr(existing, "config_json", {}) or {})).encode()
            ).hexdigest()
            # Idempotent same content
            if checksum_old == checksum_new:
                response.headers["ETag"] = _etag_row(
                    existing.name, getattr(existing, "updated_at", None), getattr(existing, "config_json", None)
                )
                response.headers["Cache-Control"] = "no-store"
                return _serialize_one(existing)
            # else: conflict name kwa user
            raise HTTPException(status_code=409, detail="Filter name already exists")

        row = FilterModel(**filter_data.dict())
        if hasattr(row, "user_id"):
            row.user_id = user_id
        if hasattr(row, "created_at"):
            row.created_at = _utcnow()
        if hasattr(row, "updated_at"):
            row.updated_at = _utcnow()
        db.add(row)
        db.commit()
        db.refresh(row)

    response.headers["ETag"] = _etag_row(
        getattr(row, "name", ""), getattr(row, "updated_at", None), getattr(row, "config_json", None)
    )
    response.headers["Cache-Control"] = "no-store"
    return _serialize_one(row)

# ================= LIST (paged + search + tags + sorting) =================
@router.get(
    "",
    response_model=List[FilterOut],
    summary="Orodha ya filters (mine au global); pagination + search + tags + ETag/304"
)
def list_filters(
    response: Response,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user) if "get_current_user" in globals() else None,
    q: Optional[str] = Query(None, description="Search kwa name/description"),
    tags: Optional[str] = Query(None, description="Comma-separated tags"),
    mine: bool = Query(True, description="Onyesha za kwangu pekee (isipokuwa admin)"),
    include_public: bool = Query(True, description="Jumuisha public filters"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    sort_by: str = Query("updated_at", description="name|created_at|updated_at"),
    order: str = Query("desc", regex="^(asc|desc)$"),
    if_none_match: Optional[str] = Header(None, alias="If-None-Match"),
):
    user_id = getattr(current_user, "id", None)

    if CRUD_LIST and not FilterModel:
        rows = CRUD_LIST(db) or []
        # rudimentary in-memory filters
        if mine and not _is_admin(current_user):
            rows = [r for r in rows if getattr(r, "user_id", None) == user_id]
        if include_public:
            # assume CRUD already includes; else ongeza hapa
            pass
        if q:
            ql = q.lower()
            rows = [r for r in rows if ql in getattr(r, "name", "").lower() or ql in (getattr(r, "description", "") or "").lower()]
        if tags:
            want = {t.strip().lower() for t in tags.split(",") if t.strip()}
            def has_tags(r):
                present = set((getattr(r, "tags", []) or []))
                present = {str(x).lower() for x in present}
                return bool(want & present)
            rows = [r for r in rows if has_tags(r)]
        rows = sorted(rows, key=lambda r: getattr(r, sort_by, getattr(r, "updated_at", None)) or 0, reverse=(order=="desc"))
        total = len(rows)
        rows = rows[offset: offset+limit]
    else:
        if not FilterModel:
            raise HTTPException(status_code=500, detail="Filter listing not configured")

        qy = db.query(FilterModel)
        # ownership / visibility
        conds = []
        if mine and not _is_admin(current_user):
            if hasattr(FilterModel, "user_id"):
                conds.append(FilterModel.user_id == user_id)
        else:
            # admin: show all + optionally public
            if not include_public and hasattr(FilterModel, "user_id"):
                conds.append(FilterModel.user_id == user_id)

        if conds:
            qy = qy.filter(and_(*conds))

        # search
        if q:
            like = f"%{q}%"
            parts = []
            if hasattr(FilterModel, "name"):        parts.append(FilterModel.name.ilike(like))
            if hasattr(FilterModel, "description"): parts.append(FilterModel.description.ilike(like))
            if parts:
                qy = qy.filter(or_(*parts))

        # tags (assuming tags stored as array/json or string JSON)
        if tags:
            want = [t.strip() for t in tags.split(",") if t.strip()]
            # simple approach: if you store JSON text, use ilike; for PG JSONB use containment operator @>
            if hasattr(FilterModel, "tags_json"):
                for t in want:
                    qy = qy.filter(getattr(FilterModel, "tags_json").ilike(f"%{t}%"))
            elif hasattr(FilterModel, "tags"):
                for t in want:
                    qy = qy.filter(getattr(FilterModel, "tags").ilike(f"%{t}%"))

        # hide soft-deleted
        if hasattr(FilterModel, "deleted_at"):
            qy = qy.filter(FilterModel.deleted_at.is_(None))

        # sorting whitelist
        sort_map = {
            "name": getattr(FilterModel, "name", None),
            "created_at": getattr(FilterModel, "created_at", None),
            "updated_at": getattr(FilterModel, "updated_at", None),
        }
        col = sort_map.get(sort_by) or getattr(FilterModel, "updated_at", getattr(FilterModel, "id"))
        qy = qy.order_by(col.asc() if order == "asc" else col.desc())

        total = qy.count()
        rows = qy.offset(offset).limit(limit).all()

    etag = _etag_many(rows)
    if if_none_match and if_none_match == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED)

    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "public, max-age=20"
    response.headers["X-Total-Count"]  = str(total)
    response.headers["X-Limit"]        = str(limit)
    response.headers["X-Offset"]       = str(offset)
    return [_serialize_one(r) for r in rows]

# ================= GET ONE =================
@router.get("/{filter_id}", response_model=FilterOut, summary="Pata filter moja")
def get_filter(
    filter_id: int = Path(..., ge=1),
    response: Response = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user) if "get_current_user" in globals() else None,
):
    # CRUD preferred
    if CRUD_GET and not FilterModel:
        row = CRUD_GET(db, filter_id)
    else:
        if not FilterModel:
            raise HTTPException(status_code=500, detail="Filter storage not configured")
        row = db.query(FilterModel).filter(FilterModel.id == filter_id).first()

    if not row:
        raise HTTPException(status_code=404, detail="Filter not found")

    # visibility check for non-admin: must be owner or public
    if not _is_admin(current_user):
        uid = getattr(current_user, "id", None)
        is_owner = hasattr(row, "user_id") and getattr(row, "user_id") == uid
        is_public = hasattr(row, "is_public") and bool(getattr(row, "is_public"))
        if not (is_owner or is_public):
            raise HTTPException(status_code=403, detail="Not allowed")

    if response is not None:
        response.headers["ETag"] = _etag_row(getattr(row, "name", ""), getattr(row, "updated_at", None), getattr(row, "config_json", None))
        response.headers["Cache-Control"] = "no-store"

    return _serialize_one(row)

# ================= UPDATE (If-Match) =================
@router.put("/{filter_id}", response_model=FilterOut, summary="Sasisha filter (optimistic via If-Match)")
def update_filter(
    filter_id: int,
    payload: FilterUpdate,
    response: Response,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user) if "get_current_user" in globals() else None,
    if_match: Optional[str] = Header(None, alias="If-Match"),
):
    if CRUD_UPDATE and not FilterModel:
        row = CRUD_UPDATE(db, filter_id, payload)
        if not row:
            raise HTTPException(status_code=404, detail="Filter not found")
    else:
        if not FilterModel:
            raise HTTPException(status_code=500, detail="Filter storage not configured")
        row = db.query(FilterModel).filter(FilterModel.id == filter_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="Filter not found")

        # ownership for non-admin
        if not _is_admin(current_user):
            if hasattr(row, "user_id") and getattr(row, "user_id") != getattr(current_user, "id", None):
                raise HTTPException(status_code=403, detail="Not allowed")

        # optimistic locking
        current_etag = _etag_row(getattr(row, "name", ""), getattr(row, "updated_at", None), getattr(row, "config_json", None))
        if if_match and if_match != current_etag:
            raise HTTPException(status_code=412, detail="ETag mismatch (record changed)")

        # update
        for k, v in payload.dict(exclude_unset=True).items():
            setattr(row, k, v)
        if hasattr(row, "updated_at"):
            row.updated_at = _utcnow()
        db.commit()
        db.refresh(row)

    response.headers["ETag"] = _etag_row(getattr(row, "name", ""), getattr(row, "updated_at", None), getattr(row, "config_json", None))
    response.headers["Cache-Control"] = "no-store"
    return _serialize_one(row)

# ================= DELETE (soft/hard) =================
@router.delete("/{filter_id}", response_model=dict, summary="Futa filter (soft delete ikiwa ina deleted_at)")
def delete_filter(
    filter_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user) if "get_current_user" in globals() else None,
):
    if CRUD_DELETE and not FilterModel:
        ok = CRUD_DELETE(db, filter_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Filter not found")
        return {"detail": "Filter deleted"}

    if not FilterModel:
        raise HTTPException(status_code=500, detail="Filter storage not configured")

    row = db.query(FilterModel).filter(FilterModel.id == filter_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Filter not found")

    # ownership for non-admin
    if not _is_admin(current_user):
        if hasattr(row, "user_id") and getattr(row, "user_id") != getattr(current_user, "id", None):
            raise HTTPException(status_code=403, detail="Not allowed")

    if hasattr(row, "deleted_at"):
        row.deleted_at = _utcnow()
        if hasattr(row, "updated_at"):
            row.updated_at = _utcnow()
        db.commit()
    else:
        db.delete(row)
        db.commit()
    return {"detail": "Filter deleted"}


