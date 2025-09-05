from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from datetime import datetime, timedelta

from backend.models.message import MessageLog
from backend.models import SmartOrder
from backend.models.payment import Payment
# --- Get Message Count ---


def count_messages(db: Session, user_id: int) -> int:
    return db.query(MessageLog).filter(
        MessageLog.sender_id == str(user_id)).count()

# --- Get Order Count ---


def count_orders(db: Session, user_id: int) -> int:
    return db.query(SmartOrder).filter(
        SmartOrder.customer_id == user_id).count()

# --- Get Payment Total ---


def total_payments(db: Session, user_id: int) -> float:
    records = db.query(Payment).filter(Payment.user_id == user_id).all()
    return sum([p.amount for p in records])

# --- Count This Week ---


def count_this_week(db: Session, model, user_field: str, user_id: int):
    now = datetime.utcnow()
    last_7_days = now - timedelta(days=7)
    query = db.query(model).filter(
        getattr(model, user_field) == user_id,
        model["created_at"] >= last_7_days
    )
    return query.count()


