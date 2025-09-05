from __future__ import annotations
# backend/routes/campaigns.py
import os
import time
import re
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any
from contextlib import suppress

from fastapi import (
    APIRouter, Depends, HTTPException, status, Header, Response, Query, Path
)
from sqlalchemy.orm import Session
from sqlalchemy import func

from backend.db import get_db
from backend.dependencies import get_current_user
from backend.models.campaign import Campaign
from backend.models.product import Product
from backend.models.user import User
# ====== Schemas (tumia zako, toa fallback endapo hazipo) ======
try:
    from backend.schemas import CampaignCreate, CampaignOut, CampaignUpdate
except Exception:
    from pydantic import BaseModel, Field

    class CampaignCreate(BaseModel):
        title: str = Field(..., min_length=3, max_length=140)
        product_id: int
        rate: float = Field(..., ge=0.0, le=100.0, description="commission %")
        duration: int = Field(..., ge=1, le=365, description="days")
        starts_at: Optional[datetime] = None

    class CampaignUpdate(BaseModel):
        title: Optional[str] = Field(None, min_length=3, max_length=140)
        rate: Optional[float] = Field(None, ge=0.0, le=100.0)
        extend_days: Optional[int] = Field(None, ge=1, le=365)
        starts_at: Optional[datetime] = None
        ends_at: Optional[datetime] = None

    class CampaignOut(BaseModel):
        id: int
        title: str
        slug: Optional[str] = None
        product_id: int
        owner_id: int
        commission_rate: float
        status: str
        starts_at: Optional[datetime] = None
        ends_at: Optional[datetime] = None
        created_at: Optional[datetime] = None
        updated_at: Optional[datetime] = None


router = APIRouter(prefix="/campaigns", tags=["Campaigns"])

# ====== Config ======
MIN_RATE = float(os.getenv("CAMPAIGN_MIN_RATE", "0"))
MAX_RATE = float(os.getenv("CAMPAIGN_MAX_RATE", "100"))
MIN_DAYS = int(os.getenv("CAMPAIGN_MIN_DAYS", "1"))
MAX_DAYS = int(os.getenv("CAMPAIGN_MAX_DAYS", "365"))
MAX_ACTIVE_PER_PRODUCT = int(os.getenv("CAMPAIGN_MAX_ACTIVE_PER_PRODUCT", "1"))

CREATE_RATE_PER_MIN = int(os.getenv("CAMPAIGN_CREATE_RATE_PER_MIN", "10"))
_RATE: Dict[int, List[float]] = {}
_IDEMP: Dict[tuple[int, str], float] = {}
_IDEMP_TTL = 10 * 60  # sekunde

ALLOWED_SORT = ("created_at", "updated_at", "starts_at", "ends_at", "id")
ALLOWED_ORDER = ("asc", "desc")
MAX_LIMIT = 200


# ====== Helpers ======
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _rate_ok(uid: int) -> None:
    now = time.time()
    q = _RATE.setdefault(uid, [])
    while q and (now - q[0]) > 60.0:
        q.pop(0)
    if len(q) >= CREATE_RATE_PER_MIN:
        raise HTTPException(status_code=429, detail="Too many create attempts. Try again shortly.")
    q.append(now)

def _check_idempotency(uid: int, key: Optional[str]) -> None:
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

def _compute_status(starts_at: Optional[datetime], ends_at: Optional[datetime]) -> str:
    now = _utcnow()
    if starts_at and starts_at > now:
        return "scheduled"
    if ends_at and ends_at < now:
        return "ended"
    return "active"

def _slugify(s: str) -> str:
    s = re.sub(r"[^\w\s-]", "", s).strip().lower()
    s = re.sub(r"[\s_-]+", "-", s)
    return s[:60]

def _etag_for(c: Campaign) -> str:
    base = (
        f"{getattr(c, 'id', '')}-{getattr(c, 'updated_at', '') or getattr(c, 'created_at', '')}"
    )
    return 'W/"' + hashlib.sha256(str(base).encode("utf-8")).hexdigest()[:16] + '"'

def _order_by_whitelist(model, sort_by: str, order: str):
    key = sort_by if sort_by in ALLOWED_SORT else "created_at"
    col = getattr(model, key)
    return col.asc() if order == "asc" else col.desc()

def _clamp_limit(limit: Optional[int]) -> int:
    if not limit:
        return 50
    return max(1, min(int(limit), MAX_LIMIT))


