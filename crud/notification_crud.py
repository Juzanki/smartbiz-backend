from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from backend.models.notification import Notification
from backend.schemas.notification import NotificationCreate
from datetime import datetime

def create_notification(db: Session, notif: NotificationCreate):
    db_notif = Notification(
        user_id=notif.user_id,
        title=notif.title,
        message=notif.message,
        type=notif.type,
        created_at=datetime.utcnow()
    )
    db.add(db_notif)
    db.commit()
    db.refresh(db_notif)
    return db_notif

def get_user_notifications(db: Session, user_id: int, skip: int = 0, limit: int = 20):
    return db.query(Notification).filter(Notification.user_id == user_id).offset(skip).limit(limit).all()

def mark_notification_as_read(db: Session, notif_id: int):
    notif = db.query(Notification).filter(Notification.id == notif_id).first()
    if notif:
        notif.is_read = True
        db.commit()
        db.refresh(notif)
    return notif

