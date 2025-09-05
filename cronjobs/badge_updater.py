from backend.schemas.user import UserOut
from backend.db import SessionLocal
from backend.models.live_streams import LiveStream
from backend.models.user import User as UserModel
from backend.models.gift import Gift
from backend.models.leaderboard_notification import LeaderboardNotification
from datetime import datetime

def update_badges():
    db = SessionLocal()
    active_streams = db.query(LiveStream).filter(LiveStream.ended_at == None).all()
    for stream in active_streams:
        top_users = (
            db.query(Gift.user_id)
            .filter(Gift.stream_id == stream.id)
            .group_by(Gift.user_id)
            .order_by(db.func.sum(Gift.amount).desc())
            .limit(3)
            .all()
        )
        for i, (user_id,) in enumerate(top_users):
            user = db.query(User).filter_by(id=user_id).first()
            if user:
                prev = user.badge_level
                if i == 0:
                    user.badge_level = "gold"
                elif i == 1:
                    user.badge_level = "silver"
                elif i == 2:
                    user.badge_level = "bronze"
                if user.badge_level != prev:
                    print(f"Updated user {user.username} to {user.badge_level} badge")
        db.commit()
    db.close()

