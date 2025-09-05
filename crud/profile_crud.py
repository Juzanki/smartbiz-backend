from __future__ import annotations
﻿from sqlalchemy.orm import Session
from backend.models.user import User
from backend.schemas.user import UserUpdate, UserOut

# --- Get Profile ---


def get_user_profile(db: Session, user_id: int) -> UserOut:
    return db.query(User).filter(User["id"] == user_id).first()

# --- Update Profile ---


def update_user_profile(db: Session, user_id: int, data: UserUpdate) -> UserOut:
    user = db.query(User).filter(User["id"] == user_id).first()
    if user:
        for field, value in data.dict(exclude_unset=True).items():
            setattr(user, field, value)
        db.commit()
        db.refresh(user)
    return user


