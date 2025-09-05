# -*- coding: utf-8 -*-
from __future__ import annotations
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Header, Query, status
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from sqlalchemy import and_

from backend.db import get_db
from backend.auth import get_current_user
from backend.models.user import User
from backend.schemas.gift import GiftCreate, GiftOut
from backend.models.gift import Gift
from backend.crud import gift_crud

router = APIRouter(prefix="/gifts", tags=["Gifts"])

# ---------- Helpers ----------
def _normalize_name(name: str) -> str:
    return (name or "").strip()

def _is_admin(user: User) -> bool:
    return getattr(user, "role", None) in {"admin", "owner"}

# ---------- Create ----------
@router.post("/", response_model=GiftOut, status_code=status.HTTP_201_CREATED)
def create_gift(
    gift: GiftCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    """
    Unda Gift mpya.
    - Inahitaji `admin/owner`.
    - Uniqueness: jina la gift ni la kipekee (case-insensitive).
    - Idempotency (optional): tumia `Idempotency-Key` ikiwa umoongeza column/index upande wa DB/CRUD.
    """
    if not _is_admin(current_user):
        raise HTTPException(status_code=403, detail="Only admin can create gifts")

    name_norm = _normalize_name(gift.name)
    if not name_norm:
        raise HTTPException(status_code=422, detail="Gift name is required")

    # Anti-duplicate (case-insensitive)
    existing = None
    try:
        # Jaribu CRUD ikiwa unayo njia iliyopo
        try:
            existing = gift_crud.get_gift_by_name(db, name_norm)
        except TypeError:
            existing = None
        if not existing:
            existing = (
                db.query(Gift)
                .filter(Gift.name.ilike(name_norm))
                .first()
            )
    except Exception:
        existing = (
            db.query(Gift)
            .filter(Gift.name.ilike(name_norm))
            .first()
        )

    if existing:
        raise HTTPException(status_code=409, detail="Gift with that name already exists")

    # Unda kupitia CRUD ili kuheshimu business rules zako
    try:
        try:
            # Ikiwa umeipanua CRUD kupokea idempotency_key
            return gift_crud.create_gift(db, gift, idempotency_key=idempotency_key)  # type: ignore
        except TypeError:
            return gift_crud.create_gift(db, gift)
    except IntegrityError as ie:
        # DB unique constraint (mf. unique (lower(name)) ) iki-trigger
        db.rollback()
        raise HTTPException(status_code=409, detail="Gift with that name already exists") from ie
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to create gift") from exc

# ---------- List (legacy: returns List[GiftOut]) ----------
@router.get("/", response_model=List[GiftOut])
def list_gifts(
    db: Session = Depends(get_db),
    q: Optional[str] = Query(None, description="Search by name (ILIKE)"),
    is_active: Optional[bool] = Query(None),
    tier: Optional[int] = Query(None, ge=0, description="Filter by tier/level if available"),
    min_price: Optional[float] = Query(None, ge=0),
    max_price: Optional[float] = Query(None, ge=0),
    order: str = Query("created_at", description="Order by field: created_at|price|name"),
    sort: str = Query("desc", pattern="^(asc|desc)$"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """
    Orodha ya gifts (legacy response). Ina filters + pagination lakini inarudisha List tu bila meta.
    Fields zinazotumika kwa filters zinategemea columns kwenye model yako (Gift).
    """
    qset = db.query(Gift)

    if q:
        like = f"%{q.strip()}%"
        qset = qset.filter(Gift.name.ilike(like))

    conds = []
    if is_active is not None and hasattr(Gift, "is_active"):
        conds.append(Gift.is_active == is_active)
    if tier is not None and hasattr(Gift, "tier"):
        conds.append(Gift.tier == tier)
    if min_price is not None and hasattr(Gift, "price"):
        conds.append(Gift.price >= min_price)
    if max_price is not None and hasattr(Gift, "price"):
        conds.append(Gift.price <= max_price)

    if conds:
        qset = qset.filter(and_(*conds))

    # ordering
    colmap = {
        "created_at": getattr(Gift, "created_at", None),
        "price": getattr(Gift, "price", None),
        "name": getattr(Gift, "name", None),
    }
    col = colmap.get(order, getattr(Gift, "created_at", None))
    if col is not None:
        qset = qset.order_by(col.asc() if sort == "asc" else col.desc())

    return qset.offset(offset).limit(limit).all()

# ---------- Page (new: items + meta) ----------
from pydantic import BaseModel, Field

class PageMeta(BaseModel):
    total: int
    limit: int
    offset: int

class GiftPage(BaseModel):
    items: List[GiftOut]
    meta: PageMeta

@router.get("/page", response_model=GiftPage)
def list_gifts_page(
    db: Session = Depends(get_db),
    q: Optional[str] = Query(None),
    is_active: Optional[bool] = Query(None),
    tier: Optional[int] = Query(None, ge=0),
    min_price: Optional[float] = Query(None, ge=0),
    max_price: Optional[float] = Query(None, ge=0),
    order: str = Query("created_at"),
    sort: str = Query("desc", pattern="^(asc|desc)$"),
    limit: int = Query(30, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """
    Orodha ya gifts (paged) yenye `items + meta` — inafaa kwa mobile infinite scroll.
    """
    qset = db.query(Gift)

    if q:
        like = f"%{q.strip()}%"
        qset = qset.filter(Gift.name.ilike(like))

    if is_active is not None and hasattr(Gift, "is_active"):
        qset = qset.filter(Gift.is_active == is_active)
    if tier is not None and hasattr(Gift, "tier"):
        qset = qset.filter(Gift.tier == tier)
    if min_price is not None and hasattr(Gift, "price"):
        qset = qset.filter(Gift.price >= min_price)
    if max_price is not None and hasattr(Gift, "price"):
        qset = qset.filter(Gift.price <= max_price)

    total = qset.count()

    colmap = {
        "created_at": getattr(Gift, "created_at", None),
        "price": getattr(Gift, "price", None),
        "name": getattr(Gift, "name", None),
    }
    col = colmap.get(order, getattr(Gift, "created_at", None))
    if col is not None:
        qset = qset.order_by(col.asc() if sort == "asc" else col.desc())

    items = qset.offset(offset).limit(limit).all()
    return GiftPage(items=items, meta=PageMeta(total=total, limit=limit, offset=offset))
