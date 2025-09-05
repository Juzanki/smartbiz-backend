from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from backend.models.stream_settings import StreamSettings
from backend.schemas.stream_settings_schemas import *

def create_or_update_settings(db: Session, stream_id: int, data: StreamSettingsBase):
    settings = db.query(StreamSettings).filter(StreamSettings.stream_id == stream_id).first()
    if settings:
        settings.camera_on = data.camera_on
        settings.mic_on = data.mic_on
    else:
        settings = StreamSettings(stream_id=stream_id, **data.dict())
        db.add(settings)
    db.commit()
    db.refresh(settings)
    return settings

def get_settings(db: Session, stream_id: int):
    return db.query(StreamSettings).filter(StreamSettings.stream_id == stream_id).first()

