from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from backend.models.announcement import Announcement
from backend.schemas.announcement import AnnouncementCreate

def create_announcement(db: Session, data: AnnouncementCreate):
    new_announcement = Announcement(**data.dict())
    db.add(new_announcement)
    db.commit()
    db.refresh(new_announcement)
    return new_announcement

def get_announcements(db: Session):
    return db.query(Announcement).order_by(Announcement.created_at.desc()).all()

