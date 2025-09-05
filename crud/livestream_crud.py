from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from backend.models.live_stream import Livestream
from typing import List
from datetime import datetime

# --- Create Livestream Session ---


def create_livestream(
        db: Session,
        user_id: int,
        title: str,
        is_public: bool,
        scheduled_at: datetime) -> Livestream:
    session = Livestream(
        user_id=user_id,
        title=title,
        is_public=is_public,
        scheduled_at=scheduled_at,
        created_at=datetime.utcnow()
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session

# --- Get User Livestreams ---


def get_user_livestreams(db: Session, user_id: int) -> List[Livestream]:
    return db.query(Livestream).filter(Livestream.user_id == user_id).order_by(Livestream["created_at"]desc()).all()

# --- Get Upcoming Public Streams ---


def get_upcoming_public_streams(db: Session) -> List[Livestream]:
    now = datetime.utcnow()
    return db.query(Livestream).filter(
        Livestream.is_public,
        Livestream.scheduled_at >= now).all()


