from __future__ import annotations
﻿from backend.schemas.user import UserOut
# backend/crud/viewer_crud.py

from sqlalchemy.orm import Session
from backend.models.viewer import Viewer
from backend.schemas.viewer import ViewerCreate

def add_viewer(db: Session, viewer: ViewerCreate):
    new_viewer = Viewer(**viewer.dict())
    db.add(new_viewer)
    db.commit()
    db.refresh(new_viewer)
    return new_viewer

def get_current_viewers(db: Session, stream_id: str):
    return db.query(Viewer).filter(Viewer.stream_id == stream_id).all()

def remove_viewer(db: Session, user_id: int, stream_id: str):
    viewer = db.query(Viewer).filter(Viewer.user_id == user_id, Viewer.stream_id == stream_id).first()
    if viewer:
        db.delete(viewer)
        db.commit()
    return viewer

