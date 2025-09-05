from __future__ import annotations
# backend/routes/post_live_notifications.py
import hashlib
from contextlib import suppress
from datetime import datetime, timezone
from typing import Optional, List, Any, Dict

from fastapi import (
    APIRouter, Depends, HTTPException, status, Query, Header, Response, Path
)
from sqlalchemy.orm import Session
from sqlalchemy import func

from backend.dependencies import get_db, get_current_user

# ====== Schemas (tumia zako; hizi ni fallback kama hazipo) ====================
try:
    from backend.schemas.post_live_notification_schemas import (
        PostLiveNotificationOut, PostLiveNotificationUpdate
    )
except Exception:
    from pydantic import BaseModel

    class PostLiveNotificationOut(BaseModel):
        id: int
        user_id: int
        post_id: Optional[int] = None
        title: Optional[str] = None
        message: Optional[str] = None
        is_read: bool = False
        created_at: Optional[datetime] = None
        updated_at: Optional[datetime] = None
        seen_at: Optional[datetime] = None

        class Config:
            orm_mode = True
        model_config = {"from_attributes": True}

    class PostLiveNotificationUpdate(BaseModel):
        is_read: Optional[bool] = None
        seen_at: Optional[datetime] = None

# ====== CRUD / Model fallbacks ===============================================
with suppress(Exception):
    from backend.crud import post_live_notification_crud as _crud

CRUD_LIST   = getattr(_crud, "get_user_notifications", None) if "_crud" in globals() else None
CRUD_COUNT  = getattr(_crud, "count_unread", None) if "_crud" in globals() else None
CRUD_MARK   = getattr(_crud, "mark_as_read", None) if "_crud" in globals() else None
CRUD_MARKALL= getattr(_crud, "mark_all_as_read", None) if "_crud" in globals() else None
CRUD_GETONE = getattr(_crud, "get_one", None) if "_crud" in globals() else None
CRUD_DELETE = getattr(_crud, "delete_one", None) if "_crud" in globals() else None

NotifModel = None
with suppress(Exception):
    from backend.models.post_live_notification import PostLiveNotification as NotifModel  # type: ignore

router = APIRouter(prefix="/notifications/live", tags=["Live Notifications"])

# ====== Helpers ===============================================================
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _serialize_many(rows: List[Any]) -> List[PostLiveNotificationOut]:
    out: List[PostLiveNotificationOut] = []
    for r in rows:
        if hasattr(PostLiveNotificationOut, "model_validate"):
            out.append(PostLiveNotificationOut.model_validate(r, from_attributes=True))  # pyd v2
        else:
            out.append(PostLiveNotificationOut.model_validate(r))  # pyd v1
    return out

def _etag_rows(rows: List[Any]) -> str:
    if not rows:
        return 'W/"empty"'
    last = max(
        getattr(r, "updated_at", None) or getattr(r, "created_at", None) or datetime.min
        for r in rows
    )
    ids = ",".join(str(getattr(r, "id", 0)) for r in rows[:200])
    base = f"{ids}|{last.isoformat()}"
    return 'W/"' + hashlib.sha256(base.encode()).hexdigest()[:16] + '"'

# ====== LIST: pagination + filters + ETag =====================================
@router.get(
    "",
    response_model=List[PostLiveNotificationOut],
    summary="Orodha ya notifications zangu (pagination, search, filters, ETag/304)"
)
def get_my_notifications(
    response: Response,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    unread_only: bool = Query(False),
    q: Optional[str] = Query(None, description="tafuta kwa title/message"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    sort_by: str = Query("created_at", description="created_at|updated_at|seen_at"),
    order: str = Query("desc", regex="^(asc|desc)$"),
    if_none_match: Optional[str] = Header(None, alias="If-None-Match"),
):
    # 1) Prefer CRUD if it already supports pagination/filters
    if CRUD_LIST and not NotifModel:
        rows = CRUD_LIST(db, current_user.id) or []
        # in-memory filters (fallback)
        if unread_only:
            rows = [r for r in rows if not bool(getattr(r, "is_read", False))]
        if q:
            ql = q.lower()
            rows = [r for r in rows if ql in (getattr(r, "title", "") or "").lower()
                                  or ql in (getattr(r, "message", "") or "").lower()]
        reverse = (order == "desc")
        key = lambda r: getattr(r, sort_by, getattr(r, "created_at", None)) or datetime.min
        rows = sorted(rows, key=key, reverse=reverse)
        total = len(rows)
        rows = rows[offset: offset + limit]
    else:
        if not NotifModel:
            raise HTTPException(status_code=500, detail="Notifications storage not configured")
        qry = db.query(NotifModel).filter(NotifModel.user_id == current_user.id)
        if unread_only and hasattr(NotifModel, "is_read"):
            qry = qry.filter(NotifModel.is_read.is_(False))
        if q:
            like = f"%{q}%"
            parts = []
            if hasattr(NotifModel, "title"):   parts.append(NotifModel.title.ilike(like))
            if hasattr(NotifModel, "message"): parts.append(NotifModel.message.ilike(like))
            if parts:
                from sqlalchemy import or_
                qry = qry.filter(or_(*parts))
        # soft-delete?
        if hasattr(NotifModel, "deleted_at"):
            qry = qry.filter(NotifModel.deleted_at.is_(None))

        # sorting whitelist
        cols = {
            "created_at": getattr(NotifModel, "created_at", None),
            "updated_at": getattr(NotifModel, "updated_at", None),
            "seen_at":    getattr(NotifModel, "seen_at", None),
        }
        col = cols.get(sort_by) or getattr(NotifModel, "created_at")
        qry = qry.order_by(col.asc() if order == "asc" else col.desc())

        total = qry.count()
        rows = qry.offset(offset).limit(limit).all()

    etag = _etag_rows(rows)
    if if_none_match and if_none_match == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED)

    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "public, max-age=10"
    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Limit"] = str(limit)
    response.headers["X-Offset"] = str(offset)
    return _serialize_many(rows)

