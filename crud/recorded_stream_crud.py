from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from backend.models.recorded_stream import RecordedStream
from backend.schemas.recorded_stream_schemas import *

def create_recording(db: Session, data: RecordedStreamCreate):
    recording = RecordedStream(**data.dict())
    db.add(recording)
    db.commit()
    db.refresh(recording)
    return recording

def get_recording_by_stream(db: Session, stream_id: int):
    return db.query(RecordedStream).filter(RecordedStream.stream_id == stream_id).first()

