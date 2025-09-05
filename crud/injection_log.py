from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from typing import List
from datetime import datetime
from backend.models.injection_log import InjectionLog
from backend.schemas.injection_log import InjectionLogCreate

# --- Create Injection Log ---


def create_log(db: Session, log_data: InjectionLogCreate):
    log = InjectionLog(**log_data.dict())
    db.add(log)
    db.commit()
    db.refresh(log)
    return log

# --- Get Logs ---


def get_logs(
        db: Session,
        skip: int = 0,
        limit: int = 50) -> List[InjectionLog]:
    return db.query(InjectionLog).order_by(
        InjectionLog.timestamp.desc()).offset(skip).limit(limit).all()