# ====== UNREAD COUNT ==========================================================
@router.get("/unread-count", response_model=dict, summary="Hesabu notifications ambazo hazijasomwa")
def unread_count(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if CRUD_COUNT:
        n = CRUD_COUNT(db, current_user.id)
        return {"unread": int(n or 0)}
    if not NotifModel:
        raise HTTPException(status_code=500, detail="Notifications storage not configured")
    n = db.query(func.count(NotifModel.id)).filter(
        NotifModel.user_id == current_user.id,
        getattr(NotifModel, "is_read", False) == False  # noqa: E712
    ).scalar() or 0
    return {"unread": int(n)}

# ====== MARK ONE AS READ (idempotent) ========================================
@router.post("/{notification_id}/read", response_model=PostLiveNotificationOut, summary="Mark notification moja kuwa read")
def mark_read(
    notification_id: int = Path(..., ge=1),
    response: Response = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    # CRUD preferred
    if CRUD_MARK and CRUD_GETONE:
        row = CRUD_MARK(db, current_user.id, notification_id)
        if not row:
            raise HTTPException(status_code=404, detail="Notification not found")
    else:
        if not NotifModel:
            raise HTTPException(status_code=500, detail="Notifications storage not configured")
        row = db.query(NotifModel).filter(
            NotifModel.id == notification_id,
            NotifModel.user_id == current_user.id
        ).first()
        if not row:
            raise HTTPException(status_code=404, detail="Notification not found")
        if hasattr(row, "is_read") and not row.is_read:
            row.is_read = True
            if hasattr(row, "seen_at") and row.seen_at is None:
                row.seen_at = _utcnow()
            if hasattr(row, "updated_at"):
                row.updated_at = _utcnow()
            db.commit()
            db.refresh(row)

    if response is not None:
        response.headers["Cache-Control"] = "no-store"
    return PostLiveNotificationOut.model_validate(row, from_attributes=True) if hasattr(PostLiveNotificationOut, "model_validate") else PostLiveNotificationOut.model_validate(row)

# ====== MARK ALL AS READ ======================================================
@router.post("/read-all", response_model=dict, summary="Mark notifications zote kuwa read (hiari: hadi timestamp)")
def mark_all_read(
    until: Optional[datetime] = Query(None, description="Ikiwa umetaja, mark read hadi muda huu (<= until)"),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if CRUD_MARKALL:
        n = CRUD_MARKALL(db, current_user.id, until=until)
        return {"updated": int(n or 0)}

    if not NotifModel:
        raise HTTPException(status_code=500, detail="Notifications storage not configured")

    q = db.query(NotifModel).filter(
        NotifModel.user_id == current_user.id,
        getattr(NotifModel, "is_read", False) == False  # noqa: E712
    )
    if until and hasattr(NotifModel, "created_at"):
        q = q.filter(NotifModel.created_at <= until)

    rows = q.all()
    updated = 0
    for r in rows:
        r.is_read = True
        if hasattr(r, "seen_at") and r.seen_at is None:
            r.seen_at = _utcnow()
        if hasattr(r, "updated_at"):
            r.updated_at = _utcnow()
        updated += 1
    if updated:
        db.commit()
    return {"updated": updated}

# ====== GET ONE + DELETE (optional) ==========================================
@router.get("/{notification_id}", response_model=PostLiveNotificationOut, summary="Pata notification moja")
def get_one(
    notification_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if CRUD_GETONE:
        row = CRUD_GETONE(db, current_user.id, notification_id)
    else:
        if not NotifModel:
            raise HTTPException(status_code=500, detail="Notifications storage not configured")
        row = db.query(NotifModel).filter(
            NotifModel.id == notification_id,
            NotifModel.user_id == current_user.id
        ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Notification not found")
    return PostLiveNotificationOut.model_validate(row, from_attributes=True) if hasattr(PostLiveNotificationOut, "model_validate") else PostLiveNotificationOut.model_validate(row)

@router.delete("/{notification_id}", response_model=dict, summary="Futa notification moja")
def delete_one(
    notification_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if CRUD_DELETE:
        ok = CRUD_DELETE(db, current_user.id, notification_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Notification not found")
        return {"detail": "Deleted"}
    if not NotifModel:
        raise HTTPException(status_code=500, detail="Notifications storage not configured")

    row = db.query(NotifModel).filter(
        NotifModel.id == notification_id,
        NotifModel.user_id == current_user.id
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Notification not found")

    # soft delete kama una deleted_at
    if hasattr(row, "deleted_at"):
        row.deleted_at = _utcnow()
        if hasattr(row, "updated_at"):
            row.updated_at = _utcnow()
        db.commit()
    else:
        db.delete(row)
        db.commit()
    return {"detail": "Deleted"}

