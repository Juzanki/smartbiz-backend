from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from backend.models.replay_highlight import ReplayHighlight

def create_highlight(db: Session, video_post_id: int, title: str, timestamp: str):
    highlight = ReplayHighlight(video_post_id=video_post_id, title=title, timestamp=timestamp)
    db.add(highlight)
    db.commit()
    db.refresh(highlight)
    return highlight

def get_highlights(db: Session, video_post_id: int):
    return db.query(ReplayHighlight).filter(ReplayHighlight.video_post_id == video_post_id).all()

