from __future__ import annotations
# backend/routes/announcements.py
import hashlib
from datetime import datetime, timezone
from typing import Optional, List, Iterable, Any, Annotated

from fastapi import (
    APIRouter, Depends, HTTPException, status, Query, Path, Response, Header
)
from sqlalchemy.orm import Session
from sqlalchemy import func, or_

from backend.db import get_db
from backend.models.announcement import Announcement as AnnouncementModel
from backend.models.user import User as UserModel  # ORM only for internal use (not as param type)

# ============ Schemas (tumia zako; fallback ikiwa hazipo) ============
try:
    from backend.schemas.announcement import (
        AnnouncementCreate, AnnouncementOut, AnnouncementUpdate
    )
except Exception:
    # Fallback schemas kwa Pydantic v2
    try:
        from pydantic import BaseModel, Field, ConfigDict
    except Exception:  # v1 fallback
        from pydantic import BaseModel, Field  # type: ignore
        class ConfigDict(dict):  # type: ignore
            pass

    class AnnouncementCreate(BaseModel):
        title: str = Field(..., min_length=2, max_length=200)
        body: str = Field(..., min_length=1)
        status: Optional[str] = "draft"  # draft|scheduled|published|archived
        audience: Optional[str] = None   # e.g. "all|pro|business"
        scheduled_at: Optional[datetime] = None

    class AnnouncementUpdate(BaseModel):
        title: Optional[str] = None
        body: Optional[str] = None
        status: Optional[str] = None
        audience: Optional[str] = None
        scheduled_at: Optional[datetime] = None

    class AnnouncementOut(AnnouncementCreate):
        id: int
        author_id: Optional[int] = None
        created_at: Optional[datetime] = None
        updated_at: Optional[datetime] = None
        published_at: Optional[datetime] = None
        # Ruhusu ORM -> schema (Pydantic v2)
        model_config = ConfigDict(from_attributes=True)

# ============ RBAC: admin/owner guard ============
# Lengo: admin_guard irudishe "current admin user" (si None) ili tuandike author_id
try:
    from backend.dependencies import check_admin as _check_admin  # type: ignore
    def admin_guard(user: Annotated[UserModel, Depends(_check_admin)]) -> UserModel:
        return user
except Exception:
    try:
        from backend.dependencies import get_current_user  # type: ignore
        def admin_guard(user: Annotated[Any, Depends(get_current_user)]) -> Any:
            role = getattr(user, "role", None)
            if role not in {"admin", "owner"}:
                raise HTTPException(status_code=403, detail="Not authorized")
            return user
    except Exception:
        def admin_guard() -> None:
            raise HTTPException(status_code=403, detail="Admin guard missing")

# Optional dependency kuleta user (bila ku-annotate na ORM)
try:
    from backend.dependencies import get_current_user as _get_current_user  # type: ignore
    CurrentUser = Annotated[Any, Depends(_get_current_user)]
except Exception:
    def _no_user():
        return None
    CurrentUser = Annotated[Any, Depends(_no_user)]

router = APIRouter(prefix="/announcements", tags=["Announcements"])

# ============ Helpers ============
MAX_LIMIT = 200
ALLOWED_SORT = ("created_at", "updated_at", "published_at", "id", "title")
ALLOWED_ORDER = ("asc", "desc")
ALLOWED_STATUS = {"draft", "scheduled", "published", "archived"}

IMMUTABLE_FIELDS = {"id", "author_id", "created_at", "updated_at", "published_at"}

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _clamp_limit(limit: Optional[int], default: int = 50) -> int:
    if not limit:
        return default
    return max(1, min(int(limit), MAX_LIMIT))

def _order_by_whitelist(model, sort_by: str, order: str, allow: Iterable[str]):
    key = sort_by if sort_by in allow else next(iter(allow))
    col = getattr(model, key)
    return col.asc() if order == "asc" else col.desc()

