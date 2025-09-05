from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from backend.models.superchat_model import Superchat
from backend.schemas.superchat import SuperchatCreate

def create_superchat(db: Session, sc: SuperchatCreate):
    new_sc = Superchat(**sc.dict())
    db.add(new_sc)
    db.commit()
    db.refresh(new_sc)
    return new_sc

def get_superchats_by_stream(db: Session, stream_id: str):
    return db.query(Superchat).filter(Superchat.stream_id == stream_id).order_by(Superchat.timestamp.desc()).all()

