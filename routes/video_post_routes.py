from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from backend.db import get_db
from backend.schemas.video_post_schemas import *
from backend.crud import video_post_crud

router = APIRouter()

@router.post("/", response_model=VideoPostOut)
def create_post(data: VideoPostCreate, db: Session = Depends(get_db)):
    return video_post_crud.create_video_post(db, data)

@router.get("/stream/{recorded_stream_id}", response_model=VideoPostOut)
def get_post(recorded_stream_id: int, db: Session = Depends(get_db)):
    post = video_post_crud.get_video_post_by_stream(db, recorded_stream_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    return post

@router.put("/{post_id}", response_model=VideoPostOut)
def update_post(post_id: int, update_data: VideoPostUpdate, db: Session = Depends(get_db)):
    return video_post_crud.update_video_post(db, post_id, update_data)
