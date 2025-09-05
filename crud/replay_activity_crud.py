from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from backend.models.replay_activity_log import ReplayActivityLog

def log_activity(db: Session, user_id: int | None, video_post_id: int, action: str, platform: str | None = None):
    log = ReplayActivityLog(user_id=user_id, video_post_id=video_post_id, action=action, platform=platform)
    db.add(log)
    db.commit()
    db.refresh(log)
    return log

