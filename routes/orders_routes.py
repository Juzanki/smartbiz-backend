# backend/routes/orders_checkout.py
# -*- coding: utf-8 -*-
from __future__ import annotations
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Optional, Dict, Any

from fastapi import APIRouter, Request, Depends, HTTPException, Header, status, Query
from pydantic import BaseModel, Field, ConfigDict, PositiveInt
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select

from backend.db import get_db
from backend.models.order import Order
from backend.models.product import Product
from backend.models.referral_log import ReferralLog
from backend.models.user import User
from backend.dependencies import get_current_user

router = APIRouter(prefix="/orders", tags=["Orders"])

TZS = "TZS"
REFERRAL_RATE = Decimal("0.10")  # 10%

# ---------- Schemas ----------
class CheckoutRequest(BaseModel):
    product_id: int
    quantity: PositiveInt = Field(1, description="Units to purchase")
    # Optional client-side price confirmation (defense-in-depth against sudden price changes)
    confirm_unit_price: Optional[Decimal] = Field(None, ge=0)

class OrderOut(BaseModel):
    order_id: int
    order_number: Optional[str] = None
    status: str = "pending"
    currency: str = TZS
    product_id: int
    product_name: str
    quantity: int
    unit_price: str
    subtotal: str
    referral_applied: bool = False
    referral_commission: str = "0.00"
    promoter_username: Optional[str] = None
    message: str = "Order created successfully"
    model_config = ConfigDict(from_attributes=True)

# ---------- Helpers ----------
def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def q2(v: Decimal | float | int) -> Decimal:
    try:
        return Decimal(v).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        raise HTTPException(status_code=422, detail="Invalid monetary value")

def gen_order_number() -> str:
    # Example: ORD-YYYYMM-<6 hex>
    return f"ORD-{utcnow():%Y%m}-{hex(int(utcnow().timestamp()*1e6))[2:].upper()[:6]}"

def get_ref_by(request: Request) -> Optional[str]:
    # Prefer session; allow query param as fallback
    ref = request.session.get("ref_by") if hasattr(request, "session") else None
    if not ref:
        ref = request.query_params.get("ref_by")
    if ref:
        ref = str(ref).strip()
    return ref or None

