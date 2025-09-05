from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from backend.models.post_live_notification import PostLiveNotification
from backend.models.follow import Follow  # assumes you have follow model

def notify_followers_post_live(db: Session, stream_id: int, message: str):
    from backend.models.live_stream import LiveStream
    stream = db.query(LiveStream).filter(LiveStream.id == stream_id).first()
    if not stream:
        return []

    # get all followers of the host
    followers = db.query(Follow).filter(Follow.following_id == stream.host_id).all()
    notifications = []
    for f in followers:
        n = PostLiveNotification(user_id=f.follower_id, stream_id=stream_id, message=message)
        db.add(n)
        notifications.append(n)
    db.commit()
    return notifications

def get_user_notifications(db: Session, user_id: int):
    return db.query(PostLiveNotification).filter(PostLiveNotification.user_id == user_id).order_by(PostLiveNotification.created_at.desc()).all()

