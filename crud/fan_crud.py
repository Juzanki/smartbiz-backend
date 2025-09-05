from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from backend.models.fan import Fan
from backend.schemas.fan import FanCreate

def create_or_update_fan(db: Session, fan_data: FanCreate):
    fan = db.query(Fan).filter_by(user_id=fan_data.user_id, host_id=fan_data.host_id).first()
    if fan:
        fan.total_contribution += fan_data.total_contribution
        db.commit()
        db.refresh(fan)
        return fan
    else:
        new_fan = Fan(**fan_data.dict())
        db.add(new_fan)
        db.commit()
        db.refresh(new_fan)
        return new_fan

def get_top_fans(db: Session, host_id: int, limit: int = 10):
    return db.query(Fan).filter_by(host_id=host_id).order_by(Fan.total_contribution.desc()).limit(limit).all()

