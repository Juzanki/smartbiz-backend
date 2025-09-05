# -*- coding: utf-8 -*-
from __future__ import annotations
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Header, Query, status
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from backend.db import get_db
from backend.auth import get_current_user
from backend.models.user import User
from backend.schemas.gift_ad import (
    GiftTransactionCreate, GiftTransactionOut,
    AdEarningCreate, AdEarningOut
)
from backend.crud import gift_ad_crud
from backend.models.gift_transaction import GiftTransaction
from backend.models.ad_earning import AdEarning

router = APIRouter(
    prefix="/smartcoin",
    tags=["SmartCoin Earnings"]
)

# -------- Helpers --------
def _force_sender(payload: GiftTransactionCreate, user_id: int) -> GiftTransactionCreate:
    """Override sender_id from current_user (ignore client value)."""
    try:
        # Pydantic v2
        return payload.model_copy(update={"sender_id": user_id})
    except AttributeError:
        # Pydantic v1
        return payload.copy(update={"sender_id": user_id})


# -------- Endpoints --------
@router.post("/send-gift", response_model=GiftTransactionOut, status_code=status.HTTP_201_CREATED)
def send_gift(
    gift: GiftTransactionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    """
    Tuma zawadi (SmartCoin):
    - sender_id hutoka *daima* kwa current_user (client input inapuuzwa).
    - Idempotency-Key (optional) kuzuia malipo kurudiwa kwa bahati mbaya (duplicate POST).
    """
    # Security: force sender
    gift = _force_sender(gift, current_user.id)

    # Basic validations
    if getattr(gift, "amount", 0) is None or gift.amount <= 0:
        raise HTTPException(status_code=422, detail="Amount must be greater than 0")
    if gift.recipient_id == current_user.id:
        raise HTTPException(status_code=400, detail="You cannot send a gift to yourself")

    # Role check is not needed here (any logged-in user can send)
    try:
        # Support idempotency without breaking older CRUD signatures
        try:
            return gift_ad_crud.send_gift_and_credit(db, gift, idempotency_key=idempotency_key)
        except TypeError:
            # Fallback if CRUD has not been updated for idempotency_key
            if idempotency_key:
                # If your DB has a unique index on (idempotency_key), this will raise IntegrityError on dup
                pass
            return gift_ad_crud.send_gift_and_credit(db, gift)
    except IntegrityError as ie:
        db.rollback()
        # Heuristic: if DB constraint for idempotency triggers
        raise HTTPException(status_code=409, detail="Duplicate request (idempotency)") from ie
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to send gift") from exc


@router.post("/credit-ad", response_model=AdEarningOut)
def credit_ad_earning(
    ad: AdEarningCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    """
    Credit ya mapato ya matangazo (admin/owner only).
    - Inapendekezwa kuweka idempotency ili kuepuka double credit.
    """
    if current_user.role not in {"admin", "owner"}:
        raise HTTPException(status_code=403, detail="Only admin can credit ads")

    if getattr(ad, "amount", 0) is None or ad.amount <= 0:
        raise HTTPException(status_code=422, detail="Amount must be greater than 0")

    try:
        try:
            return gift_ad_crud.credit_ad_earning(db, ad, idempotency_key=idempotency_key)
        except TypeError:
            return gift_ad_crud.credit_ad_earning(db, ad)
    except IntegrityError as ie:
        db.rollback()
        raise HTTPException(status_code=409, detail="Duplicate request (idempotency)") from ie
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to credit ad earning") from exc


# ------- My earnings (legacy: list only) -------
@router.get("/my-gifts", response_model=List[GiftTransactionOut])
def my_gift_earnings(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """
    Zawadi *zilizopokelewa* na mimi (legacy list). Inasaidia limit/offset bila meta.
    Tumia /my-gifts/page kwa pagination yenye meta.
    """
    return (
        db.query(GiftTransaction)
        .filter(GiftTransaction.recipient_id == current_user.id)
        .order_by(GiftTransaction.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )


@router.get("/my-ads", response_model=List[AdEarningOut])
def my_ad_earnings(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """
    Mapato ya matangazo ya akaunti yangu (legacy list). Inasaidia limit/offset bila meta.
    Tumia /my-ads/page kwa pagination yenye meta.
    """
    return (
        db.query(AdEarning)
        .filter(AdEarning.user_id == current_user.id)
        .order_by(AdEarning.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )


# ------- Paginated versions (new) -------
from pydantic import BaseModel, Field

class PageMeta(BaseModel):
    total: int = Field(..., description="Total records matching the filter")
    limit: int
    offset: int

class GiftPage(BaseModel):
    items: List[GiftTransactionOut]
    meta: PageMeta

class AdPage(BaseModel):
    items: List[AdEarningOut]
    meta: PageMeta


@router.get("/my-gifts/page", response_model=GiftPage)
def my_gifts_page(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """
    Paginated: zawadi *zilizopokelewa* na mimi, zenye `items + meta`.
    Bora kwa mobile infinite scroll / lazy lists.
    """
    q = db.query(GiftTransaction).filter(GiftTransaction.recipient_id == current_user.id)
    total = q.count()
    items = (
        q.order_by(GiftTransaction.created_at.desc())
         .offset(offset).limit(limit).all()
    )
    return GiftPage(items=items, meta=PageMeta(total=total, limit=limit, offset=offset))


@router.get("/my-ads/page", response_model=AdPage)
def my_ads_page(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """
    Paginated: mapato ya matangazo ya akaunti yangu, `items + meta`.
    """
    q = db.query(AdEarning).filter(AdEarning.user_id == current_user.id)
    total = q.count()
    items = (
        q.order_by(AdEarning.created_at.desc())
         .offset(offset).limit(limit).all()
    )
    return AdPage(items=items, meta=PageMeta(total=total, limit=limit, offset=offset))
