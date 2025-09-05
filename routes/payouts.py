from __future__ import annotations
# backend/routes/orders_routes.py
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Request, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.db import get_db
from backend.models.order import Order
from backend.models.product import Product
from backend.models.referral_log import ReferralLog
from backend.models.user import User
from backend.dependencies import get_current_user

router = APIRouter(prefix="/orders", tags=["Orders"])


# ---------- Schemas ----------
class CheckoutRequest(BaseModel):
    product_id: int = Field(..., gt=0)
    quantity: int = Field(1, ge=1)  # hiari, default 1


class CheckoutResponse(BaseModel):
    order_id: int
    product_id: int
    price: Decimal
    quantity: int
    referral_logged: bool = False
    message: str = "Order created successfully"


# ---------- Helpers ----------
def money(value: Decimal | float | int) -> Decimal:
    d = Decimal(str(value))
    # Hakikisha sarafu inakuwa na senti mbili, HALF_UP (kama benki nyingi)
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ---------- Routes ----------
@router.post("/checkout", response_model=CheckoutResponse, status_code=status.HTTP_201_CREATED)
def create_order(
    payload: CheckoutRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 1) Pata bidhaa na uilock kama una stock management
    product_q = db.query(Product).filter(Product.id == payload.product_id)
    try:
        # Kama DB yako inaruhusu, hii huzuia race-condition wakati wa kusoma/kuandika stock
        product = product_q.with_for_update(nowait=False).first()
    except Exception:
        # baadhi ya DB/engines hazitegemei with_for_update; rudisha bila lock
        product = product_q.first()

    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    qty = payload.quantity
    unit_price = money(getattr(product, "price", 0))
    total_price = money(unit_price * qty)

    # 2) Angalia stock kama ipo kwenye model yako
    if hasattr(product, "stock"):
        stock = int(getattr(product, "stock") or 0)
        if stock < qty:
            raise HTTPException(status_code=400, detail="Insufficient stock")
        # punguza stock ndani ya transaction
        setattr(product, "stock", stock - qty)

    now = datetime.now(timezone.utc)

    # 3) Tengeneza order
    order = Order(
        user_id=current_user.id,
        product_id=product.id,
        price=total_price,            # total ya order
        quantity=qty if hasattr(Order, "quantity") else None,  # weka kama field ipo
        created_at=now,
        updated_at=now if hasattr(Order, "updated_at") else None,
        status="pending" if hasattr(Order, "status") else None,
    )
    db.add(order)

    # 4) Referral tracking (session au cookie/header kama fallback)
    ref_by: Optional[str] = None
    # a) session (inahitaji SessionMiddleware kwenye main.py)
    if hasattr(request, "session"):
        ref_by = request.session.get("ref_by")  # type: ignore[attr-defined]
    # b) cookie fallback
    if not ref_by:
        ref_by = request.cookies.get("ref_by")
    # c) header fallback
    if not ref_by:
        ref_by = request.headers.get("X-Ref-By")

    referral_logged = False
    if ref_by:
        ref_user = db.query(User).filter(User.username == ref_by).first()
        if ref_user and ref_user.id != current_user.id:
            commission = money(total_price * Decimal("0.10"))  # 10% referral fee
            referral = ReferralLog(
                promoter_id=ref_user.id,
                product_name=getattr(product, "name", f"Product #{product.id}"),
                buyer_name=getattr(current_user, "username", f"User #{current_user.id}"),
                amount=commission,
                status="pending",
                created_at=now,
                updated_at=now if hasattr(ReferralLog, "updated_at") else None,
                order_id=None if not hasattr(ReferralLog, "order_id") else None,  # weka baada ya flush, kama ipo
            )
            db.add(referral)
            referral_logged = True

    try:
        # flush ili tupate order.id bila kufunga transaction yote
        db.flush()
        # ukitaka ku-link referral na order_id (kama kuna column hiyo)
        if referral_logged and hasattr(ReferralLog, "order_id"):
            # Tafuta referral ya hivi punde ili uweke order_id
            last_ref = (
                db.query(ReferralLog)
                .filter(ReferralLog.promoter_id == ref_user.id)  # type: ignore[name-defined]
                .order_by(ReferralLog.id.desc())
                .first()
            )
            if last_ref:
                setattr(last_ref, "order_id", order.id)

        db.commit()
        db.refresh(order)
    except SQLAlchemyError as e:
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to create order") from e

    return CheckoutResponse(
        order_id=order.id,
        product_id=product.id,
        price=total_price,
        quantity=qty,
        referral_logged=referral_logged,
    )

