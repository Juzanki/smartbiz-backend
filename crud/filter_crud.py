from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from backend.models.filter import Filter
from backend.schemas.filter import FilterCreate

def create_filter(db: Session, data: FilterCreate):
    new_filter = Filter(**data.dict())
    db.add(new_filter)
    db.commit()
    db.refresh(new_filter)
    return new_filter

def get_all_filters(db: Session):
    return db.query(Filter).filter(Filter.is_active == True).all()

