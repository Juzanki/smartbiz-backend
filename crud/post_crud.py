from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from backend.models.post import SocialMediaPost
from ..schemas.post import PostCreate
from typing import List

from ..models.post import Post


# --- Create Social Media Post ---
def create_post(db: Session, post: PostCreate) -> SocialMediaPost:
    db_post = SocialMediaPost(**post.dict())
    db.add(db_post)
    db.commit()
    db.refresh(db_post)
    return db_post

# --- Get All Posts ---


def get_posts(
        db: Session,
        skip: int = 0,
        limit: int = 100) -> List[SocialMediaPost]:
    return db.query(SocialMediaPost).offset(skip).limit(limit).all()

# --- Get User Posts ---


def get_user_posts(db: Session, user_id: int) -> List[SocialMediaPost]:
    return db.query(SocialMediaPost).filter(
        SocialMediaPost.user_id == user_id).all()




