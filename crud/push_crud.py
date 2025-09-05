from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from backend.models.push_subscription import PushSubscription
from backend.schemas.push_subscription import PushSubscriptionCreate
from datetime import datetime

def create_or_update_subscription(db: Session, sub: PushSubscriptionCreate):
    # Check if it exists
    existing = db.query(PushSubscription).filter(PushSubscription.endpoint == sub.endpoint).first()
    if existing:
        existing.p256dh = sub.p256dh
        existing.auth = sub.auth
        db.commit()
        db.refresh(existing)
        return existing

    new_sub = PushSubscription(
        user_id=sub.user_id,
        endpoint=sub.endpoint,
        p256dh=sub.p256dh,
        auth=sub.auth,
        created_at=datetime.utcnow()
    )
    db.add(new_sub)
    db.commit()
    db.refresh(new_sub)
    return new_sub

def get_user_subscriptions(db: Session, user_id: int):
    return db.query(PushSubscription).filter(PushSubscription.user_id == user_id).all()

