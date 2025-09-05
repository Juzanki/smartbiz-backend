from __future__ import annotations
# backend/routes/comments.py
import os
import time
import hashlib
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from contextlib import suppress

from fastapi import (
    APIRouter, Depends, HTTPException, status, Query, Path, Response, Header
)
from sqlalchemy.orm import Session
from sqlalchemy import func

# ---- DB session ----
try:
    from backend.db import get_db  # preferred
except Exception:
    from backend.dependencies import get_db  # fallback

# ---- Auth / RBAC ----
try:
    from backend.auth import get_current_user
except Exception:
    from backend.dependencies import get_current_user  # fallback

# ---- Schemas (use yours; small fallbacks if missing) ----
try:
    from backend.schemas.comment_schemas import (
        VideoCommentCreate, VideoCommentOut, VideoCommentUpdate
    )
except Exception:
    from pydantic import BaseModel, Field

    class VideoCommentCreate(BaseModel):
        video_post_id: int
        content: str = Field(..., min_length=1, max_length=2000)
        parent_id: Optional[int] = None  # replies (optional)

    class VideoCommentUpdate(BaseModel):
        content: str = Field(..., min_length=1, max_length=2000)

    class VideoCommentOut(BaseModel):
        id: int
        video_post_id: int
        user_id: int
        content: str
        parent_id: Optional[int] = None
        is_hidden: Optional[bool] = False
        deleted_at: Optional[datetime] = None
        created_at: Optional[datetime] = None
        updated_at: Optional[datetime] = None

# ---- Models / CRUD (prefer model for pagination; fallback to CRUD helpers) ----
with suppress(Exception):
    from backend.models.comment import VideoComment  # if you have a dedicated model

crud_create = None
crud_list = None
crud_delete = None
with suppress(Exception):
    from backend.crud import comment_crud as _cc
    crud_create = getattr(_cc, "create_comment", None)
    crud_list = getattr(_cc, "get_comments_by_video", None)
    crud_delete = getattr(_cc, "delete_comment", None)

router = APIRouter(prefix="/comments", tags=["Comments"])

# ===================== Config & Helpers =====================
MAX_LEN = int(os.getenv("COMMENT_MAX_LEN", "2000"))
RATE_PER_MIN = int(os.getenv("COMMENT_RATE_PER_MIN", "30"))          # per user
RATE_PER_VIDEO_PER_MIN = int(os.getenv("COMMENT_VIDEO_RATE_PER_MIN", "60"))
MAX_LIMIT = 200
DEFAULT_LIMIT = 50
ALLOWED_ORDER = ("asc", "desc")

_RATE_USER: Dict[int, List[float]] = {}
_RATE_VIDEO: Dict[tuple[int, int], List[float]] = {}
_IDEMP: Dict[tuple[int, str], float] = {}
_IDEMP_TTL = 10 * 60  # seconds

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _clamp_limit(v: Optional[int]) -> int:
    if not v:
        return DEFAULT_LIMIT
    return max(1, min(int(v), MAX_LIMIT))

def _rate_ok(user_id: int, video_post_id: Optional[int]) -> None:
    now = time.time()
    q = _RATE_USER.setdefault(user_id, [])
    while q and (now - q[0]) > 60.0:
        q.pop(0)
    if len(q) >= RATE_PER_MIN:
        raise HTTPException(status_code=429, detail="Too many comments this minute")
    q.append(now)

    if video_post_id is not None:
        key = (user_id, int(video_post_id))
        vq = _RATE_VIDEO.setdefault(key, [])
        while vq and (now - vq[0]) > 60.0:
            vq.pop(0)
        if len(vq) >= RATE_PER_VIDEO_PER_MIN:
            raise HTTPException(status_code=429, detail="Slow down on this video")
        vq.append(now)

def _idempotency_check(uid: int, key: Optional[str]) -> None:
    if not key:
        return
    now = time.time()
    stale = [(k_uid, k) for (k_uid, k), ts in _IDEMP.items() if now - ts > _IDEMP_TTL]
    for s in stale:
        _IDEMP.pop(s, None)
    token = (uid, key.strip())
    if token in _IDEMP:
        raise HTTPException(status_code=409, detail="Duplicate request (Idempotency-Key)")
    _IDEMP[token] = now