def _apply_headers(resp: Response, *, total: Optional[int], limit: int, offset: int, cursor_next: Optional[int]):
    if total is not None:
        resp.headers["X-Total-Count"] = str(total)
    resp.headers["X-Limit"] = str(limit)
    resp.headers["X-Offset"] = str(offset)
    if cursor_next:
        resp.headers["X-Cursor-Next"] = str(cursor_next)

def _compute_etag(obj: AnnouncementModel) -> str:
    if getattr(obj, "updated_at", None):
        base = str(int(obj.updated_at.replace(tzinfo=timezone.utc).timestamp()))
    else:
        raw = "|".join(str(getattr(obj, k, "")) for k in ("id", "title", "status", "published_at"))
        base = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f'W/"{base}"'

def _next_cursor(items: List[AnnouncementModel]) -> Optional[int]:
    if not items:
        return None
    return int(items[-1].id)

def _sanitize_status(val: Optional[str]) -> Optional[str]:
    if not val:
        return val
    v = str(val).strip().lower()
    return v if v in ALLOWED_STATUS else None

# ============================ CREATE (admin) ============================ #
@router.post(
    "/",
    response_model=AnnouncementOut,
    status_code=status.HTTP_201_CREATED,
    summary="🔐 Create announcement (admin/owner)"
)
def create_announcement(
    data: AnnouncementCreate,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    admin_user: Annotated[Any, Depends(admin_guard)],
    current_user: CurrentUser,  # optional; inaweza kuwa sawa na admin_user
):
    inst = AnnouncementModel()
    for k, v in data.dict().items():
        if k in IMMUTABLE_FIELDS or not hasattr(inst, k):
            continue
        if k == "status":
            v = _sanitize_status(v) or "draft"
        setattr(inst, k, v)

    author_id = getattr(admin_user, "id", None) or (getattr(current_user, "id", None) if current_user else None)
    if hasattr(inst, "author_id"):
        inst.author_id = author_id

    if hasattr(inst, "created_at"):
        inst.created_at = _utcnow()
    if hasattr(inst, "updated_at"):
        inst.updated_at = _utcnow()
    if getattr(inst, "status", None) == "published" and hasattr(inst, "published_at"):
        inst.published_at = _utcnow()

    db.add(inst)
    try:
        db.commit()
        db.refresh(inst)
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to create announcement")

    response.headers["ETag"] = _compute_etag(inst)
    response.headers["Cache-Control"] = "no-store"
    return inst

