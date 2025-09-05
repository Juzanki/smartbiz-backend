from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from backend.models import StreamComment
from datetime import datetime
from typing import List

# --- Add Comment to Stream ---


def add_comment(
        db: Session,
        stream_id: int,
        user_id: int,
        message: str) -> StreamComment:
    comment = StreamComment(
        stream_id=stream_id,
        user_id=user_id,
        message=message,
        sent_at=datetime.utcnow()
    )
    db.add(comment)
    db.commit()
    db.refresh(comment)
    return comment

# --- Get Comments for a Stream ---


def get_stream_comments(db: Session, stream_id: int) -> List[StreamComment]:
    return db.query(StreamComment).filter(
        StreamComment.stream_id == stream_id).order_by(
        StreamComment.sent_at.asc()).all()


