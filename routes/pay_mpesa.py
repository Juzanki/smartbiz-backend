# backend/routes/payments_mpesa_routes.py
# -*- coding: utf-8 -*-
from __future__ import annotations
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Header, Query, status
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func

from backend.db import get_db
from backend.models.payment import Payment
from backend.models.user import User
from backend.schemas import PaymentRequest, PaymentResponse, ConfirmMpesaRequest
from backend.auth import get_current_user
from backend.dependencies import check_admin  # admin gate

router = APIRouter(tags=["Payments"])

# === M-PESA Configuration (env-overridable) ===
PAYBILL_NUMBER = os.getenv("MPESA_PAYBILL", "5261077")
ACCOUNT_NAME = os.getenv("MPESA_ACCOUNT_NAME", "Ukumbi wa Mjasiriamali")  # display only
DEFAULT_CURRENCY = os.getenv("PAYMENT_CURRENCY", "TZS")

UTC_NOW = lambda: datetime.now(timezone.utc)

# --- Phone helpers (Tanzania) ---
# Accept: +2557XXXXXXXX / 07XXXXXXXX / 01XXXXXXXX etc., normalize to +255XXXXXXXXX
TZ_LEADING = re.compile(r"^(?:\+?255|0)(\d{9})$")

def normalize_tz_phone(raw: str) -> str:
    s = (raw or "").strip().replace(" ", "").replace("-", "")
    m = TZ_LEADING.match(s)
    if not m:
        raise HTTPException(status_code=422, detail="Invalid Tanzanian phone number")
    return f"+255{m.group(1)}"

# --- Payment helpers ---
ALLOWED_STATUSES = {"pending", "confirmed", "failed", "canceled"}

def ensure_amount_positive(amount: float | int):
    try:
        # Fast validation; money math handled elsewhere in your pipeline
        if float(amount) <= 0:
            raise ValueError()
    except Exception:
        raise HTTPException(status_code=400, detail="Amount must be greater than 0")

def new_reference(prefix: str = "MPESA") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:6].upper()}"

def to_payment_response(p: Payment, instructions: Optional[str] = None) -> PaymentResponse:
    # Build a PaymentResponse even if your model doesnâ€™t have all the fields.
    payload = dict(
        id=getattr(p, "id"),
        reference=getattr(p, "reference"),
        amount=getattr(p, "amount"),
        status=getattr(p, "status"),
        phone_number=getattr(p, "phone_number"),
        method=getattr(p, "method", "mpesa"),
        created_at=getattr(p, "created_at"),
        updated_at=getattr(p, "updated_at", None),
    )
    # Optional extras if your Pydantic model permits:
    if hasattr(p, "currency"):
        payload["currency"] = getattr(p, "currency")
    if instructions:
        payload["instructions"] = instructions
    return PaymentResponse(**payload)


# =============================================================================
# Public: Initiate payment (creates a pending record)
# =============================================================================
@router.post(
    "/pay-mpesa",
    response_model=PaymentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Initiate M-PESA payment (manual Lipa na M-PESA instructions)"
)
def initiate_mpesa_payment(
    payload: PaymentRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
) -> PaymentResponse:
    """
    Create a **pending** payment and return manual Lipa na M-PESA instructions.
    Use the `Idempotency-Key` header to safely retry the same request.
    """
    ensure_amount_positive(payload.amount)
    try:
        normalized_phone = normalize_tz_phone(payload.phone_number)
    except HTTPException:
        # Fall back to original error bubbles up
        raise

    # If client supplied a reference, use it; otherwise generate one.
    reference = (payload.reference or "").strip().upper() or new_reference()

    # Idempotency: if your Payment has idempotency_key, reuse existing
    if idempotency_key and hasattr(Payment, "idempotency_key"):
        existing = (
            db.query(Payment)
              .filter(
                  Payment.user_id == current_user.id,
                  Payment.method == "mpesa",
                  Payment.status.in_(("pending", "confirmed")),
                  getattr(Payment, "idempotency_key") == idempotency_key,
              )
              .order_by(Payment.created_at.desc())
              .first()
        )
        if existing:
            # Return existing record (200)
            from fastapi import Response
            Response.status_code = status.HTTP_200_OK  # type: ignore
            return to_payment_response(existing)

    payment = Payment(
        id=str(uuid.uuid4()),
        user_id=current_user.id,
        method="mpesa",
        amount=payload.amount,
        status="pending",
        phone_number=normalized_phone,
        created_at=UTC_NOW(),
        reference=reference,
    )

    # Optional standard fields
    if hasattr(Payment, "currency"):
        setattr(payment, "currency", DEFAULT_CURRENCY)
    if idempotency_key and hasattr(Payment, "idempotency_key"):
        setattr(payment, "idempotency_key", idempotency_key)

    db.add(payment)
    try:
        db.commit()
        db.refresh(payment)
    except IntegrityError as ie:
        db.rollback()
        # If reference must be unique and collided, mint a new one (or surface 409)
        raise HTTPException(status_code=409, detail=f"Payment conflict: {str(getattr(ie, 'orig', ie))}")
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to create payment: {exc}")

    instructions = (
        "Pay with M-PESA (Lipa na M-PESA):\n"
        f"â€¢ Business Number (Paybill): {PAYBILL_NUMBER}\n"
        f"â€¢ Account: {ACCOUNT_NAME}\n"
        f"â€¢ Amount: {payment.amount} {getattr(payment, 'currency', DEFAULT_CURRENCY)}\n"
        f"â€¢ Reference (Account/Reason): {payment.reference}\n"
        "After paying, return here and confirm using your reference."
    )
    return to_payment_response(payment, instructions=instructions)