# ---------- Endpoint ----------
@router.post(
    "/checkout",
    response_model=OrderOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create an order (with optional referral tracking)"
)
def create_order(
    payload: CheckoutRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    """
    Creates a new order:
    - Validates product and (if available) stock.
    - Uses Decimal for money calculations.
    - Supports `Idempotency-Key` (if your `Order` model has `idempotency_key`).
    - Tracks referrals from `session['ref_by']` (or `?ref_by=`) and prevents self-referrals.
    """

    # Idempotency: if key exists and model supports it, return existing order
    if idempotency_key and hasattr(Order, "idempotency_key"):
        existing = (
            db.query(Order)
              .filter(getattr(Order, "idempotency_key") == idempotency_key, getattr(Order, "user_id") == current_user.id)
              .order_by(getattr(Order, "id").desc())
              .first()
        )
        if existing:
            # 200 OK with existing payload
            return _order_to_out(db, existing, message="Order previously created (idempotent)")

    # Lock the product row if possible to avoid race on stock (optional)
    product = None
    try:
        product = db.execute(
            select(Product).where(Product.id == payload.product_id).with_for_update()
        ).scalar_one_or_none()
    except Exception:
        # Fallback without explicit lock
        product = db.query(Product).filter(Product.id == payload.product_id).first()

    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    unit_price = q2(getattr(product, "price", 0))
    if payload.confirm_unit_price is not None and q2(payload.confirm_unit_price) != unit_price:
        raise HTTPException(status_code=409, detail="Price changed. Please review before proceeding.")

    # Stock check (only if model has a stock field)
    if hasattr(product, "stock"):
        current_stock = int(getattr(product, "stock") or 0)
        if current_stock < payload.quantity:
            raise HTTPException(status_code=409, detail="Insufficient stock")
        # reserve/decrement
        setattr(product, "stock", current_stock - payload.quantity)

    subtotal = q2(unit_price * payload.quantity)

    # Build order
    order_values: Dict[str, Any] = dict(
        user_id=current_user.id,
        product_id=product.id,
        price=unit_price,       # keep original column if you use 'price'
        created_at=utcnow(),
    )
    # Optional/modern fields if present
    if hasattr(Order, "quantity"):
        order_values["quantity"] = payload.quantity
    if hasattr(Order, "currency"):
        order_values["currency"] = TZS
    if hasattr(Order, "status"):
        order_values["status"] = "pending"
    if hasattr(Order, "total"):
        order_values["total"] = subtotal
    if hasattr(Order, "order_number"):
        order_values["order_number"] = gen_order_number()
    if idempotency_key and hasattr(Order, "idempotency_key"):
        order_values["idempotency_key"] = idempotency_key

    order = Order(**order_values)
    db.add(order)

    # Referral tracking
    promoter_username = None
    commission = Decimal("0.00")
    ref_by = get_ref_by(request)
    if ref_by:
        ref_user = db.query(User).filter(User.username == ref_by).first()
        if ref_user and ref_user.id != current_user.id:
            promoter_username = ref_user.username
            commission = q2(subtotal * REFERRAL_RATE)
            referral_vals: Dict[str, Any] = dict(
                promoter_id=ref_user.id,
                product_name=getattr(product, "name", f"Product {product.id}"),
                buyer_name=getattr(current_user, "username", f"User {current_user.id}"),
                amount=commission,
                status="pending",
                created_at=utcnow(),
            )
            # Optional links if your model supports them
            if hasattr(ReferralLog, "order_id"):
                referral_vals["order_id"] = getattr(order, "id", None)
            if hasattr(ReferralLog, "product_id"):
                referral_vals["product_id"] = product.id
            db.add(ReferralLog(**referral_vals))

    try:
        db.commit()
        db.refresh(order)
    except IntegrityError as ie:
        db.rollback()
        # If idempotency key unique constraint raced, fetch and return existing
        if idempotency_key and hasattr(Order, "idempotency_key"):
            existing = (
                db.query(Order)
                  .filter(getattr(Order, "idempotency_key") == idempotency_key, getattr(Order, "user_id") == current_user.id)
                  .order_by(getattr(Order, "id").desc())
                  .first()
            )
            if existing:
                return _order_to_out(db, existing, message="Order previously created (idempotent)")
        raise HTTPException(status_code=409, detail=f"Order conflict: {ie.orig if hasattr(ie, 'orig') else str(ie)}")
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to create order: {exc}")

    return OrderOut(
        order_id=getattr(order, "id"),
        order_number=getattr(order, "order_number", None),
        status=str(getattr(order, "status", "pending")),
        currency=getattr(order, "currency", TZS),
        product_id=product.id,
        product_name=getattr(product, "name", f"Product {product.id}"),
        quantity=getattr(order, "quantity", payload.quantity),
        unit_price=str(unit_price),
        subtotal=str(subtotal),
        referral_applied=bool(promoter_username),
        referral_commission=str(commission),
        promoter_username=promoter_username,
        message="Order created successfully",
    )

# ---------- Internal: convert existing order to response ----------
def _order_to_out(db: Session, order: Order, message: str) -> OrderOut:
    # Fetch product name for a better response
    product = db.query(Product).filter(Product.id == getattr(order, "product_id")).first()
    unit_price = q2(getattr(order, "price", 0))
    qty = getattr(order, "quantity", 1)
    subtotal = q2(getattr(order, "total", unit_price * qty))

    return OrderOut(
        order_id=getattr(order, "id"),
        order_number=getattr(order, "order_number", None),
        status=str(getattr(order, "status", "pending")),
        currency=getattr(order, "currency", TZS),
        product_id=getattr(order, "product_id"),
        product_name=getattr(product, "name", f"Product {getattr(order, 'product_id')}") if product else f"Product {getattr(order, 'product_id')}",
        quantity=qty,
        unit_price=str(unit_price),
        subtotal=str(subtotal),
        referral_applied=False,  # unknown on re-fetch; compute if you store it on Order
        referral_commission="0.00",
        promoter_username=None,
        message=message,
    )

