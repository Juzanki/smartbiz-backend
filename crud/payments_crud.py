from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from typing import List
from backend.models.payment import Payment
from backend.schemas.payments import PaymentResponse

# --- Get All Payments ---


def get_all_payments(db: Session) -> List[Payment]:
    return db.query(Payment).order_by(Payment["created_at"]desc()).all()

# --- Get Payments by User ---


def get_user_payments(db: Session, user_id: int) -> List[Payment]:
    return db.query(Payment).filter(Payment.user_id == user_id).order_by(Payment["created_at"]desc()).all()

# --- Get Payment by Reference ---


def get_payment_by_reference(db: Session, reference: str) -> Payment:
    return db.query(Payment).filter(Payment.reference == reference).first()


