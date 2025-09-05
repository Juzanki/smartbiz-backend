from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from backend.models.video_comment import VideoComment
from backend.schemas.comment_schemas import VideoCommentCreate

def create_comment(db: Session, user_id: int, data: VideoCommentCreate):
    comment = VideoComment(**data.dict(), user_id=user_id)
    db.add(comment)
    db.commit()
    db.refresh(comment)
    return comment

def get_comments_by_video(db: Session, video_post_id: int):
    return db.query(VideoComment).filter(VideoComment.video_post_id == video_post_id).order_by(VideoComment.timestamp.asc()).all()

def delete_comment(db: Session, comment_id: int, user_id: int):
    comment = db.query(VideoComment).filter(VideoComment.id == comment_id, VideoComment.user_id == user_id).first()
    if comment:
        db.delete(comment)
        db.commit()
    return comment

