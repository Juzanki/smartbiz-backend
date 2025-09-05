from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from backend.dependencies import get_db, get_current_user_optional
from backend.crud import view_stat_crud
from backend.schemas.view_stat_schemas import ViewStatOut

router = APIRouter()

@router.post("/view/{video_post_id}", response_model=ViewStatOut)
def record_view(video_post_id: int, db: Session = Depends(get_db), user_id: int = Depends(get_current_user_optional)):
    return view_stat_crud.log_view(db, user_id, video_post_id)

@router.get("/{video_post_id}", response_model=list[ViewStatOut])
def get_stats(video_post_id: int, db: Session = Depends(get_db)):
    return view_stat_crud.get_video_stats(db, video_post_id)
