from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from backend.db import get_db
from backend.schemas.viewer import ViewerCreate, ViewerOut
from backend.crud import viewer_crud

router = APIRouter()

@router.post("/", response_model=ViewerOut)
def join_stream(viewer: ViewerCreate, db: Session = Depends(get_db)):
    return viewer_crud.add_viewer(db, viewer)

@router.get("/{stream_id}", response_model=list[ViewerOut])
def get_viewers(stream_id: str, db: Session = Depends(get_db)):
    return viewer_crud.get_current_viewers(db, stream_id)

@router.delete("/", response_model=ViewerOut)
def leave_stream(user_id: int, stream_id: str, db: Session = Depends(get_db)):
    removed = viewer_crud.remove_viewer(db, user_id, stream_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Viewer not found.")
    return removed
