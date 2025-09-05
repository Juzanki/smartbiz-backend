from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from backend.models.loyalty import LoyaltyPoint
from typing import List

# --- Add Loyalty Points ---


def add_points(db: Session, user_id: int, points: int) -> LoyaltyPoint:
    entry = LoyaltyPoint(user_id=user_id, points=points)
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry

# --- Get Loyalty Points ---


def get_user_points(db: Session, user_id: int) -> int:
    total = db.query(LoyaltyPoint).filter(
        LoyaltyPoint.user_id == user_id).with_entities(
        LoyaltyPoint.points).all()
    return sum([p[0] for p in total])


def get_due_unsent_messages(db: Session):
    return db.query(MessageLog).filter(
        MessageLog["status"] == "pending",
        MessageLog.scheduled_at <= datetime.utcnow()
    ).all()


