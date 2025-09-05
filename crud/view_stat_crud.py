from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from backend.models.view_stat import ViewStat

def log_view(db: Session, user_id: int | None, video_post_id: int):
    stat = ViewStat(user_id=user_id, video_post_id=video_post_id)
    db.add(stat)
    db.commit()
    db.refresh(stat)
    return stat

def get_video_stats(db: Session, video_post_id: int):
    return db.query(ViewStat).filter(ViewStat.video_post_id == video_post_id).all()

