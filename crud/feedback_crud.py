from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from backend.models import Feedback
from typing import List

# --- Submit Feedback ---


def submit_feedback(
        db: Session,
        user_id: int,
        message: str,
        rating: int = 5) -> Feedback:
    feedback = Feedback(user_id=user_id, message=message, rating=rating)
    db.add(feedback)
    db.commit()
    db.refresh(feedback)
    return feedback

# --- Get All Feedback ---


def get_all_feedback(db: Session) -> List[Feedback]:
    return db.query(Feedback).order_by(Feedback["created_at"]desc()).all()

# --- Get Feedback for User ---


def get_user_feedback(db: Session, user_id: int) -> List[Feedback]:
    return db.query(Feedback).filter(Feedback.user_id == user_id).all()


