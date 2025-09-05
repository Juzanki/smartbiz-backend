from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from backend.models.video_post import VideoPost
from backend.schemas.video_post_schemas import *

def create_video_post(db: Session, data: VideoPostCreate):
    post = VideoPost(**data.dict())
    db.add(post)
    db.commit()
    db.refresh(post)
    return post

def get_video_post_by_stream(db: Session, recorded_stream_id: int):
    return db.query(VideoPost).filter(VideoPost.recorded_stream_id == recorded_stream_id).first()

def update_video_post(db: Session, post_id: int, update_data: VideoPostUpdate):
    post = db.query(VideoPost).filter(VideoPost.id == post_id).first()
    if post:
        for key, value in update_data.dict(exclude_unset=True).items():
            setattr(post, key, value)
        db.commit()
        db.refresh(post)
    return post

