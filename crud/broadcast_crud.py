from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from typing import List
from backend.models import Broadcast
from datetime import datetime

# --- Create Broadcast ---


def create_broadcast(
        db: Session,
        user_id: int,
        message: str,
        channels: List[str]) -> Broadcast:
    broadcast = Broadcast(
        user_id=user_id,
        message=message,
        channels=','.join(channels),
        created_at=datetime.utcnow()
    )
    db.add(broadcast)
    db.commit()
    db.refresh(broadcast)
    return broadcast

# --- Get Broadcasts by User ---


def get_user_broadcasts(db: Session, user_id: int) -> List[Broadcast]:
    return db.query(Broadcast).filter(Broadcast.user_id == user_id).order_by(Broadcast["created_at"]desc()).all()