# ============================= LIST (public) ============================= #
@router.get(
    "/",
    response_model=List[AnnouncementOut],
    summary="List announcements (public; admin may include unpublished)"
)
def list_announcements(
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    # filters
    q: Optional[str] = Query(None, description="search title/body"),
    status_f: Optional[str] = Query("published", alias="status"),
    audience: Optional[str] = Query(None, description="e.g. all|pro|business"),
    created_from: Optional[datetime] = Query(None),
    created_to: Optional[datetime] = Query(None),
    # sort/paginate
    sort_by: str = Query("published_at", pattern=r"^(created_at|updated_at|published_at|id|title)$"),
    order: str = Query("desc", pattern=r"^(asc|desc)$"),
    limit: int = Query(20, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0, description="Ignored if cursor provided"),
    cursor: Optional[int] = Query(None),
    with_count: bool = Query(False),
    # admin toggle
    include_unpublished: bool = Query(False, description="Admin-only: include non-published items"),
    current_user: CurrentUser = None,
):
    """
    - Public by default returns **published** only.
    - Admin/owner can set `include_unpublished=true` to see all statuses.
    - Supports **cursor** (id-based) or offset pagination.
    """
    limit = _clamp_limit(limit)
    qy = db.query(AnnouncementModel)

    is_admin = getattr(current_user, "role", None) in {"admin", "owner"} if current_user else False
    if include_unpublished and not is_admin:
        raise HTTPException(status_code=403, detail="Not authorized to view unpublished items")

    if status_f:
        st = _sanitize_status(status_f)
        if st:
            qy = qy.filter(AnnouncementModel.status == st)
        elif not (include_unpublished and is_admin):
            qy = qy.filter(AnnouncementModel.status == "published")
    else:
        if not (include_unpublished and is_admin):
            qy = qy.filter(AnnouncementModel.status == "published")

    if audience and hasattr(AnnouncementModel, "audience"):
        qy = qy.filter(AnnouncementModel.audience == audience)

    if q:
        like = f"%{q.strip()}%"
        conds = []
        for field in ("title", "body"):
            if hasattr(AnnouncementModel, field):
                conds.append(getattr(AnnouncementModel, field).ilike(like))
        if conds:
            qy = qy.filter(or_(*conds))

    if created_from and hasattr(AnnouncementModel, "created_at"):
        qy = qy.filter(AnnouncementModel.created_at >= created_from)
    if created_to and hasattr(AnnouncementModel, "created_at"):
        qy = qy.filter(AnnouncementModel.created_at <= created_to)

    qy = qy.order_by(_order_by_whitelist(AnnouncementModel, sort_by, order, ALLOWED_SORT))

    total = None
    if with_count:
        total = qy.with_entities(func.count(AnnouncementModel.id)).scalar() or 0

    # Cursor pagination
    if cursor and hasattr(AnnouncementModel, "id"):
        if order == "desc":
            qy = qy.filter(AnnouncementModel.id < cursor)
        else:
            qy = qy.filter(AnnouncementModel.id > cursor)
        items = qy.limit(limit).all()
        off = 0
    else:
        items = qy.offset(offset).limit(limit).all()
        off = offset

    _apply_headers(
        response,
        total=total,
        limit=limit,
        offset=off,
        cursor_next=_next_cursor(items) if order == "desc" else None
    )
    return items