# ====== CREATE ======
@router.post(
    "/create",
    response_model=CampaignOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create campaign (idempotent, validated)"
)
def create_campaign(
    payload: CampaignCreate,
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    _rate_ok(current_user.id)
    _check_idempotency(current_user.id, idempotency_key)

    # Validate rate/duration
    if not (MIN_RATE <= float(payload.rate) <= MAX_RATE):
        raise HTTPException(status_code=422, detail=f"Rate must be {MIN_RATE}â€“{MAX_RATE}%")
    if not (MIN_DAYS <= int(payload.duration) <= MAX_DAYS):
        raise HTTPException(status_code=422, detail=f"Duration must be {MIN_DAYS}â€“{MAX_DAYS} days")

    # Product must exist and belong to user (or user is admin/owner)
    product = db.query(Product).filter(Product.id == payload.product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    if product.owner_id != current_user.id and getattr(current_user, "role", "user") not in {"admin", "owner"}:
        raise HTTPException(status_code=403, detail="Permission denied for this product")

    # Lock product row (best-effort) to avoid race on active count
    with suppress(Exception):
        db.query(Product).filter(Product.id == payload.product_id).with_for_update().first()

    # Check active campaigns limit per product
    now = _utcnow()
    active_count = (
        db.query(Campaign)
        .filter(
            Campaign.product_id == payload.product_id,
            func.coalesce(Campaign.ends_at, now + timedelta(days=36500)) >= now,
        )
        .count()
    )
    if active_count >= MAX_ACTIVE_PER_PRODUCT:
        raise HTTPException(status_code=409, detail="Active campaign limit reached for this product")

    # Compute times & status
    starts_at = payload.starts_at if payload.starts_at else _utcnow()
    ends_at = starts_at + timedelta(days=int(payload.duration))
    status_str = _compute_status(starts_at, ends_at)

    # Slug (unique-ish per product)
    base_slug = _slugify(payload.title)
    slug = f"{base_slug}-{payload.product_id}"

    c = Campaign(
        title=payload.title.strip(),
        slug=slug if hasattr(Campaign, "slug") else None,
        product_id=payload.product_id,
        owner_id=current_user.id,
        commission_rate=float(payload.rate),
        starts_at=starts_at,
        ends_at=ends_at,
        status=status_str if hasattr(Campaign, "status") else None,
    )

    try:
        db.add(c)
        db.commit()
        db.refresh(c)
    except Exception as e:
        db.rollback()
        # on failure: allow next try with same idempotency key
        if idempotency_key:
            _IDEMP.pop((current_user.id, idempotency_key), None)
        raise HTTPException(status_code=500, detail=f"Create failed: {e}")

    # Headers for mobile
    response.headers["Cache-Control"] = "no-store"
    response.headers["ETag"] = _etag_for(c)

    # Audit (optional)
    with suppress(Exception):
        from backend.routes.audit_log import emit_audit  # type: ignore
        emit_audit(
            db,
            action="campaign.create",
            status="success",
            severity="info",
            actor_id=current_user.id,
            actor_email=getattr(current_user, "email", None),
            resource_type="campaign",
            resource_id=str(getattr(c, "id", None)),
            meta={"product_id": payload.product_id, "rate": payload.rate, "starts_at": str(starts_at), "ends_at": str(ends_at)},
        )

    # Serialize (Pydantic v1/v2 friendly)
    out = {
        "id": c.id,
        "title": c.title,
        "slug": getattr(c, "slug", None),
        "product_id": c.product_id,
        "owner_id": c.owner_id,
        "commission_rate": c.commission_rate,
        "status": getattr(c, "status", _compute_status(c.starts_at, c.ends_at)),
        "starts_at": c.starts_at,
        "ends_at": c.ends_at,
        "created_at": getattr(c, "created_at", None),
        "updated_at": getattr(c, "updated_at", None),
    }
    if hasattr(CampaignOut, "model_validate"):
        return CampaignOut.model_validate(out)  # type: ignore
    return CampaignOut(**out)  # type: ignore


# ====== GET ONE ======
@router.get("/{campaign_id}", response_model=CampaignOut, summary="Get one campaign")
def get_campaign(
    campaign_id: int = Path(..., ge=1),
    response: Response = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    c = db.query(Campaign).filter(Campaign.id == campaign_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Campaign not found")
    # must be owner or admin/owner
    if c.owner_id != current_user.id and getattr(current_user, "role", "user") not in {"admin", "owner"}:
        raise HTTPException(status_code=403, detail="Forbidden")

    if response:
        response.headers["Cache-Control"] = "no-store"
        response.headers["ETag"] = _etag_for(c)

    data = {
        "id": c.id, "title": c.title, "slug": getattr(c, "slug", None),
        "product_id": c.product_id, "owner_id": c.owner_id,
        "commission_rate": c.commission_rate,
        "status": getattr(c, "status", _compute_status(c.starts_at, c.ends_at)),
        "starts_at": c.starts_at, "ends_at": c.ends_at,
        "created_at": getattr(c, "created_at", None),
        "updated_at": getattr(c, "updated_at", None),
    }
    if hasattr(CampaignOut, "model_validate"):
        return CampaignOut.model_validate(data)  # type: ignore
    return CampaignOut(**data)  # type: ignore


# ====== LIST MINE (paged) ======
@router.get("", response_model=List[CampaignOut], summary="List my campaigns")
def list_my_campaigns(
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    sort_by: str = Query("created_at"),
    order: str = Query("desc"),
    limit: int = Query(20, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    with_count: bool = Query(False),
):
    limit = _clamp_limit(limit)
    q = db.query(Campaign).filter(Campaign.owner_id == current_user.id)
    q = q.order_by(_order_by_whitelist(Campaign, sort_by, order))
    total = q.count() if with_count else None
    rows = q.offset(offset).limit(limit).all()

    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Limit"] = str(limit)
    response.headers["X-Offset"] = str(offset)
    if total is not None:
        response.headers["X-Total-Count"] = str(total)

    out = []
    for c in rows:
        item = {
            "id": c.id, "title": c.title, "slug": getattr(c, "slug", None),
            "product_id": c.product_id, "owner_id": c.owner_id,
            "commission_rate": c.commission_rate,
            "status": getattr(c, "status", _compute_status(c.starts_at, c.ends_at)),
            "starts_at": c.starts_at, "ends_at": c.ends_at,
            "created_at": getattr(c, "created_at", None),
            "updated_at": getattr(c, "updated_at", None),
        }
        out.append(CampaignOut.model_validate(item) if hasattr(CampaignOut, "model_validate") else CampaignOut(**item))  # type: ignore
    return out


# ====== END (manual) ======
@router.post("/{campaign_id}/end", response_model=CampaignOut, summary="End campaign now")
def end_campaign(
    campaign_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    c = db.query(Campaign).filter(Campaign.id == campaign_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if c.owner_id != current_user.id and getattr(current_user, "role", "user") not in {"admin", "owner"}:
        raise HTTPException(status_code=403, detail="Forbidden")

    c.ends_at = _utcnow()
    if hasattr(c, "status"):
        c.status = "ended"
    db.commit()
    db.refresh(c)

    data = {
        "id": c.id, "title": c.title, "slug": getattr(c, "slug", None),
        "product_id": c.product_id, "owner_id": c.owner_id,
        "commission_rate": c.commission_rate,
        "status": getattr(c, "status", _compute_status(c.starts_at, c.ends_at)),
        "starts_at": c.starts_at, "ends_at": c.ends_at,
        "created_at": getattr(c, "created_at", None),
        "updated_at": getattr(c, "updated_at", None),
    }
    return CampaignOut.model_validate(data) if hasattr(CampaignOut, "model_validate") else CampaignOut(**data)  # type: ignore


# ====== EXTEND / PATCH ======
@router.patch("/{campaign_id}", response_model=CampaignOut, summary="Update campaign (rate/title/dates/extend)")
def update_campaign(
    campaign_id: int,
    payload: CampaignUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    c = db.query(Campaign).filter(Campaign.id == campaign_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if c.owner_id != current_user.id and getattr(current_user, "role", "user") not in {"admin", "owner"}:
        raise HTTPException(status_code=403, detail="Forbidden")

    if payload.title is not None:
        c.title = payload.title.strip()
        if hasattr(c, "slug"):
            c.slug = f"{_slugify(c.title)}-{c.product_id}"

    if payload.rate is not None:
        if not (MIN_RATE <= float(payload.rate) <= MAX_RATE):
            raise HTTPException(status_code=422, detail=f"Rate must be {MIN_RATE}â€“{MAX_RATE}%")
        c.commission_rate = float(payload.rate)

    # Extend convenience
    if payload.extend_days:
        c.ends_at = (c.ends_at or _utcnow()) + timedelta(days=int(payload.extend_days))

    # Direct date updates (guard rails)
    if payload.starts_at:
        c.starts_at = payload.starts_at
    if payload.ends_at:
        if payload.ends_at <= (c.starts_at or _utcnow()):
            raise HTTPException(status_code=422, detail="ends_at must be after starts_at")
        c.ends_at = payload.ends_at

    if hasattr(c, "status"):
        c.status = _compute_status(c.starts_at, c.ends_at)

    db.commit()
    db.refresh(c)

    data = {
        "id": c.id, "title": c.title, "slug": getattr(c, "slug", None),
        "product_id": c.product_id, "owner_id": c.owner_id,
        "commission_rate": c.commission_rate,
        "status": getattr(c, "status", _compute_status(c.starts_at, c.ends_at)),
        "starts_at": c.starts_at, "ends_at": c.ends_at,
        "created_at": getattr(c, "created_at", None),
        "updated_at": getattr(c, "updated_at", None),
    }
    return CampaignOut.model_validate(data) if hasattr(CampaignOut, "model_validate") else CampaignOut(**data)  # type: ignore

