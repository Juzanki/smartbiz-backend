from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from backend.models.connected_platform import ConnectedPlatform
from backend.schemas.platforms import PlatformConnectRequest
from typing import List
from datetime import datetime

# --- Connect Platform ---


def connect_platform(
        db: Session,
        user_id: int,
        data: PlatformConnectRequest) -> PlatformConnection:
    connection = PlatformConnection(
        user_id=user_id,
        platform=data["platform"],
        access_token=data.access_token,
        connected_at=datetime.utcnow()
    )
    db.add(connection)
    db.commit()
    db.refresh(connection)
    return connection

# --- Get User Connected Platforms ---


def get_connected_platforms(
        db: Session,
        user_id: int) -> List[PlatformConnection]:
    return db.query(PlatformConnection).filter(
        PlatformConnection.user_id == user_id).all()