# ============================= GET ONE (public) ============================= #
@router.get(
    "/{announcement_id}",
    response_model=AnnouncementOut,
    summary="Get single announcement"
)
def get_announcement(
    response: Response,  # zisizo na default kwanza
    db: Annotated[Session, Depends(get_db)],
    announcement_id: int = Path(..., ge=1),  # yenye default baadae
    current_user: CurrentUser = None,
):
    item = db.query(AnnouncementModel).filter(AnnouncementModel.id == announcement_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Announcement not found")

    is_admin = getattr(current_user, "role", None) in {"admin", "owner"} if current_user else False
    if not is_admin and getattr(item, "status", None) != "published":
        raise HTTPException(status_code=404, detail="Announcement not found")

    response.headers["ETag"] = _compute_etag(item)
    response.headers["Cache-Control"] = "no-store"
    return item

# ========================= UPDATE/PATCH (admin) ========================= #
@router.patch(
    "/{announcement_id}",
    response_model=AnnouncementOut,
    summary="🔐 Update announcement (partial)"
)
def update_announcement(
    data: AnnouncementUpdate,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    admin_user: Annotated[Any, Depends(admin_guard)],
    announcement_id: int = Path(..., ge=1),
    if_match: Optional[str] = Header(None, alias="If-Match")
):
    inst = (
        db.query(AnnouncementModel)
        .filter(AnnouncementModel.id == announcement_id)
        .with_for_update(of=AnnouncementModel)
        .first()
    )
    if not inst:
        raise HTTPException(status_code=404, detail="Announcement not found")

    current_etag = _compute_etag(inst)

    if if_match and if_match.strip() != current_etag:
        raise HTTPException(
            status_code=412,  # Precondition Failed
            detail="ETag mismatch. Refresh and retry with latest version."
        )

    payload = data.dict(exclude_unset=True)
    for k, v in payload.items():
        if k in IMMUTABLE_FIELDS or not hasattr(inst, k):
            continue
        if k == "status":
            v = _sanitize_status(v)
            if not v:
                continue
        if isinstance(v, str):
            v = v.strip()
            if k == "title" and len(v) < 2:
                continue
        setattr(inst, k, v)

    if hasattr(inst, "updated_at"):
        inst.updated_at = _utcnow()

    try:
        db.commit()
        db.refresh(inst)
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to update announcement")

    response.headers["ETag"] = _compute_etag(inst)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Previous-ETag"] = current_etag
    return inst

# ====================== PUBLISH / UNPUBLISH (admin) ====================== #
@router.put(
    "/{announcement_id}/publish",
    response_model=AnnouncementOut,
    summary="🔐 Publish announcement now"
)
def publish_announcement(
    announcement_id: int,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    admin_user: Annotated[Any, Depends(admin_guard)],
):
    inst = (
        db.query(AnnouncementModel)
        .filter(AnnouncementModel.id == announcement_id)
        .with_for_update(of=AnnouncementModel)
        .first()
    )
    if not inst:
        raise HTTPException(status_code=404, detail="Announcement not found")

    if hasattr(inst, "status"):
        inst.status = "published"
    if hasattr(inst, "published_at"):
        inst.published_at = _utcnow()
    if hasattr(inst, "updated_at"):
        inst.updated_at = _utcnow()

    try:
        db.commit()
        db.refresh(inst)
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to publish announcement")

    response.headers["ETag"] = _compute_etag(inst)
    response.headers["Cache-Control"] = "no-store"
    return inst

@router.put(
    "/{announcement_id}/unpublish",
    response_model=AnnouncementOut,
    summary="🔐 Unpublish announcement"
)
def unpublish_announcement(
    announcement_id: int,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    admin_user: Annotated[Any, Depends(admin_guard)],
):
    inst = (
        db.query(AnnouncementModel)
        .filter(AnnouncementModel.id == announcement_id)
        .with_for_update(of=AnnouncementModel)
        .first()
    )
    if not inst:
        raise HTTPException(status_code=404, detail="Announcement not found")

    if hasattr(inst, "status"):
        inst.status = "draft"
    if hasattr(inst, "updated_at"):
        inst.updated_at = _utcnow()

    try:
        db.commit()
        db.refresh(inst)
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to unpublish announcement")

    response.headers["ETag"] = _compute_etag(inst)
    response.headers["Cache-Control"] = "no-store"
    return inst

# ================================ DELETE (admin) ================================ #
@router.delete(
    "/{announcement_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="🔐 Delete announcement"
)
def delete_announcement(
    announcement_id: int,
    db: Annotated[Session, Depends(get_db)],
    admin_user: Annotated[Any, Depends(admin_guard)],
):
    inst = db.query(AnnouncementModel).filter(AnnouncementModel.id == announcement_id).first()
    if not inst:
        raise HTTPException(status_code=404, detail="Announcement not found")

    try:
        db.delete(inst)
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to delete announcement")
    return None

# ================================ BULK OPS ================================ #
@router.post(
    "/bulk/publish",
    summary="🔐 Bulk publish announcements"
)
def bulk_publish(
    ids: List[int],
    db: Annotated[Session, Depends(get_db)],
    admin_user: Annotated[Any, Depends(admin_guard)],
):
    if not ids:
        return {"published": 0}
    items = db.query(AnnouncementModel).filter(AnnouncementModel.id.in_([int(i) for i in ids])).all()
    count = 0
    for inst in items:
        if hasattr(inst, "status"):
            inst.status = "published"
        if hasattr(inst, "published_at"):
            inst.published_at = _utcnow()
        if hasattr(inst, "updated_at"):
            inst.updated_at = _utcnow()
        count += 1
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="Bulk publish failed")
    return {"published": count}

# ================================ STATS ================================ #
@router.get(
    "/stats",
    summary="🔐 Announcements stats (counts by status)"
)
def announce_stats(
    db: Annotated[Session, Depends(get_db)],
    admin_user: Annotated[Any, Depends(admin_guard)],
):
    rows = (
        db.query(AnnouncementModel.status, func.count(AnnouncementModel.id))
        .group_by(AnnouncementModel.status)
        .all()
    )
    return [{"status": (s or "unknown"), "count": int(c or 0)} for (s, c) in rows]
