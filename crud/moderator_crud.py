from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from backend.models import Moderator
from typing import List

# --- Add Moderator ---


def add_moderator(db: Session, stream_id: int, user_id: int) -> Moderator:
    mod = Moderator(stream_id=stream_id, user_id=user_id)
    db.add(mod)
    db.commit()
    db.refresh(mod)
    return mod

# --- Remove Moderator ---


def remove_moderator(db: Session, stream_id: int, user_id: int) -> bool:
    mod = db.query(Moderator).filter(
        Moderator.stream_id == stream_id,
        Moderator.user_id == user_id).first()
    if mod:
        db.delete(mod)
        db.commit()
        return True
    return False

# --- Get Moderators for a Stream ---


def get_moderators(db: Session, stream_id: int) -> List[Moderator]:
    return db.query(Moderator).filter(Moderator.stream_id == stream_id).all()