def _validate_text(t: str) -> str:
    txt = (t or "").strip()
    if not txt:
        raise HTTPException(status_code=422, detail="Comment cannot be empty")
    if len(txt) > MAX_LEN:
        raise HTTPException(status_code=413, detail=f"Comment too long (>{MAX_LEN} chars)")
    return txt

def _etag_for(obj: Any) -> str:
    base = f"{getattr(obj, 'id', '')}-{getattr(obj, 'updated_at', '') or getattr(obj, 'created_at', '')}-{getattr(obj, 'content', '')}"
    return 'W/"' + hashlib.sha256(str(base).encode("utf-8")).hexdigest()[:16] + '"'

def _serialize_one(row: Any) -> VideoCommentOut:
    if hasattr(VideoCommentOut, "model_validate"):
        return VideoCommentOut.model_validate(row, from_attributes=True)  # Pydantic v2
    return VideoCommentOut.model_validate(row)  # Pydantic v1

# ===================== Create =====================
@router.post(
    "",
    response_model=VideoCommentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Post a new comment (idempotent + rate-limited)"
)
def post_comment(
    data: VideoCommentCreate,
    response: Response,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    content = _validate_text(data.content)
    _rate_ok(current_user.id, data.video_post_id)
    _idempotency_check(current_user.id, idempotency_key)

    # Anti-duplicate (same user, same video, same content within 60s)
    if "VideoComment" in globals():
        now = _utcnow()
        lookback = now.timestamp() - 60
        recent = (
            db.query(VideoComment)
            .filter(
                VideoComment.user_id == current_user.id,
                VideoComment.video_post_id == data.video_post_id,
                func.md5(VideoComment.content) == func.md5(content),
            )
            .order_by(VideoComment.id.desc())
            .first()
        )
        if recent and getattr(recent, "created_at", None):
            ts = recent.created_at.replace(tzinfo=timezone.utc).timestamp()
            if ts >= lookback:
                raise HTTPException(status_code=409, detail="Duplicate comment detected")

    # Prefer your CRUD if available (with sender ID override)
    if crud_create:
        try:
            row = crud_create(db, current_user.id, data)
        except TypeError:
            row = crud_create(db, data)  # legacy signature
    elif "VideoComment" in globals():
        row = VideoComment(
            user_id=current_user.id,
            video_post_id=data.video_post_id,
            content=content,
            parent_id=getattr(data, "parent_id", None),
        )
        # timestamps
        now = _utcnow()
        if hasattr(row, "created_at") and not getattr(row, "created_at", None):
            row.created_at = now
        if hasattr(row, "updated_at"):
            row.updated_at = now
        db.add(row)
        db.commit()
        db.refresh(row)
    else:
        raise HTTPException(status_code=500, detail="Comment model/CRUD not configured")

    response.headers["Cache-Control"] = "no-store"
    response.headers["ETag"] = _etag_for(row)
    return _serialize_one(row)

# ===================== List for a video =====================
@router.get(
    "/video/{video_post_id}",
    response_model=List[VideoCommentOut],
    summary="Get comments for a video (pagination + order + delta)"
)
def get_comments(
    video_post_id: int,
    response: Response,
    db: Session = Depends(get_db),
    # pagination & sync
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    order: str = Query("desc", description="asc|desc"),
    after_id: Optional[int] = Query(None, description="Fetch > after_id (delta sync)"),
    include_deleted: bool = Query(False),
    include_hidden: bool = Query(False),
):
    limit = _clamp_limit(limit)

    if "VideoComment" in globals():
        q = db.query(VideoComment).filter(VideoComment.video_post_id == video_post_id)
        if not include_deleted and hasattr(VideoComment, "deleted_at"):
            q = q.filter(VideoComment.deleted_at.is_(None))
        if not include_hidden and hasattr(VideoComment, "is_hidden"):
            q = q.filter((VideoComment.is_hidden.is_(False)) | (VideoComment.is_hidden.is_(None)))
        if after_id:
            q = q.filter(VideoComment.id > int(after_id))
        q = q.order_by(VideoComment.id.asc() if order == "asc" else VideoComment.id.desc())
        total = q.count()
        rows = q.offset(offset).limit(limit).all()
    elif crud_list:
        rows = crud_list(db, video_post_id) or []
        # basic in-memory paging (fallback)
        rows = sorted(rows, key=lambda r: getattr(r, "id", 0), reverse=(order == "desc"))
        if after_id:
            rows = [r for r in rows if getattr(r, "id", 0) > int(after_id)]
        total = len(rows)
        rows = rows[offset: offset + limit]
    else:
        raise HTTPException(status_code=500, detail="Comment listing not configured")

    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Limit"] = str(limit)
    response.headers["X-Offset"] = str(offset)

    return [_serialize_one(r) for r in rows]

# ===================== Edit =====================
@router.patch(
    "/{comment_id}",
    response_model=VideoCommentOut,
    summary="Edit a comment (optimistic via If-Match ETag)"
)
def edit_comment(
    comment_id: int,
    payload: VideoCommentUpdate,
    response: Response,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    if_match: Optional[str] = Header(None, alias="If-Match"),
):
    if "VideoComment" not in globals():
        raise HTTPException(status_code=501, detail="Direct editing requires model access")

    row = db.query(VideoComment).filter(VideoComment.id == comment_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Comment not found")
    if getattr(row, "user_id", None) != getattr(current_user, "id", None) and getattr(current_user, "role", "user") not in {"admin", "owner"}:
        raise HTTPException(status_code=403, detail="Not allowed")

    # optimistic locking: If-Match ETag
    current_etag = _etag_for(row)
    if if_match and if_match != current_etag:
        raise HTTPException(status_code=412, detail="ETag mismatch (comment changed)")

    row.content = _validate_text(payload.content)
    if hasattr(row, "updated_at"):
        row.updated_at = _utcnow()
    db.commit()
    db.refresh(row)

    response.headers["Cache-Control"] = "no-store"
    response.headers["ETag"] = _etag_for(row)
    return _serialize_one(row)

# ===================== Delete (soft if supported) =====================
@router.delete(
    "/{comment_id}",
    response_model=dict,
    summary="Delete a comment (owner/admin; soft-delete if supported)"
)
def delete_comment(
    comment_id: int = Path(..., ge=1),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    # Prefer your CRUD if provided
    if crud_delete:
        ok = crud_delete(db, comment_id, getattr(current_user, "id", None))
        if not ok:
            raise HTTPException(status_code=404, detail="Comment not found or unauthorized")
        return {"detail": "Comment deleted"}

    if "VideoComment" not in globals():
        raise HTTPException(status_code=501, detail="Delete requires model or CRUD")

    row = db.query(VideoComment).filter(VideoComment.id == comment_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Comment not found")
    is_admin = getattr(current_user, "role", "user") in {"admin", "owner"}
    if getattr(row, "user_id", None) != getattr(current_user, "id", None) and not is_admin:
        raise HTTPException(status_code=403, detail="Not allowed")

    # Soft delete if column exists; else hard delete
    if hasattr(row, "deleted_at"):
        row.deleted_at = _utcnow()
        if hasattr(row, "updated_at"):
            row.updated_at = _utcnow()
        db.commit()
    else:
        db.delete(row)
        db.commit()
    return {"detail": "Comment deleted"}

# ===================== Admin moderation (hide/unhide) =====================
@router.post(
    "/{comment_id}/moderate/{action}",
    response_model=VideoCommentOut,
    summary="Admin: hide/unhide a comment"
)
def moderate_comment(
    comment_id: int,
    action: str,
    response: Response,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    reason: Optional[str] = Query(None, max_length=300),
):
    if getattr(current_user, "role", "user") not in {"admin", "owner"}:
        raise HTTPException(status_code=403, detail="Admin only")

    if "VideoComment" not in globals():
        raise HTTPException(status_code=501, detail="Moderation requires model access")

    row = db.query(VideoComment).filter(VideoComment.id == comment_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Comment not found")

    if action not in {"hide", "unhide"}:
        raise HTTPException(status_code=422, detail="Action must be 'hide' or 'unhide'")

    if hasattr(row, "is_hidden"):
        row.is_hidden = (action == "hide")
    if reason and hasattr(row, "moderation_reason") and action == "hide":
        row.moderation_reason = reason
    if hasattr(row, "updated_at"):
        row.updated_at = _utcnow()
    db.commit()
    db.refresh(row)

    response.headers["Cache-Control"] = "no-store"
    response.headers["ETag"] = _etag_for(row)
    return _serialize_one(row)

