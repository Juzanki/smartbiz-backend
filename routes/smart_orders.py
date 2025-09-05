# backend/routes/smart_orders.py
# -*- coding: utf-8 -*-
from __future__ import annotations
"""
Smart Orders routes: place an order via QR/NFC with safe stock decrement.

Key improvements:
- Strong input validation (positive IDs, quantity >= 1)
- Row-level lock on Product to prevent overselling under concurrency
- Decimal-based money math (rounded to 2dp)
- Timezone-aware timestamps
- Clear response model & HTTP statuses
- Robust error handling + rollback on failure
"""

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, conint
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.db import get_db
from backend.models.order import Order
from backend.models.product import Product
from backend.models.user import User
from backend.auth import get_current_user

router = APIRouter(prefix="/smart-orders", tags=["Smart Orders"])


# ====== Schemas ======
class SmartOrderRequest(BaseModel):
    product_id: conint(strict=True, ge=1) = Field(..., description="Product ID to purchase")
    quantity: conint(strict=True, ge=1) = Field(..., description="Units to purchase (>=1)")
    channel: Literal["qr", "nfc", "link"] = Field("qr", description="How the order was initiated")


class SmartOrderResponse(BaseModel):
    message: str
    order_id: int
    product_id: int
    product_name: str
    quantity: int
    unit_price: float
    total: float
    status: str


# ====== Helpers ======
def _to_decimal(x) -> Decimal:
    """
    Convert numeric DB value to Decimal safely.
    If already Decimal, return as-is; if None, treat as 0.
    """
    if x is None:
        return Decimal("0")
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))


# ====== Routes ======
@router.post(
    "/place",
    response_model=SmartOrderResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Place order via QR/NFC",
)
def place_smart_order(
    payload: SmartOrderRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SmartOrderResponse:
    """
    Reserve stock and create a 'pending' order in a single, atomic transaction.

    Concurrency safety:
    - Locks the Product row via SELECT ... FOR UPDATE to prevent race conditions
      when multiple customers attempt to buy the last units simultaneously.
    """
    try:
        # Lock the product row to prevent concurrent decrements
        product: Product | None = (
            db.query(Product)
            .filter(Product.id == payload.product_id)
            .with_for_update()  # row-level lock until commit/rollback
            .one_or_none()
        )

        if not product:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")

        if product.stock is None or product.stock < payload.quantity:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Insufficient stock",
            )

        unit_price_dec = _to_decimal(product.price)
        total_dec = (unit_price_dec * Decimal(payload.quantity)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        # Create order (pending) and reserve stock
        new_order = Order(
            user_id=current_user.id,
            product_id=product.id,
            quantity=payload.quantity,
            total=float(total_dec),  # if your column is Numeric(10,2), Decimal also works
            status="pending",
            created_at=datetime.now(timezone.utc),
            # Optional: store channel if your Order model has it
            # channel=payload.channel,
        )

        product.stock = (product.stock or 0) - payload.quantity  # decrement reserved stock

        db.add(new_order)
        db.commit()
        db.refresh(new_order)

        return SmartOrderResponse(
            message="âœ… Order placed successfully via QR/NFC",
            order_id=new_order.id,
            product_id=product.id,
            product_name=getattr(product, "name", f"Product #{product.id}"),
            quantity=payload.quantity,
            unit_price=float(unit_price_dec),
            total=float(total_dec),
            status=new_order.status,
        )

    except HTTPException:
        # Bubble up known client errors
        db.rollback()
        raise
    except SQLAlchemyError as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database error while placing order",
        ) from e
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unexpected error while placing order",
        ) from e

