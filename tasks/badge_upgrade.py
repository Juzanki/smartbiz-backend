# === backend/tasks/badge_upgrade.py ===

from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime

from backend.models.user import User
from backend.models.gift_transaction import GiftTransaction
from backend.models.live_stream import LiveStream
from backend.models.badge_history import BadgeHistory
from backend.db import SessionLocal

# === Threshold Constants ===
TOP_FAN_THRESHOLD = 100000    # Coins sent
STREAMER_THRESHOLD = 5        # Live sessions hosted
SUPPORTER_THRESHOLD = 50      # Interactions (likes, comments, shares)


# === Helper: Save badge history record ===
def save_badge_history(user_id: int, badge_type: str, db: Session):
    history = BadgeHistory(
        user_id=user_id,
        badge_type=badge_type,
        timestamp=datetime.utcnow()
    )
    db.add(history)


# === Helper: Notify user (optional future implementation) ===
# from backend.models import Notification
# def notify_user(user_id: int, badge_type: str, db: Session):
#     message = f"ðŸŽ‰ You've earned the '{badge_type}' badge! Keep it up!"
#     db.add(Notification(
#         user_id=user_id,
#         type="badge_upgrade",
#         message=message,
#         created_at=datetime.utcnow()
#     ))


# === Main Logic: Calculate and Assign Badge ===
def calculate_user_badge(user: User, db: Session):
    # Gifts sent
    total_coins_sent = db.query(func.sum(GiftTransaction.coin_amount)).filter_by(sender_id=user.id).scalar() or 0

    # Live streams hosted
    total_streams = db.query(LiveStream).filter_by(host_id=user.id).count()

    # Social interactions
    total_interactions = (
        (user.total_likes or 0) +
        (user.total_comments or 0) +
        (user.total_shares or 0)
    )

    # Determine new badge
    new_badge = None
    if total_coins_sent >= TOP_FAN_THRESHOLD:
        new_badge = "top-fan"
    elif total_streams >= STREAMER_THRESHOLD:
        new_badge = "streamer"
    elif total_interactions >= SUPPORTER_THRESHOLD:
        new_badge = "supporter"

    # Only update if badge is different
    if new_badge and user.badge_type != new_badge:
        print(f"[Badge Upgrade] {user.display_name} â†’ {new_badge}")
        user.badge_type = new_badge
        save_badge_history(user.id, new_badge, db)
        # notify_user(user.id, new_badge, db)
    else:
        print(f"[Badge Check] {user.display_name} retains badge: {user.badge_type or 'None'}")


# === Runner: Batch upgrade task ===
def run_badge_upgrade_task():
    db = SessionLocal()
    try:
        users = db.query(User).all()
        for user in users:
            calculate_user_badge(user, db)
        db.commit()
        print("[âœ”] Badge upgrade task completed.")
    except Exception as e:
        db.rollback()
        print(f"[âŒ] Badge upgrade failed: {e}")
    finally:
        db.close()