# =============================================================================
# Admin: Manually confirm a payment by reference
# =============================================================================
@router.post(
    "/confirm-mpesa",
    response_model=PaymentResponse,
    summary="Admin: confirm an M-PESA payment by reference",
    dependencies=[Depends(check_admin)],
)
def confirm_mpesa_payment(
    payload: ConfirmMpesaRequest,
    db: Session = Depends(get_db),
) -> PaymentResponse:
    """
    Manually confirm a pending M-PESA payment by **reference**.
    Admin-only (prevents users from confirming other users' payments).
    """
    ref = (payload.reference or "").strip().upper()
    if not ref:
        raise HTTPException(status_code=422, detail="Reference is required")

    payment = (
        db.query(Payment)
        .filter(func.upper(Payment.reference) == ref)
        .first()
    )

    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    if payment.status.lower() == "confirmed":
        # Idempotent behavior: return 200 OK with the current (confirmed) record
        return to_payment_response(payment, instructions="Payment already confirmed.")

    # Optional: bind the M-PESA receipt code to the record if your schema has it
    if hasattr(payment, "provider_ref") and getattr(payload, "mpesa_receipt", None):
        setattr(payment, "provider_ref", payload.mpesa_receipt)

    payment.status = "confirmed"
    payment.updated_at = UTC_NOW()
    db.commit()
    db.refresh(payment)

    return to_payment_response(payment, instructions="M-PESA payment confirmed manually.")


# =============================================================================
# User: My payment history (paged)
# =============================================================================
@router.get("/my-payments", summary="List my payments (paged)")
def get_my_payments(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status_filter: Optional[str] = Query(None, pattern="^(pending|confirmed|failed|canceled)$"),
    method: Optional[str] = Query(None, description="Filter by method, e.g. 'mpesa'"),
):
    q = db.query(Payment).filter(Payment.user_id == current_user.id)
    if status_filter:
        q = q.filter(Payment.status == status_filter)
    if method:
        q = q.filter(Payment.method == method)

    total = q.count()
    col = getattr(Payment, "created_at", Payment.id)
    rows = q.order_by(col.desc()).offset(offset).limit(limit).all()

    return {
        "items": [
            {
                "reference": p.reference,
                "amount": p.amount,
                "status": p.status,
                "method": p.method,
                "currency": getattr(p, "currency", DEFAULT_CURRENCY),
                "phone_number": p.phone_number,
                "created_at": getattr(p, "created_at").isoformat() if getattr(p, "created_at", None) else None,
                "updated_at": getattr(p, "updated_at").isoformat() if getattr(p, "updated_at", None) else None,
            }
            for p in rows
        ],
        "meta": {"total": total, "limit": limit, "offset": offset},
    }


# =============================================================================
# Admin: All payments (paged + filters)
# =============================================================================
@router.get("/admin/payments", summary="Admin â€“ view all payments", dependencies=[Depends(check_admin)])
def get_all_payments_for_admin(
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user_id: Optional[int] = Query(None),
    status_filter: Optional[str] = Query(None, pattern="^(pending|confirmed|failed|canceled)$"),
    method: Optional[str] = Query(None),
    q_ref: Optional[str] = Query(None, description="Search by reference (case-insensitive, partial)"),
):
    q = db.query(Payment)
    if user_id is not None:
        q = q.filter(Payment.user_id == user_id)
    if status_filter:
        q = q.filter(Payment.status == status_filter)
    if method:
        q = q.filter(Payment.method == method)
    if q_ref:
        q = q.filter(func.upper(Payment.reference).like(f"%{q_ref.strip().upper()}%"))

    total = q.count()
    col = getattr(Payment, "created_at", Payment.id)
    rows = q.order_by(col.desc()).offset(offset).limit(limit).all()

    return {
        "items": [
            {
                "reference": p.reference,
                "amount": p.amount,
                "status": p.status,
                "method": p.method,
                "currency": getattr(p, "currency", DEFAULT_CURRENCY),
                "phone_number": p.phone_number,
                "user_id": p.user_id,
                "created_at": getattr(p, "created_at").isoformat() if getattr(p, "created_at", None) else None,
                "updated_at": getattr(p, "updated_at").isoformat() if getattr(p, "updated_at", None) else None,
            }
            for p in rows
        ],
        "meta": {"total": total, "limit": limit, "offset": offset},
    }

